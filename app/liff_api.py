"""LIFF の画面（docs/liff-menu-design.md §3）と、その画面が呼ぶ API。

既存の LIFF アプリ（/liff/poll）に `?view=` の切り替えを足す。画面の処理は /liff/views.js。
本人確認は LINE の IDトークン（schedule_poll.verify_id_token）。**役割に応じたアクセス制御は
ここ（サーバー側）で必ず行う**。書き込みは画面の［送信］系のボタンから呼ばれる API だけ。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, Response

from . import schedule_poll as sp
from .attendance import record_attendance
from .deps import get_repo
from .events import resolve_targets
from .jpdate import month_day_time
from .models import AttendanceStatus, Event, EventStatus, Member, MemberStatus, PollStatus
from .repository import Repository

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


def viewer(request: Request) -> Member:
    """IDトークンで本人を確かめ、LINE 連携済みの会員を返す。"""
    if not sp.is_configured():
        raise LiffError(503, "LINE の画面は準備中です。")
    claims = sp.verify_id_token(request.headers.get("Authorization"))
    member = get_repo().get_member_by_line_user_id(claims["sub"])
    if member is None or member.status != MemberStatus.active:
        raise LiffError(403, "LINE 連携が済んでいません。事務局にお問い合わせください。")
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
