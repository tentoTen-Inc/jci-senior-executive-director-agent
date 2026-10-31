"""専務の承認/差戻しフローのテスト。"""
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


def make(stage=ProposalStage.submitted):
    return Proposal(proposal_id="p1", title="A", stage=stage)


def test_approve_from_submitted_advances_to_goyaku(repo):
    repo.upsert_proposal(make(ProposalStage.submitted))
    res = client.post("/api/proposals/p1/approve", json={"comment": "OK"}, headers=IAP)
    assert res.status_code == 200
    body = res.json()
    assert body["sed_approval"]["status"] == "approved"
    assert body["sed_approval"]["by"] == "sed@10to10.co.jp"
    assert body["stage"] == "goyaku"
    assert any(a.action == "proposal.approve" for a in repo.list_audit())


def test_approve_other_stage_keeps_stage(repo):
    repo.upsert_proposal(make(ProposalStage.board))
    res = client.post("/api/proposals/p1/approve", json={}, headers=IAP)
    assert res.json()["stage"] == "board"  # 進めない
    assert res.json()["sed_approval"]["status"] == "approved"


def test_return_sets_returned_and_back_to_entry(repo):
    repo.upsert_proposal(make(ProposalStage.submitted))
    res = client.post(
        "/api/proposals/p1/return", json={"comment": "予算根拠を補足してください"}, headers=IAP
    )
    assert res.status_code == 200
    body = res.json()
    assert body["sed_approval"]["status"] == "returned"
    assert body["sed_approval"]["comment"] == "予算根拠を補足してください"
    assert body["stage"] == "entry"  # 委員会が直して［提出する］を押し直す
    assert any(a.action == "proposal.return" for a in repo.list_audit())


def test_not_found():
    assert client.post("/api/proposals/nope/approve", json={}).status_code == 404
    assert client.post("/api/proposals/nope/return", json={}).status_code == 404
