"""管理API（最小実装, docs/mvp-design.md §8）。

役員向けの管理操作。認証は IAP（Cloud Run 前段）で行う前提で、本コードは
``X-Goog-Authenticated-User-Email`` を監査用 actor として読むのみ。
"""
from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel

from . import line_push
from .attendance import aggregate, record_attendance
from .audit import write_audit
from .delivery import execute_delivery
from .deps import get_repo
from .events import resolve_targets
from .home import build_home
from .invite import issue_invite
from .kpi import attendance_trends, kpi_overview
from .meeting_package import build_package
from .members_view import attendance_history, invite_status
from .models import (
    AttendanceStatus,
    DeliveryJob,
    DeliveryStatus,
    Event,
    EventStatus,
    EventType,
    Member,
    ReminderPolicy,
    Settings,
    TargetScope,
)
from .reminders import default_policies, resolve_audience, stage_job_id
from .summary import build_summary_text, close_event, plan_summary_notification

# prefix なし: main.py が /admin（互換）と /api（SPA用）の両方に mount する
router = APIRouter(tags=["admin"])


def _actor(email: str | None) -> str:
    return email or "unknown"


@router.get("/home")
def home():
    return build_home(get_repo(), now=datetime.now())


@router.get("/kpi/overview")
def kpi_overview_endpoint():
    return kpi_overview(get_repo(), now=datetime.now())


@router.get("/kpi/trends")
def kpi_trends_endpoint():
    return attendance_trends(get_repo())


@router.get("/delivery-logs")
def delivery_logs(result: str | None = None, limit: int = 200):
    """配信ログ（新しい順）。result=ok|blocked|failed でフィルタ。"""
    logs = sorted(get_repo().list_delivery_logs(), key=lambda x: x.sent_at, reverse=True)
    if result:
        logs = [x for x in logs if x.result.value == result]
    return logs[:limit]


# --------------------------------------------------------------------------- #
# イベント
# --------------------------------------------------------------------------- #
class EventCreate(BaseModel):
    type: EventType
    title: str
    datetime_start: datetime
    datetime_end: datetime | None = None
    location: str | None = None
    target_scope: TargetScope = TargetScope()
    attendance_deadline: datetime | None = None
    reminder_policy_id: str | None = None
    quorum: int | None = None  # 定足数（人数・F4-5）
    status: EventStatus = EventStatus.open


@router.post("/events")
def create_event(payload: EventCreate):
    repo = get_repo()
    event = Event(event_id=f"ev_{uuid.uuid4().hex[:10]}", **payload.model_dump())
    repo.upsert_event(event)
    return event


@router.get("/events")
def list_events(status: EventStatus | None = None):
    return get_repo().list_events(status=status)


@router.get("/events/{event_id}")
def get_event(event_id: str):
    event = get_repo().get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    return {"event": event, "summary": aggregate(get_repo(), event_id)}


# --------------------------------------------------------------------------- #
# 出欠
# --------------------------------------------------------------------------- #
class AttendanceUpdate(BaseModel):
    status: AttendanceStatus
    proxy_member_id: str | None = None  # 委任先（status=委任 のとき・F4-5）


@router.get("/events/{event_id}/attendances")
def list_attendances(event_id: str):
    repo = get_repo()
    if repo.get_event(event_id) is None:
        raise HTTPException(status_code=404, detail="event not found")
    return {
        "attendances": repo.list_attendances(event_id),
        "summary": aggregate(repo, event_id),
    }


@router.put("/events/{event_id}/attendances/{member_id}")
def update_attendance(
    event_id: str,
    member_id: str,
    payload: AttendanceUpdate,
    x_goog_authenticated_user_email: str | None = Header(default=None),
):
    repo = get_repo()
    if repo.get_event(event_id) is None:
        raise HTTPException(status_code=404, detail="event not found")
    att = record_attendance(repo, event_id, member_id, payload.status, now=datetime.now())
    if payload.proxy_member_id is not None:
        if repo.get_member(payload.proxy_member_id) is None:
            raise HTTPException(status_code=400, detail="委任先の会員が見つかりません。")
        if payload.status != AttendanceStatus.委任:
            raise HTTPException(
                status_code=400, detail="委任先は status=委任 のときだけ設定できます。"
            )
        att.proxy_member_id = payload.proxy_member_id
        repo.upsert_attendance(att)
    return att


