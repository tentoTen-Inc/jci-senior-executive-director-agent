"""LINE の資料・やり取りの索引と検索・回答への組み込み（docs/lake-ai-design.md P6-4a）。"""
import json
from datetime import datetime

import pytest

from app import lake_knowledge, lake_query, llm, main
from app.assistant import answer_member_question
from app.llm import Generation
from app.member_menu import handle_member_text
from app.models import LineGroup, Member
from app.repository import InMemoryRepository

NOW = datetime(2026, 10, 6, 21, 35)
CHUNK = {
    "chunk_id": "file:634944787685835148:0", "source_kind": "file",
    "source_id": "634944787685835148", "scope_type": "user", "scope_id": "U_sed",
    "title": "年内スケジュール.pdf",
    "content": (
        "資料「年内スケジュール.pdf」(2026-10-06)\n"
        "10月08日(木) 16:00～ 今次年度引継ぎ 場所：会津若松"
    ),
    "sent_at": "2026-10-06T11:58:09+00:00", "distance": 0.21,
}


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("LAKE_EMBEDDING_MODEL", "embedding_model")


def _member() -> Member:
    return Member(member_id="sed", name="専務 太郎", officer_role="専務理事", line_user_id="U_sed")


# --------------------------------------------------------------------------- #
# SQL
# --------------------------------------------------------------------------- #
def test_disabled_without_model(monkeypatch):
    monkeypatch.delenv("LAKE_EMBEDDING_MODEL", raising=False)
    called = []
    monkeypatch.setattr(lake_query, "query", lambda *a: called.append(a))
    monkeypatch.setattr(lake_query, "execute", lambda *a: called.append(a))
    assert lake_knowledge.index_knowledge() is None
    assert lake_knowledge.search("U1", "福島ブロックの予定") == []
    assert called == []


def test_index_sql_shape(enabled):
    sql = lake_knowledge.index_sql()
    assert sql.startswith("INSERT INTO `test-project.line_lake.knowledge_chunks`")
    assert "MODEL `test-project.line_lake.embedding_model`" in sql
    assert "'RETRIEVAL_DOCUMENT' AS task_type" in sql and "768 AS output_dimensionality" in sql
    assert "SUBSTR(f.text, n * 700 + 1, 800)" in sql  # 800字・100字重ね
    assert "CHAR_LENGTH(m.text) >= 20" in sql  # 短い挨拶は検索対象外
    assert "FROM `test-project.line_lake.v_messages_for_ai`" in sql  # 送信取消は除外済みのビュー
    assert "WHERE k.chunk_id IS NULL" in sql and "LIMIT 500" in sql  # 未索引だけ・上限あり


def test_cleanup_removes_unsent(enabled):
    sql = lake_knowledge.cleanup_sql()
    assert "WHERE unsent" in sql and "status = 'deleted_unsent'" in sql


def test_search_sql_scopes_to_what_user_can_see(enabled):
    sql = lake_knowledge.search_sql()
    assert "k.scope_type = 'user' AND k.scope_id = @user_id" in sql  # 自分の個別トークだけ
    assert "WHERE user_id = @user_id" in sql  # 自分が発言したグループだけ
    assert "'RETRIEVAL_QUERY' AS task_type" in sql
    assert "ML.DISTANCE(k.embedding, q.embedding, 'COSINE')" in sql
    assert "distance <= @max_distance" in sql and "LIMIT 5" in sql
    # 範囲の絞り込みは距離計算と同じ WHERE で行う（VECTOR_SEARCH の事後フィルタにしない）
    assert sql.index("scope_id = @user_id") < sql.index("WHERE distance")


def test_index_knowledge_reports_counts(enabled, monkeypatch):
    results = iter([3, 1])
    monkeypatch.setattr(lake_query, "execute", lambda sql, params=None: next(results))
    assert lake_knowledge.index_knowledge() == {"indexed": 3, "removed": 1}


def test_index_failure_does_not_raise(enabled, monkeypatch):
    def boom(sql, params=None):
        raise lake_query.QueryError("403: connection denied")

    monkeypatch.setattr(lake_query, "execute", boom)
    assert lake_knowledge.index_knowledge() == {"error": "index_failed"}


def test_search_passes_parameters(enabled, monkeypatch):
    seen = {}
    monkeypatch.setattr(lake_query, "query",
                        lambda sql, params=None: seen.update(params) or [CHUNK])
    assert lake_knowledge.search("U_sed", "福島ブロックの予定を教えて") == [CHUNK]
    assert seen == {"user_id": "U_sed", "question": "福島ブロックの予定を教えて",
                    "max_distance": lake_knowledge.MAX_DISTANCE}


def test_search_without_line_user_is_empty(enabled, monkeypatch):
    monkeypatch.setattr(lake_query, "query", lambda *a: pytest.fail("呼ばれない"))
    assert lake_knowledge.search(None, "質問") == []


def test_search_failure_is_empty(enabled, monkeypatch):
    def boom(sql, params=None):
        raise lake_query.QueryError("timeout")

    monkeypatch.setattr(lake_query, "query", boom)
    assert lake_knowledge.search("U_sed", "質問") == []


