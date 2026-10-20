"""LINE 連携の申請・承認と予約投稿の API（docs/line-link-design.md）。

- `router`: 管理API（/api/line-link, /api/scheduled-posts）
- `public_router`: LIFF 画面が呼ぶ API（/liff/api/link。LINE の IDトークンで本人確認）
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Body, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import line_link
from . import schedule_poll as sp
from .audit import write_audit
from .deps import get_repo
from .models import ScheduledPost, ScheduledPostStatus

router = APIRouter(tags=["line-link"])
public_router = APIRouter(tags=["liff"])


def _actor(email: str | None) -> str:
    return email or "unknown"


def _error(exc: sp.PollError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


# --------------------------------------------------------------------------- #
# 管理API
# --------------------------------------------------------------------------- #
class ApproveIn(BaseModel):
    member_id: str | None = None  # 日程調整の回答から承認するとき


@router.get("/line-link")
def link_requests():
    return line_link.pending_list(get_repo())


@router.post("/line-link/{line_user_id}/approve")
def approve_link(line_user_id: str, payload: ApproveIn = ApproveIn(),
                 x_goog_authenticated_user_email: str | None = Header(default=None)):
    try:
        member = line_link.approve(get_repo(), line_user_id,
                                   _actor(x_goog_authenticated_user_email), datetime.now(),
                                   member_id=payload.member_id)
    except sp.PollError as exc:
        raise _error(exc) from exc
    return member


@router.post("/line-link/{line_user_id}/reject")
def reject_link(line_user_id: str,
                x_goog_authenticated_user_email: str | None = Header(default=None)):
    try:
        return line_link.reject(get_repo(), line_user_id,
                                _actor(x_goog_authenticated_user_email), datetime.now())
    except sp.PollError as exc:
        raise _error(exc) from exc


class PostCreate(BaseModel):
    group_id: str = Field(min_length=1)
    kind: str = Field(pattern="^(link_invite|poll_announce)$")
    poll_id: str | None = None
    send_at: datetime


@router.get("/scheduled-posts")
def scheduled_posts():
    return get_repo().list_scheduled_posts()


@router.post("/scheduled-posts")
def create_scheduled_post(payload: PostCreate,
                          x_goog_authenticated_user_email: str | None = Header(default=None)):
    repo = get_repo()
    if payload.kind == "poll_announce" and not repo.get_poll(payload.poll_id or ""):
        raise HTTPException(status_code=400, detail="日程調整が見つかりません。")
    post = ScheduledPost(post_id=line_link.new_post_id(), created_at=datetime.now(),
                         created_by=_actor(x_goog_authenticated_user_email),
                         **payload.model_dump())
    repo.save_scheduled_post(post)
    write_audit(repo, actor=post.created_by or "unknown", action="post.schedule",
                target=post.post_id, detail=f"{post.kind} → {post.group_id} @ {post.send_at}")
    return post


@router.post("/scheduled-posts/{post_id}/cancel")
def cancel_scheduled_post(post_id: str,
                          x_goog_authenticated_user_email: str | None = Header(default=None)):
    repo = get_repo()
    post = next((p for p in repo.list_scheduled_posts() if p.post_id == post_id), None)
    if post is None or post.status != ScheduledPostStatus.pending:
        raise HTTPException(status_code=404, detail="送信待ちの予約が見つかりません。")
    post = post.model_copy(update={"status": ScheduledPostStatus.cancelled})
    repo.save_scheduled_post(post)
    write_audit(repo, actor=_actor(x_goog_authenticated_user_email), action="post.cancel",
                target=post_id)
    return post


# --------------------------------------------------------------------------- #
# LIFF（公開。IDトークンで本人確認）
# --------------------------------------------------------------------------- #
def _viewer(request: Request) -> tuple[str, str | None]:
    if not sp.is_configured():
        raise sp.PollError(503, "準備中です。")
    claims = sp.verify_id_token(request.headers.get("Authorization"))
    line_link.authorize(get_repo(), claims["sub"])
    return claims["sub"], claims.get("name")


@public_router.get("/liff/api/link")
def liff_link_view(request: Request):
    try:
        user_id, name = _viewer(request)
    except sp.PollError as exc:
        return JSONResponse({"detail": exc.message}, status_code=exc.status)
    return line_link.view(get_repo(), user_id, name)


@public_router.post("/liff/api/link")
def liff_link_request(request: Request, body: dict = Body(default_factory=dict)):
    try:
        user_id, name = _viewer(request)
        line_link.request_link(get_repo(), user_id, name, str(body.get("member_id") or ""),
                               datetime.now())
    except sp.PollError as exc:
        return JSONResponse({"detail": exc.message}, status_code=exc.status)
    return line_link.view(get_repo(), user_id, name)
