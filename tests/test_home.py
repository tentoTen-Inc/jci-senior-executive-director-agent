"""ホーム集約のテスト。"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import main
from app.deps import set_repo
from app.home import build_home
from app.models import (
    DeliveryLog,
    DeliveryResult,
    Escalation,
    Event,
    EventStatus,
    EventType,
    Member,
    TargetScope,
    TargetScopeKind,
)
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
NOW = datetime(2026, 6, 20, 10, 0)
# test_home_endpoint は /api/home 経由（内部で実時刻 datetime.now() を使う）で
# アサートするため、イベント日時をハードコードすると実行時刻の進行で過去になり
# 「未来イベント」フィルタから外れて壊れる（time-bomb）。常に実行時刻より
# 先の日付になるよう動的に決める。
EVENT_START = datetime.now() + timedelta(days=5)


def build_repo() -> InMemoryRepository:
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="m1", name="A", line_user_id="U1"))
    r.upsert_member(Member(member_id="m2", name="B"))  # 未連携
    r.upsert_event(Event(
        event_id="e1", type=EventType.例会, title="6月例会",
        datetime_start=EVENT_START,
        target_scope=TargetScope(kind=TargetScopeKind.all),
        status=EventStatus.open,
    ))
    r.save_escalation(Escalation(escalation_id="es1", member_id="m1", created_at=NOW))
    r.save_delivery_log(DeliveryLog(log_id="l1", job_id="j", member_id="m1", content="x",
                                    result=DeliveryResult.ok, sent_at=NOW))
    r.save_delivery_log(DeliveryLog(log_id="l2", job_id="j", member_id="m2", content="x",
                                    result=DeliveryResult.failed, sent_at=NOW))
    return r


def test_build_home_aggregates():
    repo = build_repo()
    h = build_home(repo, now=NOW)
    assert h.member_count == 2
    assert h.action_required.unlinked_members == 1
    assert h.action_required.open_escalations == 1
    # m1(連携) のみ配信対象 → 未回答1
    assert h.action_required.unanswered_total == 1
    assert h.delivery_success_rate == 0.5
    assert len(h.upcoming_events) == 1


@pytest.fixture
def _wire():
    repo = build_repo()
    set_repo(repo)
    yield
    set_repo(None)


def test_home_endpoint(_wire):
    res = client.get("/api/home")
    assert res.status_code == 200
    body = res.json()
    assert body["member_count"] == 2
    assert body["upcoming_events"][0]["title"] == "6月例会"
