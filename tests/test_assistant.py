"""自然言語問い合わせ応答（F8 / docs/nl-assistant-design.md）のテスト。"""
from datetime import datetime

import pytest

from app import llm
from app.assistant import ESCALATION_NOTE, MAX_TURNS, answer_member_question
from app.member_menu import handle_member_text
from app.models import (
    Attendance,
    AttendanceStatus,
    Event,
    EventStatus,
    EventType,
    ExternalNotice,
    Member,
    NoticeDigest,
    TargetScope,
    TargetScopeKind,
)
from app.rag import build_context
from app.repository import InMemoryRepository

NOW = datetime(2026, 7, 25, 10, 0)
ME = "m1"


def answer_json(answer="次回の例会は8月20日19時、会場は猪苗代町体験交流館です。", grounded=True,
                needs_human=False) -> str:
    import json

    return json.dumps(
        {"answer": answer, "grounded": grounded, "needs_human": needs_human},
        ensure_ascii=False,
    )


@pytest.fixture
def repo():
    r = InMemoryRepository()
    r.upsert_member(Member(
        member_id=ME, name="遠藤太郎", line_user_id="U1",
        committee="総務委員会", officer_role="専務理事",
    ))
    r.upsert_member(Member(
        member_id="m2", name="佐藤次郎", line_user_id="U2",
        committee="コト創り委員会", phone="090-0000-0000", email="jiro@example.jp",
    ))
    r.upsert_event(Event(
        event_id="e1", type=EventType.例会, title="8月例会",
        datetime_start=datetime(2026, 8, 20, 19, 0),
        location="猪苗代町体験交流館",
        attendance_deadline=datetime(2026, 8, 13, 23, 59),
        target_scope=TargetScope(kind=TargetScopeKind.all),
        status=EventStatus.open,
    ))
    r.upsert_attendance(Attendance(
        event_id="e1", member_id=ME, status=AttendanceStatus.未回答,
    ))
    r.upsert_attendance(Attendance(
        event_id="e1", member_id="m2", status=AttendanceStatus.欠席,
    ))
    return r


def member(repo) -> Member:
    return repo.get_member(ME)


# --------------------------------------------------------------------------- #
# コンテキスト（RAG）
# --------------------------------------------------------------------------- #
def test_context_includes_own_and_event_info(repo):
    ctx = build_context(repo, member(repo), now=NOW)
    assert "遠藤太郎" in ctx
    assert "総務委員会" in ctx and "専務理事" in ctx
    assert "8月例会" in ctx
    assert "猪苗代町体験交流館" in ctx
    assert "2026-08-13 23:59" in ctx  # 出欠締切
    assert "あなたの回答: 未回答" in ctx
    assert "全体の回答: 1/2名" in ctx  # 人数だけ


def test_context_excludes_other_members_personal_info(repo):
    """他会員の氏名・連絡先・個別の出欠はコンテキストに載せない（F8-4 一次防御）。"""
    ctx = build_context(repo, member(repo), now=NOW)
    assert "佐藤次郎" not in ctx
    assert "090-0000-0000" not in ctx
    assert "jiro@example.jp" not in ctx
    assert "U2" not in ctx


def test_context_includes_delivered_notices_and_own_tasks(repo):
    repo.upsert_notice(ExternalNotice(
        notice_id="n1", received_at=datetime(2026, 7, 20), subject="ブロック会員大会",
        body_text="原文", status="delivered",
        digest=NoticeDigest(
            summary="参加者登録の依頼", announcement="ご確認ください",
            deadline=datetime(2026, 8, 5),
        ),
    ))
    from app.notice_actions import create_actions

    create_actions(
        repo, repo.get_notice("n1"),
        titles=["参加者登録"], assignees=[ME, "m2"], due=None, now=NOW,
    )
    ctx = build_context(repo, member(repo), now=NOW)
    assert "ブロック会員大会" in ctx and "参加者登録の依頼" in ctx
    assert "あなたの未対応タスク" in ctx and "参加者登録" in ctx


def test_context_without_events(repo):
    r = InMemoryRepository()
    r.upsert_member(Member(member_id=ME, name="太郎"))
    ctx = build_context(r, r.get_member(ME), now=NOW)
    assert "予定されているイベントはありません。" in ctx


