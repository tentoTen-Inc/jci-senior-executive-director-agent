"""日程調整（docs/schedule-poll-design.md）。

候補日を LINE グループ（委員会あてでグループが無ければ対象者の個別トーク）に案内し、
各自が LIFF 画面で ○△× を回答する。回答者の本人確認は LIFF の IDトークン（LINE ログイン）、
閲覧権は案内先グループのメンバーか連携済みの対象会員。集計・催促・決定（イベント作成→
カレンダー登録）もここで行う。

`_verify_id_token_raw` / `_is_group_member_raw` がテストのモック境界。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

from linebot.v3.messaging import FlexContainer, FlexMessage, Message, TextMessage

from . import config, line_push
from .audit import write_audit
from .calendar_sync import push_event
from .jpdate import month_day_time, weekday
from .models import (
    Event,
    EventStatus,
    LineGroup,
    Member,
    MemberStatus,
    PollAnswer,
    PollResponse,
    PollStatus,
    SchedulePoll,
    TargetScopeKind,
)
from .repository import Repository

logger = logging.getLogger("jci-agent.schedule_poll")

VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"
LINE_API = "https://api.line.me/v2/bot"
COMMENT_MAX = 200
_TOKEN_TTL = 300  # 秒。IDトークン検証結果のキャッシュ
_MEMBER_TTL = 600  # 秒。グループのメンバー確認のキャッシュ
_token_cache: dict[str, tuple[float, dict]] = {}
_member_cache: dict[tuple[str, str], tuple[float, bool]] = {}


class PollError(Exception):
    """利用者に見せる理由付きのエラー（status は HTTP ステータス）。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- #
# 設定
# --------------------------------------------------------------------------- #
def liff_id() -> str | None:
    return os.environ.get("LIFF_ID", "").strip() or None


def login_channel_id() -> str | None:
    return os.environ.get("LINE_LOGIN_CHANNEL_ID", "").strip() or None


def is_configured() -> bool:
    return bool(liff_id() and login_channel_id())


def liff_url(poll_id: str) -> str:
    return f"https://liff.line.me/{liff_id()}?poll={urllib.parse.quote(poll_id)}"


