"""名簿・イベントの編集API（F10-5）のテスト。"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import main
from app.deps import set_repo
from app.models import (
    Contact,
    Event,
    EventStatus,
    EventType,
    Member,
    MemberStatus,
    MemberType,
    TargetScope,
    TargetScopeKind,
)
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
IAP = {"X-Goog-Authenticated-User-Email": "sed@10to10.co.jp"}


@pytest.fixture(autouse=True)
def repo():
    r = InMemoryRepository()
    r.upsert_member(Member(
        member_id="m1", name="遠藤太郎", kana="えんどうたろう", line_user_id="U1",
        committee="総務委員会", contact=Contact(email="taro@example.jp"),
    ))
    r.upsert_event(Event(
        event_id="ev1", type=EventType.例会, title="8月例会",
        datetime_start=datetime(2026, 8, 20, 19, 0), location="会館",
        target_scope=TargetScope(kind=TargetScopeKind.all), status=EventStatus.open,
    ))
    set_repo(r)
    yield r
    set_repo(None)


# --------------------------------------------------------------------------- #
# 会員
# --------------------------------------------------------------------------- #
def test_create_member(repo):
    res = client.post("/api/members/new", json={
        "name": "新入会員", "kana": "しんにゅうかいいん", "committee": "コト創り委員会",
        "committee_role": "委員", "contact": {"email": "new@example.jp"},
    }, headers=IAP)
    assert res.status_code == 200
    body = res.json()
    assert body["member_id"].startswith("mem_")
    assert body["status"] == "active" and body["member_type"] == "regular"
    assert body["line_user_id"] is None  # 連携は招待コード経由
    saved = repo.get_member(body["member_id"])
    assert saved.contact.email == "new@example.jp"
    assert any(a.action == "member.create" for a in repo.list_audit())


def test_create_member_requires_name():
    assert client.post("/api/members/new", json={"name": "  "}).status_code == 400


def test_create_member_route_not_shadowed_by_get():
    """POST /members/new が /members/{id} 系に飲まれていないこと。"""
    assert client.post("/api/members/new", json={"name": "太郎"}).status_code == 200


def test_get_member_and_404():
    assert client.get("/api/members/m1").json()["name"] == "遠藤太郎"
    assert client.get("/api/members/nope").status_code == 404


def test_update_member_partial(repo):
    res = client.put("/api/members/m1", json={
        "committee": "コト創り委員会", "officer_role": "専務理事",
    }, headers=IAP)
    assert res.status_code == 200
    saved = repo.get_member("m1")
    assert (saved.committee, saved.officer_role) == ("コト創り委員会", "専務理事")
    # 指定しなかった項目は保持される
    assert saved.name == "遠藤太郎" and saved.kana == "えんどうたろう"
    assert saved.contact.email == "taro@example.jp"
    assert saved.line_user_id == "U1"  # LINE連携は編集で壊さない
    audit = [a for a in repo.list_audit() if a.action == "member.update"]
    assert audit and audit[0].detail == "committee,officer_role"


def test_update_member_status_and_type(repo):
    res = client.put("/api/members/m1", json={"status": "inactive", "member_type": "ob"})
    assert res.status_code == 200
    saved = repo.get_member("m1")
    assert saved.status == MemberStatus.inactive
    assert saved.member_type == MemberType.ob
    assert saved.is_deliverable is False  # 退会/OBは配信対象外になる


def test_update_member_contact_replaces_object(repo):
    client.put("/api/members/m1", json={"contact": {"mobile": "090-1111-2222"}})
    saved = repo.get_member("m1")
    assert saved.contact.mobile == "090-1111-2222"
    assert saved.contact.email is None  # contact はオブジェクト単位で置き換わる


def test_update_member_rejects_empty_name_and_404():
    assert client.put("/api/members/m1", json={"name": " "}).status_code == 400
    assert client.put("/api/members/nope", json={"name": "x"}).status_code == 404


# --------------------------------------------------------------------------- #
# イベント
# --------------------------------------------------------------------------- #
def test_create_event_is_audited(repo):
    res = client.post("/api/events", json={
        "type": "理事会", "title": "9月理事会",
        "datetime_start": "2026-09-10T19:00:00",
        "target_scope": {"kind": "all", "value": []},
        "quorum": 5,
    }, headers=IAP)
    assert res.status_code == 200
    assert any(a.action == "event.create" for a in repo.list_audit())


def test_update_event_partial(repo):
    res = client.put("/api/events/ev1", json={
        "location": "体験交流館",
        "attendance_deadline": "2026-08-13T23:59:00",
        "quorum": 11,
    }, headers=IAP)
    assert res.status_code == 200
    saved = repo.get_event("ev1")
    assert saved.location == "体験交流館"
    assert saved.attendance_deadline == datetime(2026, 8, 13, 23, 59)
    assert saved.quorum == 11
    # 指定しなかった項目は保持
    assert saved.title == "8月例会"
    assert saved.datetime_start == datetime(2026, 8, 20, 19, 0)
    audit = [a for a in repo.list_audit() if a.action == "event.update"]
    assert audit and "quorum" in audit[0].detail


def test_update_event_target_scope_and_status(repo):
    res = client.put("/api/events/ev1", json={
        "target_scope": {"kind": "committee", "value": ["総務委員会"]},
        "status": "closed",
    })
    assert res.status_code == 200
    saved = repo.get_event("ev1")
    assert saved.target_scope.kind.value == "committee"
    assert saved.target_scope.value == ["総務委員会"]
    assert saved.status == EventStatus.closed


def test_update_event_rejects_empty_title_and_404():
    assert client.put("/api/events/ev1", json={"title": ""}).status_code == 400
    assert client.put("/api/events/nope", json={"title": "x"}).status_code == 404


def test_editing_requires_auth():
    noauth = TestClient(main.app)
    assert noauth.put("/api/members/m1", json={"name": "x"}).status_code == 401
    assert noauth.put("/api/events/ev1", json={"title": "x"}).status_code == 401
    assert noauth.post("/api/members/new", json={"name": "x"}).status_code == 401
