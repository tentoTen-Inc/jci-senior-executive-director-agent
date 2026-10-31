"""議案の［提出する］と、専務への提出・提出締切の知らせ（docs/proposal-ledger-design.md §10）。

- LIFF「委員会の議案」の［提出する］／［提出を取り消す］（ボタンからだけ。チャットは検索のみ）。
  押せるのは、その委員会の資料を更新できる理事メンバー（committee_docs.can_edit）。
- 提出されたら専務理事に「【提出】…」を知らせる。静音時間は明けた tick で送る。
- 提出締切（deadlines.submit）を過ぎたら、最初の tick（静音時間を除く）で、上程先の会議ごとに
  各議案の段階（提出済み／未提出）・空欄の欄の数・議案書リンクをまとめて1回だけ知らせる。
- 提出締切を過ぎてもエントリーのままの議案は、担当副理事長の個別トークに「【未提出】…」と
  ［提出予定日を選ぶ］（datetimepicker）を送り、提出予定日を回収する。予定日を過ぎてもまだなら、
  翌日以降にもう一度聞く。届かなければ専務に「直接ご確認ください」と知らせる（§10.5）。
- 送った知らせは `Proposal.notices` に記録し、同じ知らせを二度送らない。
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from linebot.v3.messaging import FlexContainer, FlexMessage, Message, TextMessage

from . import committee_docs, line_push
from .audit import write_audit
from .delivery import execute_delivery
from .guardrails import is_quiet_hours
from .jpdate import month_day_time, weekday
from .models import (
    DeliveryJob,
    DeliveryStatus,
    Member,
    MemberStatus,
    Proposal,
    ProposalAlert,
    ProposalHistory,
    ProposalStage,
    SedApproval,
    SubmitPlan,
)
from .proposal_sync import SUMMARY_TYPE, blank_count
from .repository import Repository
from .sed_notice import NOTIFY_ROLES, notify_sed

logger = logging.getLogger("jci-agent.proposal_submit")

JST = timezone(timedelta(hours=9))
STAGE_ORDER = list(ProposalStage)
#: LIFF から提出したときの履歴の by（専務への提出の知らせはこれだけを対象にする）
LIFF_BY_PREFIX = "LINE: "
#: 締切を過ぎてからこの期間を過ぎた締切は知らせない（運用開始前の古い締切で一斉に送らない）
DEADLINE_WINDOW = timedelta(days=3)
NO_MEETING = "上程先未設定"
#: ［提出予定日を選ぶ］の postback（`psp|<proposal_id>`。日付は params.date）
ACTION_PLAN = "psp"


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
    if p.sed_approval.status == "returned":  # 差し戻しから出し直したら、もう一度専務が判断する
        p = p.model_copy(update={"sed_approval": SedApproval()})
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
            blanks = f"・空欄 {blank_count(p)}欄" if p.sections else ""  # 予算書などは欄を読まない
            lines.append(f"・{doc_label(p)}: {state}{blanks}")
            if not submitted(p):
                lines.append(f"  担当への連絡: {alert_status(p)}")
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
    """tick: 未提出の議案を担当副理事長に知らせ、提出・提出予定・締切の記入状況を専務に知らせる。"""
    overdue = 0
    for p in repo.list_proposals(status="open"):
        try:
            overdue += send_overdue_alert(repo, p, now)
        except Exception:  # noqa: BLE001 - 1件の失敗で他を止めない
            logger.exception("未提出の知らせに失敗: %s", p.proposal_id)
    plans = sum(1 for p in repo.list_proposals(status="open") if _notify_plan(repo, p, now))
    proposals = repo.list_proposals(status="open")
    sent_submits = sum(1 for p in proposals if _notify_submitted(repo, p, now))
    due = [p for p in repo.list_proposals(status="open") if _due_for_deadline_notice(p, now)]
    if due and notify_sed(repo, deadline_text(repo, due), now, "proposal_deadline_notice"):
        for p in due:
            repo.upsert_proposal(p.model_copy(update={
                "notices": {**p.notices, _deadline_key(p): now}}))
    else:
        due = []
    return {"submitted": sent_submits, "deadline": len(due), "overdue": overdue, "plans": plans}


# --------------------------------------------------------------------------- #
# 未提出の議案: 担当副理事長に提出予定日を聞く（§10.5）
# --------------------------------------------------------------------------- #
def day_label(d: date) -> str:
    return f"{d.month}月{d.day}日({weekday(d)})"


def supervisors(repo: Repository, committee: str) -> list[Member]:
    """担当副理事長（その委員会を supervised_committees に持つ在籍の会員）。"""
    return [m for m in repo.list_members()
            if m.status == MemberStatus.active and committee in m.supervised_committees]


def _alert_targets(repo: Repository, p: Proposal) -> list[Member]:
    """知らせる相手。担当副理事長 → 議案の担当者（予算など委員会の外の議案）→ 専務理事。"""
    owner = repo.get_member(p.owner_member_id) if p.owner_member_id else None
    return supervisors(repo, p.committee or "") or (
        [owner] if owner and owner.status == MemberStatus.active else []) or [
        m for m in repo.list_members()
        if m.officer_role in NOTIFY_ROLES and m.status == MemberStatus.active]


def latest_plan(p: Proposal) -> SubmitPlan | None:
    return p.submit_plans[-1] if p.submit_plans else None


def overdue_key(p: Proposal, now: datetime) -> str | None:
    """まだ送っていない「未提出」の知らせのキー。送るものが無ければ None。

    締切を設定した後に過ぎた締切だけを対象にする（運用開始前のデータで一斉に送らない）。
    予定日を過ぎてもまだ未提出なら、翌日以降に予定日ごとに1回。
    """
    due = p.deadlines.submit
    if p.status != "open" or p.stage != ProposalStage.entry or due is None:
        return None
    due = _naive(due)
    set_at = p.submit_deadline_set_at
    if due > now or set_at is None or _naive(set_at) > due:
        return None
    plan = latest_plan(p)
    if plan is None:
        key = f"overdue:{due.isoformat()}"
    elif now.date() > plan.date:
        key = f"plan:{plan.date.isoformat()}"
    else:
        return None
    return None if key in p.notices else key


def alert_message(p: Proposal, key: str, today: date) -> FlexMessage:
    deadline = month_day_time(_naive(p.deadlines.submit))
    plan = latest_plan(p)
    if key.startswith("plan:") and plan is not None:
        text = (f"【未提出】{doc_label(p)}は、提出予定日（{day_label(plan.date)}）を過ぎても"
                "まだ提出されていません。新しい提出予定日を教えてください。")
    else:
        text = (f"【未提出】{doc_label(p)}の提出締切（{deadline}）を過ぎています。"
                "提出予定日を教えてください。")
    buttons = [{"type": "button", "style": "primary", "action": {
        "type": "datetimepicker", "label": "提出予定日を選ぶ",
        "data": f"{ACTION_PLAN}|{p.proposal_id}", "mode": "date",
        "initial": today.isoformat(), "min": today.isoformat()}}]
    if p.storage_uri:
        buttons.append({"type": "button", "style": "link", "action": {
            "type": "uri", "label": "議案書を開く", "uri": p.storage_uri}})
    bubble = {
        "type": "bubble",
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": text, "wrap": True, "size": "sm"},
            {"type": "text", "text": "提出は LINE のメニュー［委員会の議案］の［提出する］から。",
             "wrap": True, "size": "xs", "color": "#888888", "margin": "md"},
        ]},
        "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": buttons},
    }
    return FlexMessage(alt_text=text, contents=FlexContainer.from_dict(bubble))


def _push_one(repo: Repository, member: Member, message: Message, body: str,
              now: datetime) -> bool | None:
    """1人に送る（配信のガードレール・記録あり）。送れた True、届かない False、保留 None。"""
    job = DeliveryJob(job_id=f"proposal_overdue_{now:%Y%m%d%H%M%S}_{member.member_id}"
                             f"_{uuid.uuid4().hex[:4]}",
                      type="proposal_overdue", targets=[member.member_id], scheduled_at=now,
                      status=DeliveryStatus.queued)
    repo.save_delivery_job(job)
    try:
        report = execute_delivery(repo, job, body, now=now,
                                  sender=line_push.member_messages_sender(repo, [message]))
    except Exception:  # noqa: BLE001 - 友だち未追加など。届かなかったとして扱う
        logger.exception("未提出の知らせを送れませんでした: %s", member.member_id)
        return False
    if report.halted or report.deferred:
        return None
    return report.sent > 0


def send_overdue_alert(repo: Repository, p: Proposal, now: datetime) -> int:
    """未提出の議案を担当副理事長に知らせる。送った（試みた）人数を返す。"""
    key = overdue_key(p, now)
    if key is None or is_quiet_hours(now, repo.get_settings()):
        return 0
    message = alert_message(p, key, now.date())
    alerts = []
    for member in _alert_targets(repo, p):
        ok = _push_one(repo, member, message, message.alt_text, now)
        if ok is None:  # 停止中など。次の tick でまとめて送り直す
            return 0
        alerts.append(ProposalAlert(key=key, at=now, member_id=member.member_id,
                                    name=member.name, ok=ok))
        if not ok and member.officer_role not in NOTIFY_ROLES:
            notify_sed(repo, (f"【未提出】{p.committee}: {member.name}さんに LINE で連絡できない"
                              f"ため、直接ご確認ください。\n{doc_label(p)}"
                              + (f"\n{p.storage_uri}" if p.storage_uri else "")),
                       now, "proposal_overdue_unreachable")
    repo.upsert_proposal(p.model_copy(update={
        "notices": {**p.notices, key: now}, "alerts": [*p.alerts, *alerts]}))
    return len(alerts)


def alert_status(p: Proposal) -> str:
    """締切の記入状況に添える、担当（副理事長・議案の担当者）への連絡状況。"""
    if not p.alerts:
        return "未連絡"
    last_key = p.alerts[-1].key
    parts = [f"{a.name}さん{'に送信済み' if a.ok else 'に連絡できず'}"
             for a in p.alerts if a.key == last_key]
    plan = latest_plan(p)
    if plan is not None:
        parts.append(f"提出予定 {day_label(plan.date)}")
    return "・".join(parts)


def can_answer_plan(member: Member, p: Proposal) -> bool:
    """提出予定日を答えられる人（担当副理事長・委員長・理事長・専務理事）。"""
    return (p.committee in member.supervised_committees
            or committee_docs.can_edit(member, p.committee or "")
            or member.officer_role in NOTIFY_ROLES)


def plan_text(p: Proposal) -> str:
    plan = latest_plan(p)
    before = next((x for x in reversed(p.submit_plans[:-1]) if x.notified_at), None)
    change = f"（{day_label(before.date)} から変更）" if before else ""
    return (f"【提出予定】{p.committee}: {plan.by}さん → {day_label(plan.date)}{change}\n"
            f"{doc_label(p)}" + (f"\n{p.storage_uri}" if p.storage_uri else ""))


def _notify_plan(repo: Repository, p: Proposal, now: datetime) -> bool:
    plan = latest_plan(p)
    if plan is None or plan.notified_at is not None:
        return False
    if not notify_sed(repo, plan_text(p), now, "proposal_plan_notice"):
        return False
    plans = [*p.submit_plans[:-1], plan.model_copy(update={"notified_at": now})]
    repo.upsert_proposal(p.model_copy(update={"submit_plans": plans}))
    return True


def handle_plan_postback(repo: Repository, member: Member, data: str, params: dict | None,
                         now: datetime) -> list[Message]:
    """［提出予定日を選ぶ］（datetimepicker）で選んだ日を、議案の提出予定日として保存する。"""
    proposal_id = data.split("|", 1)[1] if "|" in data else ""
    p = repo.get_proposal(proposal_id)
    if p is None or p.status != "open":
        return [TextMessage(text="議案が見つかりません。")]
    if not can_answer_plan(member, p):
        return [TextMessage(text="この議案の提出予定日は、担当副理事長・委員長が答えられます。")]
    if p.stage != ProposalStage.entry:
        return [TextMessage(text=f"{doc_label(p)}はすでに提出されています。ありがとうございます。")]
    try:
        picked = date.fromisoformat((params or {}).get("date") or "")
    except ValueError:
        return [TextMessage(text="日付を読み取れませんでした。もう一度ボタンから選んでください。")]
    if picked < now.date():
        return [TextMessage(text="今日以降の日付を選んでください。")]
    plan = SubmitPlan(date=picked, member_id=member.member_id, by=member.name, at=now)
    p = p.model_copy(update={"submit_plans": [*p.submit_plans, plan]})
    repo.upsert_proposal(p)
    write_audit(repo, actor=f"line:{member.member_id}", action="proposal.submit_plan",
                target=p.proposal_id, detail=f"{doc_label(p)} → {picked.isoformat()}")
    _notify_plan(repo, p, now)
    return [TextMessage(text=(f"提出予定日を {day_label(picked)} で承りました。"
                              "提出するときは、メニュー［委員会の議案］の［提出する］を"
                              "押してください。日付を変えるときは、もう一度ボタンから選べます。"))]


def plan_label(p: Proposal) -> str | None:
    """画面用の提出予定日（まだ提出していないときだけ）。"""
    plan = latest_plan(p)
    if plan is None or p.stage != ProposalStage.entry:
        return None
    return f"{day_label(plan.date)}（{plan.by}さん）"