# --------------------------------------------------------------------------- #
# 本人確認・閲覧権
# --------------------------------------------------------------------------- #
def _verify_id_token_raw(id_token: str, client_id: str) -> dict:
    """LINE で IDトークンを検証して claims を返す（テストでモックする境界）。"""
    data = urllib.parse.urlencode({"id_token": id_token, "client_id": client_id}).encode()
    req = urllib.request.Request(VERIFY_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise PollError(401, "LINE ログインの確認に失敗しました。開き直してください。") from exc


def verify_id_token(authorization: str | None) -> dict:
    """`Authorization: Bearer <IDトークン>` を検証して {sub, name} を返す。"""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise PollError(401, "LINE ログインが必要です。")
    now = time.monotonic()
    cached = _token_cache.get(token)
    if cached and cached[0] > now:
        return cached[1]
    claims = _verify_id_token_raw(token, login_channel_id() or "")
    if not claims.get("sub"):
        raise PollError(401, "LINE ログインの確認に失敗しました。開き直してください。")
    _token_cache[token] = (now + _TOKEN_TTL, claims)
    return claims


def _is_group_member_raw(group_id: str, user_id: str) -> bool:
    """LINE API でグループのメンバーか確かめる（テストでモックする境界）。"""
    req = urllib.request.Request(f"{LINE_API}/group/{group_id}/member/{user_id}")
    req.add_header("Authorization", f"Bearer {config.line_channel_access_token()}")
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def is_group_member(group_id: str, user_id: str) -> bool:
    key = (group_id, user_id)
    now = time.monotonic()
    cached = _member_cache.get(key)
    if cached and cached[0] > now:
        return cached[1]
    result = _is_group_member_raw(group_id, user_id)
    _member_cache[key] = (now + _MEMBER_TTL, result)
    return result


def target_members(repo: Repository, poll: SchedulePoll) -> list[Member]:
    """回答してほしい会員。LINE 未連携も含む（events.resolve_scope は連携済みだけ）。"""
    members = [m for m in repo.list_members() if m.status == MemberStatus.active]
    scope = poll.target_scope
    if scope.kind == TargetScopeKind.committee:
        # 委員会に所属する会員（兼務を含む）＋担当の副理事長
        wanted = set(scope.value)
        members = [m for m in members
                   if (m.committee_names | set(m.supervised_committees)) & wanted]
    elif scope.kind == TargetScopeKind.officer:
        members = [m for m in members if m.officer_role in scope.value]
    elif scope.kind == TargetScopeKind.custom:
        order = {member_id: i for i, member_id in enumerate(scope.value)}
        members = sorted((m for m in members if m.member_id in order),
                         key=lambda m: order[m.member_id])
    return members


def committee_names(repo: Repository) -> list[str]:
    """組織図（会員の所属・副理事長の担当）にある委員会の名前。"""
    names: set[str] = set()
    for m in repo.list_members():
        if m.status == MemberStatus.active:
            names |= m.committee_names | set(m.supervised_committees)
    return sorted(n for n in names if n)


def _norm(name: str) -> str:
    return re.sub(r"[\s　]", "", name)


def active_groups(repo: Repository) -> list[LineGroup]:
    """専務AIが入っている（抜けていない）名前の分かるグループ。"""
    return [g for g in repo.list_line_groups() if g.group_name
            and (not g.left_at or (g.joined_at and g.joined_at > g.left_at))]


def committee_group(repo: Repository, committee: str) -> LineGroup | None:
    """名前にその委員会名が含まれるグループ（無ければ None＝対象者に個別に送る）。"""
    wanted = _norm(committee)
    return next((g for g in active_groups(repo) if wanted and wanted in _norm(g.group_name)),
                None)


def destination(repo: Repository, poll: SchedulePoll) -> dict:
    """送り先（確認画面に出す）: グループか、連携済みの対象者に個別か。"""
    targets = target_members(repo, poll)
    linked = [m for m in targets if m.line_user_id]
    if poll.group_id:
        group = next((g for g in repo.list_line_groups() if g.group_id == poll.group_id), None)
        return {"mode": "group", "group_name": group.group_name if group else None,
                "targets": len(targets), "linked": len(linked)}
    return {"mode": "individual", "group_name": None, "targets": len(targets),
            "linked": len(linked)}


def authorize_viewer(repo: Repository, poll: SchedulePoll, user_id: str) -> Member | None:
    """閲覧してよいか確かめ、連携済みの会員なら返す。だめなら PollError(403)。"""
    linked = repo.get_member_by_line_user_id(user_id)
    if linked is not None and linked.member_id in {m.member_id for m in target_members(repo, poll)}:
        return linked
    if poll.group_id and is_group_member(poll.group_id, user_id):
        return linked
    raise PollError(403, "この日程調整を見る権限がありません（案内されたグループのメンバーのみ）。")


# --------------------------------------------------------------------------- #
# 表示・回答
# --------------------------------------------------------------------------- #
def candidate_label(start: datetime, end: datetime | None = None) -> str:
    label = f"{start.month}/{start.day}({weekday(start)}) {start:%H:%M}〜"
    return label + (f"{end:%H:%M}" if end else "")


def _respondent_name(resp: PollResponse, names: dict[str, str]) -> str:
    if resp.member_id and resp.member_id in names:
        return names[resp.member_id]
    return f"{resp.display_name or '（名前なし）'}（LINE名）"


def view(repo: Repository, poll: SchedulePoll, user_id: str, display_name: str | None,
         linked: Member | None) -> dict:
    """LIFF 画面に渡す内容。他の人の LINE userId は出さない。"""
    targets = target_members(repo, poll)
    names = {m.member_id: m.name for m in repo.list_members()}
    responses = repo.list_poll_responses(poll.poll_id)
    mine = next((r for r in responses if r.line_user_id == user_id), None)
    taken = {r.member_id for r in responses if r.member_id and r.line_user_id != user_id}
    counts = {c.candidate_id: {a.value: 0 for a in PollAnswer} for c in poll.candidates}
    for r in responses:
        for cid, answer in r.answers.items():
            if cid in counts:
                counts[cid][answer.value] += 1
    me_member_id = linked.member_id if linked else (mine.member_id if mine else None)
    return {
        "poll": {
            "poll_id": poll.poll_id,
            "title": poll.title,
            "audience": poll.audience,
            "location": poll.location,
            "deadline": month_day_time(poll.deadline) if poll.deadline else None,
            "status": poll.status.value,
            "candidates": [
                {"candidate_id": c.candidate_id, "label": candidate_label(c.start, c.end)}
                for c in poll.candidates
            ],
            "decided_candidate_id": poll.decided_candidate_id,
        },
        "me": {
            "display_name": display_name,
            "member_id": me_member_id,
            "member_name": names.get(me_member_id) if me_member_id else None,
            "linked": linked is not None,  # 連携済みなら「あなたは」を選ばせない
            "answers": {k: v.value for k, v in (mine.answers if mine else {}).items()},
            "comment": mine.comment if mine else None,
        },
        "members": [{"member_id": m.member_id, "name": m.name, "taken": m.member_id in taken}
                    for m in targets],
        "responses": [
            {"name": _respondent_name(r, names),
             "answers": {k: v.value for k, v in r.answers.items()},
             "comment": r.comment, "is_me": r.line_user_id == user_id}
            for r in sorted(responses, key=lambda r: r.updated_at or datetime.min)
        ],
        "counts": counts,
    }


def save_response(repo: Repository, poll: SchedulePoll, user_id: str, display_name: str | None,
                  linked: Member | None, body: dict, now: datetime) -> PollResponse:
    if poll.status != PollStatus.open:
        raise PollError(409, "この日程調整は締め切られました。")
    raw_answers = body.get("answers") or {}
    ids = {c.candidate_id for c in poll.candidates}
    if set(raw_answers) != ids:
        raise PollError(400, "すべての候補日に ○△× を付けてください。")
    try:
        answers = {cid: PollAnswer(value) for cid, value in raw_answers.items()}
    except ValueError as exc:
        raise PollError(400, "回答は ○（ok）・△（maybe）・×（ng）のいずれかです。") from exc
    comment = (body.get("comment") or "").strip()[:COMMENT_MAX] or None

    member_id = linked.member_id if linked else body.get("member_id") or None
    if member_id and not linked:
        # 未連携の人は対象者一覧から自分を選ぶ（自己申告）。同じ人を別の LINE で選ぶことはできない
        if member_id not in {m.member_id for m in target_members(repo, poll)}:
            raise PollError(400, "対象者の一覧から選んでください。")
        for r in repo.list_poll_responses(poll.poll_id):
            if r.member_id == member_id and r.line_user_id != user_id:
                raise PollError(409, "その方はすでに別の LINE アカウントで回答しています。")
    response = PollResponse(
        poll_id=poll.poll_id, line_user_id=user_id, display_name=display_name,
        member_id=member_id, answers=answers, comment=comment, updated_at=now,
    )
    repo.save_poll_response(response)
    return response


# --------------------------------------------------------------------------- #
# 管理（集計・送信・決定）
# --------------------------------------------------------------------------- #
def summary(repo: Repository, poll: SchedulePoll) -> dict:
    """会員×候補日の表・候補日ごとの集計・未回答者。"""
    targets = target_members(repo, poll)
    names = {m.member_id: m.name for m in repo.list_members()}
    responses = repo.list_poll_responses(poll.poll_id)
    by_member = {r.member_id: r for r in responses if r.member_id}

    def row(name: str, r: PollResponse | None, member_id: str | None) -> dict:
        return {
            "member_id": member_id, "name": name,
            "display_name": r.display_name if r else None,
            "answers": {k: v.value for k, v in r.answers.items()} if r else None,
            "comment": r.comment if r else None,
            "updated_at": r.updated_at if r else None,
        }

    rows = [row(m.name, by_member.get(m.member_id), m.member_id) for m in targets]
    target_ids = {m.member_id for m in targets}
    others = [row(_respondent_name(r, names), r, r.member_id) for r in responses
              if r.member_id not in target_ids]
    counts = {c.candidate_id: {a.value: 0 for a in PollAnswer} for c in poll.candidates}
    for r in responses:
        for cid, answer in r.answers.items():
            if cid in counts:
                counts[cid][answer.value] += 1
    return {
        "poll": poll,
        "candidates": [{**c.model_dump(mode="json"), "label": candidate_label(c.start, c.end)}
                       for c in poll.candidates],
        "rows": rows,
        "others": others,
        "counts": counts,
        "pending": [m.name for m in targets if m.member_id not in by_member],
        "liff_url": liff_url(poll.poll_id) if is_configured() else None,
        "destination": destination(repo, poll),
    }


def announce_message(poll: SchedulePoll) -> Message:
    lines = [{"type": "text", "text": f"・{candidate_label(c.start, c.end)}", "size": "sm",
              "wrap": True} for c in poll.candidates]
    body: list[dict] = []
    if poll.audience:
        body.append({"type": "text", "text": poll.audience, "size": "sm", "color": "#555555"})
    body += [{"type": "text", "text": "候補日", "weight": "bold", "size": "sm", "margin": "md"},
             *lines]
    if poll.location:
        body.append({"type": "text", "text": f"場所: {poll.location}", "size": "sm",
                     "margin": "md", "wrap": True})
    if poll.deadline:
        body.append({"type": "text", "text": f"回答締切: {month_day_time(poll.deadline)}",
                     "size": "sm", "color": "#C0392B"})
    bubble = {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "日程調整のお願い", "size": "xs", "color": "#888888"},
            {"type": "text", "text": poll.title, "weight": "bold", "size": "lg", "wrap": True},
        ]},
        "body": {"type": "box", "layout": "vertical", "spacing": "xs", "contents": body},
        "footer": {"type": "box", "layout": "vertical", "contents": [
            {"type": "button", "style": "primary",
             "action": {"type": "uri", "label": "日程を回答する", "uri": liff_url(poll.poll_id)}},
        ]},
    }
    return FlexMessage(alt_text=f"【日程調整】{poll.title} の回答をお願いします",
                       contents=FlexContainer.from_dict(bubble))


