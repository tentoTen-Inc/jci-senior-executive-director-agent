"""アンケート集計（docs/survey-design.md §3.2, F7-2 の定量側）。

Likert（scale）は平均と分布、自由記述（text）は一覧を返す。保存はせず都度算出する。
"""
from __future__ import annotations

from pydantic import BaseModel

from .models import Survey


class ScaleStat(BaseModel):
    question_id: str
    title: str
    section: str | None = None
    scale_low: int | None = None
    scale_high: int | None = None
    answered: int
    average: float | None  # 回答が無ければ None（0と区別する）
    distribution: dict[str, int]  # 値 -> 件数


class TextStat(BaseModel):
    question_id: str
    title: str
    section: str | None = None
    answers: list[str]


class SurveyAggregate(BaseModel):
    survey_id: str
    title: str
    kind: str
    responses: int
    scales: list[ScaleStat]
    texts: list[TextStat]


def _to_number(value: str) -> float | None:
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return None


def aggregate_survey(survey: Survey) -> SurveyAggregate:
    scales: list[ScaleStat] = []
    texts: list[TextStat] = []

    for q in survey.questions:
        values = [r.answers.get(q.question_id, "") for r in survey.responses]
        filled = [v for v in values if v.strip()]
        if q.type == "scale":
            numbers = [n for n in (_to_number(v) for v in filled) if n is not None]
            distribution: dict[str, int] = {}
            for n in numbers:
                key = str(int(n)) if n.is_integer() else str(n)
                distribution[key] = distribution.get(key, 0) + 1
            scales.append(ScaleStat(
                question_id=q.question_id,
                title=q.title,
                section=q.section,
                scale_low=q.scale_low,
                scale_high=q.scale_high,
                answered=len(numbers),
                average=round(sum(numbers) / len(numbers), 2) if numbers else None,
                distribution=distribution,
            ))
        elif q.type == "text":
            texts.append(TextStat(
                question_id=q.question_id, title=q.title, section=q.section, answers=filled,
            ))

    return SurveyAggregate(
        survey_id=survey.survey_id,
        title=survey.title,
        kind=survey.kind,
        responses=len(survey.responses),
        scales=scales,
        texts=texts,
    )


def overall_average(aggregate: SurveyAggregate) -> float | None:
    """全 scale 設問の平均（過去回比較に使う）。回答が無ければ None。"""
    averages = [s.average for s in aggregate.scales if s.average is not None]
    return round(sum(averages) / len(averages), 2) if averages else None


def free_texts(aggregate: SurveyAggregate) -> list[str]:
    """自由記述をまとめて返す（Gemini 要約の入力）。設問文を添えて文脈を残す。"""
    out: list[str] = []
    for t in aggregate.texts:
        for a in t.answers:
            out.append(f"[{t.title}] {a}")
    return out
