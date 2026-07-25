"""対外連絡のアクション追跡（docs/external-notice-design.md §4 P3-4, F5-3/F5-5）。

`NoticeDigest.actions` を会員単位で追跡できるタスクにし、未対応者だけを催促する。
LINE の「対応しました」ボタン（postback）からも完了を記録する。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from linebot.v3.messaging import (
    Message,
    PostbackAction,
    QuickReply,
    QuickReplyItem,
    TextMessage,
)

from .models import ExternalNotice, NoticeAction
from .repository import Repository

#: LINE postback の action 種別（line_messages の ACTION_* と同じ名前空間）
ACTION_NOTICE_DONE = "ntca"


def create_actions(
    repo: Repository,
    notice: ExternalNotice,
    *,
    titles: list[str],
    assignees: list[str],
    due: datetime | None,
    now: datetime,
) -> list[NoticeAction]:
    """アクションを起票する。既に同じ title のアクションがあれば作り直さない（冪等）。"""
    existing = {a.title: a for a in repo.list_notice_actions(notice_id=notice.notice_id)}
    created: list[NoticeAction] = []
    for title in titles:
        if not title.strip() or title in existing:
            continue
        action = NoticeAction(
            action_id=f"nta_{uuid.uuid4().hex[:10]}",
            notice_id=notice.notice_id,
            title=title.strip(),
            due=due if due is not None else (notice.digest.deadline if notice.digest else None),
            assignees=list(assignees),
            created_at=now,
        )
        repo.upsert_notice_action(action)
        created.append(action)
    return created


def mark_done(repo: Repository, action_id: str, member_id: str) -> NoticeAction | None:
    """会員1名の対応を記録する。全員完了したら status=closed。"""
    action = repo.get_notice_action(action_id)
    if action is None:
        return None
    if member_id not in action.done_by:
        action.done_by.append(member_id)
    if not action.pending:
        action.status = "closed"
    repo.upsert_notice_action(action)
    return action


def build_reminder_message(notice: ExternalNotice, action: NoticeAction) -> Message:
    """未対応者へ送る催促メッセージ（「対応しました」ボタン付き）。"""
    lines = [f"【対応のお願い】{notice.subject}", f"・{action.title}"]
    if action.due is not None:
        lines.append(f"・期限: {action.due.month}月{action.due.day}日")
    lines.append("対応が済んだら下のボタンを押してください。")
    qr = QuickReply(
        items=[
            QuickReplyItem(
                action=PostbackAction(
                    label="対応しました",
                    data=f"{ACTION_NOTICE_DONE}|{action.action_id}|done",
                    display_text="対応しました",
                )
            )
        ]
    )
    return TextMessage(text="\n".join(lines), quick_reply=qr)


def handle_done_postback(repo: Repository, member_id: str, data: str) -> list[Message]:
    """`ntca|<action_id>|done` を処理して返信メッセージを返す。"""
    parts = data.split("|")
    action_id = parts[1] if len(parts) > 1 else ""
    action = mark_done(repo, action_id, member_id)
    if action is None:
        return [TextMessage(text="対象の依頼が見つかりませんでした。事務局にご連絡ください。")]
    return [TextMessage(text=f"「{action.title}」の対応を記録しました。ありがとうございます！")]


def open_action_count(repo: Repository) -> int:
    """未対応者が残っているアクション数（ホームの「要対応」用）。"""
    return sum(1 for a in repo.list_notice_actions(status="open") if a.pending)
