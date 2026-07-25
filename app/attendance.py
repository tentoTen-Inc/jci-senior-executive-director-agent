"""出欠の記録と集計（docs/mvp-design.md §4.2）。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from .events import resolve_targets
from .models import (
    AbsenceReason,
    Attendance,
    AttendanceHistory,
    AttendanceStatus,
    LateLeave,
)
from .repository import Repository


def record_attendance(
    repo: Repository,
    event_id: str,
    member_id: str,
    status: AttendanceStatus,
    *,
    now: datetime,
    late_leave: LateLeave | None = None,
    absence_reasons: list[AbsenceReason] | None = None,
    free_text: str | None = None,
) -> Attendance:
    """出欠を記録（変更時は履歴に追記）。"""
    att = repo.get_attendance(event_id, member_id) or Attendance(
        event_id=event_id, member_id=member_id
    )
    att.status = status
    att.responded_at = now
    if late_leave is not None:
        att.late_leave = late_leave
    if absence_reasons is not None:
        att.absence_reasons = absence_reasons
    if free_text is not None:
        att.free_text = free_text
    att.history.append(AttendanceHistory(at=now, status=status))
    repo.upsert_attendance(att)
    return att


def set_late_leave(repo: Repository, event_id: str, member_id: str, late_leave: LateLeave,
                   *, now: datetime) -> Attendance | None:
    att = repo.get_attendance(event_id, member_id)
    if att is None:
        return None
    att.late_leave = late_leave
    repo.upsert_attendance(att)
    return att


def add_absence_reason(repo: Repository, event_id: str, member_id: str, reason: AbsenceReason,
                       *, now: datetime) -> Attendance | None:
    att = repo.get_attendance(event_id, member_id)
    if att is None:
        return None
    if reason not in att.absence_reasons:
        att.absence_reasons.append(reason)
    repo.upsert_attendance(att)
    return att


class AttendanceSummary(BaseModel):
    event_id: str
    total_targets: int
    counts: dict[str, int]  # status -> 人数
    answered: int
    unanswered: int
    attendance_rate: float  # 出席+Web出席 / 対象者
    unanswered_member_ids: list[str]
    # 定足数判定の補助（F4-5）。委任を数えるかは規約次第なので両方を返す。
    present: int = 0  # 出席＋Web出席
    proxies: int = 0  # 委任
    present_with_proxies: int = 0  # 出席＋Web出席＋委任
    quorum: int | None = None  # イベントに設定された定足数（人数）
    quorum_met: bool | None = None  # 委任を含めて充足しているか
    quorum_met_without_proxies: bool | None = None  # 委任を除いて充足しているか


def aggregate(repo: Repository, event_id: str) -> AttendanceSummary:
    """対象者ベースで集計。回答が無い対象者は未回答として数える。"""
    event = repo.get_event(event_id)
    targets = resolve_targets(repo, event) if event else []
    target_ids = [m.member_id for m in targets]

    by_member = {a.member_id: a for a in repo.list_attendances(event_id)}

    counts: dict[str, int] = {s.value: 0 for s in AttendanceStatus}
    unanswered_ids: list[str] = []
    present = 0
    answered = 0

    for mid in target_ids:
        att = by_member.get(mid)
        status = att.status if att else AttendanceStatus.未回答
        counts[status.value] += 1
        if status == AttendanceStatus.未回答:
            unanswered_ids.append(mid)
        else:
            answered += 1
        if status in (AttendanceStatus.出席, AttendanceStatus.WEB出席):
            present += 1

    total = len(target_ids)
    rate = round(present / total, 3) if total else 0.0
    proxies = counts[AttendanceStatus.委任.value]
    quorum = event.quorum if event else None
    return AttendanceSummary(
        event_id=event_id,
        total_targets=total,
        counts=counts,
        answered=answered,
        unanswered=total - answered,
        attendance_rate=rate,
        unanswered_member_ids=unanswered_ids,
        present=present,
        proxies=proxies,
        present_with_proxies=present + proxies,
        quorum=quorum,
        quorum_met=(present + proxies >= quorum) if quorum is not None else None,
        quorum_met_without_proxies=(present >= quorum) if quorum is not None else None,
    )
