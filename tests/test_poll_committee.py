"""委員会あての日程調整（docs/schedule-poll-design.md §9）。

対象者（所属・兼務・担当副理事長）、送り先（委員会名を含むグループ／無ければ個別）、確認画面の送り先。
"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import main
from app import schedule_poll as sp
from app.deps import set_repo
from app.models import (
    CommitteeSeat,
    EventType,
    LineGroup,
    Member,
    MemberStatus,
    SchedulePoll,
    TargetScope,
    TargetScopeKind,
)
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

admin = TestClient(main.app, headers=ADMIN_AUTH)
public = TestClient(main.app)
FUTURE = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%dT19:00:00")
USERS = {"tok-sed": "U_sed", "tok-chair": "U_chair", "tok-out": "U_out"}
GENERAL = "総務委員会"
COMMUNITY = "地域未来創造委員会"


@pytest.fixture
def repo(monkeypatch):
    r = InMemoryRepository()
    members = [
        Member(member_id="m_chair", name="委員長 太郎", line_user_id="U_chair",
               committees=[CommitteeSeat(committee=GENERAL, role="委員長")]),
        Member(member_id="m_mem", name="委員 花子",  # 未連携
               committees=[CommitteeSeat(committee=GENERAL, role="委員")]),
        Member(member_id="m_dual", name="兼務 次郎", line_user_id="U_dual",
               committees=[CommitteeSeat(committee=COMMUNITY, role="委員"),
                           CommitteeSeat(committee=GENERAL, role="委員")]),
        Member(member_id="m_vp", name="副理事長 三郎", line_user_id="U_vp",
               officer_role="副理事長", supervised_committees=[GENERAL]),
        Member(member_id="m_sed", name="専務 四郎", line_user_id="U_sed", officer_role="専務理事"),
        Member(member_id="m_out", name="他委員会 五郎", line_user_id="U_out",
               committees=[CommitteeSeat(committee=COMMUNITY, role="委員")]),
        Member(member_id="m_old", name="退会 六郎", status=MemberStatus.inactive,
               committees=[CommitteeSeat(committee=GENERAL, role="委員")]),
    ]
    for m in members:
        r.upsert_member(m)
    r.save_line_group(LineGroup(group_id="G_comm", group_name=f"2027 {COMMUNITY}"))
    r.save_line_group(LineGroup(group_id="G_left", group_name=f"{GENERAL}（旧）",
                                joined_at=datetime(2026, 1, 1), left_at=datetime(2026, 5, 1)))
    set_repo(r)
    monkeypatch.setenv("LIFF_ID", "2000000000-abcdEFGH")
    monkeypatch.setenv("LINE_LOGIN_CHANNEL_ID", "2000000000")
    sp._token_cache.clear()
    sp._member_cache.clear()
    monkeypatch.setattr(sp, "_verify_id_token_raw", lambda t, c: {"sub": USERS[t]})
    monkeypatch.setattr(sp, "_is_group_member_raw",
                        lambda g, u: pytest.fail("個別のときはグループを確かめない"))
    yield r
    set_repo(None)


@pytest.fixture
def pushed(monkeypatch):
    sent = []
    monkeypatch.setattr(sp.line_push, "push_messages",
                        lambda to, msgs: sent.append((to, msgs)) or True)
    return sent


def _poll(committee=GENERAL, group_id=None) -> SchedulePoll:
    return SchedulePoll(poll_id="p", title="第1回総務委員会", event_type=EventType.委員会,
                        target_scope=TargetScope(kind=TargetScopeKind.committee,
                                                 value=[committee]), group_id=group_id)


def _create(**overrides) -> dict:
    body = {"title": "第1回総務委員会", "event_type": "委員会", "audience": f"{GENERAL}の皆さま",
            "candidates": [{"start": FUTURE}],
            "target_scope": {"kind": "committee", "value": [GENERAL]}, "group_id": None} | overrides
    res = admin.post("/api/polls", json=body)
    assert res.status_code == 200, res.text
    return res.json()


# --------------------------------------------------------------------------- #
# 対象者・送り先
# --------------------------------------------------------------------------- #
def test_targets_are_members_dual_seats_and_supervising_vp(repo):
    names = {m.member_id for m in sp.target_members(repo, _poll())}
    assert names == {"m_chair", "m_mem", "m_dual", "m_vp"}  # 退会・他委員会・専務は入らない


def test_committee_names_come_from_org_chart(repo):
    assert sp.committee_names(repo) == sorted([GENERAL, COMMUNITY])


def test_committee_group_is_found_by_name_and_left_groups_are_ignored(repo):
    assert sp.committee_group(repo, COMMUNITY).group_id == "G_comm"
    assert sp.committee_group(repo, GENERAL) is None  # 抜けたグループは使わない


def test_destination(repo):
    assert sp.destination(repo, _poll()) == {"mode": "individual", "group_name": None,
                                             "targets": 4, "linked": 3}
    group = sp.destination(repo, _poll(COMMUNITY, "G_comm"))
    assert group["mode"] == "group" and group["group_name"] == f"2027 {COMMUNITY}"


def test_admin_committee_list(repo):
    rows = {r["committee"]: r for r in admin.get("/api/polls/committees").json()}
    assert rows[GENERAL]["mode"] == "individual" and rows[GENERAL]["group_id"] is None
    assert rows[GENERAL]["linked"] == 3 and rows[GENERAL]["targets"] == 4
    assert rows[COMMUNITY]["group_id"] == "G_comm" and rows[COMMUNITY]["mode"] == "group"


# --------------------------------------------------------------------------- #
# 作成の検証
# --------------------------------------------------------------------------- #
def test_create_committee_without_group_sends_individually(repo):
    data = _create()
    assert data["poll"]["group_id"] is None
    assert data["destination"] == {"mode": "individual", "group_name": None, "targets": 4,
                                   "linked": 3}
    assert {r["member_id"] for r in data["rows"]} == {"m_chair", "m_mem", "m_dual", "m_vp"}


@pytest.mark.parametrize("overrides, detail", [
    ({"target_scope": {"kind": "committee", "value": ["ない委員会"]}}, "委員会を選んでください"),
    ({"target_scope": {"kind": "committee", "value": []}}, "委員会を選んでください"),
    ({"target_scope": {"kind": "all", "value": []}}, "グループを選んでください"),
])
def test_create_validation(repo, overrides, detail):
    body = {"title": "x", "event_type": "委員会", "candidates": [{"start": FUTURE}],
            "group_id": None} | overrides
    res = admin.post("/api/polls", json=body)
    assert res.status_code == 400 and detail in res.json()["detail"]
    assert repo.list_polls() == []


# --------------------------------------------------------------------------- #
# 案内・催促・決定（個別）
# --------------------------------------------------------------------------- #
def test_announce_individually_to_linked_targets(repo, pushed):
    poll_id = _create()["poll"]["poll_id"]
    assert admin.post(f"/api/polls/{poll_id}/announce").status_code == 200
    assert {to for to, _ in pushed} == {"U_chair", "U_dual", "U_vp"}  # 未連携・対象外には送らない


def test_announce_to_committee_group(repo, pushed):
    poll_id = _create(target_scope={"kind": "committee", "value": [COMMUNITY]},
                      group_id="G_comm")["poll"]["poll_id"]
    admin.post(f"/api/polls/{poll_id}/announce")
    assert [to for to, _ in pushed] == ["G_comm"]


def test_remind_individually_only_pending_without_others_names(repo, pushed):
    poll_id = _create()["poll"]["poll_id"]
    cid = repo.get_poll(poll_id).candidates[0].candidate_id
    res = public.put(f"/liff/api/polls/{poll_id}/response",
                     headers={"Authorization": "Bearer tok-chair"}, json={"answers": {cid: "ok"}})
    assert res.status_code == 200
    assert admin.post(f"/api/polls/{poll_id}/remind").status_code == 200
    assert {to for to, _ in pushed} == {"U_dual", "U_vp"}
    text = pushed[0][1][0].text
    assert "まだ回答をいただいていません" in text and "委員 花子" not in text


def test_decide_notifies_individually(repo, pushed, monkeypatch):
    monkeypatch.setattr(sp, "push_event", lambda repo, event: event)
    poll_id = _create()["poll"]["poll_id"]
    cid = repo.get_poll(poll_id).candidates[0].candidate_id
    res = admin.post(f"/api/polls/{poll_id}/decide", json={"candidate_id": cid})
    assert res.status_code == 200
    assert {to for to, _ in pushed} == {"U_chair", "U_dual", "U_vp"}
    assert pushed[0][1][0].text.startswith("【日程決定】")


def test_announce_refused_when_nobody_is_linked(repo, pushed):
    for m in repo.list_members():
        repo.upsert_member(m.model_copy(update={"line_user_id": None}))
    poll_id = _create()["poll"]["poll_id"]
    res = admin.post(f"/api/polls/{poll_id}/announce")
    assert res.status_code == 409 and pushed == []


# --------------------------------------------------------------------------- #
# 閲覧権（グループが無いとき）
# --------------------------------------------------------------------------- #
def test_viewer_is_target_only_when_no_group(repo):
    poll_id = _create()["poll"]["poll_id"]
    ok = public.get(f"/liff/api/polls/{poll_id}", headers={"Authorization": "Bearer tok-chair"})
    assert ok.status_code == 200
    ng = public.get(f"/liff/api/polls/{poll_id}", headers={"Authorization": "Bearer tok-out"})
    assert ng.status_code == 403


# --------------------------------------------------------------------------- #
# LIFF「日程調整を作る」
# --------------------------------------------------------------------------- #
def test_liff_form_and_create_committee(repo, pushed):
    sed = {"Authorization": "Bearer tok-sed"}
    form = public.get("/liff/api/ops/poll-form", headers=sed).json()
    assert "committee" in [t["value"] for t in form["targets"]]
    rows = {c["name"]: c for c in form["committees"]}
    assert rows[GENERAL]["mode"] == "individual" and rows[GENERAL]["linked"] == 3
    assert rows[COMMUNITY]["group_name"] == f"2027 {COMMUNITY}"
    body = {"title": "第1回総務委員会", "target": "committee", "committee": GENERAL,
            "event_type": "委員会", "candidates": [{"start": FUTURE, "end": None}]}
    created = public.post("/liff/api/ops/polls", headers=sed, json=body).json()
    assert (created["mode"], created["linked"], created["targets"]) == ("individual", 3, 4)
    assert repo.get_poll(created["poll_id"]).audience == f"{GENERAL}の皆さま"
    assert pushed == []  # 作っただけでは送らない
    res = public.post(f"/liff/api/ops/polls/{created['poll_id']}/announce", headers=sed)
    assert res.status_code == 200 and res.json()["mode"] == "individual"
    assert {to for to, _ in pushed} == {"U_chair", "U_dual", "U_vp"}


def test_liff_create_committee_requires_known_committee(repo):
    body = {"title": "x", "target": "committee", "committee": "ない委員会",
            "event_type": "委員会", "candidates": [{"start": FUTURE}]}
    res = public.post("/liff/api/ops/polls", headers={"Authorization": "Bearer tok-sed"},
                      json=body)
    assert res.status_code == 400 and repo.list_polls() == []