@router.get("/events/{event_id}/attendances.csv")
def export_attendances_csv(event_id: str):
    repo = get_repo()
    event = repo.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    by_member = {a.member_id: a for a in repo.list_attendances(event_id)}
    names = {m.member_id: m.name for m in repo.list_members()}
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "member_id", "name", "committee", "status", "proxy", "late_leave", "absence_reasons",
    ])
    for m in resolve_targets(repo, event):
        att = by_member.get(m.member_id)
        status = att.status.value if att else AttendanceStatus.未回答.value
        writer.writerow([
            m.member_id,
            m.name,
            m.committee or "",
            status,
            (names.get(att.proxy_member_id, att.proxy_member_id) if att and att.proxy_member_id
             else ""),
            (att.late_leave.value if att and att.late_leave else ""),
            ",".join(r.value for r in att.absence_reasons) if att else "",
        ])
    return Response(content=buf.getvalue(), media_type="text/csv")


@router.get("/events/{event_id}/package")
def event_package(event_id: str, download: bool = False):
    """事前共有パッケージ（目次付きMarkdown, F6-7）。"""
    repo = get_repo()
    event = repo.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    text = build_package(repo, event, now=datetime.now())
    headers = (
        {"Content-Disposition": f'attachment; filename="package_{event_id}.md"'}
        if download
        else {}
    )
    return Response(content=text, media_type="text/markdown; charset=utf-8", headers=headers)


@router.post("/events/{event_id}/remind")
def manual_remind(event_id: str, audience: str = "unanswered"):
    """未回答者(既定)へ手動催促ジョブを作成。実送信は worker/Tasks 経由。"""
    repo = get_repo()
    event = repo.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    targets = resolve_audience(repo, event, audience)
    job = DeliveryJob(
        job_id=stage_job_id(event_id, f"manual_{uuid.uuid4().hex[:6]}"),
        type="reminder",
        event_id=event_id,
        stage="manual",
        targets=targets,
        scheduled_at=datetime.now(),
        status=DeliveryStatus.queued,
    )
    repo.save_delivery_job(job)
    return {"job_id": job.job_id, "targets": targets}


@router.get("/events/{event_id}/summary")
def event_summary(event_id: str):
    repo = get_repo()
    if repo.get_event(event_id) is None:
        raise HTTPException(status_code=404, detail="event not found")
    return {"text": build_summary_text(repo, event_id)}


@router.post("/events/{event_id}/close")
def close_event_endpoint(event_id: str):
    """イベントをクローズし、五役向けサマリ通知ジョブを作成する。"""
    repo = get_repo()
    if not close_event(repo, event_id, now=datetime.now()):
        raise HTTPException(status_code=404, detail="event not found")
    summary_text = build_summary_text(repo, event_id)
    job = plan_summary_notification(repo, event_id, now=datetime.now())

    def sender(member_id: str) -> bool:
        member = repo.get_member(member_id)
        if member is None or not member.line_user_id:
            return False
        return line_push.push_text(member.line_user_id, summary_text)

    report = execute_delivery(repo, job, summary_text, now=datetime.now(), sender=sender)
    return {
        "status": "closed",
        "summary": summary_text,
        "summary_job_id": job.job_id,
        "notify_targets": job.targets,
        "notified": report.sent,
    }


# --------------------------------------------------------------------------- #
# 催促ポリシー
# --------------------------------------------------------------------------- #
@router.get("/policies")
def list_policies():
    repo = get_repo()
    found = (repo.get_policy(p.policy_id) for p in default_policies())
    return [p for p in found if p is not None]


