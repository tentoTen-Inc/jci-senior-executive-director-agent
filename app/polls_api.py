"""日程調整の API（docs/schedule-poll-design.md §4）。

- `router`: 管理API（/api/polls。既存の管理認証）
- `public_router`: LIFF 画面と、その画面が呼ぶ API（/liff。LINE の IDトークンで本人確認）
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Body, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import schedule_poll as sp
from .audit import write_audit
from .deps import get_repo
from .models import (
    EventType,
    PollCandidate,
    PollStatus,
    SchedulePoll,
    TargetScope,
    TargetScopeKind,
)
from .repository import Repository

router = APIRouter(tags=["polls"])
public_router = APIRouter(tags=["liff"])

LIFF_PAGE = Path(__file__).parent / "static" / "liff_poll.html"


def _actor(email: str | None) -> str:
    return email or "unknown"


def _error(exc: sp.PollError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


def _get(poll_id: str) -> SchedulePoll:
    poll = get_repo().get_poll(poll_id)
    if poll is None:
        raise HTTPException(status_code=404, detail="日程調整が見つかりません。")
    return poll


# --------------------------------------------------------------------------- #
# 管理API
# --------------------------------------------------------------------------- #
class CandidateIn(BaseModel):
    start: datetime
    end: datetime | None = None


class PollCreate(BaseModel):
    title: str
    event_type: EventType
    fiscal_year: int | None = Field(default=None, ge=2000, le=2099)
    location: str | None = None
    audience: str | None = None
    candidates: list[CandidateIn] = Field(min_length=1, max_length=10)
    target_scope: TargetScope = TargetScope()
    #: 案内先のグループ。委員会あてで空なら、連携済みの対象者に個別に送る
    group_id: str | None = None
    deadline: datetime | None = None


class DecideIn(BaseModel):
    candidate_id: str
    notify: bool = True  # グループへ決定を連絡する


def create(repo: Repository, payload: PollCreate, actor: str, now: datetime) -> SchedulePoll:
    """日程調整を作る（管理画面と LIFF「日程調整を作る」で共通）。案内はまだ送らない。"""
    scope = payload.target_scope
    if scope.kind == TargetScopeKind.committee:
        if not scope.value or not set(scope.value) <= set(sp.committee_names(repo)):
            raise sp.PollError(400, "委員会を選んでください。")
    elif not payload.group_id:
        raise sp.PollError(400, "案内先の LINE グループを選んでください。")
    candidates = sorted(payload.candidates, key=lambda c: c.start)
    poll = SchedulePoll(
        poll_id=sp.new_poll_id(),
        title=payload.title.strip(),
        event_type=payload.event_type,
        fiscal_year=payload.fiscal_year,
        location=payload.location,
        audience=payload.audience,
        candidates=[PollCandidate(candidate_id=sp.new_candidate_id(i), start=c.start, end=c.end)
                    for i, c in enumerate(candidates)],
        target_scope=payload.target_scope,
        group_id=payload.group_id or None,
        deadline=payload.deadline,
        created_at=now,
        created_by=actor,
    )
    repo.save_poll(poll)
    write_audit(repo, actor=actor, action="poll.create", target=poll.poll_id, detail=poll.title)
    return poll


@router.post("/polls")
def create_poll(payload: PollCreate,
                x_goog_authenticated_user_email: str | None = Header(default=None)):
    repo = get_repo()
    try:
        poll = create(repo, payload, _actor(x_goog_authenticated_user_email), datetime.now())
    except sp.PollError as exc:
        raise _error(exc) from exc
    return sp.summary(repo, poll)


@router.get("/polls/committees")  # /polls/{poll_id} より前に置く
def poll_committees():
    """委員会あての日程調整: 委員会ごとの対象者数と送り先（グループ or 個別）。"""
    repo = get_repo()
    rows = []
    for name in sp.committee_names(repo):
        probe = SchedulePoll(poll_id="-", title="-", event_type=EventType.委員会,
                             target_scope=TargetScope(kind=TargetScopeKind.committee,
                                                      value=[name]))
        group = sp.committee_group(repo, name)
        rows.append({"committee": name, "group_id": group.group_id if group else None,
                     **sp.destination(repo, probe.model_copy(
                         update={"group_id": group.group_id if group else None}))})
    return rows


@router.get("/polls")
def list_polls():
    repo = get_repo()
    return [{**p.model_dump(mode="json"), "pending": len(sp.summary(repo, p)["pending"])}
            for p in repo.list_polls()]


@router.get("/polls/{poll_id}")
def get_poll(poll_id: str):
    return sp.summary(get_repo(), _get(poll_id))


@router.post("/polls/{poll_id}/announce")
def announce_poll(poll_id: str,
                  x_goog_authenticated_user_email: str | None = Header(default=None)):
    repo = get_repo()
    try:
        poll = sp.announce(repo, _get(poll_id), _actor(x_goog_authenticated_user_email),
                           datetime.now())
    except sp.PollError as exc:
        raise _error(exc) from exc
    return sp.summary(repo, poll)


@router.post("/polls/{poll_id}/remind")
def remind_poll(poll_id: str,
                x_goog_authenticated_user_email: str | None = Header(default=None)):
    try:
        pending = sp.remind(get_repo(), _get(poll_id), _actor(x_goog_authenticated_user_email),
                            datetime.now())
    except sp.PollError as exc:
        raise _error(exc) from exc
    return {"reminded": pending}


@router.post("/polls/{poll_id}/decide")
def decide_poll(poll_id: str, payload: DecideIn,
                x_goog_authenticated_user_email: str | None = Header(default=None)):
    repo = get_repo()
    try:
        poll, event = sp.decide(repo, _get(poll_id), payload.candidate_id,
                                _actor(x_goog_authenticated_user_email), datetime.now(),
                                notify=payload.notify)
    except sp.PollError as exc:
        raise _error(exc) from exc
    return {"poll": poll, "event": event}


@router.post("/polls/{poll_id}/close")
def close_poll(poll_id: str,
               x_goog_authenticated_user_email: str | None = Header(default=None)):
    repo = get_repo()
    poll = _get(poll_id)
    if poll.status != PollStatus.open:
        raise HTTPException(status_code=409, detail="受付中の日程調整だけ締め切れます。")
    poll = poll.model_copy(update={"status": PollStatus.closed})
    repo.save_poll(poll)
    write_audit(repo, actor=_actor(x_goog_authenticated_user_email), action="poll.close",
                   target=poll.poll_id, detail=poll.title)
    return sp.summary(repo, poll)


# --------------------------------------------------------------------------- #
# LIFF（公開。IDトークンで本人確認）
# --------------------------------------------------------------------------- #
@public_router.get("/liff/poll", response_class=HTMLResponse)
def liff_page():
    """LIFF 画面。LIFF ID は環境変数から埋め込む（公開情報）。"""
    if not sp.is_configured():
        return HTMLResponse("日程調整は準備中です。", status_code=503)
    html = LIFF_PAGE.read_text(encoding="utf-8").replace("__LIFF_ID__", sp.liff_id() or "")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


def _viewer(request: Request, poll: SchedulePoll):
    if not sp.is_configured():
        raise sp.PollError(503, "日程調整は準備中です。")
    claims = sp.verify_id_token(request.headers.get("Authorization"))
    user_id = claims["sub"]
    linked = sp.authorize_viewer(get_repo(), poll, user_id)
    return user_id, claims.get("name"), linked


def _poll_error_response(exc: sp.PollError) -> JSONResponse:
    return JSONResponse({"detail": exc.message}, status_code=exc.status)


@public_router.get("/liff/api/polls/{poll_id}")
def liff_get_poll(poll_id: str, request: Request):
    poll = _get(poll_id)
    try:
        user_id, name, linked = _viewer(request, poll)
    except sp.PollError as exc:
        return _poll_error_response(exc)
    return sp.view(get_repo(), poll, user_id, name, linked)


@public_router.put("/liff/api/polls/{poll_id}/response")
def liff_put_response(poll_id: str, request: Request, body: dict = Body(default_factory=dict)):
    # 同期関数にして LINE・Firestore への問い合わせをスレッドで行う（Webhook を待たせない）
    poll = _get(poll_id)
    try:
        user_id, name, linked = _viewer(request, poll)
        sp.save_response(get_repo(), poll, user_id, name, linked, body, datetime.now())
    except sp.PollError as exc:
        return _poll_error_response(exc)
    return sp.view(get_repo(), poll, user_id, name, linked)
