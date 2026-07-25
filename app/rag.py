"""問い合わせ応答のための自LOM情報コンテキスト（docs/nl-assistant-design.md §3, F8-2/F8-4）。

個人情報ガードの一次防御は「そもそも他会員の個人情報をコンテキストに載せない」こと。
載っていない情報は漏れない。載せるのは質問者本人に関する情報と、会全体の公開情報のみ。
"""
from __future__ import annotations

from datetime import datetime

from .attendance import aggregate
from .events import resolve_targets
from .models import AttendanceStatus, EventStatus, Member
from .repository import Repository

MAX_EVENTS = 5
MAX_NOTICES = 3


def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def _self_lines(member: Member) -> list[str]:
    lines = [f"氏名: {member.name}"]
    if member.committee:
        lines.append(f"所属委員会: {member.committee}")
    if member.officer_role:
        lines.append(f"役職: {member.officer_role}")
    return ["## 質問者本人", *lines]


def _event_lines(repo: Repository, member: Member, now: datetime) -> list[str]:
    events = sorted(
        (e for e in repo.list_events(status=EventStatus.open) if e.datetime_start >= now),
        key=lambda e: e.datetime_start,
    )[:MAX_EVENTS]
    if not events:
        return ["## 今後の予定", "予定されているイベントはありません。"]

    lines = ["## 今後の予定"]
    for e in events:
        parts = [f"- {e.title} / {_fmt_dt(e.datetime_start)}"]
        if e.location:
            parts.append(f"場所: {e.location}")
        if e.attendance_deadline:
            parts.append(f"出欠締切: {_fmt_dt(e.attendance_deadline)}")
        is_target = any(m.member_id == member.member_id for m in resolve_targets(repo, e))
        parts.append("あなたは対象です" if is_target else "あなたは対象外です")
        if is_target:
            att = repo.get_attendance(e.event_id, member.member_id)
            status = att.status.value if att else AttendanceStatus.未回答.value
            parts.append(f"あなたの回答: {status}")
            # 全体の回答状況は人数のみ（誰が未回答かは個人情報なので出さない）
            s = aggregate(repo, e.event_id)
            parts.append(f"全体の回答: {s.answered}/{s.total_targets}名")
        lines.append(" / ".join(parts))
    return lines


def _notice_lines(repo: Repository, member: Member) -> list[str]:
    notices = repo.list_notices(status="delivered")[:MAX_NOTICES]
    lines: list[str] = []
    if notices:
        lines.append("## 最近の対外連絡（配信済み）")
        for n in notices:
            summary = n.digest.summary if n.digest else ""
            due = ""
            if n.digest and n.digest.deadline:
                due = f" / 期限: {n.digest.deadline.strftime('%Y-%m-%d')}"
            lines.append(f"- {n.subject}: {summary}{due}")

    mine = [
        a
        for a in repo.list_notice_actions(status="open")
        if member.member_id in a.assignees and member.member_id not in a.done_by
    ]
    if mine:
        lines.append("## あなたの未対応タスク")
        for a in mine:
            due = f" / 期限: {a.due.strftime('%Y-%m-%d')}" if a.due else ""
            lines.append(f"- {a.title}{due}")
    return lines


def build_context(repo: Repository, member: Member, *, now: datetime) -> str:
    """質問者に開示してよい情報だけを集めたコンテキスト文字列を返す。"""
    lines = [f"# 猪苗代JC の現在の情報（基準時刻 {_fmt_dt(now)}）"]
    lines += _self_lines(member)
    lines += _event_lines(repo, member, now)
    lines += _notice_lines(repo, member)
    return "\n".join(lines)
