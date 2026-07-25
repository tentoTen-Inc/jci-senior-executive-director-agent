"""Gemini内容レビュー結線のテスト（LLM境界をモック）。"""
import pytest
from fastapi.testclient import TestClient

from app import llm, main
from app.deps import set_repo
from app.llm import review_proposal
from app.models import Proposal
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)

FAKE_JSON = (
    '{"summary":"桜まつりで地域交流を図る事業。",'
    '"points":["予算の妥当性","安全管理体制"],'
    '"concerns":["雨天時の代替案が未記載","KPIの測定方法が曖昧"]}'
)


def fake_generation(content, **kw):
    return llm.Generation(text=FAKE_JSON, input_tokens=1200, output_tokens=300)


@pytest.fixture(autouse=True)
def repo():
    r = InMemoryRepository()
    set_repo(r)
    yield r
    set_repo(None)


def test_review_proposal_parses_json(monkeypatch):
    monkeypatch.setattr(llm, "generate_review", fake_generation)
    outcome = review_proposal("事業名: 桜まつり ...")
    assert outcome is not None
    review = outcome.review
    assert "桜まつり" in review.summary
    assert len(review.points) == 2
    assert any("雨天" in c for c in review.concerns)
    # コスト記録用にトークンを持ち帰る
    assert (outcome.usage.input_tokens, outcome.usage.output_tokens) == (1200, 300)


def test_review_proposal_empty_returns_none():
    assert review_proposal("") is None


def test_review_proposal_degrades_on_error(monkeypatch):
    def boom(content, **kw):
        raise RuntimeError("vertex unavailable")

    monkeypatch.setattr(llm, "generate_review", boom)
    assert review_proposal("本文あり") is None


def test_endpoint_saves_review(monkeypatch, repo):
    monkeypatch.setattr(llm, "generate_review", fake_generation)
    repo.upsert_proposal(Proposal(proposal_id="p1", title="A", content="事業名: 桜まつり"))
    res = client.post("/api/proposals/p1/llm-review")
    assert res.status_code == 200
    assert "桜まつり" in res.json()["summary"]
    assert repo.get_proposal("p1").llm_review is not None


def test_endpoint_503_when_no_content(repo):
    repo.upsert_proposal(Proposal(proposal_id="p1", title="A", content=""))
    res = client.post("/api/proposals/p1/llm-review")
    assert res.status_code == 503


def test_endpoint_404():
    assert client.post("/api/proposals/nope/llm-review").status_code == 404
