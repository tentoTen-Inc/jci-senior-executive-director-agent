"""自然文からの出欠意図解釈（F4-7）のテスト。"""
import json
from datetime import datetime

import pytest

from app import llm
from app.attendance_intent import try_attendance_intent
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

NOW = datetime(2026, 7, 25, 10, 0)


def intent_json(event_id="e1", status="欠席", confident=True) -> str:
    return json.dumps(
        {"event_id": event_id, "status": status, "confident": confident}, ensure_ascii=False
    )


@pytest.fixture
def repo():
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="m1", name="太郎", line_user_id="U1"))
    r.upsert_event(Event(
        event_id="e1", type=EventType.例会, title="8月例会",
        datetime_start=datetime(2026, 8, 20, 19, 0),
        target_scope=TargetScope(kind=TargetScopeKind.all),
        status=EventStatus.open,
    ))
    r.upsert_event(Event(
        event_id="e2", type=EventType.理事会, title="8月理事会",
        datetime_start=datetime(2026, 8, 10, 19, 0),
        target_scope=TargetScope(kind=TargetScopeKind.officer, value=["理事長"]),
        status=EventStatus.open,
    ))
    return r


def member(repo) -> Member:
    return repo.get_member("m1")


def test_confirms_before_registering(monkeypatch, repo):
    """解釈できても即登録はしない。確認メッセージ（はい＝既存postback）を返す。"""
    captured = {}

    def fake(text, events, **kw):
        captured["text"] = text
        captured["events"] = events
        return llm.Generation(text=intent_json(), input_tokens=400, output_tokens=40)

    monkeypatch.setattr(llm, "generate_attendance_intent", fake)
    messages = try_attendance_intent(repo, member(repo), "来週の例会は欠席で", now=NOW)

    assert messages is not None
    msg = messages[0]
    assert "8月例会" in msg.text and "欠席" in msg.text and "よろしいですか" in msg.text
    labels = [i.action.label for i in msg.quick_reply.items]
    assert labels == ["はい", "いいえ"]
    # 「はい」は既存の出欠postback（押すと登録され欠席理由の質問に進む）
    assert msg.quick_reply.items[0].action.data == "att|e1|欠席"
    # 出欠は未登録のまま
    assert repo.get_attendance("e1", "m1") is None

    # 候補は本人が対象のイベントのみ（理事会=理事長のみは除外）
    assert "e1" in captured["events"] and "e2" not in captured["events"]

    logs = repo.list_inference_logs()
    assert len(logs) == 1
    assert (logs[0].kind, logs[0].ok) == ("attendance_intent", True)


def test_registers_on_confirmation(monkeypatch, repo):
    """確認の「はい」を押すと既存フローで登録される。"""
    monkeypatch.setattr(
        llm, "generate_attendance_intent", lambda *a, **kw: llm.Generation(text=intent_json())
    )
    messages = try_attendance_intent(repo, member(repo), "来週の例会は欠席で", now=NOW)
    data = messages[0].quick_reply.items[0].action.data

    from app.line_messages import apply_postback

    apply_postback(repo, member(repo), data, now=NOW)
    assert repo.get_attendance("e1", "m1").status == AttendanceStatus.欠席


def test_not_confident_falls_through(monkeypatch, repo):
    monkeypatch.setattr(
        llm, "generate_attendance_intent",
        lambda *a, **kw: llm.Generation(text=intent_json(confident=False)),
    )
    assert try_attendance_intent(repo, member(repo), "出欠どうしようかな", now=NOW) is None


def test_unknown_event_or_status_falls_through(monkeypatch, repo):
    monkeypatch.setattr(
        llm, "generate_attendance_intent",
        lambda *a, **kw: llm.Generation(text=intent_json(event_id="e99")),
    )
    assert try_attendance_intent(repo, member(repo), "欠席で", now=NOW) is None

    monkeypatch.setattr(
        llm, "generate_attendance_intent",
        lambda *a, **kw: llm.Generation(text=intent_json(status="たぶん出席")),
    )
    assert try_attendance_intent(repo, member(repo), "欠席で", now=NOW) is None


def test_llm_failure_falls_through_and_is_recorded(monkeypatch, repo):
    def boom(*a, **kw):
        raise RuntimeError("vertex unavailable")

    monkeypatch.setattr(llm, "generate_attendance_intent", boom)
    assert try_attendance_intent(repo, member(repo), "来週は欠席で", now=NOW) is None
    logs = repo.list_inference_logs()
    assert len(logs) == 1 and logs[0].ok is False


def test_no_candidate_events_skips_llm(monkeypatch, repo):
    called = []
    monkeypatch.setattr(
        llm, "generate_attendance_intent",
        lambda *a, **kw: called.append(1) or llm.Generation(text=intent_json()),
    )
    # 過去だけの状態（基準時刻を未来にする）
    assert try_attendance_intent(repo, member(repo), "欠席で", now=datetime(2027, 1, 1)) is None
    assert called == []
    assert repo.list_inference_logs() == []


def test_routing_free_text_confirms(monkeypatch, repo):
    monkeypatch.setattr(
        llm, "generate_attendance_intent", lambda *a, **kw: llm.Generation(text=intent_json())
    )
    messages = handle_member_text(repo, member(repo), "来週の例会は欠席で", now=NOW)
    assert "よろしいですか" in messages[0].text


def test_routing_menu_action_keeps_existing_prompt(monkeypatch, repo):
    """リッチメニューの「出欠を回答」は従来どおり出欠依頼を返す（LLMを呼ばない）。"""
    called = []
    monkeypatch.setattr(
        llm, "generate_attendance_intent",
        lambda *a, **kw: called.append(1) or llm.Generation(text=intent_json()),
    )
    messages = handle_member_text(repo, member(repo), "menu|出欠を回答", now=NOW)
    assert called == []
    assert "8月例会" in messages[0].text
    assert [i.action.label for i in messages[0].quick_reply.items] == ["出席", "Web出席", "欠席"]


def test_routing_falls_back_when_llm_unavailable(monkeypatch, repo):
    def boom(*a, **kw):
        raise RuntimeError("vertex unavailable")

    monkeypatch.setattr(llm, "generate_attendance_intent", boom)
    messages = handle_member_text(repo, member(repo), "例会は欠席します", now=NOW)
    # 従来の出欠依頼メッセージ
    assert [i.action.label for i in messages[0].quick_reply.items] == ["出席", "Web出席", "欠席"]
