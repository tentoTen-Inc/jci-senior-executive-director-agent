"""Google フォームの取込・正規化（docs/survey-design.md §2.1/§2.2, F7-1）。

設問文・順序・個数は年度ごとに変わる前提で、**設問ID（questionId）で紐づける**汎用パーサ。
Likert の値も API 上は文字列で返るため、数値化は集計側（survey_agg）で行う。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

from .models import Survey, SurveyQuestion, SurveyResponse

logger = logging.getLogger("jci-agent.survey_import")

#: 氏名設問の判定（タイトルに含まれていれば氏名として扱う）
NAME_HINTS = ("氏名", "お名前", "名前")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # Forms API は RFC3339（末尾 Z）で返す
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        logger.info("日時の解釈に失敗しました: %r", value)
        return None


def parse_questions(form: dict) -> list[SurveyQuestion]:
    """items から設問を抽出する。

    questionItem を持たない item は直前のセクション見出しとして扱う。
    """
    questions: list[SurveyQuestion] = []
    section: str | None = None
    for item in form.get("items", []):
        question = item.get("questionItem", {}).get("question")
        if not question:
            # ページ区切り・説明テキスト＝セクション見出し
            title = item.get("title")
            if title:
                section = title
            continue
        qid = question.get("questionId")
        if not qid:
            continue
        if "scaleQuestion" in question:
            scale = question["scaleQuestion"]
            questions.append(SurveyQuestion(
                question_id=qid, title=item.get("title", ""), type="scale", section=section,
                scale_low=scale.get("low"), scale_high=scale.get("high"),
            ))
        elif "choiceQuestion" in question:
            choice = question["choiceQuestion"]
            options = [o["value"] for o in choice.get("options", []) if o.get("value")]
            questions.append(SurveyQuestion(
                question_id=qid, title=item.get("title", ""), type="choice", section=section,
                options=options,
            ))
        elif "textQuestion" in question:
            questions.append(SurveyQuestion(
                question_id=qid, title=item.get("title", ""), type="text", section=section,
            ))
        else:
            logger.info("未対応の設問種別をスキップしました: %s", item.get("title"))
    return questions


def name_question_id(questions: list[SurveyQuestion]) -> str | None:
    """氏名にあたる text 設問のIDを返す（無ければ None）。"""
    for q in questions:
        if q.type == "text" and any(h in q.title for h in NAME_HINTS):
            return q.question_id
    return None


def _answer_value(answer: dict) -> str:
    values = [
        a.get("value", "")
        for a in answer.get("textAnswers", {}).get("answers", [])
        if a.get("value")
    ]
    return ", ".join(values)


def parse_responses(raw: list[dict], questions: list[SurveyQuestion]) -> list[SurveyResponse]:
    name_qid = name_question_id(questions)
    out: list[SurveyResponse] = []
    for r in raw:
        answers = {
            qid: _answer_value(answer) for qid, answer in (r.get("answers") or {}).items()
        }
        out.append(SurveyResponse(
            response_id=r.get("responseId", ""),
            submitted_at=_parse_dt(r.get("lastSubmittedTime") or r.get("createTime")),
            respondent_email=r.get("respondentEmail"),
            respondent_name=(answers.get(name_qid) or None) if name_qid else None,
            answers=answers,
        ))
    out.sort(key=lambda x: (x.submitted_at or datetime.min))
    return out


def build_survey(
    payload: dict,
    *,
    form_id: str,
    kind: str,
    event_id: str | None,
    now: datetime,
    existing: Survey | None = None,
) -> Survey:
    """Forms API の応答を Survey に正規化する。既存があれば survey_id を引き継ぐ。"""
    form = payload.get("form", {})
    questions = parse_questions(form)
    responses = parse_responses(payload.get("responses", []), questions)
    title = form.get("info", {}).get("title") or form_id

    survey_id = existing.survey_id if existing else f"svy_{uuid.uuid4().hex[:10]}"
    survey = Survey(
        survey_id=survey_id,
        form_id=form_id,
        title=title,
        kind=kind,
        event_id=event_id if event_id is not None else (existing.event_id if existing else None),
        questions=questions,
        responses=responses,
        synced_at=now,
    )
    if existing is not None:
        # 生成物・催促履歴は取込で消さない
        survey.digest = existing.digest
        survey.reminder_count = existing.reminder_count
        survey.reminded_at = existing.reminded_at
    return survey