# --------------------------------------------------------------------------- #
# 応答
# --------------------------------------------------------------------------- #
def test_answer_uses_context_and_records_cost(monkeypatch, repo):
    captured = {}

    def fake(question, context, history, **kw):
        captured["question"] = question
        captured["context"] = context
        captured["history"] = history
        return llm.Generation(text=answer_json(), input_tokens=1500, output_tokens=120)

    monkeypatch.setattr(llm, "generate_answer", fake)
    messages = answer_member_question(repo, member(repo), "次の例会っていつ？", now=NOW)

    assert messages is not None
    assert "8月20日" in messages[0].text
    assert ESCALATION_NOTE not in messages[0].text
    assert "8月例会" in captured["context"]
    assert captured["history"] == ""  # 初回は履歴なし

    logs = repo.list_inference_logs()
    assert len(logs) == 1
    assert (logs[0].kind, logs[0].target, logs[0].ok) == ("member_qa", ME, True)
    assert (logs[0].input_tokens, logs[0].output_tokens) == (1500, 120)
    # エスカレーションはしていない
    assert repo.list_escalations() == []


def test_ungrounded_answer_escalates(monkeypatch, repo):
    monkeypatch.setattr(
        llm, "generate_answer",
        lambda *a, **kw: llm.Generation(
            text=answer_json(answer="申し訳ありません、その情報は持ち合わせておりません。",
                             grounded=False)
        ),
    )
    messages = answer_member_question(repo, member(repo), "去年の決算の内訳は？", now=NOW)
    assert ESCALATION_NOTE in messages[0].text
    escalations = repo.list_escalations(status="open")
    assert len(escalations) == 1
    assert escalations[0].kind == "question"
    assert escalations[0].text == "去年の決算の内訳は？"


def test_needs_human_escalates(monkeypatch, repo):
    monkeypatch.setattr(
        llm, "generate_answer",
        lambda *a, **kw: llm.Generation(
            text=answer_json(answer="事務局で確認いたします。", needs_human=True)
        ),
    )
    messages = answer_member_question(repo, member(repo), "会費を分割で払いたい", now=NOW)
    assert ESCALATION_NOTE in messages[0].text
    assert len(repo.list_escalations(status="open")) == 1


def test_history_is_kept_and_trimmed(monkeypatch, repo):
    seen = []

    def fake(question, context, history, **kw):
        seen.append(history)
        return llm.Generation(text=answer_json(answer=f"回答{len(seen)}"))

    monkeypatch.setattr(llm, "generate_answer", fake)
    for i in range(5):
        answer_member_question(repo, member(repo), f"質問{i}", now=NOW)

    assert seen[0] == ""
    assert "質問0" in seen[1] and "回答1" in seen[1]  # 直前の往復が渡る
    conversation = repo.get_conversation(ME)
    assert len(conversation.turns) == MAX_TURNS  # 直近3往復だけ保持
    assert conversation.turns[-2].text == "質問4"
    assert conversation.updated_at == NOW


def test_llm_failure_returns_none_and_records(monkeypatch, repo):
    def boom(*a, **kw):
        raise RuntimeError("vertex unavailable")

    monkeypatch.setattr(llm, "generate_answer", boom)
    assert answer_member_question(repo, member(repo), "次の例会は？", now=NOW) is None
    logs = repo.list_inference_logs()
    assert len(logs) == 1 and logs[0].ok is False
    assert repo.get_conversation(ME) is None  # 履歴も残さない


def test_empty_answer_is_treated_as_failure(monkeypatch, repo):
    monkeypatch.setattr(
        llm, "generate_answer",
        lambda *a, **kw: llm.Generation(text=answer_json(answer="   ")),
    )
    assert answer_member_question(repo, member(repo), "？", now=NOW) is None


# --------------------------------------------------------------------------- #
# LINE ルーティング
# --------------------------------------------------------------------------- #
def test_free_text_goes_to_assistant(monkeypatch, repo):
    monkeypatch.setattr(
        llm, "generate_answer",
        lambda *a, **kw: llm.Generation(text=answer_json(answer="会場は体験交流館です。")),
    )
    messages = handle_member_text(repo, member(repo), "会場ってどこでしたっけ", now=NOW)
    assert "体験交流館" in messages[0].text


def test_keyword_routing_still_wins(monkeypatch, repo):
    """既存の定型応答（予定）は LLM を呼ばない。"""
    called = []
    monkeypatch.setattr(
        llm, "generate_answer",
        lambda *a, **kw: called.append(1) or llm.Generation(text=answer_json()),
    )
    messages = handle_member_text(repo, member(repo), "次回の予定を教えて", now=NOW)
    assert "【次回以降の予定】" in messages[0].text
    assert called == []


def test_falls_back_to_menu_when_llm_unavailable(monkeypatch, repo):
    def boom(*a, **kw):
        raise RuntimeError("vertex unavailable")

    monkeypatch.setattr(llm, "generate_answer", boom)
    messages = handle_member_text(repo, member(repo), "駐車場は使えますか", now=NOW)
    assert messages[0].quick_reply is not None  # メニュー誘導