@router.post("/policies/seed")
def seed_policies():
    """既定の催促ポリシー(例会/理事会)をFirestoreに投入する。"""
    repo = get_repo()
    seeded = []
    for policy in default_policies():
        repo.upsert_policy(policy)
        seeded.append(policy.policy_id)
    return {"seeded": seeded}


@router.get("/policies/{policy_id}")
def get_policy(policy_id: str):
    policy = get_repo().get_policy(policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="policy not found")
    return policy


@router.put("/policies/{policy_id}")
def put_policy(
    policy_id: str,
    payload: ReminderPolicy,
    x_goog_authenticated_user_email: str | None = Header(default=None),
):
    """催促ポリシー（段階・オフセット）を更新する（docs/dashboard-design.md §5）。"""
    if payload.policy_id != policy_id:
        raise HTTPException(status_code=400, detail="policy_id mismatch")
    repo = get_repo()
    repo.upsert_policy(payload)
    write_audit(
        repo,
        actor=_actor(x_goog_authenticated_user_email),
        action="policy.update",
        target=policy_id,
        detail=f"stages={len(payload.stages)}",
    )
    return payload


# --------------------------------------------------------------------------- #
# 設定
# --------------------------------------------------------------------------- #
@router.get("/settings")
def get_settings():
    return get_repo().get_settings()


@router.put("/settings")
def put_settings(
    settings: Settings,
    x_goog_authenticated_user_email: str | None = Header(default=None),
):
    repo = get_repo()
    repo.save_settings(settings)
    write_audit(
        repo,
        actor=_actor(x_goog_authenticated_user_email),
        action="settings.update",
        detail=f"kill_switch={settings.kill_switch}",
    )
    return settings


@router.get("/audit-logs")
def audit_logs(limit: int = 100):
    return get_repo().list_audit(limit=limit)


# --------------------------------------------------------------------------- #
# 取次・問い合わせ（F8-3 のエスカレーション）
# --------------------------------------------------------------------------- #
@router.get("/escalations")
def list_escalations(status: str | None = None):
    """会員からの取次依頼・エージェントが答えられなかった質問の一覧（新しい順）。"""
    repo = get_repo()
    items = repo.list_escalations(status=status)
    members = {m.member_id: m.name for m in repo.list_members()}
    return [
        {**e.model_dump(mode="json"), "member_name": members.get(e.member_id)}
        for e in items
    ]


@router.post("/escalations/{escalation_id}/handled")
def mark_escalation_handled(
    escalation_id: str,
    x_goog_authenticated_user_email: str | None = Header(default=None),
):
    repo = get_repo()
    esc = repo.get_escalation(escalation_id)
    if esc is None:
        raise HTTPException(status_code=404, detail="escalation not found")
    esc.status = "handled"
    repo.save_escalation(esc)
    write_audit(
        repo, actor=_actor(x_goog_authenticated_user_email),
        action="escalation.handled", target=escalation_id,
    )
    return esc


# --------------------------------------------------------------------------- #
# 名簿・招待コード
# --------------------------------------------------------------------------- #
@router.get("/members")
def list_members(active_only: bool = False):
    return get_repo().list_members(active_only=active_only)


@router.get("/members/invite-status")
def members_invite_status():
    return invite_status(get_repo(), now=datetime.now())


@router.get("/members/{member_id}/attendance-history")
def member_attendance_history(member_id: str):
    hist = attendance_history(get_repo(), member_id)
    if hist is None:
        raise HTTPException(status_code=404, detail="member not found")
    return hist


@router.post("/members")
def upsert_member(member: Member):
    get_repo().upsert_member(member)
    return member


@router.post("/members/{member_id}/invite")
def create_invite(
    member_id: str,
    x_goog_authenticated_user_email: str | None = Header(default=None),
):
    repo = get_repo()
    if repo.get_member(member_id) is None:
        raise HTTPException(status_code=404, detail="member not found")
    invite = issue_invite(repo, member_id, now=datetime.now())
    write_audit(
        repo,
        actor=_actor(x_goog_authenticated_user_email),
        action="member.invite",
        target=member_id,
    )
    return invite
