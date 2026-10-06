"""AI 回答の記録と 👍/👎 評価（docs/lake-ai-design.md §5 / P6-4b）。

- 回答ごとに `ai_answer` イベント（質問・回答・使った出典・根拠の有無・モデル・トークン）を
  データレイクへ記録する。
- 回答には「👍 役に立った / 👎 違う」のクイックリプライを付け、押されたら `ai_feedback` を記録する。
- データレイクが無効なら記録もボタンも付けない（押されても記録できないため）。
"""
from __future__ import annotations

import uuid

from linebot.v3.messaging import (
    Message,
    PostbackAction,
    QuickReply,
    QuickReplyItem,
    TextMessage,
)

from . import lake
from .models import InferenceUsage, Member

KIND_ANSWER = "ai_answer"
KIND_FEEDBACK = "ai_feedback"
ACTION_FEEDBACK = "aifb"
RATINGS = {"up": "👍 役に立った", "down": "👎 違う"}

THANKS = {
    "up": "ありがとうございます！今後の回答の参考にします。",
    "down": (
        "ご指摘ありがとうございます。回答の改善に使わせていただきます。\n"
        "急ぎの確認が必要でしたら「事務局に連絡」と送ってください。"
    ),
}


def new_answer_id() -> str:
    return f"ans_{uuid.uuid4().hex[:12]}"


def record_answer(
    answer_id: str, member: Member, question: str, answer: str, *,
    sources: list[dict], grounded: bool, needs_human: bool, usage: InferenceUsage,
) -> None:
    """AI 回答を記録する（評価データの元）。"""
    lake.publish([lake.envelope(KIND_ANSWER, {
        "answer_id": answer_id,
        "member_id": member.member_id,
        "user_id": member.line_user_id,
        "question": question,
        "answer": answer,
        "sources": [
            {"chunk_id": s.get("chunk_id"), "title": s.get("title"),
             "scope_type": s.get("scope_type"), "distance": s.get("distance")}
            for s in sources
        ],
        "grounded": grounded,
        "needs_human": needs_human,
        "model": usage.model,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
    }, id=answer_id)])


def with_feedback_buttons(message: TextMessage, answer_id: str) -> TextMessage:
    """回答に 👍/👎 のクイックリプライを付ける。"""
    message.quick_reply = QuickReply(items=[
        QuickReplyItem(action=PostbackAction(
            label=label, data=f"{ACTION_FEEDBACK}|{answer_id}|{rating}", display_text=label,
        ))
        for rating, label in RATINGS.items()
    ])
    return message


def handle_feedback_postback(member: Member, data: str) -> list[Message]:
    """`aifb|<answer_id>|up|down` を記録してお礼を返す。"""
    parts = data.split("|")
    if len(parts) != 3 or parts[2] not in RATINGS:
        return [TextMessage(text="この評価は受け付けられませんでした。")]
    _, answer_id, rating = parts
    lake.publish([lake.envelope(KIND_FEEDBACK, {
        "answer_id": answer_id, "rating": rating,
        "member_id": member.member_id, "user_id": member.line_user_id,
    })])
    return [TextMessage(text=THANKS[rating])]
