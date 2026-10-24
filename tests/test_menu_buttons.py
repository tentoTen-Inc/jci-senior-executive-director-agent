"""リッチメニューのボタン（docs/liff-menu-design.md §2・§4）。

LIFF の画面ができるまでの、トークでの代わりの一覧・役割の確認・旧メニューの互換。
"""
from datetime import datetime, timedelta

import pytest

from app.member_menu import handle_member_text
from app.models import (
    Attendance,
    AttendanceStatus,
    CommitteeSeat,
    Event,
    EventStatus,
    EventType,
    LinkRequest,
    Member,
    MemberType,
    PollCandidate,
    PollResponse,
    Proposal,
    ProposalStage,
    SchedulePoll,
    TargetScope,
    TargetScopeKind,
)
from tests.test_chat_read_only import GuardRepo

NOW = datetime(2026, 10, 25, 20, 0)
REG = Member(member_id="m_reg", name="一般 会員", line_user_id="U_reg",
             committees=[CommitteeSeat(committee="総務委員会", role="委員")])
CHAIR = Member(member_id="m_chair", name="委員長 太郎", line_user_id="U_chair",
               committees=[CommitteeSeat(committee="総務委員会", role="委員長")])
SED = Member(member_id="m_sed", name="専務 三郎", line_user_id="U_sed", officer_role="専務理事")
OFFICE = Member(member_id="m_off", name="事務局 花子", line_user_id="U_off",
                member_type=MemberType.office)


@pytest.fixture
def repo():
    r = GuardRepo()
    r.writes = []
    for m in (REG, CHAIR, SED, OFFICE):
        r.upsert_member(m)
    r.upsert_event(Event(event_id="ev_nov", title="11月例会", type=EventType.例会,
                         datetime_start=NOW + timedelta(days=20), status=EventStatus.open))
    r.upsert_attendance(Attendance(event_id="ev_nov", member_id="m_chair",
                                   status=AttendanceStatus.出席))
    r.save_poll(SchedulePoll(
        poll_id="p1", title="第2回五役会", event_type=EventType.五役会, group_id="G",
        candidates=[PollCandidate(candidate_id="c1", start=NOW + timedelta(days=10))],
        target_scope=TargetScope(kind=TargetScopeKind.all)))
    r.save_poll_response(PollResponse(poll_id="p1", line_user_id="U_chair", member_id="m_chair"))
    for pid, committee, stage in [("pr1", "総務委員会", ProposalStage.sed_review),
                                  ("pr2", "地域未来創造委員会", ProposalStage.entry)]:
        r.upsert_proposal(Proposal(proposal_id=pid, title=f"{committee}の3月例会",
                                   committee=committee, stage=stage,
                                   storage_uri=f"https://docs.google.com/document/d/{pid}/edit"))
    r.save_link_request(LinkRequest(line_user_id="U_new", display_name="しんにゅう",
                                    member_id="m_reg", requested_at=NOW))
    r.guarding = True
    return r


def _tap(repo, member, label):
    return handle_member_text(repo, member, f"menu|{label}", now=NOW)


def test_events_button_shows_schedule_and_attendance(repo):
    msgs = _tap(repo, REG, "予定と出欠")
    assert "11月例会" in msgs[0].text
    assert [i.action.label for i in msgs[1].quick_reply.items] == ["出席", "Web出席", "欠席"]


def test_polls_button_lists_pending_polls(repo):
    assert "第2回五役会（未回答）" in _tap(repo, REG, "日程調整")[0].text
    assert "第2回五役会（回答済み）" in _tap(repo, CHAIR, "日程調整")[0].text


def test_ask_button_shows_examples(repo):
    [msg] = _tap(repo, REG, "AIに聞く")
    assert "書き換えることはありません" in msg.text
    assert msg.quick_reply.items[0].action.type == "message"  # 押すと質問として送られる


def test_proposals_by_role(repo):
    chair = _tap(repo, CHAIR, "委員会の議案")[0].text
    assert "総務委員会｜" in chair and "地域未来創造委員会" not in chair  # 自分の委員会だけ
    assert "（専務確認）" in chair and "docs.google.com/document/d/pr1" in chair
    sed = _tap(repo, SED, "委員会の議案")[0].text
    assert "総務委員会｜" in sed and "地域未来創造委員会｜" in sed  # 五役は全委員会
    assert "理事メンバー" in _tap(repo, REG, "委員会の議案")[0].text


@pytest.mark.parametrize("label", ["出欠の状況", "日程調整を作る", "承認待ち"])
def test_ops_buttons_are_for_ops_only(repo, label):
    assert "五役・監事・事務局のみ" in _tap(repo, CHAIR, label)[0].text
    assert "五役・監事・事務局のみ" not in _tap(repo, OFFICE, label)[0].text


def test_attendance_status_lists_unanswered(repo):
    text = _tap(repo, SED, "出欠の状況")[0].text
    assert "11月例会" in text and "出席 1" in text and "未回答: " in text
    assert "一般 会員" in text and "委員長 太郎" not in text.split("未回答: ")[1]


def test_approvals_lists_requests_and_review(repo):
    text = _tap(repo, SED, "承認待ち")[0].text
    assert "一般 会員 ← しんにゅう" in text
    assert "総務委員会｜総務委員会の3月例会" in text


@pytest.mark.parametrize("label", ["予定と出欠", "日程調整", "AIに聞く", "委員会の議案",
                                   "出欠の状況", "日程調整を作る", "承認待ち", "管理画面を開く",
                                   "次回の予定", "自分の出欠状況", "出欠を回答", "使い方"])
def test_view_buttons_never_write(repo, label):
    for member in (REG, CHAIR, SED, OFFICE):
        assert _tap(repo, member, label), label
    assert repo.writes == []


def test_legacy_buttons_still_work(repo):
    assert "11月例会" in _tap(repo, REG, "次回の予定")[0].text
    assert "あなたの出欠状況" in _tap(repo, REG, "自分の出欠状況")[0].text
    assert "専務AIの使い方" in _tap(repo, REG, "使い方")[0].text


def test_tab_switch_postback_gets_no_reply(monkeypatch):
    """五役のタブ切り替え（richmenuswitch の data=tab:…）には返事をしない。"""
    from app import main

    for data in ("tab:main", "tab:ops"):
        assert main.handle_postback("U_anyone", data) == []
