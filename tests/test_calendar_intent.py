"""LINE の自然文での予定登録・変更・中止（F3-2 / docs/calendar-design.md §5.2）のテスト。"""
import json
from datetime import datetime, timedelta

import pytest

from app import gcal, llm, main
from app.calendar_intent import (
    END_BEFORE_START_TEXT,
    PAST_TEXT,
    handle_calendar_postback,
    try_calendar_intent,
)
from app.deps import set_repo
from app.llm import Generation
from app.models import (
    Event,
    EventOrigin,
    EventStatus,
    EventType,
    GcalSyncState,
    Member,
    MemberType,
)
from app.repository import InMemoryRepository

NOW = datetime(2026, 10, 6, 12, 0)  # 火曜


class FakeCalendar:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.events: dict[str, dict] = {}
        self._seq = 0

    def __call__(self, method: str, url: str, body: dict | None = None) -> dict:
        self.calls.append(method)
        gid = url.rsplit("/events", 1)[1].lstrip("/")
        if method == "POST":
            self._seq += 1
            gid = f"g{self._seq}"
            self.events[gid] = body
            return {"id": gid, "etag": '"1"'}
        if method == "PATCH":
            self.events[gid] = body
            return {"id": gid, "etag": '"2"'}
        if method == "DELETE":
            self.events.pop(gid, None)
            return {}
        raise AssertionError(method)


@pytest.fixture
def repo(monkeypatch):
    monkeypatch.setenv("GCAL_CALENDAR_ID", "inawashiro.jc@gmail.com")
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="sed", name="専務 太郎", officer_role="専務理事",
                           line_user_id="U_sed"))
    r.upsert_member(Member(member_id="office", name="事務局 花子",
                           member_type=MemberType.office, line_user_id="U_office"))
    r.upsert_member(Member(member_id="m1", name="一般 次郎", line_user_id="U_m1"))
    r.upsert_event(Event(
        event_id="ev1", type=EventType.理事会, title="第8回理事会",
        datetime_start=datetime(2026, 10, 20, 19, 0), datetime_end=datetime(2026, 10, 20, 21, 0),
        location="体験交流館", status=EventStatus.open, gcal_event_id="g0",
        gcal_sync_state=GcalSyncState.synced,
    ))
    set_repo(r)
    yield r
    set_repo(None)


@pytest.fixture
def cal(monkeypatch):
    fake = FakeCalendar()
    fake.events["g0"] = {}
    monkeypatch.setattr(gcal, "_request", fake)
    return fake


def _llm(monkeypatch, **data):
    """Gemini の応答を固定する。呼ばれた回数を返すリストを返す。"""
    calls = []

    def fake(text, events, today, **kwargs):
        calls.append({"text": text, "events": events, "today": today})
        return Generation(text=json.dumps(data), input_tokens=10, output_tokens=5)

    monkeypatch.setattr(llm, "generate_calendar_intent", fake)
    return calls


def _member(repo, member_id="sed"):
    return repo.get_member(member_id)


def _postback_data(messages) -> dict[str, str]:
    items = messages[0].quick_reply.items
    return {i.action.label: i.action.data for i in items}


# --------------------------------------------------------------------------- #
# 対象者・起動条件
# --------------------------------------------------------------------------- #
def test_regular_member_cannot_edit(repo, monkeypatch):
    calls = _llm(monkeypatch, action="create")
    assert try_calendar_intent(repo, _member(repo, "m1"), "理事会を登録して", now=NOW) is None
    assert calls == []  # Gemini も呼ばない


def test_officer_without_write_words_is_ignored(repo, monkeypatch):
    calls = _llm(monkeypatch, action="create")
    assert try_calendar_intent(repo, _member(repo), "次回の予定は？", now=NOW) is None
    assert calls == []


def test_non_calendar_intent_falls_through(repo, monkeypatch):
    _llm(monkeypatch, action=None, confident=False)
    assert try_calendar_intent(repo, _member(repo), "例会は欠席で登録して", now=NOW) is None


