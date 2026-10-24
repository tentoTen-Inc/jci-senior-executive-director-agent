"""LIFF の画面（docs/liff-menu-design.md §3）と、その画面が呼ぶ API。

既存の LIFF アプリ（/liff/poll）に `?view=` の切り替えを足す。画面の処理は /liff/views.js。
本人確認は LINE の IDトークン（schedule_poll.verify_id_token）。**役割に応じたアクセス制御は
ここ（サーバー側）で必ず行う**。書き込みは画面の［送信］系のボタンから呼ばれる API だけ。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from . import committee_docs, doc_session, feedback_box, line_link, line_push, roles
from . import schedule_poll as sp
from .attendance import aggregate, record_attendance
from .audit import write_audit
from .delivery import execute_delivery
from .deps import get_repo
from .events import resolve_targets
from .guardrails import is_quiet_hours
from .jpdate import month_day_time
from .line_messages import build_attendance_request
from .menu_lists import NOT_BOARD_TEXT, NOT_OPS_TEXT, STAGE_LABELS
from .models import (
    AttendanceStatus,
    DeliveryJob,
    DeliveryStatus,
    Event,
    EventStatus,
    EventType,
    Member,
    MemberStatus,
    PollStatus,
    ProposalStage,
    SchedulePoll,
    TargetScope,
    TargetScopeKind,
)
from .polls_api import PollCreate
from .polls_api import create as create_poll
from .reminders import stage_job_id
from .repository import Repository

logger = logging.getLogger("jci-agent.liff")

router = APIRouter(tags=["liff"])

VIEWS_JS = Path(__file__).parent / "static" / "liff_views.js"
#: LIFF で回答できる出欠（委任・未回答は選ばせない）
ANSWERS = [AttendanceStatus.出席, AttendanceStatus.WEB出席, AttendanceStatus.欠席]
#: 終了時刻の無い予定は、開始からこの時間までを「これから」に含める
DEFAULT_DURATION = timedelta(hours=2)


class LiffError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _error(exc: Exception) -> JSONResponse:
    status = getattr(exc, "status", 500)
    return JSONResponse({"detail": getattr(exc, "message", "エラーが発生しました。")},
                        status_code=status)


def _actor(member: Member) -> str:
    return f"liff:{member.member_id}"


def viewer(request: Request) -> Member:
    """IDトークンで本人を確かめ、LINE 連携済みの会員を返す。"""
    if not sp.is_configured():
        raise LiffError(503, "LINE の画面は準備中です。")
    claims = sp.verify_id_token(request.headers.get("Authorization"))
    member = get_repo().get_member_by_line_user_id(claims["sub"])
    if member is None or member.status != MemberStatus.active:
        raise LiffError(403, "LINE 連携が済んでいません。事務局にお問い合わせください。")
    return member


def ops_viewer(request: Request) -> Member:
    """五役・監事・事務局だけ（運営のタブの画面）。"""
    member = viewer(request)
    if not roles.is_ops(member):
        raise LiffError(403, NOT_OPS_TEXT)
    return member


@router.get("/liff/views.js")
def views_js():
    return Response(VIEWS_JS.read_text(encoding="utf-8"), media_type="application/javascript",
                    headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- #
# 予定と出欠（全員）
# --------------------------------------------------------------------------- #
def _upcoming(repo: Repository, member: Member, now: datetime) -> list[Event]:
    events = [e for e in repo.list_events(status=EventStatus.open)
              if (e.datetime_end or e.datetime_start + DEFAULT_DURATION) >= now
              and any(t.member_id == member.member_id for t in resolve_targets(repo, e))]
    return sorted(events, key=lambda e: e.datetime_start)


def events_view(repo: Repository, member: Member, now: datetime) -> dict:
    rows = []
    for e in _upcoming(repo, member, now):
        att = repo.get_attendance(e.event_id, member.member_id)
        rows.append({
            "event_id": e.event_id, "title": e.title, "type": str(e.type),
            "when": month_day_time(e.datetime_start), "location": e.location,
            "status": (att.status if att else AttendanceStatus.未回答).value,
            "deadline": month_day_time(e.attendance_deadline) if e.attendance_deadline else None,
        })
    return {"member_name": member.name, "events": rows, "answers": [a.value for a in ANSWERS]}


def save_answers(repo: Repository, member: Member, answers: dict, now: datetime) -> int:
    """［この内容で回答する］: 変更のあった出欠だけ記録する（対象・受付中・これからの予定のみ）。"""
    allowed = {e.event_id: e for e in _upcoming(repo, member, now)}
    changed = 0
    for event_id, value in (answers or {}).items():
        if event_id not in allowed:
            raise LiffError(403, "回答できない予定が含まれています。")
        try:
            status = AttendanceStatus(value)
        except ValueError:
            raise LiffError(400, "出欠の選び方が正しくありません。") from None
        if status not in ANSWERS:
            raise LiffError(400, "出欠の選び方が正しくありません。")
        att = repo.get_attendance(event_id, member.member_id)
        if att is not None and att.status == status:
            continue
        record_attendance(repo, event_id, member.member_id, status, now=now)
        changed += 1
    return changed


@router.get("/liff/api/me/events")
def liff_events(request: Request):
    try:
        member = viewer(request)
        return events_view(get_repo(), member, datetime.now())
    except (LiffError, sp.PollError) as exc:
        return _error(exc)


@router.put("/liff/api/me/attendance")
def liff_attendance(request: Request, body: dict = Body(default_factory=dict)):
    try:
        member = viewer(request)
        now = datetime.now()
        changed = save_answers(get_repo(), member, body.get("answers") or {}, now)
        return {**events_view(get_repo(), member, now), "changed": changed}
    except (LiffError, sp.PollError) as exc:
        return _error(exc)


# --------------------------------------------------------------------------- #
# 日程調整（全員）
# --------------------------------------------------------------------------- #
def polls_view(repo: Repository, member: Member) -> dict:
    rows = []
    for poll in repo.list_polls():
        if poll.status != PollStatus.open:
            continue
        if member.member_id not in {m.member_id for m in sp.target_members(repo, poll)}:
            continue
        answered = any(r.line_user_id == member.line_user_id or r.member_id == member.member_id
                       for r in repo.list_poll_responses(poll.poll_id))
        rows.append({"poll_id": poll.poll_id, "title": poll.title, "answered": answered,
                     "deadline": month_day_time(poll.deadline) if poll.deadline else None,
                     "candidates": len(poll.candidates)})
    rows.sort(key=lambda r: (r["answered"], r["deadline"] or "~"))
    return {"polls": rows}


@router.get("/liff/api/me/polls")
def liff_polls(request: Request):
    try:
        return polls_view(get_repo(), viewer(request))
    except (LiffError, sp.PollError) as exc:
        return _error(exc)


# --------------------------------------------------------------------------- #
# 委員会の議案（理事・五役）
# --------------------------------------------------------------------------- #
DEADLINES = (("entry", "エントリー"), ("submit", "資料提出"), ("deliver", "配信"))
DOC_ID_IN_URL = re.compile(r"/document/d/([\w-]+)")


def _doc_id(proposal) -> str | None:
    if proposal.doc_id:
        return proposal.doc_id
    found = DOC_ID_IN_URL.search(proposal.storage_uri or "")
    return found.group(1) if found else None


def _visible_proposals(repo: Repository, member: Member) -> list:
    """見られる議案（理事は自分の委員会、五役・事務局は全委員会）。"""
    if not roles.is_board(member) and not roles.is_ops(member):
        raise LiffError(403, NOT_BOARD_TEXT)
    proposals = [p for p in repo.list_proposals(status="open") if p.committee]
    allowed = set(roles.proposal_committees(member, sorted({p.committee for p in proposals})))
    return sorted((p for p in proposals if p.committee in allowed),
                  key=lambda p: (p.committee, p.event_date or "", p.title))


def proposals_view(repo: Repository, member: Member) -> dict:
    rows = []
    for p in _visible_proposals(repo, member):
        deadlines = [{"label": label, "at": month_day_time(getattr(p.deadlines, key))}
                     for key, label in DEADLINES if getattr(p.deadlines, key)]
        rows.append({
            "proposal_id": p.proposal_id, "committee": p.committee, "title": p.title,
            "stage": STAGE_LABELS.get(p.stage, str(p.stage)), "event_date": p.event_date,
            "deadlines": deadlines, "url": p.storage_uri,
            "feedback": [f.model_dump() for f in p.feedback],
            "can_work": bool(_doc_id(p)) and committee_docs.can_edit(member, p.committee),
        })
    return {"proposals": rows, "work_enabled": committee_docs.is_enabled()}


def start_work(repo: Repository, member: Member, proposal_id: str, now: datetime) -> str:
    """［この資料で作業する］: サーバーで作業セッションを始め、ボットからトークに送る。"""
    proposal = next((p for p in _visible_proposals(repo, member)
                     if p.proposal_id == proposal_id), None)
    if proposal is None:
        raise LiffError(404, "議案が見つかりません。")
    doc_id = _doc_id(proposal)
    if not doc_id or not committee_docs.can_edit(member, proposal.committee):
        raise LiffError(403, doc_session.NOT_EDITABLE_TEXT)
    if not committee_docs.is_enabled():
        raise LiffError(503, doc_session.DISABLED_TEXT)
    messages = doc_session.open_session(repo, member, doc_id, proposal.title,
                                        proposal.committee, now)
    try:
        sent = line_push.push_messages(member.line_user_id, messages)
    except Exception:  # noqa: BLE001 - 友だち未追加など。セッションは始まっている
        logger.exception("作業開始の送信に失敗: member=%s", member.member_id)
        sent = False
    if not sent:
        raise LiffError(503, "作業は始めましたが、トークに送れませんでした。"
                             "トークを開いて、直したい内容を送ってください。")
    return proposal.title


@router.get("/liff/api/proposals")
def liff_proposals(request: Request):
    try:
        return proposals_view(get_repo(), viewer(request))
    except (LiffError, sp.PollError) as exc:
        return _error(exc)


@router.post("/liff/api/proposals/{proposal_id}/work")
def liff_proposal_work(proposal_id: str, request: Request):
    try:
        title = start_work(get_repo(), viewer(request), proposal_id, datetime.now())
        return {"ok": True, "title": title}
    except (LiffError, sp.PollError) as exc:
        return _error(exc)


# --------------------------------------------------------------------------- #
# 出欠の状況（五役・事務局）
# --------------------------------------------------------------------------- #
MAX_STATUS_EVENTS = 10
#: 同じ予定への［未回答者に催促］は、この間に1回まで（押し間違いでの二重送信を防ぐ）
REMIND_INTERVAL = timedelta(hours=1)


def _remind_job_id(event_id: str, now: datetime) -> str:
    return stage_job_id(event_id, f"liff_{now:%Y%m%d%H}")


def attendance_status_view(repo: Repository, now: datetime) -> dict:
    events = sorted((e for e in repo.list_events(status=EventStatus.open)
                     if (e.datetime_end or e.datetime_start + DEFAULT_DURATION) >= now),
                    key=lambda e: e.datetime_start)[:MAX_STATUS_EVENTS]
    names = {m.member_id: m.name for m in repo.list_members()}
    rows = []
    for e in events:
        summary = aggregate(repo, e.event_id)
        rows.append({
            "event_id": e.event_id, "title": e.title, "when": month_day_time(e.datetime_start),
            "location": e.location, "targets": summary.total_targets,
            "counts": {k: v for k, v in summary.counts.items() if v},
            "unanswered": [names.get(i, i) for i in summary.unanswered_member_ids],
            "reminded": repo.get_delivery_job(_remind_job_id(e.event_id, now)) is not None,
        })
    return {"events": rows}


def remind_unanswered(repo: Repository, member: Member, event_id: str, now: datetime) -> dict:
    """［未回答者に催促］（画面で確認したあと）: 未回答者に出欠のお願いを個別に送る。

    送り方は定期の催促と同じ（出欠依頼メッセージ・配信のガードレール・配信記録）。
    """
    event = repo.get_event(event_id)
    if (event is None or event.status != EventStatus.open
            or (event.datetime_end or event.datetime_start + DEFAULT_DURATION) < now):
        raise LiffError(404, "出欠を受け付けている予定が見つかりません。")
    settings = repo.get_settings()
    if is_quiet_hours(now, settings):
        raise LiffError(409, f"夜間（{settings.quiet_hours.start}〜{settings.quiet_hours.end}）"
                             "は催促を送れません。朝になってからお試しください。")
    job_id = _remind_job_id(event_id, now)
    if repo.get_delivery_job(job_id) is not None:
        raise LiffError(409, "この予定には少し前に催促を送っています。時間をおいてください。")
    targets = aggregate(repo, event_id).unanswered_member_ids
    if not targets:
        raise LiffError(409, "未回答の方はいません。")
    job = DeliveryJob(job_id=job_id, type="reminder", event_id=event_id, stage="liff",
                      targets=targets, scheduled_at=now, status=DeliveryStatus.queued)
    repo.save_delivery_job(job)
    message = build_attendance_request(event)
    report = execute_delivery(repo, job, message.text, now=now,
                              sender=line_push.member_messages_sender(repo, [message]))
    write_audit(repo, actor=_actor(member), action="event.remind", target=event_id,
                detail=f"未回答 {len(targets)} 人・送信 {report.sent} 人")
    if report.halted:
        raise LiffError(409, "安全確認のため送信を止めました: " + "、".join(report.problems))
    return {"sent": report.sent, "blocked": report.blocked, "failed": report.failed}


@router.get("/liff/api/ops/attendance")
def liff_attendance_status(request: Request):
    try:
        ops_viewer(request)
        return attendance_status_view(get_repo(), datetime.now())
    except (LiffError, sp.PollError) as exc:
        return _error(exc)


@router.post("/liff/api/ops/events/{event_id}/remind")
def liff_remind(event_id: str, request: Request):
    try:
        member = ops_viewer(request)
        repo, now = get_repo(), datetime.now()
        result = remind_unanswered(repo, member, event_id, now)
        return {**result, **attendance_status_view(repo, now)}
    except (LiffError, sp.PollError) as exc:
        return _error(exc)


# --------------------------------------------------------------------------- #
# 日程調整を作る（五役・事務局）
# --------------------------------------------------------------------------- #
GOYAKU_ROLES = ["理事長", "副理事長", "専務理事", "監事"]
RIJI_ROLES = ("委員長", "副委員長")
#: 対象者と案内先のグループ（管理画面の「日程調整」と同じ）。委員会は委員会を選ぶ
POLL_TARGETS = {
    "goyaku": {"label": "五役", "audience": "五役の皆さま", "group": "2027年度　五役",
               "event_type": EventType.五役会},
    "riji": {"label": "理事", "audience": "理事の皆さま", "group": "2027 猪苗代青年会議所 理事会",
             "event_type": EventType.理事会},
    "committee": {"label": "委員会", "audience": None, "group": None,
                  "event_type": EventType.委員会},
    "all": {"label": "会員全員", "audience": "会員の皆さま", "group": "【JCI猪苗代】",
            "event_type": EventType.例会},
}


def _norm(name: str) -> str:
    return re.sub(r"[\s　]", "", name)


def _find_group(repo: Repository, name: str):
    """名前でグループを探す（完全一致 → 空白を無視して一致 → 含む。抜けたグループは除く）。"""
    groups = sp.active_groups(repo)
    return (next((g for g in groups if g.group_name == name), None)
            or next((g for g in groups if _norm(g.group_name) == _norm(name)), None)
            or next((g for g in groups if _norm(name) in _norm(g.group_name)), None))


def _target_scope(repo: Repository, target: str, committee: str | None = None) -> TargetScope:
    if target == "goyaku":
        return TargetScope(kind=TargetScopeKind.officer, value=GOYAKU_ROLES)
    if target == "riji":
        ids = [m.member_id for m in repo.list_members() if m.status == MemberStatus.active
               and (m.officer_role in GOYAKU_ROLES
                    or any(c.role in RIJI_ROLES for c in m.committees))]
        return TargetScope(kind=TargetScopeKind.custom, value=ids)
    if target == "committee":
        return TargetScope(kind=TargetScopeKind.committee, value=[committee or ""])
    return TargetScope(kind=TargetScopeKind.all)


def _committee_destination(repo: Repository, committee: str) -> dict:
    """委員会の送り先: 名前に委員会名を含むグループ、無ければ連携済みの対象者に個別。"""
    group = sp.committee_group(repo, committee)
    probe = SchedulePoll(poll_id="-", title="-", event_type=EventType.委員会,
                         group_id=group.group_id if group else None,
                         target_scope=_target_scope(repo, "committee", committee))
    return sp.destination(repo, probe)


def poll_form_view(repo: Repository) -> dict:
    targets = []
    for key, t in POLL_TARGETS.items():
        group = _find_group(repo, t["group"]) if t["group"] else None
        targets.append({"value": key, "label": t["label"], "event_type": t["event_type"].value,
                        "group_name": group.group_name if group else None})
    committees = [{"name": name, **_committee_destination(repo, name)}
                  for name in sp.committee_names(repo)]
    return {"targets": targets, "committees": committees,
            "event_types": [t.value for t in EventType]}


def make_poll(repo: Repository, member: Member, body: dict, now: datetime) -> dict:
    """［作成する］: 日程調整を作る（まだ案内しない）。案内先は対象者から決める。"""
    key = body.get("target") or ""
    target = POLL_TARGETS.get(key)
    if target is None:
        raise LiffError(400, "対象者を選んでください。")
    committee = None
    if key == "committee":
        committee = body.get("committee") or ""
        if committee not in sp.committee_names(repo):
            raise LiffError(400, "委員会を選んでください。")
        group = sp.committee_group(repo, committee)  # 無ければ個別に送る
        audience = f"{committee}の皆さま"
    else:
        group = _find_group(repo, target["group"])
        if group is None:
            raise LiffError(409, f"案内先のグループ「{target['group']}」が見つかりません。"
                                 "管理画面から作成してください。")
        audience = target["audience"]
    try:
        payload = PollCreate(
            title=(body.get("title") or "").strip(), event_type=body.get("event_type"),
            location=(body.get("location") or "").strip() or None,
            audience=audience, candidates=body.get("candidates") or [],
            target_scope=_target_scope(repo, key, committee),
            group_id=group.group_id if group else None,
            deadline=body.get("deadline") or None)
    except ValidationError:
        raise LiffError(400, "入力を確かめてください（会議の名前・候補日時は1〜10件）。") from None
    if not payload.title:
        raise LiffError(400, "会議の名前を入力してください。")
    poll = create_poll(repo, payload, _actor(member), now)
    dest = sp.destination(repo, poll)
    return {"poll_id": poll.poll_id, "title": poll.title, "group_name": dest["group_name"],
            "mode": dest["mode"], "linked": dest["linked"],
            "candidates": [sp.candidate_label(c.start, c.end) for c in poll.candidates],
            "targets": dest["targets"]}


def announce_poll(repo: Repository, member: Member, poll_id: str, now: datetime) -> dict:
    """［案内する］（画面で送り先を確認したあと）。"""
    poll = repo.get_poll(poll_id)
    if poll is None:
        raise LiffError(404, "日程調整が見つかりません。")
    if poll.announced_at:
        raise LiffError(409, "この日程調整はすでに案内しています。")
    sp.announce(repo, poll, _actor(member), now)
    return {"ok": True, **sp.destination(repo, poll)}


@router.get("/liff/api/ops/poll-form")
def liff_poll_form(request: Request):
    try:
        ops_viewer(request)
        return poll_form_view(get_repo())
    except (LiffError, sp.PollError) as exc:
        return _error(exc)


@router.post("/liff/api/ops/polls")
def liff_poll_create(request: Request, body: dict = Body(default_factory=dict)):
    try:
        member = ops_viewer(request)
        return make_poll(get_repo(), member, body, datetime.now())
    except (LiffError, sp.PollError) as exc:
        return _error(exc)


@router.post("/liff/api/ops/polls/{poll_id}/announce")
def liff_poll_announce(poll_id: str, request: Request):
    try:
        member = ops_viewer(request)
        return announce_poll(get_repo(), member, poll_id, datetime.now())
    except (LiffError, sp.PollError) as exc:
        return _error(exc)


# --------------------------------------------------------------------------- #
# 承認待ち（五役・事務局。連携の承認・却下は専務理事のみ＝LINE のボタンと同じ）
# --------------------------------------------------------------------------- #
def approvals_view(repo: Repository, member: Member) -> dict:
    requests = [{"line_user_id": r["line_user_id"], "display_name": r.get("display_name"),
                 "member_name": r.get("member_name"),
                 "requested_at": r.get("requested_at")}
                for r in line_link.pending_list(repo)["requests"]]
    review = [{"proposal_id": p.proposal_id, "committee": p.committee, "title": p.title,
               "url": p.storage_uri}
              for p in repo.list_proposals(status="open")
              if p.stage == ProposalStage.sed_review and p.sed_approval.status == "pending"]
    return {"requests": requests, "review": review,
            "can_decide": member.officer_role in line_link.APPROVER_ROLES}


def decide_link(repo: Repository, member: Member, line_user_id: str, action: str,
                now: datetime) -> str:
    if member.officer_role not in line_link.APPROVER_ROLES:
        raise LiffError(403, "LINE 連携の承認は専務理事のみ行えます。")
    if action == "approve":
        return f"{line_link.approve(repo, line_user_id, _actor(member), now).name} さんの" \
               "LINE 連携を承認しました。"
    line_link.reject(repo, line_user_id, _actor(member), now)
    return "申請を却下しました。"


@router.get("/liff/api/ops/approvals")
def liff_approvals(request: Request):
    try:
        return approvals_view(get_repo(), ops_viewer(request))
    except (LiffError, sp.PollError) as exc:
        return _error(exc)


@router.post("/liff/api/ops/link-requests/{line_user_id}/{action}")
def liff_link_decide(line_user_id: str, action: str, request: Request):
    try:
        member = ops_viewer(request)
        if action not in ("approve", "reject"):
            raise LiffError(404, "操作が見つかりません。")
        repo = get_repo()
        text = decide_link(repo, member, line_user_id, action, datetime.now())
        return {"message": text, **approvals_view(repo, member)}
    except (LiffError, sp.PollError) as exc:
        return _error(exc)


# --------------------------------------------------------------------------- #
# 意見箱（全員。docs/feedback-box-design.md §3）
# --------------------------------------------------------------------------- #
@router.get("/liff/api/feedback")
def liff_feedback_form(request: Request):
    try:
        viewer(request)
        return {"kinds": [{"value": k.value, "label": feedback_box.KIND_LABELS[k]}
                          for k in feedback_box.SELECTABLE],
                "max_length": feedback_box.TEXT_MAX}
    except (LiffError, sp.PollError) as exc:
        return _error(exc)


@router.post("/liff/api/feedback")
def liff_feedback_send(request: Request, body: dict = Body(default_factory=dict)):
    """［送る］（画面で確認したあと）。匿名なら会員ID を保存しない。"""
    try:
        member = viewer(request)
        feedback_box.submit(get_repo(), member, str(body.get("kind") or ""),
                            str(body.get("text") or ""), bool(body.get("anonymous")),
                            datetime.now())
        return {"ok": True}
    except (LiffError, sp.PollError, feedback_box.FeedbackError) as exc:
        return _error(exc)
