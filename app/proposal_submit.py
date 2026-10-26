"""議案の［提出する］と、専務への提出・提出締切の知らせ（docs/proposal-ledger-design.md §10）。

- LIFF「委員会の議案」の［提出する］／［提出を取り消す］（ボタンからだけ。チャットは検索のみ）。
  押せるのは、その委員会の資料を更新できる理事メンバー（committee_docs.can_edit）。
- 提出されたら専務理事に「【提出】…」を知らせる。静音時間は明けた tick で送る。
- 提出締切（deadlines.submit）を過ぎたら、最初の tick（静音時間を除く）で、上程先の会議ごとに
  各議案の段階（提出済み／未提出）・空欄の欄の数・議案書リンクをまとめて1回だけ知らせる。
- 送った知らせは `Proposal.notices` に記録し、同じ知らせを二度送らない。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import committee_docs
from .audit import write_audit
from .jpdate import month_day_time
from .models import Member, Proposal, ProposalHistory, ProposalStage
from .proposal_sync import SUMMARY_TYPE, blank_count
from .repository import Repository
from .sed_notice import notify_sed

JST = timezone(timedelta(hours=9))
STAGE_ORDER = list(ProposalStage)
#: LIFF から提出したときの履歴の by（専務への提出の知らせはこれだけを対象にする）
LIFF_BY_PREFIX = "LINE: "
#: 締切を過ぎてからこの期間を過ぎた締切は知らせない（運用開始前の古い締切で一斉に送らない）
DEADLINE_WINDOW = timedelta(days=3)
NO_MEETING = "上程先未設定"


class SubmitError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(JST).replace(tzinfo=None) if dt.tzinfo else dt


def submitted(p: Proposal) -> bool:
    return STAGE_ORDER.index(ProposalStage(p.stage)) >= STAGE_ORDER.index(ProposalStage.submitted)


def meeting_label(repo: Repository, p: Proposal) -> str | None:
    """上程先の会議。日程調整中なら「〇〇（日程調整中）」、決まれば会議の名前と日付。"""
    event_id = p.event_id
    poll = repo.get_poll(p.meeting_poll_id) if p.meeting_poll_id else None
    if not event_id and poll is not None:
        if not poll.event_id:
            return f"{poll.title}（日程調整中）"
        event_id = poll.event_id
    event = repo.get_event(event_id) if event_id else None
    if event is not None:
        return f"{event.title}（{month_day_time(event.datetime_start)}）"
    return poll.title if poll is not None else None


def doc_label(p: Proposal) -> str:
    if p.doc_type == SUMMARY_TYPE:
        return f"{p.committee or ''} 年間計画（サマリー）".strip()
    return f"{p.committee or ''}「{p.title}」"


# --------------------------------------------------------------------------- #
# ［提出する］・［提出を取り消す］
# --------------------------------------------------------------------------- #
def can_submit(member: Member, p: Proposal) -> bool:
    return (p.status == "open" and p.stage == ProposalStage.entry and bool(p.committee)
            and committee_docs.can_edit(member, p.committee))


def can_withdraw(member: Member, p: Proposal) -> bool:
    return (p.status == "open" and p.stage == ProposalStage.submitted and bool(p.committee)
            and committee_docs.can_edit(member, p.committee))


def _get(repo: Repository, proposal_id: str) -> Proposal:
    p = repo.get_proposal(proposal_id)
    if p is None or p.status != "open":
        raise SubmitError(404, "議案が見つかりません。")
    return p


def _change(repo: Repository, member: Member, p: Proposal, stage: ProposalStage,
            now: datetime, action: str) -> Proposal:
    by = f"{LIFF_BY_PREFIX}{member.name}"
    p = p.model_copy(update={
        "stage": stage, "history": [*p.history, ProposalHistory(at=now, stage=stage, by=by)]})
    repo.upsert_proposal(p)
    write_audit(repo, actor=f"liff:{member.member_id}", action=action, target=p.proposal_id,
                detail=doc_label(p))
    return p


def submit(repo: Repository, member: Member, proposal_id: str, now: datetime) -> Proposal:
    """エントリー → 資料提出。専務への知らせはすぐ送る（静音時間なら明けてから）。"""
    p = _get(repo, proposal_id)
    if not committee_docs.can_edit(member, p.committee or ""):
        raise SubmitError(403, "この委員会の資料を提出できるのは、理事長・専務理事・担当副理事長・"
                               "委員長です。")
    if p.stage != ProposalStage.entry:
        raise SubmitError(409, "エントリーの議案だけ提出できます。画面を開き直してください。")
    p = _change(repo, member, p, ProposalStage.submitted, now, "proposal.submit")
    _notify_submitted(repo, p, now)
    return p


def withdraw(repo: Repository, member: Member, proposal_id: str, now: datetime) -> Proposal:
    """資料提出 → エントリー（提出の取り消し）。"""
    p = _get(repo, proposal_id)
    if not committee_docs.can_edit(member, p.committee or ""):
        raise SubmitError(403, "この委員会の資料の提出を取り消せるのは、理事長・専務理事・"
                               "担当副理事長・委員長です。")
    if p.stage != ProposalStage.submitted:
        raise SubmitError(409, "資料提出の議案だけ取り消せます。画面を開き直してください。")
    return _change(repo, member, p, ProposalStage.entry, now, "proposal.withdraw")


# --------------------------------------------------------------------------- #
# 専務への知らせ
# --------------------------------------------------------------------------- #
def _submit_key(p: Proposal) -> str | None:
    """まだ知らせていない、LIFF からの提出（資料提出のままのときだけ）。"""
    if p.stage != ProposalStage.submitted or not p.history:
        return None
    last = p.history[-1]
    if last.stage != ProposalStage.submitted or not last.by.startswith(LIFF_BY_PREFIX):
        return None
    key = f"submitted:{last.at.isoformat()}"
    return None if key in p.notices else key


def submit_text(p: Proposal) -> str:
    who = p.history[-1].by.removeprefix(LIFF_BY_PREFIX)
    lines = [f"【提出】{doc_label(p)}が提出されました（{who}さん）。",
             f"空欄の欄: {blank_count(p)}"]
    if p.storage_uri:
        lines.append(p.storage_uri)
    return "\n".join(lines)


def _notify_submitted(repo: Repository, p: Proposal, now: datetime) -> bool:
    key = _submit_key(p)
    if key is None or not notify_sed(repo, submit_text(p), now, "proposal_submit_notice"):
        return False
    repo.upsert_proposal(p.model_copy(update={"notices": {**p.notices, key: now}}))
    return True


def _deadline_key(p: Proposal) -> str:
    return f"deadline:{_naive(p.deadlines.submit).isoformat()}"


def deadline_text(repo: Repository, proposals: list[Proposal]) -> str:
    """上程先の会議ごとに、段階・空欄の数・議案書リンクを並べる。"""
    groups: dict[str, list[Proposal]] = {}
    for p in proposals:
        groups.setdefault(meeting_label(repo, p) or NO_MEETING, []).append(p)
    blocks = []
    for meeting, items in groups.items():
        due = min(_naive(p.deadlines.submit) for p in items)
        done = sum(1 for p in items if submitted(p))
        lines = [f"■{meeting}", f"提出締切 {month_day_time(due)}・提出済み {done}/{len(items)}"]
        for p in sorted(items, key=lambda p: (not submitted(p), p.committee or "")):
            state = "提出済み" if submitted(p) else "未提出"
            lines.append(f"・{doc_label(p)}: {state}・空欄 {blank_count(p)}欄")
            if p.storage_uri:
                lines.append(f"  {p.storage_uri}")
        blocks.append("\n".join(lines))
    return "【提出締切】締切を過ぎた議案の記入状況です。\n\n" + "\n\n".join(blocks)


def _due_for_deadline_notice(p: Proposal, now: datetime) -> bool:
    due = p.deadlines.submit
    if p.status != "open" or due is None:
        return False
    due = _naive(due)
    return due <= now <= due + DEADLINE_WINDOW and _deadline_key(p) not in p.notices


def process(repo: Repository, now: datetime) -> dict:
    """tick: まだ知らせていない提出と、締切を過ぎた議案の記入状況を専務に知らせる。"""
    proposals = repo.list_proposals(status="open")
    sent_submits = sum(1 for p in proposals if _notify_submitted(repo, p, now))
    due = [p for p in repo.list_proposals(status="open") if _due_for_deadline_notice(p, now)]
    if due and notify_sed(repo, deadline_text(repo, due), now, "proposal_deadline_notice"):
        for p in due:
            repo.upsert_proposal(p.model_copy(update={
                "notices": {**p.notices, _deadline_key(p): now}}))
    else:
        due = []
    return {"submitted": sent_submits, "deadline": len(due)}