def remind_text(poll: SchedulePoll, pending: list[str]) -> str:
    lines = [f"【日程調整のお願い】{poll.title}",
             f"未回答の方: {'・'.join(f'{n}さん' for n in pending)}"]
    if poll.deadline:
        lines.append(f"締切: {month_day_time(poll.deadline)}")
    lines.append(f"回答はこちら: {liff_url(poll.poll_id)}")
    return "\n".join(lines)


def decided_text(poll: SchedulePoll) -> str:
    c = poll.candidate(poll.decided_candidate_id or "")
    when = month_day_time(c.start) + "〜" if c else "（日時不明）"
    place = f"（{poll.location}）" if poll.location else ""
    return f"【日程決定】{poll.title}\n{when}{place}に決まりました。ご回答ありがとうございました。"


def remind_personal_text(poll: SchedulePoll) -> str:
    lines = [f"【日程調整のお願い】{poll.title}", "まだ回答をいただいていません。"]
    if poll.deadline:
        lines.append(f"締切: {month_day_time(poll.deadline)}")
    lines.append(f"回答はこちら: {liff_url(poll.poll_id)}")
    return "\n".join(lines)


def _send(group_id: str, messages: list[Message]) -> None:
    if not line_push.push_messages(group_id, messages):
        raise PollError(503, "LINE のアクセストークンが未設定のため送れません。")


