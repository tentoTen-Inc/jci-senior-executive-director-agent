"""AI 回答の記録・👍/👎・振り返り API（docs/lake-ai-design.md §5 / P6-4b）。"""
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import lake, lake_knowledge, lake_query, llm, main
from app.assistant import answer_member_question
from app.deps import set_repo
from app.llm import Generation
from app.models import Member
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

NOW = datetime(2026, 10, 6, 21, 40)
admin = TestClient(main.app, headers=ADMIN_AUTH)


@pytest.fixture
def repo():
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="sed", name="専務 太郎", officer_role="専務理事",
                           line_user_id="U_sed"))
    set_repo(r)
    yield r
    set_repo(None)


@pytest.fixture
def published(monkeypatch):
    monkeypatch.setenv("LINE_EVENTS_TOPIC", "line-events")
    envs: list[dict] = []
    monkeypatch.setattr(lake, "publish", lambda e: envs.extend(e) or True)
    return envs


def _llm(monkeypatch, *, grounded=True, needs_human=False):
    def fake(question, context, history, **kwargs):
        return Generation(text=json.dumps({
            "answer": "資料によると10月8日に引継ぎがあります[1]。",
            "grounded": grounded, "needs_human": needs_human,
        }), input_tokens=1200, output_tokens=80)

    monkeypatch.setattr(llm, "generate_answer", fake)


SOURCE = {"chunk_id": "file:m2:0", "title": "年内スケジュール.pdf", "scope_type": "user",
          "scope_id": "U_sed", "content": "資料「年内スケジュール.pdf」\n本文", "distance": 0.21,
          "sent_at": "2026-10-06T11:58:09+00:00"}


# --------------------------------------------------------------------------- #
# 回答の記録と 👍/👎
# --------------------------------------------------------------------------- #
def test_answer_is_recorded_with_sources_and_buttons(repo, published, monkeypatch):
    monkeypatch.setattr(lake_knowledge, "search", lambda uid, q: [SOURCE])
    _llm(monkeypatch)
    [msg] = answer_member_question(repo, repo.get_member("sed"), "福島ブロックの予定を教えて",
                                   now=NOW)

    [env] = [e for e in published if e["kind"] == "ai_answer"]
    record = env["payload"]
    assert env["id"] == record["answer_id"]  # 重複排除キー
    assert record["question"] == "福島ブロックの予定を教えて"
    assert record["answer"] == msg.text
    assert record["sources"] == [{"chunk_id": "file:m2:0", "title": "年内スケジュール.pdf",
                                  "scope_type": "user", "distance": 0.21}]  # 本文は記録しない
    assert record["grounded"] is True and record["needs_human"] is False
    assert record["input_tokens"] == 1200 and record["user_id"] == "U_sed"

    labels = [i.action.label for i in msg.quick_reply.items]
    datas = [i.action.data for i in msg.quick_reply.items]
    assert labels == ["👍 役に立った", "👎 違う"]
    assert datas == [f"aifb|{record['answer_id']}|up", f"aifb|{record['answer_id']}|down"]


def test_no_buttons_when_lake_disabled(repo, monkeypatch):
    monkeypatch.delenv("LINE_EVENTS_TOPIC", raising=False)
    monkeypatch.setattr(lake_knowledge, "search", lambda uid, q: [])
    _llm(monkeypatch)
    [msg] = answer_member_question(repo, repo.get_member("sed"), "質問です", now=NOW)
    assert msg.quick_reply is None  # 押されても記録できないので付けない


def test_escalated_answer_is_recorded_as_such(repo, published, monkeypatch):
    monkeypatch.setattr(lake_knowledge, "search", lambda uid, q: [])
    _llm(monkeypatch, grounded=False, needs_human=True)
    answer_member_question(repo, repo.get_member("sed"), "会費の減免を相談したい", now=NOW)
    record = next(e["payload"] for e in published if e["kind"] == "ai_answer")
    assert record["grounded"] is False and record["needs_human"] is True
    assert "事務局に取次ぎました" in record["answer"]  # 実際に送った文面を残す


@pytest.mark.parametrize("rating, thanks", [("up", "ありがとうございます"),
                                            ("down", "ご指摘ありがとうございます")])
def test_feedback_postback_is_recorded(repo, published, rating, thanks):
    reply = main.handle_postback("U_sed", f"aifb|ans_abc|{rating}")
    assert thanks in reply[0].text
    [env] = published
    assert env["kind"] == "ai_feedback"
    assert env["payload"] == {"answer_id": "ans_abc", "rating": rating,
                              "member_id": "sed", "user_id": "U_sed"}


def test_invalid_feedback_is_rejected(repo, published):
    reply = main.handle_postback("U_sed", "aifb|ans_abc|meh")
    assert "受け付けられません" in reply[0].text
    assert published == []


# --------------------------------------------------------------------------- #
# 振り返り API
# --------------------------------------------------------------------------- #
@pytest.fixture
def bq(monkeypatch):
    monkeypatch.setenv("LINE_EVENTS_TOPIC", "line-events")
    calls: list[tuple[str, dict]] = []
    rows: list[dict] = []
    monkeypatch.setattr(lake_query, "query",
                        lambda sql, params=None: calls.append((sql, params or {})) or rows)
    return calls, rows


def test_answers_filter(repo, bq):
    calls, _ = bq
    assert admin.get("/api/ai/answers", params={"filter": "down"}).status_code == 200
    sql, params = calls[0]
    assert "v_ai_answers" in sql and "WHERE rating = 'down'" in sql
    assert params == {"limit": 100}
    assert admin.get("/api/ai/answers", params={"filter": "evil'--"}).status_code == 400


def test_summary(repo, bq):
    calls, rows = bq
    rows.append({"answers": 5, "up": 2, "down": 1})
    assert admin.get("/api/ai/summary").json() == {"answers": 5, "up": 2, "down": 1}
    assert "INTERVAL 30 DAY" in calls[0][0]


def test_csv_export(repo, bq):
    _, rows = bq
    rows.append({"answered_at": "2026-10-06T12:40:00+00:00", "question": "予定は？",
                 "answer": "10月8日です[1]。", "rating": "up", "grounded": True,
                 "needs_human": False, "source_count": 1, "sources": "[]",
                 "model": "gemini-2.5-pro", "input_tokens": 10, "output_tokens": 5,
                 "answer_id": "ans_1"})
    res = admin.get("/api/ai/answers.csv")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    body = res.content.decode("utf-8")
    assert body.startswith("﻿answered_at,question,answer,rating")  # Excel 用の BOM
    assert "予定は？,10月8日です[1]。,up" in body


def test_ai_api_requires_admin(repo, bq):
    assert TestClient(main.app).get("/api/ai/answers").status_code == 401
