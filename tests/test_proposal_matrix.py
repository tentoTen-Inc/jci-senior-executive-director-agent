"""委員会別提出マトリクスのテスト。"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import main
from app.deps import set_repo
from app.models import Proposal, ProposalDeadlines, ProposalStage
from app.proposal_matrix import UNSET_COMMITTEE, build_proposal_matrix
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
NOW = datetime(2026, 7, 25, 10, 0)


@pytest.fixture(autouse=True)
def repo():
    r = InMemoryRepository()
    set_repo(r)
    yield r
    set_repo(None)


def _prop(pid: str, committee: str | None, stage: ProposalStage, **deadlines) -> Proposal:
    return Proposal(
        proposal_id=pid,
        title=f"{pid}議案",
        committee=committee,
        stage=stage,
        deadlines=ProposalDeadlines(**deadlines),
    )


def test_cell_states_from_real_deadlines():
    """締切は Proposal.deadlines の実データ、完了判定は stage の進み具合。"""
    m = build_proposal_matrix(
        [
            # エントリー締切超過（stage=entry のまま）
            _prop("p1", "総務委員会", ProposalStage.entry, entry=NOW - timedelta(days=2)),
            # 提出締切が2日後 → 間近
            _prop("p2", "総務委員会", ProposalStage.submitted, submit=NOW + timedelta(days=2)),
            # 配信締切が10日後 → 期限内
            _prop("p3", "総務委員会", ProposalStage.submitted, deliver=NOW + timedelta(days=10)),
        ],
        now=NOW,
    )
    row = m.rows[0]
    assert row.committee == "総務委員会"
    assert row.total == 3
    # p2/p3 は submitted 以降なのでエントリーは完了、p1 のみ遅延
    assert row.cells["entry"].counts == {
        "done": 2, "overdue": 1, "soon": 0, "pending": 0, "unset": 0,
    }
    assert row.cells["entry"].state == "overdue"
    assert row.cells["entry"].samples == ["p1議案（〆7/23）"]
    # 提出: p2=soon, p3=unset(締切未設定), p1=unset
    assert row.cells["submit"].state == "soon"
    assert row.cells["submit"].counts["soon"] == 1
    assert row.cells["submit"].counts["unset"] == 2
    # 配信: 誰も board 未到達。p3 のみ締切あり(期限内)
    assert row.cells["deliver"].counts == {
        "done": 0, "overdue": 0, "soon": 0, "pending": 1, "unset": 2,
    }
    assert row.cells["deliver"].state == "unset"
    assert row.cells["deliver"].next_deadline == NOW + timedelta(days=10)


def test_done_when_stage_passed_milestone():
    """締切を過ぎていても、該当マイルストーンに到達済みなら完了扱い。"""
    m = build_proposal_matrix(
        [_prop("p1", "総務委員会", ProposalStage.board, entry=NOW - timedelta(days=30),
               submit=NOW - timedelta(days=20), deliver=NOW - timedelta(days=10))],
        now=NOW,
    )
    cells = m.rows[0].cells
    assert [cells[k].state for k in ("entry", "submit", "deliver")] == ["done", "done", "done"]
    assert cells["deliver"].next_deadline is None


def test_committee_grouping_and_unset_last():
    m = build_proposal_matrix(
        [
            _prop("p1", "コト創り委員会", ProposalStage.entry),
            _prop("p2", None, ProposalStage.entry),
            _prop("p3", "総務委員会", ProposalStage.entry),
        ],
        now=NOW,
    )
    assert [r.committee for r in m.rows] == ["コト創り委員会", "総務委員会", UNSET_COMMITTEE]
    assert m.totals["entry"]["unset"] == 3


def test_empty():
    m = build_proposal_matrix([], now=NOW)
    assert m.rows == []
    assert m.totals["entry"] == {"done": 0, "pending": 0, "unset": 0, "soon": 0, "overdue": 0}


def test_matrix_api_excludes_closed_by_default(repo):
    repo.upsert_proposal(_prop("p1", "総務委員会", ProposalStage.entry))
    closed = _prop("p2", "閉幕委員会", ProposalStage.verified)
    closed.status = "closed"
    repo.upsert_proposal(closed)

    res = client.get("/api/proposals/matrix")
    assert res.status_code == 200
    assert [r["committee"] for r in res.json()["rows"]] == ["総務委員会"]

    # status を明示すればクローズ済みも見られる
    res = client.get("/api/proposals/matrix?status=closed")
    assert [r["committee"] for r in res.json()["rows"]] == ["閉幕委員会"]


def test_matrix_route_not_shadowed_by_proposal_id(repo):
    """/proposals/matrix が /proposals/{id} に飲まれていないこと。"""
    res = client.get("/api/proposals/matrix")
    assert res.status_code == 200
    assert "rows" in res.json()
    assert client.get("/api/proposals/nonexistent").status_code == 404
