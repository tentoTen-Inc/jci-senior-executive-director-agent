"""LIFF「予定と出欠」「日程調整（一覧）」（docs/liff-menu-design.md §3）。"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import main
from app import schedule_poll as sp
from app.deps import set_repo
from app.models import (
    Attendance,
    AttendanceStatus,
    CommitteeSeat,
    Event,
    EventStatus,
    EventType,
    Member,
    MemberStatus,
    PollCandidate,
    PollResponse,
    PollStatus,
    SchedulePoll,
    TargetScope,
    TargetScopeKind,
)
from app.repository import InMemoryRepository

client = TestClient(main.app)
FUTURE = datetime.now() + timedelta(days=10)
USERS = {"tok-reg": "U_reg", "tok-chair": "U_chair", "tok-stranger": "U_nobody",
         "tok-old": "U_old"}


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def repo(monkeypatch):
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="m_reg", name="一般 会員", line_user_id="U_reg",
                           committees=[CommitteeSeat(committee="総務委員会", role="委員")]))
    r.upsert_member(Member(member_id="m_chair", name="委員長 太郎", line_user_id="U_chair",
                           committees=[CommitteeSeat(committee="地域未来創造委員会",
                                                     role="委員長")]))
    r.upsert_member(Member(member_id="m_old", name="退会 次郎", line_user_id="U_old",
                           status=MemberStatus.inactive))
    r.upsert_event(Event(event_id="ev_all", title="11月例会", type=EventType.例会,
                         datetime_start=FUTURE, status=EventStatus.open))
    r.upsert_event(Event(event_id="ev_draft", title="下書きの予定", type=EventType.例会,
                         datetime_start=FUTURE, status=EventStatus.draft))
    r.upsert_event(Event(event_id="ev_past", title="終わった例会", type=EventType.例会,
                         datetime_start=datetime.now() - timedelta(days=3),
                         status=EventStatus.open))
    r.upsert_event(Event(event_id="ev_committee", title="総務委員会", type=EventType.委員会,
                         datetime_start=FUTURE, status=EventStatus.open,
                         target_scope=TargetScope(kind=TargetScopeKind.committee,
                                                  value=["総務委員会"])))
    r.save_poll(SchedulePoll(poll_id="p_open", title="第2回五役会", event_type=EventType.五役会,
                             group_id="G", candidates=[PollCandidate(candidate_id="c1",
                                                                     start=FUTURE)]))
    r.save_poll(SchedulePoll(poll_id="p_closed", title="締め切った調整",
                             event_type=EventType.五役会, group_id="G",
                             status=PollStatus.closed))
    r.save_poll_response(PollResponse(poll_id="p_open", line_user_id="U_chair",
                                      member_id="m_chair"))
    set_repo(r)
    monkeypatch.setenv("LIFF_ID", "2000000000-abcdEFGH")
    monkeypatch.setenv("LINE_LOGIN_CHANNEL_ID", "2000000000")
    sp._token_cache.clear()

    def verify(token, client_id):
        if token not in USERS:
            raise sp.PollError(401, "LINE ログインの確認に失敗しました。")
        return {"sub": USERS[token], "name": "LINE名"}
    monkeypatch.setattr(sp, "_verify_id_token_raw", verify)
    yield r
    set_repo(None)


def test_views_js_is_served():
    res = client.get("/liff/views.js")
    assert res.status_code == 200 and "LiffViews" in res.text


def test_events_lists_only_my_upcoming_open_events(repo):
    data = client.get("/liff/api/me/events", headers=_auth("tok-reg")).json()
    assert [e["event_id"] for e in data["events"]] == ["ev_all", "ev_committee"]
    assert data["events"][0]["status"] == "未回答"
    assert data["answers"] == ["出席", "Web出席", "欠席"]
    # 委員会の予定は委員会の人だけ
    chair = client.get("/liff/api/me/events", headers=_auth("tok-chair")).json()
    assert [e["event_id"] for e in chair["events"]] == ["ev_all"]


def test_answer_records_only_changes(repo):
    repo.upsert_attendance(Attendance(event_id="ev_committee", member_id="m_reg",
                                      status=AttendanceStatus.欠席))
    res = client.put("/liff/api/me/attendance", headers=_auth("tok-reg"),
                     json={"answers": {"ev_all": "出席", "ev_committee": "欠席"}})
    assert res.status_code == 200 and res.json()["changed"] == 1
    assert repo.get_attendance("ev_all", "m_reg").status == AttendanceStatus.出席
    assert res.json()["events"][0]["status"] == "出席"


@pytest.mark.parametrize("answers, status", [
    ({"ev_past": "出席"}, 403),  # 終わった予定
    ({"ev_draft": "出席"}, 403),  # 受付中でない
    ({"ev_committee": "出席"}, 403),  # 対象でない（委員長は総務委員会ではない）
    ({"ev_all": "委任"}, 400),  # LIFF では選べない
    ({"ev_all": "でたらめ"}, 400),
])
def test_answer_is_checked_on_server(repo, answers, status):
    res = client.put("/liff/api/me/attendance", headers=_auth("tok-chair"),
                     json={"answers": answers})
    assert res.status_code == status
    assert repo.get_attendance("ev_all", "m_chair") is None


@pytest.mark.parametrize("token, status", [("tok-stranger", 403), ("tok-old", 403),
                                           ("bad", 401)])
def test_unlinked_or_invalid_users_are_refused(repo, token, status):
    assert client.get("/liff/api/me/events", headers=_auth(token)).status_code == status
    assert client.get("/liff/api/me/polls", headers=_auth(token)).status_code == status


def test_refused_when_liff_not_configured(repo, monkeypatch):
    monkeypatch.delenv("LIFF_ID")
    assert client.get("/liff/api/me/events", headers=_auth("tok-reg")).status_code == 503


def test_polls_lists_open_polls_with_answered_flag(repo):
    reg = client.get("/liff/api/me/polls", headers=_auth("tok-reg")).json()["polls"]
    assert [(p["poll_id"], p["answered"]) for p in reg] == [("p_open", False)]
    chair = client.get("/liff/api/me/polls", headers=_auth("tok-chair")).json()["polls"]
    assert [(p["poll_id"], p["answered"]) for p in chair] == [("p_open", True)]


def test_liff_page_routes_views(repo):
    html = client.get("/liff/poll").text
    assert '<script src="/liff/views.js"></script>' in html
    assert 'param("view")' in html
