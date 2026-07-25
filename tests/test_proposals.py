"""議案CRUD・ステージ遷移のテスト。"""
import pytest
from fastapi.testclient import TestClient

from app import main
from app.deps import set_repo
from app.models import Proposal, ProposalStage
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
IAP = {"X-Goog-Authenticated-User-Email": "sed@10to10.co.jp"}


@pytest.fixture(autouse=True)
def repo():
    r = InMemoryRepository()
    set_repo(r)
    yield r
    set_repo(None)


def test_proposal_model_defaults():
    p = Proposal(proposal_id="p1", title="6月例会事業計画")
    assert p.stage == ProposalStage.entry
    assert p.sed_approval.status == "pending"
    assert p.status == "open"


def test_create_and_get(repo):
    res = client.post(
        "/api/proposals",
        json={"title": "7月例会事業計画(案)", "committee": "コト創り委員会"},
        headers=IAP,
    )
    assert res.status_code == 200
    pid = res.json()["proposal_id"]
    assert res.json()["history"][0]["by"] == "sed@10to10.co.jp"
    got = client.get(f"/api/proposals/{pid}")
    assert got.status_code == 200
    assert got.json()["committee"] == "コト創り委員会"
    # 監査
    assert any(a.action == "proposal.create" for a in repo.list_audit())


def test_list_and_status_filter():
    client.post("/api/proposals", json={"title": "A"}, headers=IAP)
    client.post("/api/proposals", json={"title": "B"}, headers=IAP)
    assert len(client.get("/api/proposals").json()) == 2
    assert len(client.get("/api/proposals?status=open").json()) == 2
    assert len(client.get("/api/proposals?status=closed").json()) == 0


def test_update():
    pid = client.post("/api/proposals", json={"title": "A"}, headers=IAP).json()["proposal_id"]
    res = client.put(f"/api/proposals/{pid}", json={"committee": "総務委員会", "number": "第1号"})
    assert res.status_code == 200
    assert res.json()["committee"] == "総務委員会"
    assert res.json()["number"] == "第1号"


def test_stage_transition(repo):
    pid = client.post("/api/proposals", json={"title": "A"}, headers=IAP).json()["proposal_id"]
    res = client.post(
        f"/api/proposals/{pid}/stage",
        json={"stage": ProposalStage.sed_review.value},
        headers=IAP,
    )
    assert res.status_code == 200
    assert res.json()["stage"] == "sed_review"
    # history に追記
    assert res.json()["history"][-1]["stage"] == "sed_review"
    assert any(a.action == "proposal.stage" for a in repo.list_audit())


def test_not_found():
    assert client.get("/api/proposals/nope").status_code == 404
    assert client.post("/api/proposals/nope/stage", json={"stage": "board"}).status_code == 404


def test_update_proposal_keeps_nested_model_typed(repo):
    """部分更新でネストしたモデル(deadlines)が dict に落ちないこと。"""
    pid = client.post("/api/proposals", json={"title": "A"}, headers=IAP).json()["proposal_id"]
    res = client.put(
        f"/api/proposals/{pid}",
        json={"deadlines": {"submit": "2026-08-05T23:59:00"}, "committee": "総務委員会"},
    )
    assert res.status_code == 200
    saved = repo.get_proposal(pid)
    from datetime import datetime

    assert saved.deadlines.submit == datetime(2026, 8, 5, 23, 59)  # モデルとして保持
    assert saved.committee == "総務委員会"
    assert saved.title == "A"  # 未指定は保持
