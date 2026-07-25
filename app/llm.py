"""Vertex AI Gemini による議案の内容レビュー（F6-5, docs/dashboard-design.md §6）。

- 本番は Vertex AI の Gemini を呼ぶ。
- 認証/ライブラリ未設定や呼び出し失敗時は graceful degrade（reviewed=False を返す）。
- テストでは `generate_review` をモックする。
- 消費トークンは応答の usage_metadata から取得し、コスト記録に使う（§6 月間コスト）。

レビューは「助言」であり最終判断は人間（専務理事）が行う（F6-8）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from pydantic import BaseModel

from . import config
from .models import InferenceUsage, LlmReview, NoticeDigest

logger = logging.getLogger("jci-agent.llm")

DEFAULT_MODEL = "gemini-2.5-pro"

_PROMPT = """あなたは青年会議所(JC)の専務理事を補佐するアシスタントです。
次の議案(事業計画書)の内容をレビューし、JSONで出力してください。
出力フィールド:
- summary: 議案の3行以内の要約
- points: 審議のポイント(論点)の配列(最大5件)
- concerns: 曖昧・矛盾・記載不足など要確認点の配列(最大5件)
助言が目的で最終判断は人間が行います。事実を断定しすぎないでください。

# 議案本文
{content}

# 出力(JSONのみ)
"""


_NOTICE_PROMPT = """あなたは青年会議所(JC)の専務理事を補佐するアシスタントです。
ブロック協議会等から届いた対外連絡を、LOM会員向けに整理してJSONで出力してください。
出力フィールド:
- summary: 連絡内容の3行以内の要約
- announcement: 会員へLINEで流す告知文(敬体・200字以内・重要な日付と場所は必ず含める)
- audience_hint: 想定する伝達対象("全員" "総務委員会" "出向者" 等。不明なら null)
- deadline: 期限や締切があれば "YYYY-MM-DD" 形式(なければ null)
- actions: 必要なアクション(参加登録/資料提出/出向 等)の配列(最大5件)
原文にない事実を追加しないでください。判断に迷う点は actions に「専務確認」として残してください。

# 対外連絡（原文）
件名: {subject}
差出人: {sender}
本文:
{body}

# 出力(JSONのみ)
"""


class Generation(BaseModel):
    """LLM応答の生テキストと消費トークン。"""

    text: str
    input_tokens: int = 0
    output_tokens: int = 0


class ReviewOutcome(BaseModel):
    """レビュー結果とコスト記録用の使用量。"""

    review: LlmReview
    usage: InferenceUsage


def model_name() -> str:
    import os

    return os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)


def _call_gemini(prompt: str, *, model: str, project: str, location: str) -> Generation:
    """Vertex AI Gemini に JSON 応答を求め、応答と使用トークンを返す。"""
    import vertexai
    from vertexai.generative_models import GenerativeModel

    vertexai.init(project=project, location=location)
    gm = GenerativeModel(model)
    resp = gm.generate_content(
        prompt,
        generation_config={"response_mime_type": "application/json"},
    )
    usage = getattr(resp, "usage_metadata", None)
    return Generation(
        text=resp.text,
        input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
        output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
    )


def _target() -> tuple[str, str, str]:
    """(model, project, location) を返す。"""
    import os

    return (
        model_name(),
        config.PROJECT_ID,
        os.environ.get("VERTEX_AI_LOCATION", "asia-northeast1"),
    )


def generate_review(content: str, *, model: str, project: str, location: str) -> Generation:
    """議案レビューの生成（テストでモックする境界）。"""
    return _call_gemini(
        _PROMPT.format(content=content), model=model, project=project, location=location
    )


def review_proposal(content: str) -> ReviewOutcome | None:
    """議案本文をレビューして結果と使用トークンを返す。失敗時は None。"""
    if not content or not content.strip():
        return None
    model, project, location = _target()
    try:
        gen = generate_review(content, model=model, project=project, location=location)
        data = json.loads(gen.text)
        review = LlmReview(
            summary=str(data.get("summary", "")).strip(),
            points=[str(x) for x in data.get("points", [])][:5],
            concerns=[str(x) for x in data.get("concerns", [])][:5],
            model=model,
        )
    except Exception:  # noqa: BLE001 - LLM未設定/失敗でも本処理は止めない
        logger.exception("LLMレビューに失敗しました")
        return None
    return ReviewOutcome(
        review=review,
        usage=InferenceUsage(
            model=model,
            input_tokens=gen.input_tokens,
            output_tokens=gen.output_tokens,
        ),
    )


# --------------------------------------------------------------------------- #
# 対外連絡の要約・告知文生成（F5-2/F5-3, docs/external-notice-design.md §3.2）
# --------------------------------------------------------------------------- #
class DigestOutcome(BaseModel):
    digest: NoticeDigest
    usage: InferenceUsage


def generate_notice_digest(
    subject: str, sender: str, body: str, *, model: str, project: str, location: str
) -> Generation:
    """対外連絡の要約生成（テストでモックする境界）。"""
    prompt = _NOTICE_PROMPT.format(subject=subject, sender=sender or "不明", body=body)
    return _call_gemini(prompt, model=model, project=project, location=location)


def _parse_date(value) -> datetime | None:
    """"YYYY-MM-DD" を datetime にする。解釈できなければ None（期限を捏造しない）。"""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        logger.info("期限の解釈に失敗しました: %r", value)
        return None


def digest_notice(subject: str, sender: str, body: str) -> DigestOutcome | None:
    """対外連絡の要約・告知文・対象・期限・アクションを生成する。失敗時は None。"""
    if not body or not body.strip():
        return None
    model, project, location = _target()
    try:
        gen = generate_notice_digest(
            subject, sender, body, model=model, project=project, location=location
        )
        data = json.loads(gen.text)
        digest = NoticeDigest(
            summary=str(data.get("summary", "")).strip(),
            announcement=str(data.get("announcement", "")).strip(),
            audience_hint=(str(data["audience_hint"]).strip() or None)
            if data.get("audience_hint")
            else None,
            deadline=_parse_date(data.get("deadline")),
            actions=[str(x) for x in data.get("actions", [])][:5],
            model=model,
        )
    except Exception:  # noqa: BLE001 - LLM未設定/失敗でも本処理は止めない
        logger.exception("対外連絡の要約生成に失敗しました")
        return None
    return DigestOutcome(
        digest=digest,
        usage=InferenceUsage(
            model=model,
            input_tokens=gen.input_tokens,
            output_tokens=gen.output_tokens,
        ),
    )
