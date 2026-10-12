"""AI に任せるタスクごとのモデル選定（2026-10-12 専務の方針: 内容に応じて最適なモデルを使う）。"""
import json

import pytest

from app import llm
from app.inference import estimate_cost_usd
from app.llm import Generation


@pytest.mark.parametrize("task, model", [
    ("member_qa", "gemini-2.5-flash"),        # 返事を待たせる → 速さ優先
    ("attendance_intent", "gemini-2.5-flash"),
    ("calendar_intent", "gemini-2.5-flash"),
    ("proposal_review", "gemini-2.5-pro"),    # 腰を据えて読む → 品質優先
    ("external_notice_digest", "gemini-2.5-pro"),
    ("survey_digest", "gemini-2.5-pro"),
])
def test_default_model_per_task(task, model, monkeypatch):
    monkeypatch.delenv(f"GEMINI_MODEL_{task.upper()}", raising=False)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-pro")  # 全体の既定があっても表が優先
    assert llm.model_for(task) == model


def test_task_override_by_env(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL_MEMBER_QA", "gemini-2.5-pro")
    assert llm.model_for("member_qa") == "gemini-2.5-pro"


def test_unknown_task_falls_back_to_global(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    assert llm.model_for("new_task") == "gemini-2.5-flash"


def test_member_answer_uses_flash(monkeypatch):
    seen = {}

    def fake(question, context, history, *, model, project, location):
        seen["model"] = model
        return Generation(text=json.dumps({"answer": "a", "grounded": True, "needs_human": False}))

    monkeypatch.setattr(llm, "generate_answer", fake)
    outcome = llm.answer_question("福島ブロックの予定を教えて", "材料")
    assert seen["model"] == "gemini-2.5-flash"
    assert outcome.usage.model == "gemini-2.5-flash"  # コストも Flash の単価で計上される


def test_review_still_uses_pro(monkeypatch):
    seen = {}

    def fake(content, *, model, project, location):
        seen["model"] = model
        return Generation(text=json.dumps({"summary": "s", "points": [], "concerns": []}))

    monkeypatch.setattr(llm, "generate_review", fake)
    llm.review_proposal("議案本文")
    assert seen["model"] == "gemini-2.5-pro"


def test_flash_is_cheaper_than_pro():
    flash = estimate_cost_usd("gemini-2.5-flash", 2000, 300)
    pro = estimate_cost_usd("gemini-2.5-pro", 2000, 300)
    assert flash < pro / 3


# --------------------------------------------------------------------------- #
# 「考える時間」と REST 呼び出し（2026-10-12: Flash 既定は 37 秒、上限0で 1.8 秒）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("task, budget", [
    ("member_qa", 0), ("attendance_intent", 0), ("calendar_intent", 512),
    ("proposal_review", None), ("survey_digest", None),
])
def test_thinking_budget_per_task(task, budget, monkeypatch):
    monkeypatch.delenv(f"GEMINI_THINKING_{task.upper()}", raising=False)
    assert llm.thinking_budget_for(task) == budget


def test_thinking_budget_override(monkeypatch):
    monkeypatch.setenv("GEMINI_THINKING_MEMBER_QA", "1024")
    assert llm.thinking_budget_for("member_qa") == 1024


def _capture_post(monkeypatch, response: dict):
    sent = {}

    def fake(url, body):
        sent["url"], sent["body"] = url, body
        return response

    monkeypatch.setattr(llm, "_post_gemini", fake)
    return sent


OK_RESPONSE = {
    "candidates": [{"content": {"parts": [
        {"text": "考え中…", "thought": True},
        {"text": json.dumps({"answer": "回答", "grounded": True, "needs_human": False})},
    ]}}],
    "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 200,
                      "thoughtsTokenCount": 50},
}


def test_member_qa_request_disables_thinking(monkeypatch):
    sent = _capture_post(monkeypatch, OK_RESPONSE)
    out = llm.answer_question("福島ブロックの予定を教えて", "材料")
    assert sent["url"].endswith("/publishers/google/models/gemini-2.5-flash:generateContent")
    assert "asia-northeast1-aiplatform.googleapis.com" in sent["url"]
    assert sent["body"]["generationConfig"] == {
        "responseMimeType": "application/json", "thinkingConfig": {"thinkingBudget": 0},
    }
    assert out.result.answer == "回答"  # 「考え中」の部分は本文に混ぜない
    assert out.usage.output_tokens == 250  # 考えたトークンも出力として課金されるので含める


def test_pro_task_keeps_default_thinking(monkeypatch):
    sent = _capture_post(monkeypatch, {
        "candidates": [{"content": {"parts": [{"text": json.dumps(
            {"summary": "s", "points": [], "concerns": []})}]}}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    })
    llm.review_proposal("議案本文")
    assert "gemini-2.5-pro" in sent["url"]
    assert "thinkingConfig" not in sent["body"]["generationConfig"]


def test_empty_response_is_failure(monkeypatch):
    _capture_post(monkeypatch, {"candidates": [{"content": {"parts": []}}]})
    assert llm.answer_question("質問", "材料") is None  # 呼び出し側は従来どおり縮退する
