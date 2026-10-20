"""LIFF での LINE 連携の申請・承認と予約投稿（docs/line-link-design.md）のテスト。"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import line_link, main
from app import schedule_poll as sp
from app.deps import set_repo
from app.models import LineGroup, Member, PollAnswer, PollResponse, ScheduledPost, SchedulePoll
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

admin = TestClient(main.app, headers=ADMIN_AUTH)
public = TestClient(main.app)
USERS = {"tok-sed": ("U_sed", "たかゆき"), "tok-a": ("U_a", "宮澤重嗣"), "tok-b": ("U_b", "別人"),
         "tok-x": ("U_x", "部外者")}
GROUP_MEMBERS = {"U_sed", "U_a", "U_b"}


@pytest.fixture
def repo(monkeypatch):
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="m2", name="遠藤 孝行", officer_role="専務理事",
                           line_user_id="U_sed"))
    r.upsert_member(Member(member_id="m7", name="宮澤 重嗣", officer_role="監事"))
    r.upsert_member(Member(member_id="m14", name="村尾 碧"))
    r.save_line_group(LineGroup(group_id="Cgroup", group_name="【JCI猪苗代】"))
    set_repo(r)
    monkeypatch.setenv("LIFF_ID", "2000000000-abcdEFGH")
    monkeypatch.setenv("LINE_LOGIN_CHANNEL_ID", "2000000000")
    sp._token_cache.clear()
    sp._member_cache.clear()
    monkeypatch.setattr(sp, "_verify_id_token_raw",
                        lambda t, c: {"sub": USERS[t][0], "name": USERS[t][1]})
    monkeypatch.setattr(sp, "_is_group_member_raw",
                        lambda g, u: g == "Cgroup" and u in GROUP_MEMBERS)
    yield r
    set_repo(None)


@pytest.fixture
def pushed(monkeypatch):
    sent = []
    monkeypatch.setattr(line_link.line_push, "push_messages",
                        lambda to, msgs: sent.append((to, msgs)) or True)
    monkeypatch.setattr(line_link.line_push, "push_text",
                        lambda to, text: sent.append((to, text)) or True)
    return sent


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_view_lists_unlinked_members_for_group_members_only(repo):
    assert public.get("/liff/api/link", headers=_auth("tok-x")).status_code == 403
    view = public.get("/liff/api/link", headers=_auth("tok-a")).json()
    assert view["status"] == "none"
    assert [m["name"] for m in view["members"]] == ["宮澤 重嗣", "村尾 碧"]  # 連携済みは出ない


def test_request_notifies_sed_and_approve_by_postback(repo, pushed):
    res = public.post("/liff/api/link", headers=_auth("tok-a"), json={"member_id": "m7"})
    assert res.json()["status"] == "pending"
    (to, msgs), = pushed
    assert to == "U_sed"  # 専務に承認ボタン付きで届く
    buttons = msgs[0].to_dict()["contents"]["footer"]["contents"]
    assert buttons[0]["action"]["data"] == "link|approve|U_a"
    # 専務がボタンを押す
    reply = main.handle_postback("U_sed", "link|approve|U_a")
    assert "宮澤 重嗣 さんの LINE 連携を承認しました" in reply[0].text
    assert repo.get_member("m7").line_user_id == "U_a"
    assert pushed[-1] == ("U_a", pushed[-1][1]) and "連携が完了しました" in pushed[-1][1]
    assert public.get("/liff/api/link", headers=_auth("tok-a")).json()["status"] == "linked"


def test_only_sed_can_approve(repo, pushed):
    public.post("/liff/api/link", headers=_auth("tok-a"), json={"member_id": "m7"})
    repo.upsert_member(Member(member_id="m14", name="村尾 碧", line_user_id="U_b"))
    reply = main.handle_postback("U_b", "link|approve|U_a")
    assert "専務理事のみ" in reply[0].text
    assert repo.get_member("m7").line_user_id is None


def test_same_member_cannot_be_requested_twice(repo, pushed):
    public.post("/liff/api/link", headers=_auth("tok-a"), json={"member_id": "m7"})
    res = public.post("/liff/api/link", headers=_auth("tok-b"), json={"member_id": "m7"})
    assert res.status_code == 409
    view = public.get("/liff/api/link", headers=_auth("tok-b")).json()
    assert {m["name"]: m["taken"] for m in view["members"]}["宮澤 重嗣"] is True
    # 連携済みの会員は選べない
    assert public.post("/liff/api/link", headers=_auth("tok-b"),
                       json={"member_id": "m2"}).status_code == 400


def test_reject(repo, pushed):
    public.post("/liff/api/link", headers=_auth("tok-b"), json={"member_id": "m7"})
    assert admin.post("/api/line-link/U_b/reject").status_code == 200
    assert repo.get_member("m7").line_user_id is None
    assert public.get("/liff/api/link", headers=_auth("tok-b")).json()["status"] == "rejected"


def test_candidates_from_poll_answers_can_be_approved(repo, pushed):
    repo.save_poll(SchedulePoll(poll_id="p1", title="第2回五役予定者会議", event_type="五役会",
                                group_id="Cgroup"))
    repo.save_poll_response(PollResponse(poll_id="p1", line_user_id="U_a", display_name="宮澤重嗣",
                                         member_id="m7", answers={"c1": PollAnswer.ok}))
    data = admin.get("/api/line-link").json()
    assert data["candidates"][0]["member_name"] == "宮澤 重嗣"
    res = admin.post("/api/line-link/U_a/approve", json={"member_id": "m7"})
    assert res.status_code == 200
    assert repo.get_member("m7").line_user_id == "U_a"
    assert admin.get("/api/line-link").json()["candidates"] == []


def test_approve_refuses_double_link(repo, pushed):
    assert admin.post("/api/line-link/U_sed/approve",
                      json={"member_id": "m7"}).status_code == 409  # 専務の LINE は連携済み


def test_scheduled_link_invite_is_sent_by_tick(repo, pushed):
    res = admin.post("/api/scheduled-posts", json={
        "group_id": "Cgroup", "kind": "link_invite", "send_at": "2026-10-21T08:00:00"})
    assert res.status_code == 200
    post_id = res.json()["post_id"]
    assert line_link.send_due_posts(repo, datetime(2026, 10, 21, 7, 59)) is None  # まだ送らない
    result = line_link.send_due_posts(repo, datetime(2026, 10, 21, 8, 0, 5))
    assert result == [{"post_id": post_id, "result": "sent"}]
    (to, msgs), = pushed
    assert to == "Cgroup"
    uri = msgs[0].to_dict()["contents"]["footer"]["contents"][0]["action"]["uri"]
    assert uri == "https://liff.line.me/2000000000-abcdEFGH?link=1"
    assert line_link.send_due_posts(repo, datetime(2026, 10, 21, 9, 0)) is None  # 二度送らない


def test_scheduled_post_held_while_kill_switch(repo, pushed):
    settings = repo.get_settings()
    settings.kill_switch = True
    repo.save_settings(settings)
    repo.save_scheduled_post(ScheduledPost(post_id="p", group_id="Cgroup", kind="link_invite",
                                           send_at=datetime(2026, 10, 21, 8, 0)))
    assert line_link.send_due_posts(repo, datetime(2026, 10, 21, 8, 1)) == [
        {"post_id": "p", "result": "held"}]
    assert pushed == []


def test_cancel_scheduled_post(repo, pushed):
    res = admin.post("/api/scheduled-posts", json={
        "group_id": "Cgroup", "kind": "link_invite", "send_at": "2026-10-21T08:00:00"})
    post_id = res.json()["post_id"]
    assert admin.post(f"/api/scheduled-posts/{post_id}/cancel").status_code == 200
    assert line_link.send_due_posts(repo, datetime(2026, 10, 21, 8, 1)) is None


def test_liff_link_page(repo):
    res = public.get("/liff/poll?link=1")
    assert res.status_code == 200 and "linkFlow" in res.text
