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
