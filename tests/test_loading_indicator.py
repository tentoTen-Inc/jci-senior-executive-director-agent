"""受け付けたことがすぐ分かるようにする「入力中…」表示（2026-10-12 専務の要望）。"""
import json
from datetime import datetime

import pytest

from app import assistant, config, lake, lake_knowledge, llm, main
from app.deps import set_repo
from app.llm import Generation
from app.models import Member
from app.repository import InMemoryRepository


class _FakeApi:
    calls: list = []

    def __init__(self, *_a, **_k):
        pass

    def show_loading_animation(self, req):
        _FakeApi.calls.append((req.chat_id, req.loading_seconds))


@pytest.fixture
def api(monkeypatch):
    _FakeApi.calls = []
    monkeypatch.setattr(config, "line_channel_access_token", lambda: "real-token")
    monkeypatch.setattr(main, "MessagingApi", _FakeApi)
    return _FakeApi.calls


@pytest.fixture
def repo(monkeypatch):
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="m2", name="遠藤 孝行", line_user_id="U_sed"))
    set_repo(r)
    monkeypatch.setattr(main, "handle_member_text", lambda repo, m, t, now: ["回答"])
    yield r
    set_repo(None)


def test_linked_member_sees_loading_before_processing(api, repo):
    order = []
    original = main.start_loading
    main.start_loading = lambda uid: (order.append("loading"), original(uid))
    try:
        main.handle_member_text = lambda repo, m, t, now: order.append("answer") or ["回答"]
        main.handle_text_message("U_sed", "福島ブロックの予定を教えて")
    finally:
        main.start_loading = original
    assert order == ["loading", "answer"]  # 処理の前に表示する
    assert api == [("U_sed", 60)]


def test_unlinked_user_has_no_loading(api, repo):
    main.handle_text_message("U_unknown", "ABC123")  # 招待コードの照合は一瞬で終わる
    assert api == []


def test_loading_failure_does_not_block_reply(repo, monkeypatch):
    monkeypatch.setattr(config, "line_channel_access_token", lambda: "real-token")

    class Boom(_FakeApi):
        def show_loading_animation(self, req):
            raise RuntimeError("429")

    monkeypatch.setattr(main, "MessagingApi", Boom)
    assert main.handle_text_message("U_sed", "質問です") == ["回答"]


def test_no_token_no_call(repo, monkeypatch):
    called = []
    monkeypatch.setattr(main, "MessagingApi", lambda *a, **k: called.append(a))
    main.start_loading("U_sed")  # conftest のトークンは PLACEHOLDER
    assert called == []


def test_answer_records_timing(monkeypatch):
    monkeypatch.setenv("LINE_EVENTS_TOPIC", "line-events")
    envs = []
    monkeypatch.setattr(lake, "publish", lambda e: envs.extend(e) or True)
    monkeypatch.setattr(lake_knowledge, "search", lambda uid, q, scopes=None: [])
    monkeypatch.setattr(llm, "generate_answer", lambda *a, **k: Generation(text=json.dumps(
        {"answer": "回答です", "grounded": True, "needs_human": False})))
    member = Member(member_id="m2", name="遠藤 孝行", line_user_id="U_sed")
    assistant.answer_member_question(InMemoryRepository(), member, "質問",
                                     now=datetime(2026, 10, 12, 12, 0))
    record = next(e["payload"] for e in envs if e["kind"] == "ai_answer")
    assert isinstance(record["context_search_ms"], int) and isinstance(record["llm_ms"], int)
