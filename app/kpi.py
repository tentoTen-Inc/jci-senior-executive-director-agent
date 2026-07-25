"""KPI集約（docs/dashboard-design.md §6）。Phase1は出欠トレンド中心。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from .attendance import aggregate
from .inference import LlmCostSummary, llm_cost_summary
from .models import DeliveryResult, EventType
from .repository import Repository


class TrendPoint(BaseModel):
    event_id: str
    title: str
    date: datetime
    type: str
    attendance_rate: float
    answer_rate: float
    total_targets: int


def attendance_trends(repo: Repository, *, event_type: EventType | None = None) -> list[TrendPoint]:
    """イベントを古い順に並べ、出席率・回答率の推移を返す。"""
    events = sorted(repo.list_events(), key=lambda e: e.datetime_start)
    points: list[TrendPoint] = []
    for ev in events:
        if event_type is not None and ev.type != event_type:
            continue
        s = aggregate(repo, ev.event_id)
        if s.total_targets == 0:
            continue
        points.append(
            TrendPoint(
                event_id=ev.event_id,
                title=ev.title,
                date=ev.datetime_start,
                type=ev.type.value,
                attendance_rate=s.attendance_rate,
                answer_rate=round(s.answered / s.total_targets, 3),
                total_targets=s.total_targets,
            )
        )
    return points


class KpiOverview(BaseModel):
    delivery_success_rate: float
    delivery_total: int
    avg_attendance_rate: float
    avg_answer_rate: float
    reminder_count: int
    cost: LlmCostSummary  # 当月のLLMコスト（§6 月間コスト）


def kpi_overview(repo: Repository, *, now: datetime) -> KpiOverview:
    logs = repo.list_delivery_logs()
    ok = sum(1 for x in logs if x.result == DeliveryResult.ok)
    failed = sum(1 for x in logs if x.result == DeliveryResult.failed)
    # reminder 配信数は DeliveryLog に type を持たないため、Phase1 では全配信ログ件数で近似。
    # 正確な内訳は Phase2 でジョブ種別を記録して集計する。
    reminders = len(logs)

    trends = attendance_trends(repo)
    avg_att = round(sum(t.attendance_rate for t in trends) / len(trends), 3) if trends else 0.0
    avg_ans = round(sum(t.answer_rate for t in trends) / len(trends), 3) if trends else 0.0

    return KpiOverview(
        delivery_success_rate=round(ok / (ok + failed), 3) if (ok + failed) else 1.0,
        delivery_total=len(logs),
        avg_attendance_rate=avg_att,
        avg_answer_rate=avg_ans,
        reminder_count=reminders,
        cost=llm_cost_summary(repo, now=now),
    )
