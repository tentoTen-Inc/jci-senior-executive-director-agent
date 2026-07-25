"""LLM推論ログとコスト集計（docs/dashboard-design.md §6「月間コスト」）。

Gemini 呼び出しごとに入出力トークンを `InferenceLog` として記録し、
単価を掛けて月間コストを概算する。単価は変動するため環境変数で上書き可能。
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime

from pydantic import BaseModel

from .models import InferenceLog, InferenceUsage
from .repository import Repository

logger = logging.getLogger("jci-agent.inference")

#: モデル別の単価（USD / 100万トークン）= (入力, 出力)。
#: Vertex AI Gemini の公開価格（2026-07 時点の把握値）。実請求は Cloud Billing を参照。
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
}
#: 未知モデルのフォールバック単価（高い方＝pro を採用し、過小見積りを避ける）。
FALLBACK_PRICE = MODEL_PRICES["gemini-2.5-pro"]


def _prices(model: str) -> tuple[float, float]:
    """単価を返す。環境変数 `LLM_PRICE_INPUT_USD_PER_1M` / `..._OUTPUT_...` が優先。"""
    env_in = os.environ.get("LLM_PRICE_INPUT_USD_PER_1M")
    env_out = os.environ.get("LLM_PRICE_OUTPUT_USD_PER_1M")
    if env_in and env_out:
        return (float(env_in), float(env_out))
    return MODEL_PRICES.get(model, FALLBACK_PRICE)


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """トークン数から概算コスト(USD)を算出する。"""
    price_in, price_out = _prices(model)
    cost = (input_tokens * price_in + output_tokens * price_out) / 1_000_000
    return round(cost, 6)


def record_inference(
    repo: Repository,
    *,
    kind: str,
    usage: InferenceUsage,
    now: datetime,
    target: str | None = None,
    ok: bool = True,
    error: str | None = None,
) -> InferenceLog:
    """推論ログを1件保存する。保存失敗は本処理を止めない（コスト記録は副作用）。"""
    log = InferenceLog(
        log_id=f"inf_{uuid.uuid4().hex[:12]}",
        at=now,
        kind=kind,
        model=usage.model,
        target=target,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=estimate_cost_usd(usage.model, usage.input_tokens, usage.output_tokens),
        ok=ok,
        error=error,
    )
    try:
        repo.save_inference_log(log)
    except Exception:  # noqa: BLE001 - 記録失敗でLLM機能自体は止めない
        logger.exception("推論ログの保存に失敗しました")
    return log


class LlmCostSummary(BaseModel):
    month_start: datetime
    calls: int
    failed_calls: int
    input_tokens: int
    output_tokens: int
    llm_cost_usd: float
    infra_cost_usd: float  # Cloud Run 等の固定概算（環境変数 INFRA_MONTHLY_USD）
    monthly_cost_usd: float


def llm_cost_summary(repo: Repository, *, now: datetime) -> LlmCostSummary:
    """当月（1日0時以降）の推論ログからコストを集計する。"""
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    logs = repo.list_inference_logs(since=month_start)
    llm_cost = round(sum(x.cost_usd for x in logs), 4)
    infra = float(os.environ.get("INFRA_MONTHLY_USD", "0"))
    return LlmCostSummary(
        month_start=month_start,
        calls=len(logs),
        failed_calls=sum(1 for x in logs if not x.ok),
        input_tokens=sum(x.input_tokens for x in logs),
        output_tokens=sum(x.output_tokens for x in logs),
        llm_cost_usd=llm_cost,
        infra_cost_usd=infra,
        monthly_cost_usd=round(llm_cost + infra, 4),
    )
