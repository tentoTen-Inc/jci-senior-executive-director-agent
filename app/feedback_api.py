"""意見箱の管理API（docs/feedback-box-design.md §5。既存の管理認証）。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Body, Header, HTTPException

from . import feedback_box
from .audit import write_audit
from .deps import get_repo

router = APIRouter(tags=["feedback"])


def _actor(email: str | None) -> str:
    return email or "unknown"


def _error(exc: feedback_box.FeedbackError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


@router.get("/feedback")
def list_feedback():
    return feedback_box.admin_view(get_repo())


@router.post("/feedback/{feedback_id}/status")
def set_feedback_status(feedback_id: str, body: dict = Body(default_factory=dict),
                        x_goog_authenticated_user_email: str | None = Header(default=None)):
    repo = get_repo()
    actor = _actor(x_goog_authenticated_user_email)
    try:
        item = feedback_box.set_status(repo, feedback_id, str(body.get("status") or ""), actor,
                                       datetime.now())
    except feedback_box.FeedbackError as exc:
        raise _error(exc) from exc
    write_audit(repo, actor=actor, action="feedback.status", target=feedback_id,
                detail=feedback_box.STATUS_LABELS[item.status])
    return feedback_box.admin_view(repo)


@router.post("/feedback/digest")
def build_feedback_digest(x_goog_authenticated_user_email: str | None = Header(default=None)):
    repo = get_repo()
    try:
        digest = feedback_box.build_digest(repo, datetime.now())
    except feedback_box.FeedbackError as exc:
        raise _error(exc) from exc
    write_audit(repo, actor=_actor(x_goog_authenticated_user_email), action="feedback.digest",
                target="feedback", detail=f"items={digest.count}")
    return feedback_box.admin_view(repo)
