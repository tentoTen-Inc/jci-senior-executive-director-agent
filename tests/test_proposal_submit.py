"""委員会サマリーの［提出する］と、専務への提出・提出締切の知らせ。

設計: docs/proposal-ledger-design.md §10。
"""
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import liff_api, line_push, main, proposal_submit
from app import schedule_poll as sp
from app.deps import set_repo
from app.models import (
    CommitteeSeat,
    Event,
    EventType,
    Member,
    MemberType,
    PollCandidate,
    Proposal,
    ProposalDeadlines,
    ProposalStage,
    SchedulePoll,
)
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app)
MORNING = datetime(2026, 10, 30, 9, 0)
NIGHT = datetime(2026, 10, 30, 22, 30)
DEADLINE = datetime(2026, 10, 31, 23, 59)
USERS = {"tok-chair": "U_chair", "tok-vp": "U_vp", "tok-other": "U_other", "tok-off": "U_off",
         "tok-reg": "U_reg"}
SECTIONS = {"スローガン": "つなぐ", "現状分析": "", "目指すべき状態": "", "事業概要": "◯◯",
            "事業手法": "書いた", "例会/事業・1月": ""}


@pytest.fixture
def pushed(monkeypatch):
    sent = []
    monkeypatch.setattr(line_push, "push_messages",
                        lambda to, msgs: sent.append((to, msgs[0].text)) or True)
    return sent


@pytest.fixture
def repo(monkeypatch, pushed):
    r = InMemoryRepository()
    for m in [
        Member(member_id="m_chair", name="委員長 太郎", line_user_id="U_chair",
               committees=[CommitteeSeat(committee="総務委員会", role="委員長")]),
        Member(member_id="m_vp", name="副理事長 次郎", line_user_id="U_vp",
               officer_role="副理事長", supervised_committees=["総務委員会"]),
        Member(member_id="m_other", name="他委員長 三郎", line_user_id="U_other",
               committees=[CommitteeSeat(committee="地域未来創造委員会", role="委員長")]),
        Member(member_id="m_off", name="事務局 花子", line_user_id="U_off",
               member_type=MemberType.office),
        Member(member_id="m_reg", name="一般 会員", line_user_id="U_reg",
               committees=[CommitteeSeat(committee="総務委員会", role="委員")]),
        Member(member_id="m_sed", name="専務 四郎", line_user_id="U_sed", officer_role="専務理事"),
    ]:
        r.upsert_member(m)
    r.save_poll(SchedulePoll(poll_id="poll_08649098fc", title="第2回五役予定者会議",
                             event_type=EventType.五役会, group_id="G",
                             candidates=[PollCandidate(candidate_id="c1",
                                                       start=datetime(2026, 11, 6, 19))]))
    for pid, committee in [("ps1", "総務委員会"), ("ps2", "地域未来創造委員会")]:
        r.upsert_proposal(Proposal(
            proposal_id=pid, title=f"2027年度 {committee} 年間計画（サマリー）",
            committee=committee, doc_type="年間計画（サマリー）", doc_id=f"doc_{pid}",
            storage_uri=f"https://docs.google.com/document/d/doc_{pid}/edit",
            sections=SECTIONS, meeting_poll_id="poll_08649098fc",
            deadlines=ProposalDeadlines(submit=DEADLINE)))
    set_repo(r)
    monkeypatch.setenv("LIFF_ID", "2000000000-abcdEFGH")
    monkeypatch.setenv("LINE_LOGIN_CHANNEL_ID", "2000000000")
    sp._token_cache.clear()
    monkeypatch.setattr(sp, "_verify_id_token_raw", lambda t, c: {"sub": USERS[t]})
    yield r
    set_repo(None)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _row(token, pid="ps1"):
    rows = client.get("/liff/api/proposals", headers=_auth(token)).json()["proposals"]
    return next(r for r in rows if r["proposal_id"] == pid)


