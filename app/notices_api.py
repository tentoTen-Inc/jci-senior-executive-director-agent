"""対外連絡API（docs/external-notice-design.md §4, F5）。

原文（body_text/添付）は不変。Gemini 生成物は digest に分けて保持する（F5-6）。
配信（P3-3）・Gmail取込（P3-2）は後続フェーズで追加する。
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from .audit import write_audit
from .deps import get_repo
from .inference import record_inference
from .llm import digest_notice
from .llm import model_name as llm_model_name
from .models import ExternalNotice, InferenceUsage, NoticeHistory

router = APIRouter(tags=["notices"])

NOTICE_STATUSES = ("new", "reviewed", "delivered", "archived")


def _actor(email: str | None) -> str:
    return email or "unknown"


def manual_source_ref(subject: str, body_text: str) -> str:
    """手動投入の冪等キー。件名＋本文のハッシュ（同じメールの二重貼り付けを防ぐ）。"""
    digest = hashlib.sha256(f"{subject}\n{body_text}".encode()).hexdigest()[:16]
    return f"manual:{digest}"


class NoticeCreate(BaseModel):
    subject: str
    body_text: str
    from_addr: str | None = None
    from_name: str | None = None
    received_at: datetime | None = None


@router.get("/notices")
def list_notices(status: str | None = None):
    return get_repo().list_notices(status=status)


@router.post("/notices")
def create_notice(
    payload: NoticeCreate,
    x_goog_authenticated_user_email: str | None = Header(default=None),
):
    """管理画面からの手動投入（補助経路・F5-1）。同一内容の再投入は既存を返す。"""
    if not payload.body_text.strip():
        raise HTTPException(status_code=400, detail="本文が空です。")
    repo = get_repo()
    actor = _actor(x_goog_authenticated_user_email)
    now = datetime.now()

    source_ref = manual_source_ref(payload.subject, payload.body_text)
    existing = repo.get_notice_by_source_ref(source_ref)
    if existing is not None:
        return existing

    notice = ExternalNotice(
        notice_id=f"ntc_{uuid.uuid4().hex[:10]}",
        source="manual",
        source_ref=source_ref,
        received_at=payload.received_at or now,
        from_addr=payload.from_addr,
        from_name=payload.from_name,
        subject=payload.subject,
        body_text=payload.body_text,
        history=[NoticeHistory(at=now, action="created", by=actor)],
    )
    repo.upsert_notice(notice)
    write_audit(
        repo, actor=actor, action="notice.create",
        target=notice.notice_id, detail=notice.subject,
    )
    return notice


@router.get("/notices/{notice_id}")
def get_notice(notice_id: str):
    notice = get_repo().get_notice(notice_id)
    if notice is None:
        raise HTTPException(status_code=404, detail="notice not found")
    return notice


@router.post("/notices/{notice_id}/digest")
def build_digest(
    notice_id: str,
    x_goog_authenticated_user_email: str | None = Header(default=None),
):
    """Gemini で要約・告知文・対象・期限・アクションを生成する（F5-2/F5-3）。"""
    repo = get_repo()
    notice = repo.get_notice(notice_id)
    if notice is None:
        raise HTTPException(status_code=404, detail="notice not found")
    actor = _actor(x_goog_authenticated_user_email)
    now = datetime.now()

    sender = notice.from_name or notice.from_addr or ""
    outcome = digest_notice(notice.subject, sender, notice.body_text)
    if outcome is None:
        record_inference(
            repo, kind="external_notice_digest", usage=InferenceUsage(model=llm_model_name()),
            now=now, target=notice_id, ok=False, error="generation_failed",
        )
        raise HTTPException(
            status_code=503,
            detail="要約を生成できませんでした（LLM未設定/失敗）。",
        )
    record_inference(
        repo, kind="external_notice_digest", usage=outcome.usage, now=now, target=notice_id,
    )

    digest = outcome.digest
    digest.generated_at = now
    notice.digest = digest
    if notice.status == "new":
        notice.status = "reviewed"
    notice.history.append(NoticeHistory(at=now, action="digest", by=actor))
    repo.upsert_notice(notice)
    write_audit(repo, actor=actor, action="notice.digest", target=notice_id)
    return notice


@router.post("/notices/{notice_id}/archive")
def archive_notice(
    notice_id: str,
    x_goog_authenticated_user_email: str | None = Header(default=None),
):
    """対応不要としてアーカイブする。"""
    repo = get_repo()
    notice = repo.get_notice(notice_id)
    if notice is None:
        raise HTTPException(status_code=404, detail="notice not found")
    actor = _actor(x_goog_authenticated_user_email)
    now = datetime.now()
    notice.status = "archived"
    notice.history.append(NoticeHistory(at=now, action="archived", by=actor))
    repo.upsert_notice(notice)
    write_audit(repo, actor=actor, action="notice.archive", target=notice_id)
    return notice