# --------------------------------------------------------------------------- #
# 回答材料
# --------------------------------------------------------------------------- #
def test_context_section_labels_sources():
    repo = InMemoryRepository()
    repo.save_line_group(LineGroup(group_id="G1", group_name="福島ブロック専務"))
    group_chunk = {**CHUNK, "scope_type": "group", "scope_id": "G1", "title": None,
                   "sent_at": "2026-10-05T00:00:00+00:00",
                   "content": "LINEの発言(2026-10-05 09:00)\n総会は12月です"}
    text = lake_knowledge.context_section(repo, [CHUNK, group_chunk])
    assert "[1] 資料「年内スケジュール.pdf」／あなたとの個別トーク／2026-10-06" in text
    assert "10月08日(木) 16:00～ 今次年度引継ぎ" in text
    assert "資料「年内スケジュール.pdf」(2026-10-06)" not in text  # 見出しの重複は外す
    assert "[2] 発言／グループ「福島ブロック専務」／2026-10-05" in text
    assert lake_knowledge.context_section(repo, []) == ""


def _capture_llm(monkeypatch) -> list[str]:
    prompts = []

    def fake(question, context, history, **kwargs):
        prompts.append(context)
        return Generation(text=json.dumps({
            "answer": "資料によると10月8日に引継ぎがあります[1]。", "grounded": True,
            "needs_human": False,
        }))

    monkeypatch.setattr(llm, "generate_answer", fake)
    return prompts


def test_assistant_adds_line_sources(enabled, monkeypatch):
    monkeypatch.setattr(lake_knowledge, "search", lambda uid, q: [CHUNK] if uid == "U_sed" else [])
    prompts = _capture_llm(monkeypatch)
    repo = InMemoryRepository()
    msgs = answer_member_question(repo, _member(), "福島ブロックの予定を教えて", now=NOW)
    assert "[1] 資料「年内スケジュール.pdf」" in prompts[0]
    assert msgs[0].text.startswith("資料によると10月8日")


def test_assistant_still_answers_without_sources(enabled, monkeypatch):
    monkeypatch.setattr(lake_knowledge, "search", lambda uid, q: [])
    prompts = _capture_llm(monkeypatch)
    answer_member_question(InMemoryRepository(), _member(), "次の例会は？", now=NOW)
    assert "LINEで共有された資料" not in prompts[0]


def test_qa_prompt_asks_for_citations():
    assert "出典番号（例: [1]）" in llm._QA_PROMPT


# --------------------------------------------------------------------------- #
# 振り分け: 長い自由文は AI へ、短いコマンドは定型
# --------------------------------------------------------------------------- #
@pytest.fixture
def routed(monkeypatch):
    calls = []

    def fake_answer(repo, member, question, *, now):
        calls.append(question)
        from linebot.v3.messaging import TextMessage
        return [TextMessage(text="AIの回答")]

    from app import member_menu
    monkeypatch.setattr(member_menu, "answer_member_question", fake_answer)
    monkeypatch.setattr(member_menu, "try_calendar_intent", lambda *a, **k: None)
    monkeypatch.setattr(member_menu, "try_attendance_intent", lambda *a, **k: None)
    return calls


@pytest.mark.parametrize("text", [
    "福島ブロックの予定を教えて",          # 13字・「予定」を含む（本番で定型に回った質問）
    "来月のブロック大会の日程を確認したい",  # 「確認」を含む長文
    "アンケートの回答期限はいつまでですか",  # 「回答」「いつ」を含む長文
    "対外連絡の内容をもう一度教えてください",  # 「連絡」を含む長文
])
def test_long_questions_go_to_ai(routed, text):
    repo = InMemoryRepository()
    msgs = handle_member_text(repo, _member(), text, now=NOW)
    assert routed == [text]
    assert msgs[0].text == "AIの回答"


@pytest.mark.parametrize("text, expected", [
    ("次回の予定", "予定されている"),
    ("出欠確認", "対象の予定はありません"),
    ("欠席", "回答が必要な出欠はありません"),
    ("事務局に連絡", "事務局への連絡を受け付けました"),
])
def test_short_commands_keep_fixed_replies(routed, text, expected):
    msgs = handle_member_text(InMemoryRepository(), _member(), text, now=NOW)
    assert routed == []
    assert expected in msgs[0].text


def test_menu_postback_keeps_fixed_reply(routed):
    msgs = handle_member_text(InMemoryRepository(), _member(), "menu|次回の予定", now=NOW)
    assert routed == [] and "予定" in msgs[0].text


def test_tick_runs_knowledge_index(monkeypatch):
    from fastapi.testclient import TestClient

    from app.deps import set_repo
    from tests.conftest import ADMIN_AUTH

    set_repo(InMemoryRepository())  # 既定の Firestore に繋がないように
    monkeypatch.setattr(main, "index_knowledge", lambda: {"indexed": 2, "removed": 0})
    monkeypatch.setattr(main, "redact_unsent", lambda: None)
    monkeypatch.setattr(main.gmail, "is_configured", lambda: False)
    try:
        res = TestClient(main.app, headers=ADMIN_AUTH).post("/tasks/tick")
    finally:
        set_repo(None)
    assert res.json()["lake"] == {"redact": None, "knowledge": {"indexed": 2, "removed": 0}}