def _send_individually(members: list[Member], messages: list[Message]) -> int:
    """連携済みの会員それぞれの個別トークに送る（送れた人数を返す）。"""
    linked = [m for m in members if m.line_user_id]
    if not linked:
        raise PollError(409, "LINE 連携済みの対象者がいないため送れません。")
    sent = 0
    for m in linked:
        try:
            if line_push.push_messages(m.line_user_id, messages):
                sent += 1
        except Exception:  # noqa: BLE001 - 友だち未追加など。他の人には送る
            logger.warning("日程調整を個別に送れませんでした: %s", m.member_id)
    if sent == 0:
        raise PollError(503, "LINE で送れませんでした（トークン未設定・友だち未追加など）。")
    return sent


def _deliver(repo: Repository, poll: SchedulePoll, messages: list[Message],
             members: list[Member] | None = None) -> None:
    """グループがあればグループへ、無ければ対象者（members）に個別に送る。"""
    if poll.group_id:
        _send(poll.group_id, messages)
    else:
        _send_individually(target_members(repo, poll) if members is None else members, messages)


def announce(repo: Repository, poll: SchedulePoll, actor: str, now: datetime) -> SchedulePoll:
    if not is_configured():
        raise PollError(400, "LIFF が未設定です（LIFF_ID・LINE_LOGIN_CHANNEL_ID）。")
    if poll.status != PollStatus.open:
        raise PollError(409, "受付中の日程調整だけ案内できます。")
    _deliver(repo, poll, [announce_message(poll)])
    poll = poll.model_copy(update={"announced_at": now})
    repo.save_poll(poll)
    write_audit(repo, actor=actor, action="poll.announce", target=poll.poll_id, detail=poll.title)
    return poll


