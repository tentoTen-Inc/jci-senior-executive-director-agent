"""LIFF での LINE 連携の申請・承認と、グループへの予約投稿（docs/line-link-design.md）。

招待コードを1人ずつ配らずに済むよう、会員は LIFF 画面で自分の名前を選んで申請し、
専務が LINE（ボタン）か管理画面で承認すると Member.line_user_id に紐づく。
本人確認は LIFF の IDトークン、申請できるのはボットが入っているグループのメンバーだけ。
承認制なので、他人の名前で申請されても専務が気づける（招待コード方式も併用できる）。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

from linebot.v3.messaging import FlexContainer, FlexMessage, Message, TextMessage

from . import line_push
from . import schedule_poll as sp
from .audit import write_audit
from .models import (
    LinkRequest,
    LinkRequestStatus,
    Member,
    MemberStatus,
    ScheduledPostStatus,
)
from .repository import Repository

logger = logging.getLogger("jci-agent.line_link")

ACTION_LINK = "link"  # postback: link|approve|<line_user_id> / link|reject|<line_user_id>
APPROVER_ROLES = ("専務理事",)
BOT_BASIC_ID = "@782uyboi"
FRIEND_URL = f"https://line.me/R/ti/p/{BOT_BASIC_ID}"


def liff_link_url() -> str:
    return f"https://liff.line.me/{sp.liff_id()}?link=1"


# --------------------------------------------------------------------------- #
# 申請（LIFF）
# --------------------------------------------------------------------------- #
def authorize(repo: Repository, user_id: str) -> None:
    """申請画面を使えるのは、ボットが入っているいずれかのグループのメンバーだけ。"""
    if repo.get_member_by_line_user_id(user_id):
        return
    for group in repo.list_line_groups():
        if (group.left_at is None and group.source_type == "group"
                and sp.is_group_member(group.group_id, user_id)):
            return
    raise sp.PollError(403, "この画面は猪苗代JCの LINE グループのメンバーだけが使えます。")


def _linkable(repo: Repository) -> list[Member]:
    return [m for m in repo.list_members()
            if m.status == MemberStatus.active and not m.line_user_id]


def view(repo: Repository, user_id: str, display_name: str | None) -> dict:
    linked = repo.get_member_by_line_user_id(user_id)
    request = repo.get_link_request(user_id)
    pending_by_others = {r.member_id for r in repo.list_link_requests()
                         if r.status == LinkRequestStatus.pending and r.line_user_id != user_id}
    names = {m.member_id: m.name for m in repo.list_members()}
    status = "linked" if linked else (request.status.value if request else "none")
    return {
        "display_name": display_name,
        "status": status,  # linked / pending / rejected / none
        "member_name": (linked.name if linked
                        else names.get(request.member_id) if request else None),
        "members": [
            {"member_id": m.member_id, "name": m.name, "taken": m.member_id in pending_by_others}
            for m in _linkable(repo)
        ],
        "friend_url": FRIEND_URL,
    }


def request_link(repo: Repository, user_id: str, display_name: str | None, member_id: str,
                 now: datetime) -> LinkRequest:
    if repo.get_member_by_line_user_id(user_id):
        raise sp.PollError(409, "すでに連携済みです。")
    if member_id not in {m.member_id for m in _linkable(repo)}:
        raise sp.PollError(400, "一覧から選んでください（すでに連携済みの方は選べません）。")
    for r in repo.list_link_requests():
        if (r.member_id == member_id and r.line_user_id != user_id
                and r.status == LinkRequestStatus.pending):
            raise sp.PollError(
                409, "その方は別の LINE アカウントで申請中です。事務局にご連絡ください。")
    request = LinkRequest(line_user_id=user_id, display_name=display_name, member_id=member_id,
                          requested_at=now)
    repo.save_link_request(request)
    write_audit(repo, actor=f"line:{user_id}", action="link.request", target=member_id,
                detail=display_name)
    _notify_approvers(repo, request)
    return request


def approval_message(repo: Repository, request: LinkRequest) -> Message:
    member = repo.get_member(request.member_id)
    name = member.name if member else request.member_id
    bubble = {
        "type": "bubble",
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
            {"type": "text", "text": "LINE 連携の申請", "size": "xs", "color": "#888888"},
            {"type": "text", "text": f"{name} さん", "weight": "bold", "size": "lg"},
            {"type": "text", "text": f"LINE の表示名: {request.display_name or '（なし）'}",
             "size": "sm", "wrap": True},
            {"type": "text", "text": "本人で間違いなければ承認してください。", "size": "sm",
             "color": "#555555", "wrap": True},
        ]},
        "footer": {"type": "box", "layout": "horizontal", "spacing": "sm", "contents": [
            {"type": "button", "style": "primary", "action": {
                "type": "postback", "label": "承認する",
                "data": f"{ACTION_LINK}|approve|{request.line_user_id}"}},
            {"type": "button", "style": "secondary", "action": {
                "type": "postback", "label": "却下",
                "data": f"{ACTION_LINK}|reject|{request.line_user_id}"}},
        ]},
    }
    return FlexMessage(alt_text=f"LINE 連携の申請: {name} さん（{request.display_name}）",
                       contents=FlexContainer.from_dict(bubble))


def _notify_approvers(repo: Repository, request: LinkRequest) -> None:
    """専務に承認ボタン付きで知らせる（送れなくても申請は残り、管理画面で承認できる）。"""
    for member in repo.list_members():
        if member.officer_role in APPROVER_ROLES and member.line_user_id:
            try:
                line_push.push_messages(member.line_user_id, [approval_message(repo, request)])
            except Exception:  # noqa: BLE001
                logger.exception("連携申請の通知に失敗: to=%s", member.member_id)


# --------------------------------------------------------------------------- #
# 承認・却下（LINE のボタン / 管理画面）
# --------------------------------------------------------------------------- #
def approve(repo: Repository, line_user_id: str, actor: str, now: datetime,
            member_id: str | None = None) -> Member:
    """申請（または日程調整の回答から分かった対応）を承認して会員に紐づける。"""
    request = repo.get_link_request(line_user_id)
    member_id = member_id or (request.member_id if request else None)
    if not member_id:
        raise sp.PollError(404, "申請が見つかりません。")
    member = repo.get_member(member_id)
    if member is None:
        raise sp.PollError(404, "会員が見つかりません。")
    other = repo.get_member_by_line_user_id(line_user_id)
    if other is not None:
        if other.member_id == member_id:
            return other
        raise sp.PollError(409, f"この LINE はすでに {other.name} さんに連携されています。")
    if member.line_user_id:
        raise sp.PollError(409, f"{member.name} さんはすでに別の LINE と連携済みです。")
    member.line_user_id = line_user_id
    member.linked_at = now
    repo.upsert_member(member)
    if request:
        repo.save_link_request(request.model_copy(update={
            "status": LinkRequestStatus.approved, "member_id": member_id,
            "decided_at": now, "decided_by": actor}))
    write_audit(repo, actor=actor, action="link.approve", target=member_id,
                detail=f"{member.name} ← {request.display_name if request else line_user_id}")
    # 立場に合ったリッチメニュー（理事・五役は作業のボタン付き）に付け替える
    from . import rich_menu_link

    rich_menu_link.link(repo, member)
    try:
        line_push.push_text(line_user_id, (
            f"{member.name} さん、LINE 連携が完了しました。\n"
            "今後は出欠のお願いや日程調整、資料のお問い合わせをこちらの個別トークでお受けします。"))
    except Exception:  # noqa: BLE001 - 友だち未追加なら届かない。連携自体は完了
        logger.info("連携完了の通知を送れませんでした（友だち未追加の可能性）: %s", member_id)
    return member


def reject(repo: Repository, line_user_id: str, actor: str, now: datetime) -> LinkRequest:
    request = repo.get_link_request(line_user_id)
    if request is None or request.status != LinkRequestStatus.pending:
        raise sp.PollError(404, "承認待ちの申請が見つかりません。")
    request = request.model_copy(update={"status": LinkRequestStatus.rejected,
                                         "decided_at": now, "decided_by": actor})
    repo.save_link_request(request)
    write_audit(repo, actor=actor, action="link.reject", target=request.member_id,
                detail=request.display_name)
    return request


def handle_postback(repo: Repository, member: Member, data: str, now: datetime) -> list[Message]:
    """専務が LINE の承認ボタンを押したとき。"""
    if member.officer_role not in APPROVER_ROLES:
        return [TextMessage(text="LINE 連携の承認は専務理事のみ行えます。")]
    parts = data.split("|")
    if len(parts) != 3:
        return [TextMessage(text="操作を読み取れませんでした。")]
    _, action, line_user_id = parts
    actor = f"line:{member.member_id}"
    try:
        if action == "approve":
            linked = approve(repo, line_user_id, actor, now)
            return [TextMessage(text=f"{linked.name} さんの LINE 連携を承認しました。")]
        if action == "reject":
            rejected = reject(repo, line_user_id, actor, now)
            names = {m.member_id: m.name for m in repo.list_members()}
            name = names.get(rejected.member_id, rejected.member_id)
            return [TextMessage(text=f"{name} さんへの申請を却下しました。")]
    except sp.PollError as exc:
        return [TextMessage(text=exc.message)]
    return [TextMessage(text="操作を読み取れませんでした。")]


def pending_list(repo: Repository) -> dict:
    """管理画面: 承認待ちの申請と、日程調整の回答から分かった連携候補。"""
    names = {m.member_id: m.name for m in repo.list_members()}
    linked_users = {m.line_user_id for m in repo.list_members() if m.line_user_id}
    linked_members = {m.member_id for m in repo.list_members() if m.line_user_id}
    requests = [{**r.model_dump(mode="json"), "member_name": names.get(r.member_id)}
                for r in repo.list_link_requests() if r.status == LinkRequestStatus.pending]
    requested_users = {r["line_user_id"] for r in requests}
    candidates: dict[str, dict] = {}
    for poll in repo.list_polls():
        for resp in repo.list_poll_responses(poll.poll_id):
            if (resp.member_id and resp.line_user_id not in linked_users
                    and resp.member_id not in linked_members
                    and resp.line_user_id not in requested_users):
                candidates[resp.line_user_id] = {
                    "line_user_id": resp.line_user_id, "display_name": resp.display_name,
                    "member_id": resp.member_id, "member_name": names.get(resp.member_id),
                    "source": f"日程調整「{poll.title}」で選択"}
    return {"requests": requests, "candidates": list(candidates.values()),
            "liff_url": liff_link_url() if sp.is_configured() else None}


# --------------------------------------------------------------------------- #
# 予約投稿（tick が送る）
# --------------------------------------------------------------------------- #
def link_invite_message() -> Message:
    bubble = {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "猪苗代専務AI からのお願い", "size": "xs", "color": "#888888"},
            {"type": "text", "text": "LINE 連携のお願い", "weight": "bold", "size": "lg"},
        ]},
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
            {"type": "text", "wrap": True, "size": "sm", "text": (
                "猪苗代専務AI とあなたの LINE をつなぐと、出欠のお願い・日程調整・"
                "資料のお問い合わせなどを個別トークでやり取りできるようになります。")},
            {"type": "text", "wrap": True, "size": "sm", "text": (
                "下のボタンから、ご自身の名前を選ぶだけで1分ほどで終わります"
                "（専務の確認後に連携されます）。")},
        ]},
        "footer": {"type": "box", "layout": "vertical", "contents": [
            {"type": "button", "style": "primary",
             "action": {"type": "uri", "label": "LINE 連携をする", "uri": liff_link_url()}},
        ]},
    }
    return FlexMessage(alt_text="【お願い】猪苗代専務AI との LINE 連携",
                       contents=FlexContainer.from_dict(bubble))


def new_post_id() -> str:
    return f"post_{uuid.uuid4().hex[:10]}"


def send_due_posts(repo: Repository, now: datetime) -> list[dict] | None:
    """送信時刻を過ぎた予約投稿を送る。緊急停止中は送らずに待つ。"""
    due = [p for p in repo.list_scheduled_posts()
           if p.status == ScheduledPostStatus.pending and p.send_at <= now]
    if not due:
        return None
    if repo.get_settings().kill_switch:
        logger.warning("緊急停止中のため予約投稿を保留: %d件", len(due))
        return [{"post_id": p.post_id, "result": "held"} for p in due]
    results = []
    for post in due:
        try:
            if post.kind == "link_invite":
                if not sp.is_configured():
                    raise sp.PollError(400, "LIFF が未設定です。")
                if not line_push.push_messages(post.group_id, [link_invite_message()]):
                    raise sp.PollError(503, "LINE のアクセストークンが未設定です。")
            elif post.kind in ("poll_announce", "poll_remind", "poll_decide"):
                poll = repo.get_poll(post.poll_id or "")
                if poll is None:
                    raise sp.PollError(404, "日程調整が見つかりません。")
                if post.kind == "poll_announce":
                    sp.announce(repo, poll, "scheduled", now)
                elif post.kind == "poll_remind":
                    sp.remind(repo, poll, "scheduled", now)
                else:
                    # 確定すると予定（下書き）を作ってカレンダーへ登録し、グループへ決定を連絡する
                    sp.decide(repo, poll, post.candidate_id or "", "scheduled", now)
            else:
                raise sp.PollError(400, f"未知の種類: {post.kind}")
            post = post.model_copy(update={"status": ScheduledPostStatus.sent, "sent_at": now})
            write_audit(repo, actor="scheduled", action="post.send", target=post.post_id,
                        detail=f"{post.kind} → {post.group_id}")
        except Exception as exc:  # noqa: BLE001 - 1件の失敗で他を止めない（再送はしない）
            logger.error("予約投稿の送信に失敗: %s %s", post.post_id, exc)
            post = post.model_copy(update={"status": ScheduledPostStatus.failed,
                                           "error": str(exc)[:300]})
        repo.save_scheduled_post(post)
        results.append({"post_id": post.post_id, "result": post.status.value})
    return results
