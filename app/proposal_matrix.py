"""委員会別提出マトリクス（docs/dashboard-design.md §3.2）。

行=委員会 / 列=締切区分（エントリー/提出/配信）/ セル=状態。
締切は `Proposal.deadlines`（entry/submit/deliver）の実データを使い、
到達済みかどうかは `Proposal.stage` の進み具合で判定する。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from .models import PROPOSAL_STAGE_ORDER, Proposal, ProposalStage

UNSET_COMMITTEE = "(未設定)"

#: 締切区分（列）とその表示名。
DEADLINE_KINDS: list[tuple[str, str]] = [
    ("entry", "エントリー"),
    ("submit", "提出"),
    ("deliver", "配信"),
]

#: 各締切区分を「完了」とみなせる最小ステージ。
#: 例: エントリー締切は資料提出ステージ以降に進んでいれば達成済み。
_DONE_FROM: dict[str, ProposalStage] = {
    "entry": ProposalStage.submitted,
    "submit": ProposalStage.sed_review,
    "deliver": ProposalStage.board,
}

#: 締切まで何日以内を「間近」とするか。
SOON_DAYS = 3

#: セル色の決定に使う深刻度。行内で最も深刻な状態をセル状態にする。
_SEVERITY: dict[str, int] = {
    "done": 0,
    "pending": 1,
    "unset": 2,
    "soon": 3,
    "overdue": 4,
}

_STAGE_INDEX = {s: i for i, s in enumerate(PROPOSAL_STAGE_ORDER)}


class MatrixCell(BaseModel):
    kind: str
    total: int
    counts: dict[str, int]
    state: str  # done | pending | unset | soon | overdue
    next_deadline: datetime | None = None  # 未完了のうち最も早い締切
    samples: list[str] = Field(default_factory=list)  # セル状態の代表議案（最大3件）


class MatrixRow(BaseModel):
    committee: str
    total: int
    cells: dict[str, MatrixCell]


class ProposalMatrix(BaseModel):
    generated_at: datetime
    kinds: list[dict[str, str]]
    rows: list[MatrixRow]
    totals: dict[str, dict[str, int]]  # 締切区分ごとの全体件数内訳


def _stage_index(stage: ProposalStage | str) -> int:
    return _STAGE_INDEX.get(ProposalStage(stage), 0)


def _cell_state(p: Proposal, kind: str, now: datetime) -> str:
    """議案1件について、ある締切区分の状態を返す。"""
    if _stage_index(p.stage) >= _STAGE_INDEX[_DONE_FROM[kind]]:
        return "done"
    due: datetime | None = getattr(p.deadlines, kind)
    if due is None:
        return "unset"
    if due < now:
        return "overdue"
    if due <= now + timedelta(days=SOON_DAYS):
        return "soon"
    return "pending"


def _label(p: Proposal, state: str, kind: str) -> str:
    due: datetime | None = getattr(p.deadlines, kind)
    name = p.number or p.title
    if state in ("overdue", "soon", "pending") and due is not None:
        return f"{name}（〆{due.month}/{due.day}）"
    return name


def _build_cell(proposals: list[Proposal], kind: str, now: datetime) -> MatrixCell:
    counts = {state: 0 for state in _SEVERITY}
    states: list[tuple[Proposal, str]] = []
    for p in proposals:
        state = _cell_state(p, kind, now)
        counts[state] += 1
        states.append((p, state))

    worst = max(
        (s for _, s in states),
        key=lambda s: _SEVERITY[s],
        default="done",
    )
    pending_due = [
        getattr(p.deadlines, kind)
        for p, s in states
        if s != "done" and getattr(p.deadlines, kind) is not None
    ]
    samples = [_label(p, s, kind) for p, s in states if s == worst][:3]
    return MatrixCell(
        kind=kind,
        total=len(proposals),
        counts=counts,
        state=worst,
        next_deadline=min(pending_due) if pending_due else None,
        samples=samples,
    )


def _committee_sort_key(name: str) -> tuple[int, str]:
    # 「(未設定)」は末尾に置く。
    return (1 if name == UNSET_COMMITTEE else 0, name)


def build_proposal_matrix(proposals: list[Proposal], *, now: datetime) -> ProposalMatrix:
    """議案リストを 委員会 × 締切区分 のマトリクスに集約する。"""
    grouped: dict[str, list[Proposal]] = {}
    for p in proposals:
        grouped.setdefault(p.committee or UNSET_COMMITTEE, []).append(p)

    rows: list[MatrixRow] = []
    for committee in sorted(grouped, key=_committee_sort_key):
        items = sorted(grouped[committee], key=lambda p: p.proposal_id)
        rows.append(
            MatrixRow(
                committee=committee,
                total=len(items),
                cells={kind: _build_cell(items, kind, now) for kind, _ in DEADLINE_KINDS},
            )
        )

    totals: dict[str, dict[str, int]] = {}
    for kind, _ in DEADLINE_KINDS:
        agg = {state: 0 for state in _SEVERITY}
        for row in rows:
            for state, n in row.cells[kind].counts.items():
                agg[state] += n
        totals[kind] = agg

    return ProposalMatrix(
        generated_at=now,
        kinds=[{"key": k, "label": label} for k, label in DEADLINE_KINDS],
        rows=rows,
        totals=totals,
    )
