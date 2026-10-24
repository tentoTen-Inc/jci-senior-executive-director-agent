"""LIFF「委員会の議案」「出欠の状況」「日程調整を作る」「承認待ち」（docs/liff-menu-design.md §3）。

役割に応じたアクセス制御がサーバー側で行われること、
書き込みが［送信］系の API だけで起きることを確かめる。
"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import liff_api, line_push, main
from app import schedule_poll as sp
from app.deps import set_repo
from app.models import (
    Attendance,
    AttendanceStatus,
    CommitteeSeat,
    Event,
    EventStatus,
    EventType,
    LineGroup,
    LinkRequest,
    LinkRequestStatus,
    Member,
    MemberType,
    Proposal,
    ProposalFeedback,
    ProposalStage,
)
from app.repository import InMemoryRepository

client = TestClient(main.app)
NOON = datetime(2026, 10, 26, 12, 0)
FUTURE = datetime.now() + timedelta(days=10)
USERS = {"tok-reg": "U_reg", "tok-chair": "U_chair", "tok-sed": "U_sed", "tok-off": "U_off",
         "tok-vp": "U_vp"}


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def pushed(monkeypatch):
    sent = []

    def push(to, messages):
        sent.append((to, messages))
        return True
    monkeypatch.setattr(line_push, "push_messages", push)
    return sent


@pytest.fixture
def repo(monkeypatch, pushed):
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="m_reg", name="一般 会員", line_user_id="U_reg",
                           committees=[CommitteeSeat(committee="総務委員会", role="委員")]))
    r.upsert_member(Member(member_id="m_chair", name="委員長 太郎", line_user_id="U_chair",
                           committees=[CommitteeSeat(committee="総務委員会", role="委員長")]))
    r.upsert_member(Member(member_id="m_sed", name="専務 三郎", line_user_id="U_sed",
                           officer_role="専務理事"))
    r.upsert_member(Member(member_id="m_vp", name="副理事長 四郎", line_user_id="U_vp",
                           officer_role="副理事長"))
    r.upsert_member(Member(member_id="m_off", name="事務局 花子", line_user_id="U_off",
                           member_type=MemberType.office))
    r.upsert_member(Member(member_id="m_new", name="未連携 五郎"))
    r.upsert_event(Event(event_id="ev_nov", title="11月例会", type=EventType.例会,
                         datetime_start=FUTURE, status=EventStatus.open))
    r.upsert_attendance(Attendance(event_id="ev_nov", member_id="m_chair",
                                   status=AttendanceStatus.出席))
    for pid, committee, stage in [("pr1", "総務委員会", ProposalStage.sed_review),
                                  ("pr2", "地域未来創造委員会", ProposalStage.entry)]:
        r.upsert_proposal(Proposal(
            proposal_id=pid, title=f"{committee}の3月例会", committee=committee, stage=stage,
            doc_id=f"doc_{pid}", storage_uri=f"https://docs.google.com/document/d/doc_{pid}/edit",
            feedback=[ProposalFeedback(meeting="五役会議①", kind="意見", no=1,
                                       text="予算の根拠を")]))
    for gid, name in [("G_goyaku", "2027年度　五役"), ("G_riji", "2027 猪苗代青年会議所 理事会")]:
        r.save_line_group(LineGroup(group_id=gid, group_name=name))
    r.save_link_request(LinkRequest(line_user_id="U_new", display_name="ごろう",
                                    member_id="m_new", requested_at=NOON))
    set_repo(r)
    monkeypatch.setenv("LIFF_ID", "2000000000-abcdEFGH")
    monkeypatch.setenv("LINE_LOGIN_CHANNEL_ID", "2000000000")
    monkeypatch.setenv("COMMITTEE_DOCS_FOLDER", "folder")
    sp._token_cache.clear()

    def verify(token, client_id):
        if token not in USERS:
            raise sp.PollError(401, "LINE ログインの確認に失敗しました。")
        return {"sub": USERS[token], "name": "LINE名"}
    monkeypatch.setattr(sp, "_verify_id_token_raw", verify)
    yield r
    set_repo(None)


# --------------------------------------------------------------------------- #
# 役割ごとのアクセス制御
# --------------------------------------------------------------------------- #
OPS_ENDPOINTS = [
    ("GET", "/liff/api/ops/attendance"),
    ("POST", "/liff/api/ops/events/ev_nov/remind"),
    ("GET", "/liff/api/ops/poll-form"),
    ("POST", "/liff/api/ops/polls"),
    ("POST", "/liff/api/ops/polls/p1/announce"),
    ("GET", "/liff/api/ops/approvals"),
    ("POST", "/liff/api/ops/link-requests/U_new/approve"),
]


@pytest.mark.parametrize("method, path", OPS_ENDPOINTS)
@pytest.mark.parametrize("token", ["tok-reg", "tok-chair"])
def test_ops_screens_refuse_non_ops(repo, pushed, method, path, token):
    res = client.request(method, path, headers=_auth(token), json={})
    assert res.status_code == 403
    assert "五役・監事・事務局のみ" in res.json()["detail"]
    assert pushed == []
    assert repo.get_link_request("U_new").status == LinkRequestStatus.pending


def test_proposals_refuse_regular_members(repo):
    res = client.get("/liff/api/proposals", headers=_auth("tok-reg"))
    assert res.status_code == 403 and "理事メンバー" in res.json()["detail"]


# --------------------------------------------------------------------------- #
# 委員会の議案
# --------------------------------------------------------------------------- #
def test_proposals_by_role(repo):
    chair = client.get("/liff/api/proposals", headers=_auth("tok-chair")).json()
    assert [p["proposal_id"] for p in chair["proposals"]] == ["pr1"]  # 自分の委員会だけ
    pr1 = chair["proposals"][0]
    assert pr1["stage"] == "専務確認" and pr1["can_work"] is True
    assert pr1["feedback"][0]["text"] == "予算の根拠を"
    sed = client.get("/liff/api/proposals", headers=_auth("tok-sed")).json()
    assert {p["proposal_id"] for p in sed["proposals"]} == {"pr1", "pr2"}
    office = client.get("/liff/api/proposals", headers=_auth("tok-off")).json()
    assert {p["proposal_id"] for p in office["proposals"]} == {"pr1", "pr2"}
    assert not any(p["can_work"] for p in office["proposals"])  # 事務局は資料を更新しない


def test_work_starts_session_on_server_and_bot_pushes(repo, pushed):
    res = client.post("/liff/api/proposals/pr1/work", headers=_auth("tok-chair"))
    assert res.status_code == 200
    [(to, messages)] = pushed
    assert to == "U_chair" and messages[0].text.startswith("📝 編集中：総務委員会の3月例会")
    state = repo.get_chat_state("m_chair")
    assert state.mode == "doc" and repo.get_doc_session(state.session_id).doc_id == "doc_pr1"


def test_work_refused_for_other_committee(repo, pushed):
    res = client.post("/liff/api/proposals/pr2/work", headers=_auth("tok-chair"))
    assert res.status_code == 404  # 見られない委員会の議案は無いものとして扱う
    assert pushed == [] and repo.get_chat_state("m_chair") is None


def test_work_refused_when_docs_disabled(repo, pushed, monkeypatch):
    monkeypatch.delenv("COMMITTEE_DOCS_FOLDER")
    res = client.post("/liff/api/proposals/pr1/work", headers=_auth("tok-chair"))
    assert res.status_code == 503 and pushed == []


# --------------------------------------------------------------------------- #
# 出欠の状況・催促
# --------------------------------------------------------------------------- #
def test_attendance_status_lists_unanswered(repo):
    data = client.get("/liff/api/ops/attendance", headers=_auth("tok-off")).json()
    [ev] = data["events"]
    assert ev["counts"]["出席"] == 1 and "一般 会員" in ev["unanswered"]
    assert "委員長 太郎" not in ev["unanswered"] and ev["reminded"] is False


def test_remind_sends_attendance_request_to_unanswered_once(repo, pushed):
    sed = repo.get_member("m_sed")
    result = liff_api.remind_unanswered(repo, sed, "ev_nov", NOON)
    targets = {to for to, _ in pushed}
    assert "U_reg" in targets and "U_chair" not in targets
    assert result["sent"] == len(pushed)
    assert pushed[0][1][0].text.startswith("【出欠確認】11月例会")
    with pytest.raises(liff_api.LiffError) as exc:  # 押し間違いの二重送信を防ぐ
        liff_api.remind_unanswered(repo, sed, "ev_nov", NOON + timedelta(minutes=5))
    assert exc.value.status == 409


def test_remind_refused_at_night(repo, pushed):
    with pytest.raises(liff_api.LiffError) as exc:
        liff_api.remind_unanswered(repo, repo.get_member("m_sed"), "ev_nov",
                                   NOON.replace(hour=23))
    assert exc.value.status == 409 and pushed == []


# --------------------------------------------------------------------------- #
# 日程調整を作る
# --------------------------------------------------------------------------- #
def _poll_body(**kw):
    start = (FUTURE + timedelta(days=5)).strftime("%Y-%m-%dT19:00")
    return {"title": "第3回五役会", "target": "goyaku", "event_type": "五役会",
            "candidates": [{"start": start, "end": None}], **kw}


def test_poll_form_shows_destination_groups(repo):
    form = client.get("/liff/api/ops/poll-form", headers=_auth("tok-vp")).json()
    groups = {t["value"]: t["group_name"] for t in form["targets"]}
    assert groups == {"goyaku": "2027年度　五役", "riji": "2027 猪苗代青年会議所 理事会",
                      "committee": None, "all": None}
    assert [c["name"] for c in form["committees"]] == ["総務委員会"]


def test_create_poll_does_not_announce_until_confirmed(repo, pushed):
    res = client.post("/liff/api/ops/polls", headers=_auth("tok-vp"), json=_poll_body())
    assert res.status_code == 200
    created = res.json()
    poll = repo.get_poll(created["poll_id"])
    assert poll.group_id == "G_goyaku" and poll.created_by == "liff:m_vp"
    assert created["group_name"] == "2027年度　五役" and created["targets"] == 2
    assert pushed == []  # 作っただけでは送らない
    res = client.post(f"/liff/api/ops/polls/{poll.poll_id}/announce", headers=_auth("tok-vp"))
    assert res.status_code == 200 and [to for to, _ in pushed] == ["G_goyaku"]
    again = client.post(f"/liff/api/ops/polls/{poll.poll_id}/announce", headers=_auth("tok-vp"))
    assert again.status_code == 409 and len(pushed) == 1


def test_create_poll_riji_targets_board_members(repo):
    created = client.post("/liff/api/ops/polls", headers=_auth("tok-off"),
                          json=_poll_body(target="riji", event_type="理事会")).json()
    poll = repo.get_poll(created["poll_id"])
    assert set(poll.target_scope.value) == {"m_chair", "m_sed", "m_vp"}


@pytest.mark.parametrize("body, status", [
    (_poll_body(target="all"), 409),  # 案内先のグループが無い
    (_poll_body(target="nobody"), 400),
    (_poll_body(title=" "), 400),
    (_poll_body(candidates=[]), 400),
])
def test_create_poll_validation(repo, body, status):
    res = client.post("/liff/api/ops/polls", headers=_auth("tok-off"), json=body)
    assert res.status_code == status
    assert repo.list_polls() == []


# --------------------------------------------------------------------------- #
# 承認待ち
# --------------------------------------------------------------------------- #
def test_approvals_list(repo):
    data = client.get("/liff/api/ops/approvals", headers=_auth("tok-off")).json()
    assert data["requests"][0]["member_name"] == "未連携 五郎"
    assert data["review"][0]["proposal_id"] == "pr1"
    assert data["can_decide"] is False
    assert client.get("/liff/api/ops/approvals",
                      headers=_auth("tok-sed")).json()["can_decide"] is True


def test_only_sed_can_approve_link(repo):
    res = client.post("/liff/api/ops/link-requests/U_new/approve", headers=_auth("tok-off"))
    assert res.status_code == 403 and repo.get_member("m_new").line_user_id is None
    res = client.post("/liff/api/ops/link-requests/U_new/approve", headers=_auth("tok-sed"))
    assert res.status_code == 200 and "承認しました" in res.json()["message"]
    assert repo.get_member("m_new").line_user_id == "U_new"
    assert res.json()["requests"] == []


def test_sed_can_reject_link(repo):
    res = client.post("/liff/api/ops/link-requests/U_new/reject", headers=_auth("tok-sed"))
    assert res.status_code == 200
    assert repo.get_link_request("U_new").status == LinkRequestStatus.rejected
