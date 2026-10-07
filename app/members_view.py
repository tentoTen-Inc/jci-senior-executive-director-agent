"""会員管理の集約ビュー（docs/dashboard-design.md §3.2）。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from .events import resolve_targets
from .models import AttendanceStatus, MemberStatus
from .repository import Repository

_PRESENT = {AttendanceStatus.出席, AttendanceStatus.WEB出席}


class InviteStatusRow(BaseModel):
    member_id: str
    name: str
    committee: str | None
    officer_role: str | None
    member_type: str
    linked: bool
    invite_issued: bool
    invite_active: bool  # 未使用かつ有効期限内のコードがある
    invite_used: bool


def invite_status(repo: Repository, *, now: datetime) -> list[InviteStatusRow]:
    codes = repo.list_invite_codes()
    by_member: dict[str, list] = {}
    for c in codes:
        by_member.setdefault(c.member_id, []).append(c)

    rows: list[InviteStatusRow] = []
    for m in repo.list_members():
        if m.status != MemberStatus.active:
            continue
        mine = by_member.get(m.member_id, [])
        used = any(c.used_at is not None for c in mine)
        active = any(c.used_at is None and c.expires_at >= now for c in mine)
        rows.append(
            InviteStatusRow(
                member_id=m.member_id,
                name=m.name,
                committee="・".join(sorted(m.committee_names)) or None,
                officer_role=m.officer_role,
                member_type=m.member_type.value,
                linked=m.line_user_id is not None,
                invite_issued=bool(mine),
                invite_active=active,
                invite_used=used,
            )
        )
    return rows


class AttendanceHistoryItem(BaseModel):
    event_id: str
    title: str
    datetime_start: datetime
    type: str
    status: str


class AttendanceHistory(BaseModel):
    member_id: str
    name: str
    counted: int  # 対象だったイベント数
    present: int  # 出席/Web出席
    answered: int
    attendance_rate: float
    items: list[AttendanceHistoryItem]


def attendance_history(repo: Repository, member_id: str) -> AttendanceHistory | None:
    member = repo.get_member(member_id)
    if member is None:
        return None
    items: list[AttendanceHistoryItem] = []
    present = answered = counted = 0
    events = sorted(repo.list_events(), key=lambda e: e.datetime_start, reverse=True)
    for ev in events:
        targets = {t.member_id for t in resolve_targets(repo, ev)}
        if member_id not in targets:
            continue
        counted += 1
        att = repo.get_attendance(ev.event_id, member_id)
        status = att.status if att else AttendanceStatus.未回答
        if status != AttendanceStatus.未回答:
            answered += 1
        if status in _PRESENT:
            present += 1
        items.append(
            AttendanceHistoryItem(
                event_id=ev.event_id,
                title=ev.title,
                datetime_start=ev.datetime_start,
                type=ev.type.value,
                status=status.value,
            )
        )
    rate = round(present / counted, 3) if counted else 0.0
    return AttendanceHistory(
        member_id=member_id,
        name=member.name,
        counted=counted,
        present=present,
        answered=answered,
        attendance_rate=rate,
        items=items,
    )
