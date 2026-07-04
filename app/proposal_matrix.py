"""委員会別提案マトリクスの集計。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .models import Proposal


def build_proposal_matrix(proposals: list[Proposal], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    proposals = sorted(proposals, key=lambda p: p.proposal_id)
    grouped: dict[str, list[Proposal]] = {}
    for p in proposals:
        grouped.setdefault(p.committee or "(未設定)", []).append(p)
    committees = sorted(grouped.keys(), key=lambda c: (c.startswith("(") or c.startswith("（"), c))
    grid = [_committee_snapshot(committee, grouped[committee]) for committee in committees]
    return {
        "committees": committees,
        "deadlines": _deadlines(now),
        "proposals": [_proposal_payload(p) for p in proposals],
        "grid": grid,
    }


def _proposal_payload(p: Proposal) -> dict[str, Any]:
    return {
        "proposal_id": p.proposal_id,
        "title": p.title,
        "committee": p.committee,
        "stage": p.stage,
        "status": p.status,
    }


def _committee_snapshot(committee: str, proposals: list[Proposal]) -> dict[str, Any]:
    counts: dict[str, int] = {"entry": 0, "submitted": 0, "other": 0}
    samples: dict[str, list[str]] = {"entry": [], "submitted": [], "other": []}
    latest: dict[str, datetime | None] = {"entry": None, "submitted": None, "other": None}
    for p in proposals:
        stage = _stage_key(p.stage)
        counts[stage] = counts.get(stage, 0) + 1
        if len(samples[stage]) < 3:
            samples[stage].append(f"{p.number or p.title}({p.stage})")
        updated = p.format_check.checked_at if p.format_check else None
        current = latest.get(stage)
        if current is None:
            latest[stage] = updated
        elif updated and updated > current:
            latest[stage] = updated
    return {
        "committee": committee,
        "counts": counts,
        "samples": samples,
        "latest": {k: v.isoformat() if v else None for k, v in latest.items()},
    }


def _stage_key(stage: str) -> str:
    if "entry" in stage:
        return "entry"
    if "submitted" in stage:
        return "submitted"
    return "other"


def _deadlines(now: datetime) -> dict[str, str | None]:
    return {
        "entry": _next_weekday(now, offset=7).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
        "submit": _next_weekday(now, offset=5).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
        "deliver": _next_weekday(now, offset=3).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
    }


def _next_weekday(now: datetime, offset: int) -> datetime:
    days_ahead = (7 - now.weekday()) % 7 + offset
    return now + timedelta(days=days_ahead)


def summarize_matrix(grid: dict[str, Any]) -> dict[str, Any]:
    return {
        "committees": grid.get("committees", []),
        "deadlines": grid.get("deadlines", {}),
        "counts_by_stage": {
            stage: sum(entry.get("counts", {}).get(stage, 0) for entry in grid.get("grid", []))
            for stage in ("entry", "submitted", "other")
        },
    }
