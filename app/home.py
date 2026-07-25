"""ホーム画面の集約データ（docs/dashboard-design.md §3.1）。"""
from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel

from .attendance import aggregate
from .models import DeliveryResult, EventStatus, MemberStatus
from .notice_actions import open_action_count
from .repository import Repository


class EventBrief(BaseModel):
    event_id: str
    title: str
    datetime_start: datetime
    location: str | None
    total_targets: int
    answered: int
    attendance_rate: float
    unanswered: int


class ActionRequired(BaseModel):
    unlinked_members: int
    open_escalations: int
    unanswered_total: int
    open_notice_actions: int = 0  # 未対応者が残る対外連絡アクション（F5-5）


class HomeData(BaseModel):
    generated_at: datetime
    kill_switch: bool
    delivery_success_rate: float
    member_count: int
    action_required: ActionRequired
    upcoming_events: list[EventBrief]
    this_week: list[EventBrief]


def _brief(repo: Repository, event) -> EventBrief:
    s = aggregate(repo, event.event_id)
    return EventBrief(
        event_id=event.event_id,
        title=event.title,
        datetime_start=event.datetime_start,
        location=event.location,
        total_targets=s.total_targets,
        answered=s.answered,
        attendance_rate=s.attendance_rate,
        unanswered=s.unanswered,
    )


def build_home(repo: Repository, *, now: datetime) -> HomeData:
    settings = repo.get_settings()

    # 配信成功率
    logs = repo.list_delivery_logs()
    ok = sum(1 for x in logs if x.result == DeliveryResult.ok)
    failed = sum(1 for x in logs if x.result == DeliveryResult.failed)
    success_rate = round(ok / (ok + failed), 3) if (ok + failed) else 1.0

    members = repo.list_members()
    active = [m for m in members if m.status == MemberStatus.active]
    unlinked = sum(1 for m in active if m.line_user_id is None)

    open_events = [e for e in repo.list_events(status=EventStatus.open)]
    future = sorted(
        (e for e in open_events if e.datetime_start >= now),
        key=lambda e: e.datetime_start,
    )
    briefs = [_brief(repo, e) for e in future]
    unanswered_total = sum(b.unanswered for b in briefs)

    week_end = now + timedelta(days=7)
    this_week = [b for b in briefs if b.datetime_start <= week_end]

    return HomeData(
        generated_at=now,
        kill_switch=settings.kill_switch,
        delivery_success_rate=success_rate,
        member_count=len(active),
        action_required=ActionRequired(
            unlinked_members=unlinked,
            open_escalations=len(repo.list_escalations(status="open")),
            unanswered_total=unanswered_total,
            open_notice_actions=open_action_count(repo),
        ),
        upcoming_events=briefs[:5],
        this_week=this_week,
    )
