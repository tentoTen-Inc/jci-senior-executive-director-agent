"""アンケートのレポート生成と過去回比較（docs/survey-design.md §5, F7-4/F7-5）。

定量（平均・分布・回答率）と定性（AI要約）をまとめる。LINE通知用の短文も作る。
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from .models import Survey
from .repository import Repository
from .survey_agg import aggregate_survey, overall_average
from .survey_match import pending_members

KIND_LABEL = {"internal": "対内", "external": "対外"}
MAX_NOTIFY_SCALES = 3


def _rate(answered: int, total: int) -> str:
    return f"{round(answered / total * 100)}%" if total else "-"


def build_report(repo: Repository, survey: Survey, *, now: datetime) -> str:
    """レポート本文（Markdown）。定量→定性→未提出の順にまとめる。"""
    agg = aggregate_survey(survey)
    lines = [
        f"# {survey.title} アンケート集計",
        "",
        f"- 区分: {KIND_LABEL.get(survey.kind, survey.kind)}",
        f"- 回答数: {agg.responses}件",
    ]

    average = overall_average(agg)
    if average is not None:
        lines.append(f"- 総合平均: {average}（5段階）")
    lines.append(f"- 作成: {now.strftime('%Y-%m-%d %H:%M')}")

    if survey.kind == "internal":
        pending = pending_members(repo, survey)
        lines.append(
            f"- 回答率: {_rate(pending.answered, pending.total_targets)}"
            f"（{pending.answered}/{pending.total_targets}名）"
        )
        if pending.unmatched:
            lines.append(f"- 会員と照合できなかった回答: {pending.unmatched}件")
    lines.append("")

    if agg.scales:
        lines.append("## 5段階評価")
        lines.append("")
        lines.append("| 設問 | 平均 | 回答数 | 分布(1→5) |")
        lines.append("|---|---|---|---|")
        for s in agg.scales:
            low = s.scale_low or 1
            high = s.scale_high or 5
            dist = " / ".join(
                str(s.distribution.get(str(k), 0)) for k in range(low, high + 1)
            )
            avg = "-" if s.average is None else f"{s.average}"
            lines.append(f"| {s.title} | {avg} | {s.answered} | {dist} |")
        lines.append("")

    if survey.digest is not None:
        d = survey.digest
        lines.append("## 自由記述の要約（AI・参考）")
        lines.append("")
        lines.append(d.summary)
        if d.sentiment:
            lines.append(
                f"- 感情傾向: 肯定 {d.sentiment.get('positive', 0)} / "
                f"中立 {d.sentiment.get('neutral', 0)} / 否定 {d.sentiment.get('negative', 0)}"
            )
        if d.themes:
            lines.append("- 分類")
            for t in d.themes:
                examples = f"（例: {' / '.join(t.examples)}）" if t.examples else ""
                lines.append(f"    - {t.label}: {t.count}件{examples}")
        if d.improvements:
            lines.append("- 改善提案")
            lines += [f"    - {x}" for x in d.improvements]
        lines.append("")
        lines.append("※ 要約・分類はAIの助言です。判断は原文も確認のうえ行ってください。")
    else:
        lines.append("## 自由記述の要約")
        lines.append("")
        lines.append("未生成です（「自由記述をAIで要約」を実行してください）。")

    return "\n".join(lines)


def build_notification_text(repo: Repository, survey: Survey) -> str:
    """五役へLINEで送る短いサマリ。"""
    agg = aggregate_survey(survey)
    lines = [f"【アンケート集計】{survey.title}", f"回答 {agg.responses}件"]
    if survey.kind == "internal":
        pending = pending_members(repo, survey)
        lines[-1] = (
            f"回答 {pending.answered}/{pending.total_targets}名"
            f"（{_rate(pending.answered, pending.total_targets)}）"
        )
    average = overall_average(agg)
    if average is not None:
        lines.append(f"総合平均 {average}（5段階）")
    for s in agg.scales[:MAX_NOTIFY_SCALES]:
        if s.average is not None:
            lines.append(f"・{s.title}: {s.average}")
    if survey.digest is not None and survey.digest.summary:
        lines.append(f"要約(AI): {survey.digest.summary}")
    return "\n".join(lines)


class SurveyTrendPoint(BaseModel):
    survey_id: str
    title: str
    kind: str
    synced_at: datetime | None
    responses: int
    overall_average: float | None


def survey_trends(repo: Repository, *, kind: str | None = None) -> list[SurveyTrendPoint]:
    """過去回のアンケートを古い順に並べ、総合平均の推移を返す（F7-5）。

    設問文は回ごとに変わるため、設問単位ではなく**全スケール設問の総合平均**で比較する。
    """
    surveys = sorted(
        repo.list_surveys(kind=kind),
        key=lambda s: (s.synced_at or datetime.min),
    )
    points: list[SurveyTrendPoint] = []
    for s in surveys:
        agg = aggregate_survey(s)
        points.append(SurveyTrendPoint(
            survey_id=s.survey_id,
            title=s.title,
            kind=s.kind,
            synced_at=s.synced_at,
            responses=agg.responses,
            overall_average=overall_average(agg),
        ))
    return points
