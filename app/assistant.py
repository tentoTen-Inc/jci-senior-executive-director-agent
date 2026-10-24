"""会員の自由文問い合わせへの応答（docs/nl-assistant-design.md, F8）。

- 自LOM情報（`rag.build_context`）だけを根拠に Gemini が答える（F8-1/F8-2）。
- 根拠不足・人の対応が必要なら［意見箱に送る］ボタンを付け、押したときだけその質問を意見箱に
  種類「AIが答えられなかった」で保存する（docs/feedback-box-design.md §3。自由文では書き込まない:
  docs/doc-session-design.md §1）。
- 他会員の個人情報はコンテキストに載せない（F8-4 の一次防御は rag 側）。
- 直近のやり取りを保持して文脈を繋ぐ（F8-5）。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from linebot.v3.messaging import (
    Message,
    PostbackAction,
    QuickReply,
    QuickReplyItem,
    TextMessage,
)

from . import ai_feedback, drive_knowledge, feedback_box, lake, lake_knowledge
from .inference import record_inference
from .llm import answer_question, model_for
from .models import (
    Conversation,
    ConversationTurn,
    InferenceUsage,
    Member,
)
from .rag import build_context
from .repository import Repository

logger = logging.getLogger("jci-agent.assistant")

#: 保持する会話ターン数（直近3往復）
MAX_TURNS = 6

ESCALATION_NOTE = ("うまく答えられなかった質問は、下の［意見箱に送る］で知らせていただけると、"
                   "答えられるよう改善に役立てます。")
#: postback の識別子。以前の［事務局に取り次ぐ］のボタン（`esc|…`）も同じ扱いで意見箱に保存する
ACTION_ESCALATE = "esc"
ESCALATE_LABEL = "意見箱に送る"
#: postback のデータの上限（300字）に収めるため、送る質問はこの長さまで
ESCALATE_TEXT_MAX = 250


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


def answer_member_question(
    repo: Repository, member: Member, question: str, *, now: datetime
) -> list[Message] | None:
    """自由文の質問に答える。LLMが使えない場合は None（呼び側でメニュー誘導）。"""
    started = time.monotonic()
    context = build_context(repo, member, now=now)
    # LINE で共有された資料・やり取りから、質問者が見られる範囲で関連するものを足す（P6-4a）
    chunks = lake_knowledge.search(member.line_user_id, question,
                                   drive_knowledge.scopes_for(member))
    searched = time.monotonic()
    lake_section = lake_knowledge.context_section(repo, chunks)
    if lake_section:
        context = f"{context}\n\n{lake_section}"
    history = _history_text(repo.get_conversation(member.member_id))

    outcome = answer_question(question, context, history)
    generated = time.monotonic()
    timing = {
        "context_search_ms": int((searched - started) * 1000),
        "llm_ms": int((generated - searched) * 1000),
    }
    logger.info("AI応答の所要時間: 材料と検索 %.1f秒 / 回答生成 %.1f秒",
                timing["context_search_ms"] / 1000, timing["llm_ms"] / 1000)
    if outcome is None:
        record_inference(
            repo, kind="member_qa", usage=InferenceUsage(model=model_for("member_qa")), now=now,
            target=member.member_id, ok=False, error="generation_failed",
        )
        return None
    record_inference(
        repo, kind="member_qa", usage=outcome.usage, now=now, target=member.member_id
    )

    result = outcome.result
    text = result.answer
    # 本文の [1] 等が何を指すか分かるよう、出典の一覧（ファイルは開けるリンク）を付ける
    footer = lake_knowledge.citation_footer(repo, chunks, result.answer, member.line_user_id)
    escalate = result.needs_human or not result.grounded
    if escalate:
        text = f"{text}\n\n{ESCALATION_NOTE}"
    # 会話の記憶にはリンク等を入れない（次の質問の文脈には本文だけで足りる）
    _remember(repo, member, question, text, now=now)
    if footer:
        text = f"{text}\n\n{footer}"

    message = TextMessage(text=text)
    if lake.is_enabled():
        # 回答・出典・根拠の有無を記録し、👍/👎 で評価してもらう（P6-4b）
        answer_id = ai_feedback.new_answer_id()
        ai_feedback.record_answer(
            answer_id, member, question, text, sources=chunks,
            grounded=result.grounded, needs_human=result.needs_human, usage=outcome.usage,
            timing=timing,
        )
        message = ai_feedback.with_feedback_buttons(message, answer_id)
    if escalate:
        items = list(message.quick_reply.items) if message.quick_reply else []
        message.quick_reply = QuickReply(items=[*items, QuickReplyItem(action=PostbackAction(
            label=ESCALATE_LABEL, data=f"{ACTION_ESCALATE}|{question[:ESCALATE_TEXT_MAX]}",
            display_text=ESCALATE_LABEL))])
    return [message]


def handle_escalate_postback(
    repo: Repository, member: Member, data: str, *, now: datetime
) -> list[Message]:
    """［意見箱に送る］（`esc|<質問>`）を押したときだけ、その質問を意見箱に保存する。"""
    question = data.split("|", 1)[1].strip() if "|" in data else ""
    if not question:
        return [TextMessage(text="この操作は無効です。")]
    feedback_box.save_unanswered(repo, member, question, now)
    return [TextMessage(text=feedback_box.UNANSWERED_SAVED)]