def remind(repo: Repository, poll: SchedulePoll, actor: str, now: datetime) -> list[str]:
    if not is_configured():
        raise PollError(400, "LIFF が未設定です（LIFF_ID・LINE_LOGIN_CHANNEL_ID）。")
    if poll.status != PollStatus.open:
        raise PollError(409, "受付中の日程調整だけ催促できます。")
    pending = summary(repo, poll)["pending"]
    if not pending:
        raise PollError(409, "未回答の方はいません。")
    if poll.group_id:
        _send(poll.group_id, [TextMessage(text=remind_text(poll, pending))])
    else:
        # 個別に送るときは、他の人の名前を並べず本人へのお願いにする
        answered = {r.member_id for r in repo.list_poll_responses(poll.poll_id) if r.member_id}
        waiting = [m for m in target_members(repo, poll) if m.member_id not in answered]
        _send_individually(waiting, [TextMessage(text=remind_personal_text(poll))])
    repo.save_poll(poll.model_copy(update={"reminded_at": now}))
    write_audit(repo, actor=actor, action="poll.remind", target=poll.poll_id,
                detail="・".join(pending))
    return pending


def decide(repo: Repository, poll: SchedulePoll, candidate_id: str, actor: str, now: datetime,
           *, notify: bool = True) -> tuple[SchedulePoll, Event]:
    """候補日で決定し、イベント（下書き）を作ってカレンダーへ登録、グループへ連絡する。"""
    if poll.status == PollStatus.decided:
        raise PollError(409, "すでに決定済みです。")
    candidate = poll.candidate(candidate_id)
    if candidate is None:
        raise PollError(400, "候補日が見つかりません。")
    event = Event(
        event_id=f"ev_{uuid.uuid4().hex[:10]}", type=poll.event_type, title=poll.title,
        datetime_start=candidate.start, datetime_end=candidate.end, location=poll.location,
        target_scope=poll.target_scope, fiscal_year=poll.fiscal_year,
        status=EventStatus.draft, updated_at=now,
    )
    repo.upsert_event(event)
    event = push_event(repo, event)
    poll = poll.model_copy(update={"status": PollStatus.decided,
                                   "decided_candidate_id": candidate_id,
                                   "event_id": event.event_id})
    repo.save_poll(poll)
    write_audit(repo, actor=actor, action="poll.decide", target=poll.poll_id,
                detail=f"{poll.title} {candidate_label(candidate.start, candidate.end)}")
    if notify:
        _deliver(repo, poll, [TextMessage(text=decided_text(poll))])
    return poll, event


def new_poll_id() -> str:
    return f"poll_{uuid.uuid4().hex[:10]}"


def new_candidate_id(i: int) -> str:
    return f"c{i + 1}"
