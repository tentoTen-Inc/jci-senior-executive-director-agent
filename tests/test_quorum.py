"""定足数判定・委任先の記録（F4-5）のテスト。"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import main
from app.attendance import aggregate
from app.deps import set_repo
from app.models import (
    Attendance,
    AttendanceStatus,
    Event,
    EventStatus,
    EventType,
    Member,
    TargetScope,
    TargetScopeKind,
)
from app.repository import InMemoryRepository
from app.summary import build_summary_text
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)


@pytest.fixture(autouse=True)
def repo():
    r = InMemoryRepository()
    for i in range(1, 6):  # m1..m5
        r.upsert_member(Member(member_id=f"m{i}", name=f"会員{i}", line_user_id=f"U{i}"))
    r.upsert_event(Event(
        event_id="e1", type=EventType.理事会, title="8月理事会",
        datetime_start=datetime(2026, 8, 10, 19, 0),
        target_scope=TargetScope(kind=TargetScopeKind.all),
        quorum=3,
        status=EventStatus.open,
    ))
    set_repo(r)
    yield r
    set_repo(None)


def answer(repo, member_id: str, status: AttendanceStatus):
    repo.upsert_attendance(Attendance(event_id="e1", member_id=member_id, status=status))


def test_quorum_counts_proxies_separately(repo):
    answer(repo, "m1", AttendanceStatus.出席)
    answer(repo, "m2", AttendanceStatus.WEB出席)
    answer(repo, "m3", AttendanceStatus.委任)
    answer(repo, "m4", AttendanceStatus.欠席)

    s = aggregate(repo, "e1")
    assert (s.present, s.proxies, s.present_with_proxies) == (2, 1, 3)
    assert s.quorum == 3
    assert s.quorum_met is True  # 委任を含めれば3名で充足
    assert s.quorum_met_without_proxies is False  # 委任を除くと2名で不足


def test_quorum_met_without_proxies(repo):
    for mid in ("m1", "m2", "m3"):
        answer(repo, mid, AttendanceStatus.出席)
    s = aggregate(repo, "e1")
    assert s.quorum_met is True and s.quorum_met_without_proxies is True


def test_no_quorum_configured(repo):
    repo.upsert_event(repo.get_event("e1").model_copy(update={"quorum": None}))
    s = aggregate(repo, "e1")
    assert s.quorum is None
    assert s.quorum_met is None and s.quorum_met_without_proxies is None


def test_summary_text_shows_both_judgements(repo):
    answer(repo, "m1", AttendanceStatus.出席)
    answer(repo, "m2", AttendanceStatus.委任)
    text = build_summary_text(repo, "e1")
    assert "委任 1" in text
    assert "定足数 3名" in text
    assert "委任含む 2名→不足" in text
    assert "委任除く 1名→不足" in text


def test_summary_text_omits_quorum_when_unset(repo):
    repo.upsert_event(repo.get_event("e1").model_copy(update={"quorum": None}))
    assert "定足数" not in build_summary_text(repo, "e1")


def test_create_event_with_quorum():
    res = client.post("/api/events", json={
        "type": "理事会", "title": "9月理事会",
        "datetime_start": "2026-09-10T19:00:00",
        "target_scope": {"kind": "all", "value": []},
        "quorum": 4,
    })
    assert res.status_code == 200
    assert res.json()["quorum"] == 4


def test_set_proxy_member(repo):
    res = client.put(
        "/api/events/e1/attendances/m3",
        json={"status": "委任", "proxy_member_id": "m1"},
    )
    assert res.status_code == 200
    assert res.json()["proxy_member_id"] == "m1"
    assert repo.get_attendance("e1", "m3").proxy_member_id == "m1"


def test_proxy_requires_committed_status_and_existing_member():
    bad_status = client.put(
        "/api/events/e1/attendances/m3", json={"status": "出席", "proxy_member_id": "m1"}
    )
    assert bad_status.status_code == 400

    bad_member = client.put(
        "/api/events/e1/attendances/m3", json={"status": "委任", "proxy_member_id": "nope"}
    )
    assert bad_member.status_code == 400


def test_csv_includes_proxy_name(repo):
    client.put("/api/events/e1/attendances/m3", json={"status": "委任", "proxy_member_id": "m1"})
    csv_text = client.get("/api/events/e1/attendances.csv").text
    assert "proxy" in csv_text.splitlines()[0]
    row = next(line for line in csv_text.splitlines() if line.startswith("m3,"))
    assert "委任" in row and "会員1" in row  # 委任先は会員名で出す
