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

from pydantic import BaseModel

from . import config
from .models import InferenceUsage, LlmReview

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


def generate_review(content: str, *, model: str, project: str, location: str) -> Generation:
    """Vertex AI Gemini を呼んで応答と使用トークンを返す（テストでモックする境界）。"""
    import vertexai
    from vertexai.generative_models import GenerativeModel

    vertexai.init(project=project, location=location)
    gm = GenerativeModel(model)
    resp = gm.generate_content(
        _PROMPT.format(content=content),
        generation_config={"response_mime_type": "application/json"},
    )
    usage = getattr(resp, "usage_metadata", None)
    return Generation(
        text=resp.text,
        input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
        output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
    )


def review_proposal(content: str) -> ReviewOutcome | None:
    """議案本文をレビューして結果と使用トークンを返す。失敗時は None。"""
    if not content or not content.strip():
        return None
    import os

    model = model_name()
    project = config.PROJECT_ID
    location = os.environ.get("VERTEX_AI_LOCATION", "asia-northeast1")
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
