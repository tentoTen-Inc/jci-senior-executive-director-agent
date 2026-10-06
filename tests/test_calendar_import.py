"""Googleカレンダー → システムの差分取込（F3-2 / docs/calendar-design.md §4.2）のテスト。"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import gcal, main
from app.calendar_import import parse_title, pull_changes
from app.deps import set_repo
from app.models import (
    Event,
    EventOrigin,
    EventStatus,
    EventType,
    GcalSyncState,
)
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
NOW = datetime(2026, 10, 6, 12, 0)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")


def _item(gid: str, summary: str, start: datetime, *, etag: str = '"e1"',
          updated: datetime | None = None, location: str | None = None,
          jci_id: str | None = None, hours: int = 2) -> dict:
    item = {
        "id": gid,
        "etag": etag,
        "status": "confirmed",
        "summary": summary,
        "start": {"dateTime": _iso(start), "timeZone": "Asia/Tokyo"},
        "end": {"dateTime": _iso(start + timedelta(hours=hours)), "timeZone": "Asia/Tokyo"},
        "updated": (updated or NOW).strftime("%Y-%m-%dT%H:%M:%S.000+09:00"),
    }
    if location:
        item["location"] = location
    if jci_id:
        item["extendedProperties"] = {"private": {"jci_event_id": jci_id}}
    return item


class FakeList:
    """gcal.list_events の代わり。呼び出しごとに用意したページを返す。"""

    def __init__(self, pages: list[dict]) -> None:
        self.pages = list(pages)
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


@pytest.fixture
def repo(monkeypatch):
    monkeypatch.setenv("GCAL_CALENDAR_ID", "inawashiro.jc@gmail.com")
    r = InMemoryRepository()
    set_repo(r)
    yield r
    set_repo(None)


def _use(monkeypatch, *pages) -> FakeList:
    fake = FakeList(list(pages))
    monkeypatch.setattr(gcal, "list_events", fake)
    return fake


def _linked_event(**kw) -> Event:
    base = dict(
        event_id="ev1", type=EventType.理事会, title="第8回理事会",
        datetime_start=datetime(2026, 10, 20, 19, 0), location="体験交流館",
        status=EventStatus.open, gcal_event_id="g1", gcal_etag='"e1"',
        gcal_sync_state=GcalSyncState.synced, updated_at=NOW - timedelta(days=1),
    )
    base.update(kw)
    return Event(**base)


# --------------------------------------------------------------------------- #
# タイトル解釈
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("summary, kind, title", [
    ("【理事会】第8回理事会", EventType.理事会, "第8回理事会"),
    ("11月例会のご案内", EventType.例会, "11月例会のご案内"),
    ("総務委員会 打合せ", EventType.委員会, "総務委員会 打合せ"),
    ("BBQ", EventType.イベント, "BBQ"),
    ("【飲み会】納会", EventType.イベント, "【飲み会】納会"),
    ("", EventType.イベント, "（無題）"),
])
def test_parse_title(summary, kind, title):
    assert parse_title(summary) == (kind, title)


# --------------------------------------------------------------------------- #
# 新規取込
# --------------------------------------------------------------------------- #
def test_new_timed_event_imported_as_draft(repo, monkeypatch):
    _use(monkeypatch, {"items": [
        _item("g9", "【例会】11月例会", datetime(2026, 11, 18, 19, 0), location="会館"),
    ], "nextSyncToken": "tok1"})

    result = pull_changes(repo, NOW)

    assert result["created"] == 1
    [event] = repo.list_events()
    assert event.status == EventStatus.draft  # 勝手に配信・催促させない
    assert event.origin == EventOrigin.gcal
    assert event.type == EventType.例会
    assert event.title == "11月例会"
    assert event.datetime_start == datetime(2026, 11, 18, 19, 0)
    assert event.datetime_end == datetime(2026, 11, 18, 21, 0)
    assert event.location == "会館"
    assert event.gcal_event_id == "g9"
    assert event.gcal_sync_state == GcalSyncState.synced
    assert repo.get_gcal_sync_state().sync_token == "tok1"
    assert any(a.action == "event.import_from_gcal" for a in repo.list_audit())


def test_all_day_and_past_events_are_skipped(repo, monkeypatch):
    all_day = {
        "id": "gd", "etag": '"x"', "status": "confirmed", "summary": "祝日",
        "start": {"date": "2026-11-03"}, "end": {"date": "2026-11-04"},
    }
    past = _item("gp", "先月の例会", NOW - timedelta(days=20))
    _use(monkeypatch, {"items": [all_day, past], "nextSyncToken": "t"})

    result = pull_changes(repo, NOW)

    assert result["created"] == 0
    assert result["skipped"] == 2
    assert repo.list_events() == []


def test_duplicated_calendar_event_becomes_new_event(repo, monkeypatch):
    """予定の複製で jci_event_id ごとコピーされても、元のイベントには紐付けない。"""
    repo.upsert_event(_linked_event())
    copy = _item("g2", "【理事会】第8回理事会", datetime(2026, 10, 27, 19, 0), jci_id="ev1")
    _use(monkeypatch, {"items": [copy], "nextSyncToken": "t"})

    result = pull_changes(repo, NOW)

    assert result["created"] == 1
    assert repo.get_event("ev1").datetime_start == datetime(2026, 10, 20, 19, 0)


# --------------------------------------------------------------------------- #
# リンク済み予定の変更
# --------------------------------------------------------------------------- #
def test_own_echo_is_ignored(repo, monkeypatch):
    repo.upsert_event(_linked_event())
    _use(monkeypatch, {"items": [
        _item("g1", "【理事会】第8回理事会", datetime(2026, 10, 20, 19, 0), etag='"e1"',
              jci_id="ev1"),
    ], "nextSyncToken": "t"})

    result = pull_changes(repo, NOW)

    assert result["skipped"] == 1 and result["updated"] == 0


def test_calendar_edit_applied_when_newer(repo, monkeypatch):
    repo.upsert_event(_linked_event())
    _use(monkeypatch, {"items": [
        _item("g1", "【理事会】第8回理事会（会場変更）", datetime(2026, 10, 21, 19, 30),
              etag='"e2"', location="役場", jci_id="ev1", updated=NOW),
    ], "nextSyncToken": "t"})

    result = pull_changes(repo, NOW)

    assert result["updated"] == 1
    event = repo.get_event("ev1")
    assert event.title == "第8回理事会（会場変更）"
    assert event.datetime_start == datetime(2026, 10, 21, 19, 30)
    assert event.location == "役場"
    assert event.gcal_etag == '"e2"'
    assert event.status == EventStatus.open  # 状態や出欠はシステムが正
    assert any(a.action == "event.update_from_gcal" for a in repo.list_audit())


def test_system_newer_pushes_back(repo, monkeypatch):
    """両方で編集されたら後勝ち。システムが新しければカレンダーを上書きし直す。"""
    repo.upsert_event(_linked_event(updated_at=NOW, location="体験交流館"))
    _use(monkeypatch, {"items": [
        _item("g1", "【理事会】第8回理事会", datetime(2026, 10, 20, 19, 0), etag='"e2"',
              location="古い会場", jci_id="ev1", updated=NOW - timedelta(hours=3)),
    ], "nextSyncToken": "t"})
    calls = []

    def fake_request(method, url, body=None):
        calls.append(method)
        return {"id": "g1", "etag": '"e3"'}

    monkeypatch.setattr(gcal, "_request", fake_request)

    result = pull_changes(repo, NOW)

    assert result["pushed_back"] == 1
    assert calls == ["PATCH"]
    event = repo.get_event("ev1")
    assert event.location == "体験交流館"
    assert event.gcal_etag == '"e3"'


# --------------------------------------------------------------------------- #
# 削除
# --------------------------------------------------------------------------- #
def test_calendar_delete_cancels_event(repo, monkeypatch):
    repo.upsert_event(_linked_event())
    _use(monkeypatch, {"items": [{"id": "g1", "status": "cancelled"}], "nextSyncToken": "t"})

    result = pull_changes(repo, NOW)

    assert result["cancelled"] == 1
    event = repo.get_event("ev1")
    assert event.status == EventStatus.cancelled
    assert event.gcal_event_id is None
    assert any(a.action == "event.cancel_from_gcal" for a in repo.list_audit())


def test_delete_of_unknown_event_is_ignored(repo, monkeypatch):
    _use(monkeypatch, {"items": [{"id": "zzz", "status": "cancelled"}], "nextSyncToken": "t"})
    assert pull_changes(repo, NOW)["skipped"] == 1


# --------------------------------------------------------------------------- #
# 差分取得・トークン
# --------------------------------------------------------------------------- #
def test_uses_sync_token_and_pages(repo, monkeypatch):
    fake = _use(
        monkeypatch,
        {"items": [], "nextPageToken": "p2"},
        {"items": [], "nextSyncToken": "tok-a"},
        {"items": [], "nextSyncToken": "tok-b"},
    )
    pull_changes(repo, NOW)
    assert fake.calls[0]["sync_token"] is None
    assert fake.calls[0]["time_min"].startswith("2026-09-06T12:00:00")  # 30日遡る
    assert fake.calls[1]["page_token"] == "p2"
    assert repo.get_gcal_sync_state().sync_token == "tok-a"

    pull_changes(repo, NOW)
    assert fake.calls[2]["sync_token"] == "tok-a"
    assert repo.get_gcal_sync_state().sync_token == "tok-b"
    assert repo.get_gcal_sync_state().last_pulled_at == NOW


def test_expired_sync_token_falls_back_to_full(repo, monkeypatch):
    state = repo.get_gcal_sync_state()
    repo.save_gcal_sync_state(state.model_copy(update={"sync_token": "old"}))
    fake = _use(
        monkeypatch,
        gcal.GcalError("gone", 410),
        {"items": [], "nextSyncToken": "fresh"},
    )

    result = pull_changes(repo, NOW)

    assert result["full_resync"] is True
    assert fake.calls[1]["sync_token"] is None
    assert repo.get_gcal_sync_state().sync_token == "fresh"


def test_failure_records_error_and_keeps_token(repo, monkeypatch):
    state = repo.get_gcal_sync_state()
    repo.save_gcal_sync_state(state.model_copy(update={"sync_token": "keep"}))
    _use(monkeypatch, gcal.GcalError("Calendar API GET 403: forbidden", 403))

    assert pull_changes(repo, NOW) == {"error": "gcal_pull_failed"}
    saved = repo.get_gcal_sync_state()
    assert saved.sync_token == "keep"
    assert "403" in saved.last_error


def test_not_configured_returns_none(repo, monkeypatch):
    monkeypatch.delenv("GCAL_CALENDAR_ID")
    assert pull_changes(repo, NOW) is None


# --------------------------------------------------------------------------- #
# API・tick
# --------------------------------------------------------------------------- #
def test_manual_pull_and_status(repo, monkeypatch):
    _use(monkeypatch, {"items": [
        _item("g9", "11月例会", datetime.now() + timedelta(days=30)),
    ], "nextSyncToken": "t"})

    res = client.post("/api/gcal/pull")
    assert res.status_code == 200
    assert res.json()["created"] == 1

    status = client.get("/api/gcal/status").json()
    assert status["last_pulled_at"] is not None
    assert status["pull_error"] is None


def test_tick_runs_pull_then_retry(repo, monkeypatch):
    order = []
    monkeypatch.setattr(main, "pull_changes", lambda r, now: order.append("pull") or {"x": 1})
    monkeypatch.setattr(main, "retry_pending", lambda r, now: order.append("retry") or {"y": 2})
    monkeypatch.setattr(main.gmail, "is_configured", lambda: False)

    res = TestClient(main.app, headers=ADMIN_AUTH).post("/tasks/tick")

    assert res.status_code == 200
    assert order == ["pull", "retry"]
    assert res.json()["gcal"] == {"pull": {"x": 1}, "retry": {"y": 2}}