# --------------------------------------------------------------------------- #
# 上程先・空欄
# --------------------------------------------------------------------------- #
def test_meeting_label_follows_poll(repo):
    p = repo.get_proposal("ps1")
    assert proposal_submit.meeting_label(repo, p) == "第2回五役予定者会議（日程調整中）"
    repo.upsert_event(Event(event_id="ev_g", title="第2回五役会", type=EventType.五役会,
                            datetime_start=datetime(2026, 11, 6, 19)))
    poll = repo.get_poll("poll_08649098fc")
    repo.save_poll(poll.model_copy(update={"event_id": "ev_g"}))  # 日程が決まった
    assert proposal_submit.meeting_label(repo, p) == "第2回五役会（11月6日(金) 19:00）"
    assert proposal_submit.meeting_label(repo, p.model_copy(
        update={"meeting_poll_id": None})) is None


def test_liff_shows_meeting_blanks_and_buttons_by_role(repo):
    row = _row("tok-chair")
    assert row["meeting"] == "第2回五役予定者会議（日程調整中）"
    assert row["blanks"] == 4  # 空欄3＋記入例「◯◯」のまま1
    assert (row["can_submit"], row["can_withdraw"]) == (True, False)
    assert _row("tok-vp")["can_submit"] is True  # 担当副理事長
    assert _row("tok-off")["can_submit"] is False  # 事務局は見るだけ


# --------------------------------------------------------------------------- #
# ［提出する］・［提出を取り消す］
# --------------------------------------------------------------------------- #
def test_submit_notifies_sed_and_can_be_withdrawn(repo, pushed, monkeypatch):
    monkeypatch.setattr(liff_api, "datetime", _Clock(MORNING))  # 静音時間に当たらないように
    res = client.post("/liff/api/proposals/ps1/submit", headers=_auth("tok-chair"))
    assert res.status_code == 200, res.text
    assert "提出しました" in res.json()["message"]
    p = repo.get_proposal("ps1")
    assert p.stage == ProposalStage.submitted
    assert p.history[-1].by == "LINE: 委員長 太郎"
    [(to, text)] = pushed
    assert to == "U_sed"
    assert text.startswith(
        "【提出】総務委員会 年間計画（サマリー）が提出されました（委員長 太郎さん）")
    assert "空欄の欄: 4" in text and "docs.google.com/document/d/doc_ps1" in text
    row = next(r for r in res.json()["proposals"] if r["proposal_id"] == "ps1")
    assert (row["stage"], row["can_submit"], row["can_withdraw"]) == ("資料提出", False, True)
    # 二重押しは断る
    assert client.post("/liff/api/proposals/ps1/submit", headers=_auth("tok-chair")).status_code \
        == 409
    res = client.post("/liff/api/proposals/ps1/withdraw", headers=_auth("tok-vp"))
    assert res.status_code == 200 and repo.get_proposal("ps1").stage == ProposalStage.entry
    assert len(pushed) == 1  # 取り消しは知らせない


@pytest.mark.parametrize("token, status", [
    ("tok-other", 404),  # 他の委員会の議案は見えない
    ("tok-off", 403),  # 事務局は更新できない
    ("tok-reg", 403),  # 理事メンバーでない
])
def test_submit_refused(repo, pushed, token, status):
    res = client.post("/liff/api/proposals/ps1/submit", headers=_auth(token))
    assert res.status_code == status
    assert repo.get_proposal("ps1").stage == ProposalStage.entry and pushed == []


def test_submit_at_night_is_notified_in_the_morning_once(repo, pushed):
    chair = repo.get_member("m_chair")
    proposal_submit.submit(repo, chair, "ps1", NIGHT)
    assert pushed == []
    assert proposal_submit.process(repo, NIGHT + timedelta(hours=10))["submitted"] == 1
    assert len(pushed) == 1 and pushed[0][1].startswith("【提出】")
    assert proposal_submit.process(repo, NIGHT + timedelta(hours=11))["submitted"] == 0
    assert len(pushed) == 1


def test_withdrawn_before_morning_is_not_notified(repo, pushed):
    chair = repo.get_member("m_chair")
    proposal_submit.submit(repo, chair, "ps1", NIGHT)
    proposal_submit.withdraw(repo, chair, "ps1", NIGHT + timedelta(minutes=5))
    proposal_submit.process(repo, NIGHT + timedelta(hours=10))
    assert pushed == []


def test_admin_stage_change_is_not_notified(repo, pushed):
    res = client.post("/api/proposals/ps1/stage", json={"stage": "submitted"},
                      headers=ADMIN_AUTH)
    assert res.status_code == 200
    proposal_submit.process(repo, MORNING)
    assert pushed == []  # 管理画面で段階を変えたときは知らせない（LIFF の提出だけ）


