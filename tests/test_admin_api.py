"""管理APIのユニットテスト（TestClient + InMemoryRepository）。"""
import pytest
from fastapi.testclient import TestClient

from app import main
from app.deps import set_repo
from app.models import AttendanceStatus, Member
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)


@pytest.fixture(autouse=True)
def repo():
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="m1", name="太郎", line_user_id="U1", committee="総務委員会"))
    r.upsert_member(Member(member_id="m2", name="次郎", line_user_id="U2", committee="総務委員会"))
    set_repo(r)
    yield r
    set_repo(None)


def create_event(**over):
    payload = {
        "type": "例会",
        "title": "6月例会",
        "datetime_start": "2026-06-25T19:00:00",
        "location": "会館",
        "target_scope": {"kind": "all", "value": []},
        "status": "open",
    }
    payload.update(over)
    res = client.post("/admin/events", json=payload)
    assert res.status_code == 200
    return res.json()


def test_create_and_get_event():
    ev = create_event()
    assert ev["event_id"].startswith("ev_")
    res = client.get(f"/admin/events/{ev['event_id']}")
    assert res.status_code == 200
    body = res.json()
    assert body["summary"]["total_targets"] == 2
    assert body["summary"]["unanswered"] == 2


def test_list_events():
    create_event()
    create_event(title="理事会", type="理事会")
    res = client.get("/admin/events")
    assert len(res.json()) == 2


def test_update_attendance_and_summary():
    ev = create_event()
    eid = ev["event_id"]
    res = client.put(
        f"/admin/events/{eid}/attendances/m1",
        json={"status": AttendanceStatus.出席.value},
    )
    assert res.status_code == 200
    summary = client.get(f"/admin/events/{eid}/attendances").json()["summary"]
    assert summary["answered"] == 1
    assert summary["attendance_rate"] == 0.5


def test_csv_export():
    ev = create_event()
    eid = ev["event_id"]
    client.put(f"/admin/events/{eid}/attendances/m1", json={"status": "出席"})
    res = client.get(f"/admin/events/{eid}/attendances.csv")
    assert res.status_code == 200
    assert "text/csv" in res.headers["content-type"]
    assert "m1" in res.text and "出席" in res.text


def test_manual_remind_targets_unanswered():
    ev = create_event()
    eid = ev["event_id"]
    client.put(f"/admin/events/{eid}/attendances/m1", json={"status": "出席"})
    res = client.post(f"/admin/events/{eid}/remind")
    assert res.status_code == 200
    assert res.json()["targets"] == ["m2"]


def test_settings_get_put():
    assert client.get("/admin/settings").json()["kill_switch"] is False
    res = client.put("/admin/settings", json={"kill_switch": True})
    assert res.status_code == 200
    assert client.get("/admin/settings").json()["kill_switch"] is True


def test_members_and_invite():
    assert len(client.get("/admin/members").json()) == 2
    res = client.post("/admin/members/m1/invite")
    assert res.status_code == 200
    assert res.json()["member_id"] == "m1"
    assert len(res.json()["code"]) == 8
    # 存在しない会員
    assert client.post("/admin/members/zzz/invite").status_code == 404


def test_event_summary_endpoint():
    ev = create_event()
    eid = ev["event_id"]
    client.put(f"/admin/events/{eid}/attendances/m1", json={"status": "出席"})
    res = client.get(f"/admin/events/{eid}/summary")
    assert res.status_code == 200
    assert "6月例会" in res.json()["text"]


def test_close_event_endpoint(repo):
    ev = create_event()
    eid = ev["event_id"]
    res = client.post(f"/admin/events/{eid}/close")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "closed"
    assert "summary_job_id" in body
    # クローズ後は status=closed
    assert repo.get_event(eid).status.value == "closed"


def test_seed_and_list_policies():
    res = client.post("/admin/policies/seed")
    assert res.status_code == 200
    assert "rp_例会_default" in res.json()["seeded"]
    listed = client.get("/admin/policies").json()
    ids = {p["policy_id"] for p in listed}
    assert "rp_例会_default" in ids and "rp_理事会_default" in ids


def test_get_and_update_policy(repo):
    client.post("/admin/policies/seed")
    got = client.get("/admin/policies/rp_例会_default")
    assert got.status_code == 200
    policy = got.json()

    policy["stages"][0]["offset_minutes"] = -4320  # 3日前へ変更
    res = client.put(
        "/admin/policies/rp_例会_default",
        json=policy,
        headers={"X-Goog-Authenticated-User-Email": "sed@10to10.co.jp"},
    )
    assert res.status_code == 200
    assert repo.get_policy("rp_例会_default").stages[0].offset_minutes == -4320
    assert any(a.action == "policy.update" for a in repo.list_audit())


def test_update_policy_id_mismatch():
    client.post("/admin/policies/seed")
    policy = client.get("/admin/policies/rp_例会_default").json()
    res = client.put("/admin/policies/rp_理事会_default", json=policy)
    assert res.status_code == 400


def test_policy_not_found():
    assert client.get("/admin/policies/nope").status_code == 404


def test_event_not_found():
    assert client.get("/admin/events/nope").status_code == 404


def test_admin_requires_auth():
    noauth = TestClient(main.app)
    assert noauth.get("/admin/events").status_code == 401
    # 不正なシークレット
    bad = TestClient(main.app, headers={"X-Admin-Token": "wrong"})
    assert bad.get("/admin/members").status_code == 401


def test_escalations_list_and_handled(repo):
    """会員からの取次依頼を一覧し、対応済みにできる（F8-3）。"""
    from datetime import datetime

    from app.models import Escalation

    repo.save_escalation(Escalation(
        escalation_id="esc1", member_id="m1", kind="question",
        text="会費を分割で払えますか", created_at=datetime(2026, 7, 25, 9, 0),
    ))
    repo.save_escalation(Escalation(
        escalation_id="esc2", member_id="m2", kind="contact",
        created_at=datetime(2026, 7, 25, 10, 0), status="handled",
    ))

    open_items = client.get("/admin/escalations?status=open").json()
    assert [e["escalation_id"] for e in open_items] == ["esc1"]
    assert open_items[0]["member_name"] == "太郎"  # 会員名を補完して返す

    # 新しい順
    assert [e["escalation_id"] for e in client.get("/admin/escalations").json()] == ["esc2", "esc1"]

    res = client.post(
        "/admin/escalations/esc1/handled",
        headers={"X-Goog-Authenticated-User-Email": "sed@10to10.co.jp"},
    )
    assert res.status_code == 200
    assert repo.get_escalation("esc1").status == "handled"
    assert any(a.action == "escalation.handled" for a in repo.list_audit())
    assert client.get("/admin/escalations?status=open").json() == []


def test_escalation_handled_404():
    assert client.post("/admin/escalations/nope/handled").status_code == 404
