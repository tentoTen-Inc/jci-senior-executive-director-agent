"""対外連絡の配信（F5-4 / P3-3）のテスト。LINE送信は push をモックする。"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import line_push, main
from app.deps import set_repo
from app.models import (
    ExternalNotice,
    Member,
    NoticeDigest,
    QuietHours,
    Settings,
)
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

# 配信APIは内部で datetime.now() を使うため、実行時刻に依存しないよう静音時間を空にする
# （start==end は `start <= t < end` が常に偽＝静音なし）。
NO_QUIET = QuietHours(start="00:00", end="00:00")

client = TestClient(main.app, headers=ADMIN_AUTH)
IAP = {"X-Goog-Authenticated-User-Email": "sed@10to10.co.jp"}
ANNOUNCE = "8月5日までに会員大会の参加可否をご回答ください。"


@pytest.fixture(autouse=True)
def repo():
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="m1", name="太郎", line_user_id="U1", committee="総務委員会"))
    r.upsert_member(
        Member(member_id="m2", name="次郎", line_user_id="U2", committee="コト創り委員会")
    )
    r.upsert_member(Member(member_id="m3", name="三郎"))  # LINE未連携 → 配信対象外
    r.save_settings(Settings(quiet_hours=NO_QUIET))
    set_repo(r)
    yield r
    set_repo(None)


@pytest.fixture(autouse=True)
def _no_real_push(monkeypatch):
    """LINE送信をモックし、(userId, 本文) を記録する。"""
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        line_push,
        "push_messages",
        lambda user_id, messages: bool(sent.append((user_id, messages[0].text))) or True,
    )
    return sent


def seed_notice(repo, *, with_digest=True, status="reviewed") -> str:
    repo.upsert_notice(ExternalNotice(
        notice_id="n1",
        received_at=datetime(2026, 7, 24),
        subject="2026ブロック会員大会の参加者登録について",
        body_text="原文",
        status=status,
        digest=NoticeDigest(summary="要約", announcement=ANNOUNCE) if with_digest else None,
    ))
    return "n1"


def test_deliver_to_all_uses_announcement(repo, _no_real_push):
    nid = seed_notice(repo)
    res = client.post(f"/api/notices/{nid}/deliver", json={}, headers=IAP)
    assert res.status_code == 200
    body = res.json()
    # LINE連携済みの2名のみ（m3は未連携で対象外）
    assert (body["targets"], body["sent"]) == (2, 2)
    assert {u for u, _ in _no_real_push} == {"U1", "U2"}
    assert {t for _, t in _no_real_push} == {ANNOUNCE}

    notice = repo.get_notice(nid)
    assert notice.status == "delivered"
    assert notice.delivery.target_count == 2
    assert notice.history[-1].action == "delivered"
    assert any(a.action == "notice.deliver" for a in repo.list_audit())
    # 配信ログが残る
    assert len(repo.list_delivery_logs(job_id=notice.delivery.job_id)) == 2


def test_deliver_to_committee_scope(repo, _no_real_push):
    nid = seed_notice(repo)
    res = client.post(
        f"/api/notices/{nid}/deliver",
        json={"target_scope": {"kind": "committee", "value": ["総務委員会"]}},
    )
    assert res.status_code == 200
    assert res.json()["sent"] == 1
    assert [u for u, _ in _no_real_push] == ["U1"]


def test_deliver_with_body_override(repo, _no_real_push):
    nid = seed_notice(repo)
    client.post(f"/api/notices/{nid}/deliver", json={"body_text": "専務が書き直した告知文"})
    assert [t for _, t in _no_real_push] == ["専務が書き直した告知文"] * 2


def test_deliver_requires_announcement(repo):
    nid = seed_notice(repo, with_digest=False)
    res = client.post(f"/api/notices/{nid}/deliver", json={})
    assert res.status_code == 400
    assert repo.get_notice(nid).status == "reviewed"


def test_deliver_twice_needs_force(repo, _no_real_push):
    nid = seed_notice(repo)
    assert client.post(f"/api/notices/{nid}/deliver", json={}).status_code == 200
    assert client.post(f"/api/notices/{nid}/deliver", json={}).status_code == 409
    assert client.post(f"/api/notices/{nid}/deliver", json={"force": True}).status_code == 200
    assert len(_no_real_push) == 4


def test_deliver_blocked_by_kill_switch(repo, _no_real_push):
    repo.save_settings(Settings(kill_switch=True, quiet_hours=NO_QUIET))
    nid = seed_notice(repo)
    res = client.post(f"/api/notices/{nid}/deliver", json={})
    assert res.status_code == 200
    body = res.json()
    assert (body["sent"], body["blocked"], body["deferred"]) == (0, 2, 0)
    assert _no_real_push == []  # 1通も送っていない
    assert {log.reason for log in repo.list_delivery_logs()} == {"kill_switch"}


def test_deliver_empty_scope_is_rejected(repo):
    nid = seed_notice(repo)
    res = client.post(
        f"/api/notices/{nid}/deliver",
        json={"target_scope": {"kind": "committee", "value": ["存在しない委員会"]}},
    )
    assert res.status_code == 400
    assert repo.get_notice(nid).status == "reviewed"


def test_deliver_unresolved_placeholder_is_halted(repo, _no_real_push):
    """テンプレ未展開のまま配信しようとしたらガードレールで中止（sanity_check）。"""
    nid = seed_notice(repo)
    res = client.post(f"/api/notices/{nid}/deliver", json={"body_text": "こんにちは {{name}} さん"})
    assert res.status_code == 409
    assert _no_real_push == []
    assert repo.get_notice(nid).status == "reviewed"
    assert any(a.action == "notice.deliver.halted" for a in repo.list_audit())


def test_deliver_404():
    assert client.post("/api/notices/nope/deliver", json={}).status_code == 404
