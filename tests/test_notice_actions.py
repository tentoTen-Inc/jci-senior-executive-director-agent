"""対外連絡アクションの追跡・催促（F5-3/F5-5 / P3-4）のテスト。"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import line_push, main
from app.deps import set_repo
from app.home import build_home
from app.models import (
    ExternalNotice,
    Member,
    NoticeDigest,
    QuietHours,
    Settings,
)
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
IAP = {"X-Goog-Authenticated-User-Email": "sed@10to10.co.jp"}
NO_QUIET = QuietHours(start="00:00", end="00:00")  # 実行時刻に依存させない


@pytest.fixture(autouse=True)
def repo():
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="m1", name="太郎", line_user_id="U1"))
    r.upsert_member(Member(member_id="m2", name="次郎", line_user_id="U2"))
    r.save_settings(Settings(quiet_hours=NO_QUIET))
    r.upsert_notice(ExternalNotice(
        notice_id="n1",
        received_at=datetime(2026, 7, 24),
        subject="2026ブロック会員大会",
        body_text="原文",
        status="delivered",
        digest=NoticeDigest(
            summary="要約",
            announcement="ご確認ください",
            deadline=datetime(2026, 8, 5),
            actions=["参加者登録", "会費の納入"],
        ),
    ))
    set_repo(r)
    yield r
    set_repo(None)


@pytest.fixture(autouse=True)
def pushed(monkeypatch):
    sent: list[tuple[str, list]] = []
    monkeypatch.setattr(
        line_push, "push_messages",
        lambda user_id, messages: bool(sent.append((user_id, messages))) or True,
    )
    return sent


def create_actions(**over):
    payload = {}
    payload.update(over)
    return client.post("/api/notices/n1/actions", json=payload, headers=IAP)


def test_create_actions_from_digest(repo):
    res = create_actions()
    assert res.status_code == 200
    actions = res.json()
    assert [a["title"] for a in actions] == ["参加者登録", "会費の納入"]
    # 期限は digest.deadline を引き継ぐ、対象は配信可能な会員
    assert actions[0]["due"].startswith("2026-08-05")
    assert actions[0]["assignees"] == ["m1", "m2"]
    assert actions[0]["status"] == "open"
    assert repo.get_notice("n1").history[-1].action == "actions_created"
    assert any(a.action == "notice.actions.create" for a in repo.list_audit())


def test_create_actions_is_idempotent(repo):
    create_actions()
    create_actions()  # 同じ title は作り直さない
    assert len(repo.list_notice_actions(notice_id="n1")) == 2


def test_create_actions_with_explicit_titles_and_due():
    res = create_actions(titles=["出向者の推薦"], due="2026-09-01T00:00:00")
    titles = [a["title"] for a in res.json()]
    assert titles == ["出向者の推薦"]
    assert res.json()[0]["due"].startswith("2026-09-01")


def test_create_actions_requires_titles(repo):
    repo.upsert_notice(ExternalNotice(
        notice_id="n2", received_at=datetime(2026, 7, 1), subject="s", body_text="b",
    ))
    assert client.post("/api/notices/n2/actions", json={}).status_code == 400


def test_done_closes_when_all_members_done(repo):
    aid = create_actions().json()[0]["action_id"]

    res = client.post(f"/api/notices/n1/actions/{aid}/done", json={"member_id": "m1"}, headers=IAP)
    assert res.status_code == 200
    assert res.json()["done_by"] == ["m1"]
    assert res.json()["status"] == "open"  # m2 が未対応

    res = client.post(f"/api/notices/n1/actions/{aid}/done", json={"member_id": "m2"})
    assert res.json()["status"] == "closed"
    assert any(a.action == "notice.actions.done" for a in repo.list_audit())


def test_done_rejects_non_assignee(repo):
    aid = create_actions().json()[0]["action_id"]
    # 起票後に入会した会員は対象に含まれない
    repo.upsert_member(Member(member_id="m9", name="九郎", line_user_id="U9"))
    res = client.post(f"/api/notices/n1/actions/{aid}/done", json={"member_id": "m9"})
    assert res.status_code == 400


def test_remind_only_pending_members(repo, pushed):
    aid = create_actions().json()[0]["action_id"]
    client.post(f"/api/notices/n1/actions/{aid}/done", json={"member_id": "m1"})

    res = client.post(f"/api/notices/n1/actions/{aid}/remind", headers=IAP)
    assert res.status_code == 200
    body = res.json()
    assert (body["pending"], body["sent"]) == (1, 1)
    assert [u for u, _ in pushed] == ["U2"]  # 対応済みの m1 には送らない
    text = pushed[0][1][0].text
    assert "参加者登録" in text and "8月5日" in text

    action = repo.get_notice_action(aid)
    assert action.reminder_count == 1
    assert action.reminded_at is not None
    assert any(a.action == "notice.actions.remind" for a in repo.list_audit())


def test_remind_conflicts_when_nobody_pending(repo, pushed):
    aid = create_actions().json()[0]["action_id"]
    for mid in ("m1", "m2"):
        client.post(f"/api/notices/n1/actions/{aid}/done", json={"member_id": mid})
    assert client.post(f"/api/notices/n1/actions/{aid}/remind").status_code == 409
    assert pushed == []


def test_remind_respects_kill_switch(repo, pushed):
    aid = create_actions().json()[0]["action_id"]
    repo.save_settings(Settings(kill_switch=True, quiet_hours=NO_QUIET))
    res = client.post(f"/api/notices/n1/actions/{aid}/remind")
    assert res.status_code == 200
    assert (res.json()["sent"], res.json()["blocked"]) == (0, 2)
    assert pushed == []


def test_line_postback_marks_done(repo):
    """会員が催促メッセージの「対応しました」を押すと完了が記録される。"""
    aid = create_actions().json()[0]["action_id"]
    messages = main.handle_postback("U1", f"ntca|{aid}|done")
    assert "参加者登録" in messages[0].text
    assert repo.get_notice_action(aid).done_by == ["m1"]


def test_line_postback_unknown_action(repo):
    messages = main.handle_postback("U1", "ntca|nope|done")
    assert "見つかりません" in messages[0].text


def test_home_counts_open_actions(repo):
    assert build_home(repo, now=datetime(2026, 7, 25)).action_required.open_notice_actions == 0
    aid = create_actions().json()[0]["action_id"]
    assert build_home(repo, now=datetime(2026, 7, 25)).action_required.open_notice_actions == 2

    # 全員完了したアクションは要対応から外れる
    for mid in ("m1", "m2"):
        client.post(f"/api/notices/n1/actions/{aid}/done", json={"member_id": mid})
    assert build_home(repo, now=datetime(2026, 7, 25)).action_required.open_notice_actions == 1


def test_actions_list_and_404():
    create_actions()
    assert len(client.get("/api/notices/n1/actions").json()) == 2
    assert len(client.get("/api/notices/n1/actions?status=closed").json()) == 0
    assert client.post("/api/notices/nope/actions", json={}).status_code == 404
    assert client.post("/api/notices/n1/actions/nope/remind").status_code == 404
    assert (
        client.post("/api/notices/n1/actions/nope/done", json={"member_id": "m1"}).status_code
        == 404
    )
