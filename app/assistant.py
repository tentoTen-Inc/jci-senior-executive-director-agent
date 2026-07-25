"""会員の自由文問い合わせへの応答（docs/nl-assistant-design.md, F8）。

- 自LOM情報（`rag.build_context`）だけを根拠に Gemini が答える（F8-1/F8-2）。
- 根拠不足・人の対応が必要なら Escalation を起票して取次ぐ（F8-3）。
- 他会員の個人情報はコンテキストに載せない（F8-4 の一次防御は rag 側）。
- 直近のやり取りを保持して文脈を繋ぐ（F8-5）。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

from linebot.v3.messaging import Message, TextMessage

from .inference import record_inference
from .llm import answer_question, model_name
from .models import (
    Conversation,
    ConversationTurn,
    Escalation,
    InferenceUsage,
    Member,
)
from .rag import build_context
from .repository import Repository

logger = logging.getLogger("jci-agent.assistant")

#: 保持する会話ターン数（直近3往復）
MAX_TURNS = 6

ESCALATION_NOTE = "この件は事務局に取次ぎました。担当者から折り返しご連絡します。"


def _history_text(conversation: Conversation | None) -> str:
    if conversation is None or not conversation.turns:
        return ""
    label = {"user": "会員", "assistant": "アシスタント"}
    return "\n".join(f"{label.get(t.role, t.role)}: {t.text}" for t in conversation.turns)


def _remember(
    repo: Repository, member: Member, question: str, answer: str, *, now: datetime
) -> None:
    conversation = repo.get_conversation(member.member_id) or Conversation(
        member_id=member.member_id
    )
    conversation.turns.append(ConversationTurn(at=now, role="user", text=question))
    conversation.turns.append(ConversationTurn(at=now, role="assistant", text=answer))
    conversation.turns = conversation.turns[-MAX_TURNS:]
    conversation.updated_at = now
    repo.save_conversation(conversation)


def _escalate(repo: Repository, member: Member, question: str, *, now: datetime) -> None:
    repo.save_escalation(
        Escalation(
            escalation_id=f"esc_{uuid.uuid4().hex[:10]}",
            member_id=member.member_id,
            kind="question",
            text=question,
            created_at=now,
        )
    )


def answer_member_question(
    repo: Repository, member: Member, question: str, *, now: datetime
) -> list[Message] | None:
    """自由文の質問に答える。LLMが使えない場合は None（呼び側でメニュー誘導）。"""
    context = build_context(repo, member, now=now)
    history = _history_text(repo.get_conversation(member.member_id))

    outcome = answer_question(question, context, history)
    if outcome is None:
        record_inference(
            repo, kind="member_qa", usage=InferenceUsage(model=model_name()), now=now,
            target=member.member_id, ok=False, error="generation_failed",
        )
        return None
    record_inference(
        repo, kind="member_qa", usage=outcome.usage, now=now, target=member.member_id
    )

    result = outcome.result
    text = result.answer
    if result.needs_human or not result.grounded:
        _escalate(repo, member, question, now=now)
        text = f"{text}\n\n{ESCALATION_NOTE}"
    _remember(repo, member, question, text, now=now)
    return [TextMessage(text=text)]
