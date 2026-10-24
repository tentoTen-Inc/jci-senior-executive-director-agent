"""リッチメニューのボタンでトークに返す一覧（docs/liff-menu-design.md §4）。

LIFF の画面ができるまで（または LIFF が未設定のとき）の代わり。いずれも読むだけで書き込まない。
役割の確認はここでも行う（委員会の議案は理事、運営のタブは五役・事務局）。
"""
from __future__ import annotations

from datetime import datetime

from linebot.v3.messaging import MessageAction, QuickReply, QuickReplyItem, TextMessage

from . import roles
from . import schedule_poll as sp
from .events import resolve_targets
from .jpdate import month_day_time
from .models import (
    AttendanceStatus,
    EventStatus,
    Member,
    PollStatus,
    Proposal,
    ProposalStage,
)
from .repository import Repository

ADMIN_URL = "https://admin.647jc.org"
NOT_OPS_TEXT = "このボタンは五役・監事・事務局のみご利用いただけます。"
NOT_BOARD_TEXT = (
    "委員会の議案は、理事メンバー（理事長・副理事長・専務理事・委員長）のみご覧いただけます。"
)
STAGE_LABELS = {
    ProposalStage.entry: "エントリー", ProposalStage.submitted: "資料提出",
    ProposalStage.sed_review: "専務確認", ProposalStage.goyaku: "五役会",
    ProposalStage.board: "理事会上程", ProposalStage.decided: "審議結果",
    ProposalStage.executing: "事業実施", ProposalStage.reported: "報告",
    ProposalStage.verified: "検証",
}
ASK_EXAMPLES = ("次の例会はいつ？", "去年の4月例会はどんな内容だった？", "総会の資料はどこにある？")
MAX_LINES = 15


def ask_message() -> TextMessage:
    """［AIに聞く］: 質問の例。トークの質問には資料・やり取りを検索して答える（書き込みなし）。"""
    return TextMessage(
        text=("知りたいことを、そのままトークに送ってください。LOM の資料やこれまでのやり取りから"
              "探して答えます（資料や予定を書き換えることはありません）。\n\n例:\n"
              + "\n".join(f"・{q}" for q in ASK_EXAMPLES)),
        quick_reply=QuickReply(items=[
            QuickReplyItem(action=MessageAction(label=q[:20], text=q)) for q in ASK_EXAMPLES]))


def polls_message(repo: Repository, member: Member) -> TextMessage:
    """［日程調整］: 回答待ちの日程調整（回答画面へのリンク）。"""
    lines = []
    for poll in sorted(repo.list_polls(), key=lambda p: p.deadline or datetime.max):
        if poll.status != PollStatus.open:
            continue
        if member.member_id not in {m.member_id for m in sp.target_members(repo, poll)}:
            continue
        answered = any(r.line_user_id == member.line_user_id or r.member_id == member.member_id
                       for r in repo.list_poll_responses(poll.poll_id))
        deadline = f"〆{month_day_time(poll.deadline)}" if poll.deadline else ""
        line = f"・{poll.title}（{'回答済み' if answered else '未回答'}）{deadline}"
        if sp.is_configured():
            line += f"\n  {sp.liff_url(poll.poll_id)}"
        lines.append(line)
    if not lines:
        return TextMessage(text="いま回答をお願いしている日程調整はありません。")
    return TextMessage(text="【日程調整】\n" + "\n".join(lines[:MAX_LINES]))


def _next_deadline(p: Proposal) -> str:
    dates = [d for d in (p.deadlines.entry, p.deadlines.submit, p.deadlines.deliver) if d]
    return f"〆{month_day_time(min(dates))}" if dates else ""


def proposals_message(repo: Repository, member: Member) -> TextMessage:
    """［委員会の議案］: 見られる委員会の議案（状態・締切・議案書リンク）。"""
    if not roles.is_board(member):
        return TextMessage(text=NOT_BOARD_TEXT)
    proposals = [p for p in repo.list_proposals(status="open") if p.committee]
    allowed = set(roles.proposal_committees(member, sorted({p.committee for p in proposals})))
    shown = sorted((p for p in proposals if p.committee in allowed),
                   key=lambda p: (p.committee, p.event_date or ""))
    if not shown:
        return TextMessage(text="見られる委員会の議案はまだありません。")
    lines = []
    for p in shown[:MAX_LINES]:
        stage = STAGE_LABELS.get(p.stage, p.stage)
        line = f"・{p.committee}｜{p.title}（{stage}）{_next_deadline(p)}"
        if p.storage_uri:
            line += f"\n  {p.storage_uri}"
        lines.append(line)
    return TextMessage(text="【委員会の議案】\n" + "\n".join(lines))


def attendance_status_message(repo: Repository, member: Member, now: datetime) -> TextMessage:
    """［出欠の状況］: 直近の受付中の予定の出欠と未回答者。"""
    if not roles.is_ops(member):
        return TextMessage(text=NOT_OPS_TEXT)
    events = sorted((e for e in repo.list_events(status=EventStatus.open)
                     if e.datetime_start >= now), key=lambda e: e.datetime_start)
    if not events:
        return TextMessage(text="出欠を受け付けている予定はありません。")
    event = events[0]
    counts: dict[str, int] = {}
    missing = []
    for target in resolve_targets(repo, event):
        att = repo.get_attendance(event.event_id, target.member_id)
        status = att.status if att else AttendanceStatus.未回答
        counts[status.value] = counts.get(status.value, 0) + 1
        if status == AttendanceStatus.未回答:
            missing.append(target.name)
    summary = "／".join(f"{k} {v}" for k, v in counts.items())
    text = f"【出欠の状況】{month_day_time(event.datetime_start)} {event.title}\n{summary}"
    if missing:
        text += "\n\n未回答: " + "、".join(missing[:30])
    return TextMessage(text=text + f"\n\n詳しくは管理画面: {ADMIN_URL}")


def approvals_message(repo: Repository, member: Member) -> TextMessage:
    """［承認待ち］: LINE 連携の申請と、専務確認待ちの議案。"""
    if not roles.is_ops(member):
        return TextMessage(text=NOT_OPS_TEXT)
    from .line_link import pending_list

    requests = pending_list(repo)["requests"]
    lines = ["【LINE 連携の申請】"]
    lines += [f"・{r.get('member_name') or '（会員未選択）'} ← "
              f"{r.get('display_name') or 'LINE名なし'}" for r in requests] or ["・ありません"]
    review = [p for p in repo.list_proposals(status="open")
              if p.stage == ProposalStage.sed_review and p.sed_approval.status == "pending"]
    lines.append("\n【専務確認待ちの議案】")
    lines += [f"・{p.committee or ''}｜{p.title}" for p in review] or ["・ありません"]
    return TextMessage(text="\n".join(lines) + f"\n\n承認は管理画面から: {ADMIN_URL}")


def poll_new_message(member: Member) -> TextMessage:
    """［日程調整を作る］: いまは管理画面で作る。"""
    if not roles.is_ops(member):
        return TextMessage(text=NOT_OPS_TEXT)
    return TextMessage(text=f"日程調整は管理画面の「日程調整」から作れます。\n{ADMIN_URL}")
