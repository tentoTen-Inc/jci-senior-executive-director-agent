"""連携済みメンバーの対話応答のユニットテスト。"""
from datetime import datetime

from app.member_menu import handle_member_text
from app.models import (
    AttendanceStatus,
    Event,
    EventStatus,
    EventType,
    Member,
    TargetScope,
    TargetScopeKind,
)
from app.repository import InMemoryRepository

NOW = datetime(2026, 6, 20, 10, 0)


def setup():
    repo = InMemoryRepository()
    member = Member(member_id="m1", name="太郎", line_user_id="U1")
    repo.upsert_member(member)
    repo.upsert_event(Event(
        event_id="e1", type=EventType.例会, title="6月例会",
        datetime_start=datetime(2026, 6, 25, 19, 0), location="会館",
        attendance_deadline=datetime(2026, 6, 25, 23, 59),
        target_scope=TargetScope(kind=TargetScopeKind.all),
        status=EventStatus.open,
    ))
    return repo, member


def test_schedule_query():
    repo, member = setup()
    msgs = handle_member_text(repo, member, "次回の予定", now=NOW)
    assert "6月例会" in msgs[0].text
    assert "会館" in msgs[0].text


def test_my_attendance_unanswered():
    repo, member = setup()
    msgs = handle_member_text(repo, member, "自分の出欠状況", now=NOW)
    assert "未回答" in msgs[0].text


def test_my_attendance_after_answer():
    repo, member = setup()
    from app.attendance import record_attendance
    record_attendance(repo, "e1", "m1", AttendanceStatus.出席, now=NOW)
    msgs = handle_member_text(repo, member, "自分の出欠状況", now=NOW)
    assert "出席" in msgs[0].text


def test_answer_prompt_returns_attendance_request():
    repo, member = setup()
    msgs = handle_member_text(repo, member, "出欠を回答", now=NOW)
    labels = [i.action.label for i in msgs[0].quick_reply.items]
    assert labels == ["出席", "Web出席", "欠席"]


def test_contact_records_escalation():
    repo, member = setup()
    msgs = handle_member_text(repo, member, "事務局に連絡", now=NOW)
    assert "受け付け" in msgs[0].text
    escalations = repo.list_escalations()
    assert len(escalations) == 1
    assert escalations[0].member_id == "m1"


def test_menu_postback_value():
    repo, member = setup()
    msgs = handle_member_text(repo, member, "menu|次回の予定", now=NOW)
    assert "6月例会" in msgs[0].text


def test_unknown_input_shows_menu():
    repo, member = setup()
    msgs = handle_member_text(repo, member, "ありがとう", now=NOW)
    assert msgs[0].quick_reply is not None


def test_menu_keyword():
    repo, member = setup()
    msgs = handle_member_text(repo, member, "メニュー", now=NOW)
    labels = [i.action.label for i in msgs[0].quick_reply.items]
    assert "次回の予定" in labels and "事務局に連絡" in labels


# --------------------------------------------------------------------------- #
# 本番で見つかった不具合の回帰テスト（2026-10-06）
# 「次回以降の予定」に終わった会議が出ていた・曜日が英語（Tue）だった
# --------------------------------------------------------------------------- #
AFTER = datetime(2026, 10, 6, 21, 35)  # 6月例会はとうに終わっている


def test_schedule_uses_japanese_weekday():
    repo, member = setup()
    text = handle_member_text(repo, member, "次回の予定", now=NOW)[0].text
    assert "6月25日(木) 19:00" in text  # 2026-06-25 は木曜
    assert "Thu" not in text


def test_past_events_are_not_shown_as_upcoming():
    repo, member = setup()
    text = handle_member_text(repo, member, "次回の予定", now=AFTER)[0].text
    assert text == "現在、予定されている例会・会議はありません。"


def test_no_attendance_prompt_for_past_event():
    repo, member = setup()
    msgs = handle_member_text(repo, member, "出欠を回答", now=AFTER)
    assert msgs[0].text == "現在、回答が必要な出欠はありません。"


def test_my_attendance_ignores_past_events():
    repo, member = setup()
    text = handle_member_text(repo, member, "自分の出欠状況", now=AFTER)[0].text
    assert text == "現在、あなたが対象の予定はありません。"


def test_ongoing_event_still_listed():
    """開始から終了（未設定なら2時間）までは「開催中」として出す。"""
    repo, member = setup()
    during = datetime(2026, 6, 25, 20, 0)
    assert "6月例会" in handle_member_text(repo, member, "次回の予定", now=during)[0].text
    after_end = datetime(2026, 6, 25, 21, 30)
    assert "6月例会" not in handle_member_text(repo, member, "次回の予定", now=after_end)[0].text


def test_upcoming_sorted_by_start():
    repo, member = setup()
    repo.upsert_event(Event(
        event_id="e0", type=EventType.理事会, title="6月理事会",
        datetime_start=datetime(2026, 6, 22, 19, 0),
        target_scope=TargetScope(kind=TargetScopeKind.all), status=EventStatus.open,
    ))
    text = handle_member_text(repo, member, "次回の予定", now=NOW)[0].text
    assert text.index("6月理事会") < text.index("6月例会")
