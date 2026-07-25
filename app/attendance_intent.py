"""自然文からの出欠意図解釈（F4-7）。

「来週の例会は欠席で」のような自由文を解釈し、**確認のうえ登録する**。
登録自体は既存の出欠 postback（`att|<event_id>|<status>`）に委ねるため、
ここでは確認メッセージ（はい＝既存postback／いいえ）を返すだけにしている。
"""
from __future__ import annotations

import logging
from datetime import datetime

from linebot.v3.messaging import (
    Message,
    PostbackAction,
    QuickReply,
    QuickReplyItem,
    TextMessage,
)

from .events import resolve_targets
from .inference import record_inference
from .line_messages import ACTION_ATT, make_postback
from .llm import model_name, parse_attendance_intent
from .models import AttendanceStatus, EventStatus, InferenceUsage, Member
from .repository import Repository

logger = logging.getLogger("jci-agent.attendance_intent")

MAX_CANDIDATES = 5
CANCEL_POSTBACK = "menu|出欠を回答"


def _candidates(repo: Repository, member: Member, now: datetime) -> list:
    """本人が対象の、これから開催されるイベント（新しい順ではなく開催順）。"""
    events = sorted(
        (e for e in repo.list_events(status=EventStatus.open) if e.datetime_start >= now),
        key=lambda e: e.datetime_start,
    )
    mine = [
        e for e in events
        if any(m.member_id == member.member_id for m in resolve_targets(repo, e))
    ]
    return mine[:MAX_CANDIDATES]


def _events_text(events: list) -> str:
    return "\n".join(
        f"- id={e.event_id} / {e.title} / {e.datetime_start.strftime('%Y-%m-%d %H:%M')}"
        for e in events
    )


def build_confirmation(event, status: str) -> Message:
    """「〜で登録しますか？」の確認メッセージ（はい＝既存の出欠postback）。"""
    when = event.datetime_start.strftime("%-m月%-d日")
    qr = QuickReply(
        items=[
            QuickReplyItem(
                action=PostbackAction(
                    label="はい",
                    data=make_postback(ACTION_ATT, event.event_id, status),
                    display_text=f"はい（{status}）",
                )
            ),
            QuickReplyItem(
                action=PostbackAction(
                    label="いいえ", data=CANCEL_POSTBACK, display_text="いいえ"
                )
            ),
        ]
    )
    return TextMessage(
        text=f"{when}の「{event.title}」を『{status}』で登録します。よろしいですか？",
        quick_reply=qr,
    )


def try_attendance_intent(
    repo: Repository, member: Member, text: str, *, now: datetime
) -> list[Message] | None:
    """自然文を出欠回答として解釈できたら確認メッセージを返す。できなければ None。"""
    events = _candidates(repo, member, now)
    if not events:
        return None

    outcome = parse_attendance_intent(text, _events_text(events))
    if outcome is None:
        record_inference(
            repo, kind="attendance_intent", usage=InferenceUsage(model=model_name()),
            now=now, target=member.member_id, ok=False, error="generation_failed",
        )
        return None
    record_inference(
        repo, kind="attendance_intent", usage=outcome.usage, now=now, target=member.member_id
    )

    intent = outcome.intent
    if not intent.confident or not intent.event_id or not intent.status:
        return None
    event = next((e for e in events if e.event_id == intent.event_id), None)
    if event is None:
        logger.info("解釈されたイベントが候補外でした: %s", intent.event_id)
        return None
    try:
        status = AttendanceStatus(intent.status).value
    except ValueError:
        logger.info("解釈された出欠が不正でした: %s", intent.status)
        return None
    if status == AttendanceStatus.未回答.value:
        return None
    return [build_confirmation(event, status)]