def test_llm_failure_falls_through_and_logs(repo, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("vertex down")

    monkeypatch.setattr(llm, "generate_calendar_intent", boom)
    assert try_calendar_intent(repo, _member(repo), "理事会を登録", now=NOW) is None
    [log] = repo.list_inference_logs()
    assert log.kind == "calendar_intent" and log.ok is False


# --------------------------------------------------------------------------- #
# 登録
# --------------------------------------------------------------------------- #
def test_create_confirm_then_apply(repo, monkeypatch, cal):
    calls = _llm(monkeypatch, action="create", type="理事会", title="第9回理事会",
                 start="2026-11-17T19:00", end="2026-11-17T21:00", location="体験交流館",
                 confident=True)
    msgs = try_calendar_intent(
        repo, _member(repo, "office"), "11/17 19時から第9回理事会を登録 会場は体験交流館", now=NOW
    )
    assert "2026-10-06(火)" in calls[0]["today"]
    assert "id=ev1" in calls[0]["events"]  # 既存予定を候補として渡す
    text = msgs[0].text
    assert "次の予定を登録します" in text
    assert "【理事会】第9回理事会" in text
    assert "11月17日(火) 19:00〜21:00" in text
    assert repo.list_events(status=EventStatus.draft) == []  # 確認前は作らない

    data = _postback_data(msgs)
    reply = handle_calendar_postback(repo, _member(repo, "office"), data["はい"], now=NOW)

    [created] = repo.list_events(status=EventStatus.draft)
    assert created.origin == EventOrigin.line
    assert created.title == "第9回理事会"
    assert created.datetime_start == datetime(2026, 11, 17, 19, 0)
    assert created.gcal_sync_state == GcalSyncState.synced
    assert cal.calls == ["POST"]
    assert "下書きで登録しました" in reply[0].text
    assert "カレンダーにも反映" in reply[0].text
    assert any(a.actor == "line:office" and a.action == "event.create" for a in repo.list_audit())


def test_create_asks_back_when_ambiguous(repo, monkeypatch):
    _llm(monkeypatch, action="create", type="理事会", confident=False,
         question="開始日時を教えてください。")
    msgs = try_calendar_intent(repo, _member(repo), "理事会を登録して", now=NOW)
    assert msgs[0].text == "開始日時を教えてください。"


def test_create_in_the_past_is_rejected(repo, monkeypatch):
    _llm(monkeypatch, action="create", type="例会", start="2026-10-01T19:00", confident=True)
    msgs = try_calendar_intent(repo, _member(repo), "例会を登録", now=NOW)
    assert msgs[0].text == PAST_TEXT


def test_create_without_title_uses_type(repo, monkeypatch, cal):
    _llm(monkeypatch, action="create", type="五役会", start="2026-10-13T20:00", confident=True)
    msgs = try_calendar_intent(repo, _member(repo), "来週火曜20時に五役会を入れて", now=NOW)
    handle_calendar_postback(repo, _member(repo), _postback_data(msgs)["はい"], now=NOW)
    [created] = repo.list_events(status=EventStatus.draft)
    assert created.type == EventType.五役会 and created.title == "五役会"


# --------------------------------------------------------------------------- #
# 変更・中止
# --------------------------------------------------------------------------- #
def test_update_moves_start_and_keeps_duration(repo, monkeypatch, cal):
    _llm(monkeypatch, action="update", event_id="ev1", start="2026-10-21T19:30", confident=True)
    msgs = try_calendar_intent(repo, _member(repo), "第8回理事会を21日19時半に変更", now=NOW)
    assert "【変更前】" in msgs[0].text and "【変更後】" in msgs[0].text
    assert "10月21日(水) 19:30〜21:30" in msgs[0].text

    reply = handle_calendar_postback(repo, _member(repo), _postback_data(msgs)["はい"], now=NOW)
    event = repo.get_event("ev1")
    assert event.datetime_start == datetime(2026, 10, 21, 19, 30)
    assert event.datetime_end == datetime(2026, 10, 21, 21, 30)
    assert event.status == EventStatus.open  # 状態は変えない
    assert cal.calls == ["PATCH"]
    assert "変更しました" in reply[0].text


def test_update_rejects_end_before_start(repo, monkeypatch):
    _llm(monkeypatch, action="update", event_id="ev1", start="2026-10-20T19:00",
         end="2026-10-20T18:00", confident=True)
    msgs = try_calendar_intent(repo, _member(repo), "理事会の時間を変更", now=NOW)
    assert msgs[0].text == END_BEFORE_START_TEXT


def test_update_unknown_event(repo, monkeypatch):
    _llm(monkeypatch, action="update", event_id="nope", location="役場", confident=True)
    msgs = try_calendar_intent(repo, _member(repo), "総会の場所を役場に変更", now=NOW)
    assert "見つかりませんでした" in msgs[0].text


def test_cancel_deletes_from_calendar(repo, monkeypatch, cal):
    _llm(monkeypatch, action="cancel", event_id="ev1", confident=True)
    msgs = try_calendar_intent(repo, _member(repo), "第8回理事会を中止", now=NOW)
    assert "中止にします" in msgs[0].text

    reply = handle_calendar_postback(repo, _member(repo), _postback_data(msgs)["はい"], now=NOW)
    event = repo.get_event("ev1")
    assert event.status == EventStatus.cancelled
    assert cal.calls == ["DELETE"]
    assert "カレンダーからも削除しました" in reply[0].text


# --------------------------------------------------------------------------- #
# 確認の安全策
# --------------------------------------------------------------------------- #
def _pending_cancel(repo, monkeypatch):
    _llm(monkeypatch, action="cancel", event_id="ev1", confident=True)
    msgs = try_calendar_intent(repo, _member(repo), "第8回理事会を中止", now=NOW)
    return _postback_data(msgs)


def test_only_requester_can_confirm(repo, monkeypatch, cal):
    data = _pending_cancel(repo, monkeypatch)
    reply = handle_calendar_postback(repo, _member(repo, "office"), data["はい"], now=NOW)
    assert reply[0].text == "この確認は無効です。"
    assert repo.get_event("ev1").status == EventStatus.open


def test_confirmation_expires(repo, monkeypatch, cal):
    data = _pending_cancel(repo, monkeypatch)
    later = NOW + timedelta(minutes=31)
    reply = handle_calendar_postback(repo, _member(repo), data["はい"], now=later)
    assert "有効期限" in reply[0].text
    assert repo.get_event("ev1").status == EventStatus.open


def test_decline_and_double_press(repo, monkeypatch, cal):
    data = _pending_cancel(repo, monkeypatch)
    assert handle_calendar_postback(repo, _member(repo), data["いいえ"], now=NOW)[0].text \
        == "取りやめました。"
    again = handle_calendar_postback(repo, _member(repo), data["はい"], now=NOW)
    assert "処理済み" in again[0].text
    assert repo.get_event("ev1").status == EventStatus.open
    assert cal.calls == []


def test_yes_twice_applies_once(repo, monkeypatch, cal):
    _llm(monkeypatch, action="create", type="例会", title="11月例会",
         start="2026-11-18T19:00", confident=True)
    msgs = try_calendar_intent(repo, _member(repo), "11月例会を登録", now=NOW)
    yes = _postback_data(msgs)["はい"]
    handle_calendar_postback(repo, _member(repo), yes, now=NOW)
    second = handle_calendar_postback(repo, _member(repo), yes, now=NOW)
    assert "処理済み" in second[0].text
    assert len(repo.list_events(status=EventStatus.draft)) == 1


# --------------------------------------------------------------------------- #
# LINE の入口（テキスト→確認→postback）
# --------------------------------------------------------------------------- #
def test_webhook_flow_prefers_calendar_over_schedule_keyword(repo, monkeypatch, cal):
    """「予定」を含んでも、役員の書き込み文は次回予定の表示より先に予定操作として扱う。"""
    _llm(monkeypatch, action="create", type="委員会", title="総務委員会",
         start="2026-10-27T19:00", confident=True)
    msgs = main.handle_text_message("U_sed", "27日19時に総務委員会の予定を追加して")
    assert "次の予定を登録します" in msgs[0].text

    reply = main.handle_postback("U_sed", _postback_data(msgs)["はい"])
    assert "下書きで登録しました" in reply[0].text
    assert any(e.title == "総務委員会" for e in repo.list_events())
