"""Proposal matrix 集計 / API のテスト。"""
from __future__ import annotations

import pytest
from datetime import datetime

from fastapi.testclient import TestClient

from app import main
from app.deps import set_repo
from app.models import Proposal, ProposalStage
from app.proposal_matrix import build_proposal_matrix
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)


@pytest.fixture(autouse=True)
def repo():
    r = InMemoryRepository()
    set_repo(r)
    yield r
    set_repo(None)


def _seed(repo):
    now = datetime.now()
    props = [
        Proposal(proposal_id="p1", title="p1", committee="コト創り委員会", stage=ProposalStage.entry,),
        Proposal(proposal_id="p2", title="p2", committee="コト創り委員会", stage=ProposalStage.submitted,),
        Proposal(proposal_id="p3", title="p3", committee="総務委員会", stage=ProposalStage.entry,),
    ]
    for p in props:
        repo.upsert_proposal(p)
    return props


def test_build_proposal_matrix(repo):
    _seed(repo)
    data = build_proposal_matrix(repo.list_proposals())
    assert data["committees"] == ["コト創り委員会", "総務委員会"]
    assert data["grid"][0]["counts"]["entry"] == 1
    assert data["grid"][0]["counts"]["submitted"] == 1
    assert data["grid"][1]["counts"]["entry"] == 1


def test_matrix_api(repo):
    _seed(repo)
    res = client.get("/api/proposals/matrix")
    assert res.status_code == 200
    data = res.json()
    assert data["grid"][0]["counts"]["entry"] == 1
    assert data["grid"][0]["counts"]["submitted"] == 1
    assert data["grid"][1]["counts"]["entry"] == 1
