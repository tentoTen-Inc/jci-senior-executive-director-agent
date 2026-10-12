"""Googleカレンダーへの反映（F3-2 / docs/calendar-design.md §4.1）のテスト。"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import calendar_sync, gcal, main
from app.calendar_import import parse_title
from app.deps import set_repo
from app.models import (
    Event,
    EventStatus,
    EventType,
    GcalSyncState,
    Member,
    TargetScope,
    TargetScopeKind,
)
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
FUTURE = datetime.now().replace(microsecond=0) + timedelta(days=14)


class FakeCalendar:
    """gcal._request の代わりに Calendar API を模倣する。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.events: dict[str, dict] = {}
        self.fail_with: gcal.GcalError | None = None
        self._seq = 0

    def __call__(self, method: str, url: str, body: dict | None = None) -> dict:
        self.calls.append((method, url, body))
        if self.fail_with is not None:
            raise self.fail_with
        event_id = url.rsplit("/events", 1)[1].lstrip("/") or None
        if method == "POST":
            self._seq += 1
            new_id = f"g{self._seq}"
            self.events[new_id] = body
            return {"id": new_id, "etag": f'"etag-{new_id}-1"'}
        if method == "PATCH":
            if event_id not in self.events:
                raise gcal.GcalError("not found", 404)
            self.events[event_id] = body
            return {"id": event_id, "etag": f'"etag-{event_id}-2"'}
        if method == "DELETE":
            self.events.pop(event_id, None)
            return {}
        raise AssertionError(method)

    def methods(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def repo():
    r = InMemoryRepository()
    r.upsert_event(Event(
        event_id="ev1", type=EventType.理事会, title="第8回理事会",
        datetime_start=FUTURE, location="体験交流館",
        target_scope=TargetScope(kind=TargetScopeKind.all), status=EventStatus.open,
    ))
    set_repo(r)
    yield r
    set_repo(None)


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("GCAL_CALENDAR_ID", "inawashiro.jc@gmail.com")
    cal = FakeCalendar()
    monkeypatch.setattr(gcal, "_request", cal)
    return cal


# --------------------------------------------------------------------------- #
# 予定の中身
# --------------------------------------------------------------------------- #
def test_calendar_body_prefixes_type_and_defaults_end():
    event = Event(
        event_id="ev9", type=EventType.理事会, title="第8回理事会",
        datetime_start=datetime(2026, 10, 20, 19, 0), location="体験交流館",
        attendance_deadline=datetime(2026, 10, 15, 21, 0),
    )
    body = calendar_sync.to_calendar_body(event)
    assert body["summary"] == "【理事会】第8回理事会"
    assert body["start"] == {"dateTime": "2026-10-20T19:00:00+09:00", "timeZone": "Asia/Tokyo"}
    assert body["end"]["dateTime"] == "2026-10-20T21:00:00+09:00"  # 終了未設定は+2時間
    assert body["location"] == "体験交流館"
    assert "出欠締切: 10/15 21:00" in body["description"]
    assert body["extendedProperties"]["private"]["jci_event_id"] == "ev9"


def test_calendar_description_shows_member_names_for_custom_scope():
    repo = InMemoryRepository()
    repo.upsert_member(Member(member_id="m1", name="遠藤孝行"))
    event = Event(
        event_id="e", type=EventType.個別, title="遠藤孝 町役場 打合せ", datetime_start=FUTURE,
        target_scope=TargetScope(kind=TargetScopeKind.custom, value=["m1", "gone"]),
    )
    body = calendar_sync.to_calendar_body(event, repo)
    assert "対象: 遠藤孝行・gone" in body["description"]  # 見つからない会員はIDのまま

    committee = event.model_copy(update={"target_scope": TargetScope(
        kind=TargetScopeKind.committee, value=["総務委員会"])})
    assert "対象: 総務委員会" in calendar_sync.to_calendar_body(committee, repo)["description"]


def test_calendar_title_not_duplicated():
    event = Event(event_id="e", type=EventType.例会, title="例会", datetime_start=FUTURE)
    assert calendar_sync.calendar_title(event) == "例会"


@pytest.mark.parametrize("kind, title, start, fiscal_year, expected", [
    # 翌年度の予定だけ年度の下2桁を前置する（docs/calendar-design.md §4.4）
    (EventType.対外, "渡部邦/遠藤孝 第2回次年度会員会議所会議", datetime(2026, 11, 28, 13, 0),
     2027, "27【対外】渡部邦/遠藤孝 第2回次年度会員会議所会議"),
    (EventType.例会, "1月例会", datetime(2027, 1, 20, 19, 0), 2027, "【例会】1月例会"),
    (EventType.委員会, "組織向上 第1回委員会", datetime(2027, 1, 12, 19, 0), None,
     "【委員会】組織向上 第1回委員会"),
    (EventType.例会, "【例会】1月例会", datetime(2026, 12, 20, 19, 0), 2027, "27【例会】1月例会"),
    (EventType.五役会, "五役会", datetime(2026, 12, 1, 20, 0), 2027, "27【五役会】"),
    (EventType.個別, "遠藤孝 町役場 打合せ", datetime(2026, 11, 18, 10, 0), None,
     "【個別】遠藤孝 町役場 打合せ"),
])
def test_calendar_title_naming_rule(kind, title, start, fiscal_year, expected):
    event = Event(event_id="e", type=kind, title=title, datetime_start=start,
                  fiscal_year=fiscal_year)
    assert calendar_sync.calendar_title(event) == expected


def test_calendar_title_round_trips_through_import():
    event = Event(event_id="e", type=EventType.五役会, title="五役会",
                  datetime_start=datetime(2026, 12, 1, 20, 0), fiscal_year=2027)
    assert parse_title(calendar_sync.calendar_title(event)) == (EventType.五役会, "五役会", 2027)


# --------------------------------------------------------------------------- #
# push_event
# --------------------------------------------------------------------------- #
def test_push_disabled_when_not_configured(repo, monkeypatch):
    monkeypatch.delenv("GCAL_CALENDAR_ID", raising=False)
    called = []
    monkeypatch.setattr(gcal, "_request", lambda *a, **k: called.append(a))
    result = calendar_sync.push_event(repo, repo.get_event("ev1"))
    assert result.gcal_sync_state == GcalSyncState.disabled
    assert called == []


def test_push_inserts_then_patches(repo, fake):
    first = calendar_sync.push_event(repo, repo.get_event("ev1"))
    assert first.gcal_event_id == "g1"
    assert first.gcal_sync_state == GcalSyncState.synced
    assert first.gcal_synced_at is not None

    moved = first.model_copy(update={"location": "役場"})
    second = calendar_sync.push_event(repo, moved)
    assert fake.methods() == ["POST", "PATCH"]
    assert second.gcal_event_id == "g1"
    assert fake.events["g1"]["location"] == "役場"
    assert repo.get_event("ev1").gcal_etag == '"etag-g1-2"'


def test_push_recreates_when_deleted_on_calendar(repo, fake):
    linked = repo.get_event("ev1").model_copy(update={"gcal_event_id": "gone"})
    result = calendar_sync.push_event(repo, linked)
    assert fake.methods() == ["PATCH", "POST"]
    assert result.gcal_event_id == "g1"
    assert result.gcal_sync_state == GcalSyncState.synced


def test_push_cancelled_deletes_and_unlinks(repo, fake):
    synced = calendar_sync.push_event(repo, repo.get_event("ev1"))
    cancelled = synced.model_copy(update={"status": EventStatus.cancelled})
    result = calendar_sync.push_event(repo, cancelled)
    assert fake.methods() == ["POST", "DELETE"]
    assert result.gcal_event_id is None
    assert "g1" not in fake.events


def test_push_failure_keeps_event_and_marks_error(repo, fake):
    fake.fail_with = gcal.GcalError("Calendar API POST 403: forbidden", 403)
    result = calendar_sync.push_event(repo, repo.get_event("ev1"))
    assert result.gcal_sync_state == GcalSyncState.error
    assert "403" in result.gcal_error
    assert repo.get_event("ev1").gcal_sync_state == GcalSyncState.error


def test_retry_pending_skips_past_events(repo, fake):
    repo.upsert_event(Event(
        event_id="past", type=EventType.例会, title="昔の例会",
        datetime_start=datetime.now() - timedelta(days=30), status=EventStatus.closed,
    ))
    summary = calendar_sync.retry_pending(repo, datetime.now())
    assert summary == {"retried": 1, "synced": 1, "failed": 0}
    assert repo.get_event("past").gcal_event_id is None
    assert repo.get_event("ev1").gcal_event_id == "g1"


def test_retry_pending_noop_when_not_configured(repo, monkeypatch):
    monkeypatch.delenv("GCAL_CALENDAR_ID", raising=False)
    assert calendar_sync.retry_pending(repo, datetime.now()) is None


# --------------------------------------------------------------------------- #
# 管理API
# --------------------------------------------------------------------------- #
def _create_payload() -> dict:
    return {
        "type": "例会",
        "title": "10月例会",
        "datetime_start": FUTURE.isoformat(),
        "location": "会館",
    }


def test_create_event_pushes_to_calendar(repo, fake):
    res = client.post("/api/events", json=_create_payload())
    assert res.status_code == 200
    body = res.json()
    assert body["gcal_event_id"] == "g1"
    assert body["gcal_sync_state"] == "synced"
    assert body["updated_at"] is not None
    assert fake.events["g1"]["summary"] == "【例会】10月例会"


def test_update_only_calls_calendar_for_calendar_fields(repo, fake):
    calendar_sync.push_event(repo, repo.get_event("ev1"))
    fake.calls.clear()

    res = client.put("/api/events/ev1", json={"quorum": 10})
    assert res.status_code == 200
    assert fake.calls == []  # 定足数はカレンダーに無い

    res = client.put("/api/events/ev1", json={"title": "第8回 臨時理事会"})
    assert res.status_code == 200
    assert fake.methods() == ["PATCH"]
    assert fake.events["g1"]["summary"] == "【理事会】第8回 臨時理事会"


def test_cancel_and_restore(repo, fake):
    calendar_sync.push_event(repo, repo.get_event("ev1"))

    res = client.post("/api/events/ev1/cancel")
    assert res.status_code == 200
    assert res.json()["status"] == "cancelled"
    assert res.json()["gcal_event_id"] is None
    assert fake.events == {}

    res = client.post("/api/events/ev1/restore", json={"status": "draft"})
    assert res.status_code == 200
    assert res.json()["status"] == "draft"
    assert res.json()["gcal_event_id"] == "g2"

    assert client.post("/api/events/ev1/restore").status_code == 409


def test_cancelled_event_excluded_from_open_list(repo, fake):
    client.post("/api/events/ev1/cancel")
    assert repo.list_events(status=EventStatus.open) == []


def test_manual_resync_and_status(repo, fake):
    fake.fail_with = gcal.GcalError("boom", 500)
    client.post("/api/events/ev1/gcal-sync")
    status = client.get("/api/gcal/status").json()
    assert status["enabled"] is True
    assert status["unsynced"] == 1
    assert status["errors"][0]["event_id"] == "ev1"

    fake.fail_with = None
    res = client.post("/api/events/ev1/gcal-sync")
    assert res.json()["gcal_sync_state"] == "synced"
    assert client.get("/api/gcal/status").json()["unsynced"] == 0


def test_backfill_requires_configuration(repo, monkeypatch):
    monkeypatch.delenv("GCAL_CALENDAR_ID", raising=False)
    assert client.post("/api/gcal/backfill").status_code == 503


def test_backfill_pushes_unsynced(repo, fake):
    res = client.post("/api/gcal/backfill")
    assert res.status_code == 200
    assert res.json()["synced"] == 1
