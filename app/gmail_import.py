"""Gmail から対外連絡を取込む（docs/external-notice-design.md §2, F5-1）。

message id を `source_ref` にして冪等。既に取込済みのメールは本文だけ更新し、
要約・配信・アクションなど後工程の結果は壊さない。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

from pydantic import BaseModel

from . import gmail
from .models import ExternalNotice, NoticeAttachment, NoticeHistory
from .repository import Repository

logger = logging.getLogger("jci-agent.gmail_import")


class GmailImportItem(BaseModel):
    message_id: str
    subject: str
    action: str  # created | updated
    attachments: int = 0


class GmailImportSummary(BaseModel):
    total: int = 0
    created: int = 0
    updated: int = 0
    dry_run: bool = False
    items: list[GmailImportItem] = []


def _source_ref(message_id: str) -> str:
    return f"gmail:{message_id}"


def import_gmail_notices(
    repo: Repository,
    *,
    now: datetime,
    label_name: str | None = None,
    newer_than: str | None = None,
    dry_run: bool = False,
    actor: str = "system",
    fetch=None,
) -> GmailImportSummary:
    """ラベル付きの新着メールを ExternalNotice として取込む。"""
    fetcher = fetch or gmail.fetch_messages
    messages = fetcher(label_name=label_name, newer_than=newer_than)

    summary = GmailImportSummary(total=len(messages), dry_run=dry_run)
    for m in messages:
        message_id = m.get("message_id") or ""
        if not message_id:
            continue
        source_ref = _source_ref(message_id)
        existing = repo.get_notice_by_source_ref(source_ref)
        attachments = [
            NoticeAttachment(
                name=a.get("name", ""),
                mime=a.get("mime"),
                text_excerpt=a.get("text_excerpt"),
            )
            for a in m.get("attachments", []) or []
        ]
        action = "updated" if existing is not None else "created"
        summary.items.append(GmailImportItem(
            message_id=message_id,
            subject=m.get("subject", ""),
            action=action,
            attachments=len(attachments),
        ))
        if action == "created":
            summary.created += 1
        else:
            summary.updated += 1

        if dry_run:
            continue

        if existing is None:
            notice = ExternalNotice(
                notice_id=f"ntc_{uuid.uuid4().hex[:10]}",
                source="gmail",
                source_ref=source_ref,
                received_at=m.get("received_at") or now,
                from_addr=m.get("from_addr"),
                from_name=m.get("from_name"),
                subject=m.get("subject", "(件名なし)"),
                body_text=m.get("body_text", ""),
                attachments=attachments,
                history=[NoticeHistory(at=now, action="imported_gmail", by=actor)],
            )
        else:
            # 後工程（要約・配信・ステータス・履歴）は保持し、原文側だけ更新する
            notice = existing
            notice.subject = m.get("subject", notice.subject)
            notice.body_text = m.get("body_text", notice.body_text)
            notice.attachments = attachments
            notice.history.append(NoticeHistory(at=now, action="reimported_gmail", by=actor))
        repo.upsert_notice(notice)

    return summary
