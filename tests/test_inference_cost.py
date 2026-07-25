"""LLM推論ログとコスト集計のテスト（docs/dashboard-design.md §6 月間コスト）。"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import llm, main
from app.deps import set_repo
from app.inference import estimate_cost_usd, llm_cost_summary, record_inference
from app.models import InferenceUsage, Proposal
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
NOW = datetime(2026, 7, 25, 10, 0)

FAKE_JSON = '{"summary":"要約","points":["論点"],"concerns":["要確認"]}'


@pytest.fixture(autouse=True)
def repo():
    r = InMemoryRepository()
    set_repo(r)
    yield r
    set_repo(None)


@pytest.fixture(autouse=True)
def _clear_price_env(monkeypatch):
    monkeypatch.delenv("LLM_PRICE_INPUT_USD_PER_1M", raising=False)
    monkeypatch.delenv("LLM_PRICE_OUTPUT_USD_PER_1M", raising=False)
    monkeypatch.delenv("INFRA_MONTHLY_USD", raising=False)


def test_estimate_cost_uses_model_price():
    # gemini-2.5-pro: 入力 $1.25/1M, 出力 $10.00/1M
    assert estimate_cost_usd("gemini-2.5-pro", 1_000_000, 0) == 1.25
    assert estimate_cost_usd("gemini-2.5-pro", 0, 1_000_000) == 10.0
    assert estimate_cost_usd("gemini-2.5-flash", 1_000_000, 1_000_000) == 2.8


def test_estimate_cost_env_override(monkeypatch):
    monkeypatch.setenv("LLM_PRICE_INPUT_USD_PER_1M", "2")
    monkeypatch.setenv("LLM_PRICE_OUTPUT_USD_PER_1M", "4")
    assert estimate_cost_usd("gemini-2.5-pro", 500_000, 250_000) == 2.0


def test_estimate_cost_unknown_model_falls_back():
    assert estimate_cost_usd("unknown-model", 1_000_000, 0) == 1.25


def test_record_inference_saves_log(repo):
    log = record_inference(
        repo,
        kind="proposal_review",
        usage=InferenceUsage(model="gemini-2.5-pro", input_tokens=2000, output_tokens=500),
        now=NOW,
        target="p1",
    )
    saved = repo.list_inference_logs()
    assert len(saved) == 1
    assert saved[0].log_id == log.log_id
    assert saved[0].target == "p1"
    assert saved[0].cost_usd == estimate_cost_usd("gemini-2.5-pro", 2000, 500)
    assert saved[0].ok is True


def test_cost_summary_counts_current_month_only(repo):
    usage = InferenceUsage(model="gemini-2.5-pro", input_tokens=1_000_000, output_tokens=100_000)
    record_inference(repo, kind="proposal_review", usage=usage, now=NOW)
    # 先月分は当月コストに含めない
    record_inference(repo, kind="proposal_review", usage=usage, now=NOW - timedelta(days=40))
    # 失敗ログはトークン0でも件数に出る
    record_inference(
        repo, kind="proposal_review", usage=InferenceUsage(model="gemini-2.5-pro"),
        now=NOW, ok=False, error="generation_failed",
    )

    s = llm_cost_summary(repo, now=NOW)
    assert s.month_start == datetime(2026, 7, 1)
    assert (s.calls, s.failed_calls) == (2, 1)
    assert s.input_tokens == 1_000_000
    assert s.output_tokens == 100_000
    assert s.llm_cost_usd == 2.25  # 1.25 + 1.0
    assert s.infra_cost_usd == 0.0
    assert s.monthly_cost_usd == 2.25


def test_cost_summary_includes_infra_estimate(repo, monkeypatch):
    monkeypatch.setenv("INFRA_MONTHLY_USD", "12.5")
    s = llm_cost_summary(repo, now=NOW)
    assert s.monthly_cost_usd == 12.5


def test_llm_review_endpoint_records_tokens(monkeypatch, repo):
    monkeypatch.setattr(
        llm, "generate_review",
        lambda content, **kw: llm.Generation(text=FAKE_JSON, input_tokens=3000, output_tokens=700),
    )
    repo.upsert_proposal(Proposal(proposal_id="p1", title="A", content="本文"))
    assert client.post("/api/proposals/p1/llm-review").status_code == 200

    logs = repo.list_inference_logs()
    assert len(logs) == 1
    assert (logs[0].kind, logs[0].target, logs[0].ok) == ("proposal_review", "p1", True)
    assert (logs[0].input_tokens, logs[0].output_tokens) == (3000, 700)
    assert logs[0].cost_usd > 0


def test_llm_review_failure_is_recorded(monkeypatch, repo):
    def boom(content, **kw):
        raise RuntimeError("vertex unavailable")

    monkeypatch.setattr(llm, "generate_review", boom)
    repo.upsert_proposal(Proposal(proposal_id="p1", title="A", content="本文"))
    assert client.post("/api/proposals/p1/llm-review").status_code == 503

    logs = repo.list_inference_logs()
    assert len(logs) == 1
    assert logs[0].ok is False
    assert logs[0].cost_usd == 0.0


def test_empty_content_is_not_recorded(repo):
    """本文が空＝LLMを呼んでいないのでコスト記録もしない。"""
    repo.upsert_proposal(Proposal(proposal_id="p1", title="A", content=""))
    assert client.post("/api/proposals/p1/llm-review").status_code == 503
    assert repo.list_inference_logs() == []


def test_kpi_overview_exposes_cost(monkeypatch, repo):
    monkeypatch.setattr(
        llm, "generate_review",
        lambda content, **kw: llm.Generation(text=FAKE_JSON, input_tokens=1000, output_tokens=200),
    )
    repo.upsert_proposal(Proposal(proposal_id="p1", title="A", content="本文"))
    client.post("/api/proposals/p1/llm-review")

    body = client.get("/api/kpi/overview").json()
    assert body["cost"]["calls"] == 1
    assert body["cost"]["input_tokens"] == 1000
    assert body["cost"]["output_tokens"] == 200
    assert body["cost"]["monthly_cost_usd"] > 0
