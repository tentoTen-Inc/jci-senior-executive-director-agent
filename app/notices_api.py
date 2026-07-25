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
from .delivery import execute_delivery
from .deps import get_repo
from .events import resolve_scope
from .inference import record_inference
from .line_push import member_text_sender
from .llm import digest_notice
from .llm import model_name as llm_model_name
from .models import (
    DeliveryJob,
    ExternalNotice,
    InferenceUsage,
    NoticeDelivery,
    NoticeHistory,
    TargetScope,
)

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


class NoticeDeliver(BaseModel):
    target_scope: TargetScope = TargetScope()
    body_text: str | None = None  # 告知文の上書き（未指定なら digest.announcement）
    force: bool = False  # 配信済みでも再配信する


@router.post("/notices/{notice_id}/deliver")
def deliver_notice(
    notice_id: str,
    payload: NoticeDeliver,
    x_goog_authenticated_user_email: str | None = Header(default=None),
):
    """告知文を対象会員へLINE配信する（F5-4）。

    既存のガードレール（静音時間・レート上限・キルスイッチ・sanity_check）を
    そのまま通す。配信可否の最終判断は専務が画面で行う。
    """
    repo = get_repo()
    notice = repo.get_notice(notice_id)
    if notice is None:
        raise HTTPException(status_code=404, detail="notice not found")

    body = (payload.body_text or (notice.digest.announcement if notice.digest else "")).strip()
    if not body:
        raise HTTPException(
            status_code=400,
            detail="告知文がありません。先に要約を生成するか、本文を指定してください。",
        )
    if notice.status == "delivered" and not payload.force:
        raise HTTPException(
            status_code=409,
            detail="この連絡は既に配信済みです。再配信する場合は force=true を指定してください。",
        )

    targets = resolve_scope(repo, payload.target_scope)
    if not targets:
        raise HTTPException(
            status_code=400, detail="配信対象が0名です。対象範囲を確認してください。"
        )

    actor = _actor(x_goog_authenticated_user_email)
    now = datetime.now()
    job = DeliveryJob(
        job_id=f"ntc_{notice_id}_{uuid.uuid4().hex[:6]}",
        type="notice",
        targets=[m.member_id for m in targets],
        idempotency_key=notice_id,
    )
    repo.save_delivery_job(job)
    report = execute_delivery(repo, job, body, now=now, sender=member_text_sender(repo, body))

    if report.halted:
        write_audit(
            repo, actor=actor, action="notice.deliver.halted",
            target=notice_id, detail=", ".join(report.problems),
        )
        raise HTTPException(
            status_code=409,
            detail=f"ガードレールにより配信を中止しました: {', '.join(report.problems)}",
        )

    notice.delivery = NoticeDelivery(job_id=job.job_id, delivered_at=now, target_count=report.sent)
    notice.status = "delivered"
    notice.history.append(NoticeHistory(at=now, action="delivered", by=actor))
    repo.upsert_notice(notice)
    write_audit(
        repo, actor=actor, action="notice.deliver", target=notice_id,
        detail=f"sent={report.sent} blocked={report.blocked} deferred={report.deferred}"
        f" failed={report.failed}",
    )
    return {
        "notice": notice,
        "job_id": job.job_id,
        "targets": len(job.targets),
        "sent": report.sent,
        "blocked": report.blocked,
        "deferred": report.deferred,
        "failed": report.failed,
    }


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