# --------------------------------------------------------------------------- #
# 提出締切の記入状況
# --------------------------------------------------------------------------- #
def test_deadline_notice_once_per_deadline(repo, pushed):
    proposal_submit.submit(repo, repo.get_member("m_chair"), "ps1", MORNING)
    pushed.clear()
    assert proposal_submit.process(repo, DEADLINE - timedelta(minutes=1))["deadline"] == 0
    after = DEADLINE + timedelta(hours=8, minutes=10)  # 11/1 08:09
    assert proposal_submit.process(repo, after)["deadline"] == 2
    [(to, text)] = pushed
    assert to == "U_sed"
    assert "■第2回五役予定者会議（日程調整中）" in text
    assert "提出締切 10月31日(土) 23:59・提出済み 1/2" in text
    assert "・総務委員会 年間計画（サマリー）: 提出済み・空欄 4欄" in text
    assert "・地域未来創造委員会 年間計画（サマリー）: 未提出・空欄 4欄" in text
    assert "docs.google.com/document/d/doc_ps2" in text
    assert proposal_submit.process(repo, after + timedelta(hours=1))["deadline"] == 0
    assert len(pushed) == 1
    # 締切を延ばしたら、その締切でまた1回知らせる
    p = repo.get_proposal("ps2")
    repo.upsert_proposal(p.model_copy(update={"deadlines": ProposalDeadlines(
        submit=DEADLINE + timedelta(days=1))}))
    assert proposal_submit.process(repo, DEADLINE + timedelta(days=1, hours=9))["deadline"] == 1


def test_deadline_notice_waits_for_quiet_hours_and_skips_old_deadlines(repo, pushed):
    assert proposal_submit.process(repo, DEADLINE + timedelta(minutes=30))["deadline"] == 0
    assert pushed == []  # 0:29 は静音時間
    assert proposal_submit.process(repo, DEADLINE + timedelta(hours=8, minutes=1))[
        "deadline"] == 2
    for p in repo.list_proposals():
        repo.upsert_proposal(p.model_copy(update={"notices": {}, "deadlines": ProposalDeadlines(
            submit=DEADLINE - timedelta(days=10))}))
    assert proposal_submit.process(repo, MORNING + timedelta(days=2))["deadline"] == 0


# --------------------------------------------------------------------------- #
# 管理画面・設定用スクリプト
# --------------------------------------------------------------------------- #
def test_admin_list_shows_meeting_and_blanks(repo):
    rows = client.get("/api/proposals?status=open", headers=ADMIN_AUTH).json()
    row = next(r for r in rows if r["proposal_id"] == "ps1")
    assert row["meeting_label"] == "第2回五役予定者会議（日程調整中）" and row["blank_count"] == 4


def _script():
    path = Path(__file__).parent.parent / "scripts" / "set_summary_deadline.py"
    spec = importlib.util.spec_from_file_location("set_summary_deadline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_sets_deadline_and_meeting_only_with_apply(repo, capsys):
    script = _script()
    for p in repo.list_proposals():
        repo.upsert_proposal(p.model_copy(update={"deadlines": ProposalDeadlines(),
                                                  "meeting_poll_id": None}))
    repo.upsert_proposal(Proposal(proposal_id="plan", title="3月例会", committee="総務委員会"))
    assert script.main([], repo=repo) == 0
    assert repo.get_proposal("ps1").deadlines.submit is None  # 確認だけ
    assert "第2回五役予定者会議" in capsys.readouterr().out
    assert script.main(["--apply"], repo=repo) == 0
    for pid in ("ps1", "ps2"):
        p = repo.get_proposal(pid)
        assert (p.deadlines.submit, p.meeting_poll_id) == (DEADLINE, "poll_08649098fc")
    assert repo.get_proposal("plan").deadlines.submit is None  # 事業計画書は触らない


class _Clock:
    """datetime.now() を固定する（他の属性は datetime のまま）。"""

    def __init__(self, now):
        self._now = now

    def now(self):
        return self._now

    def __getattr__(self, name):
        return getattr(datetime, name)
