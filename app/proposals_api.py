"""議案ライフサイクルAPI（docs/dashboard-design.md §4.1/§5, F6）。"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from .audit import write_audit
from .deps import get_repo
from .drive_import import import_drive_folder
from .format_check import run_format_check
from .llm import review_proposal
from .models import (
    Proposal,
    ProposalDeadlines,
    ProposalHistory,
    ProposalStage,
    SedApproval,
)
from .proposal_matrix import build_proposal_matrix

router = APIRouter(tags=["proposals"])


def _actor(email: str | None) -> str:
    return email or "unknown"


class ProposalCreate(BaseModel):
    title: str
    number: str | None = None
    committee: str | None = None
    owner_member_id: str | None = None
    event_id: str | None = None
    doc_type: str = "事業計画書"
    content: str | None = None
    storage_uri: str | None = None
    deadlines: ProposalDeadlines = ProposalDeadlines()
    stage: ProposalStage = ProposalStage.entry


class ProposalUpdate(BaseModel):
    title: str | None = None
    number: str | None = None
    committee: str | None = None
    owner_member_id: str | None = None
    event_id: str | None = None
    doc_type: str | None = None
    content: str | None = None
    storage_uri: str | None = None
    deadlines: ProposalDeadlines | None = None


@router.get("/proposals")
def list_proposals(status: str | None = None):
    return get_repo().list_proposals(status=status)


class DriveImport(BaseModel):
    folder_id: str
    dry_run: bool = False


@router.post("/proposals/import-drive")
def import_drive(
    payload: DriveImport,
    x_goog_authenticated_user_email: str | None = Header(default=None),
):
    """Drive フォルダ内の Google ドキュメントを議案として取り込む。"""
    repo = get_repo()
    actor = _actor(x_goog_authenticated_user_email)
    try:
        summary = import_drive_folder(
            repo, payload.folder_id, now=datetime.now(),
            dry_run=payload.dry_run, actor=actor,
        )
    except Exception as exc:  # noqa: BLE001 - Drive権限/接続失敗を502で返す
        raise HTTPException(status_code=502, detail=f"Drive取込に失敗: {exc}") from exc
    if not payload.dry_run:
        write_audit(
            repo, actor=actor, action="proposal.import_drive",
            target=payload.folder_id,
            detail=f"created={summary.created} updated={summary.updated}",
        )
    return summary


@router.get("/proposals/matrix")
def proposal_matrix():
    repo = get_repo()
    proposals = repo.list_proposals(status=None)
    return build_proposal_matrix(proposals)


@router.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: str):
    p = get_repo().get_proposal(proposal_id)
    if p is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    return p


@router.post("/proposals")
def create_proposal(
    payload: ProposalCreate,
    x_goog_authenticated_user_email: str | None = Header(default=None),
):
    repo = get_repo()
    now = datetime.now()
    actor = _actor(x_goog_authenticated_user_email)
    proposal = Proposal(
        proposal_id=f"prop_{uuid.uuid4().hex[:10]}",
        **payload.model_dump(),
        history=[ProposalHistory(at=now, stage=payload.stage, by=actor)],
    )
    repo.upsert_proposal(proposal)
    write_audit(
        repo,
        actor=actor,
        action="proposal.create",
        target=proposal.proposal_id,
        detail=proposal.title,
    )
    return proposal


@router.put("/proposals/{proposal_id}")
def update_proposal(proposal_id: str, payload: ProposalUpdate):
    repo = get_repo()
    p = repo.get_proposal(proposal_id)
    if p is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    data = payload.model_dump(exclude_unset=True)
    updated = p.model_copy(update=data)
    repo.upsert_proposal(updated)
    return updated


@router.post("/proposals/{proposal_id}/format-check")
def format_check(proposal_id: str):
    repo = get_repo()
    p = repo.get_proposal(proposal_id)
    if p is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    result = run_format_check(p)
    result.checked_at = datetime.now()
    p.format_check = result
    repo.upsert_proposal(p)
    return result


@router.post("/proposals/{proposal_id}/llm-review")
def llm_review(
    proposal_id: str,
    x_goog_authenticated_user_email: str | None = Header(default=None),
):
    repo = get_repo()
    p = repo.get_proposal(proposal_id)
    if p is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    review = review_proposal(p.content or "")
    if review is None:
        raise HTTPException(
            status_code=503,
            detail="LLMレビューを生成できませんでした（本文が空、またはLLM未設定/失敗）。",
        )
    review.reviewed_at = datetime.now()
    p.llm_review = review
    repo.upsert_proposal(p)
    write_audit(
        repo, actor=_actor(x_goog_authenticated_user_email),
        action="proposal.llm_review", target=proposal_id,
    )
    return review


class ApprovalAction(BaseModel):
    comment: str | None = None


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(
    proposal_id: str,
    payload: ApprovalAction,
    x_goog_authenticated_user_email: str | None = Header(default=None),
):
    """専務理事が議案を承認。承認時はレビュー段階なら五役会へ進める。"""
    repo = get_repo()
    p = repo.get_proposal(proposal_id)
    if p is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    actor = _actor(x_goog_authenticated_user_email)
    now = datetime.now()
    p.sed_approval = SedApproval(status="approved", by=actor, at=now, comment=payload.comment)
    if p.stage == ProposalStage.sed_review:
        p.stage = ProposalStage.goyaku
        p.history.append(ProposalHistory(at=now, stage=p.stage, by=actor))
    repo.upsert_proposal(p)
    write_audit(repo, actor=actor, action="proposal.approve", target=proposal_id)
    return p


@router.post("/proposals/{proposal_id}/return")
def return_proposal(
    proposal_id: str,
    payload: ApprovalAction,
    x_goog_authenticated_user_email: str | None = Header(default=None),
):
    """専務理事が議案を差戻し。資料提出段階へ戻す。"""
    repo = get_repo()
    p = repo.get_proposal(proposal_id)
    if p is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    actor = _actor(x_goog_authenticated_user_email)
    now = datetime.now()
    p.sed_approval = SedApproval(status="returned", by=actor, at=now, comment=payload.comment)
    p.stage = ProposalStage.submitted
    p.history.append(ProposalHistory(at=now, stage=p.stage, by=actor))
    repo.upsert_proposal(p)
    write_audit(
        repo, actor=actor, action="proposal.return",
        target=proposal_id, detail=payload.comment,
    )
    return p


class StageUpdate(BaseModel):
    stage: ProposalStage


@router.post("/proposals/{proposal_id}/stage")
def set_stage(
    proposal_id: str,
    payload: StageUpdate,
    x_goog_authenticated_user_email: str | None = Header(default=None),
):
    repo = get_repo()
    p = repo.get_proposal(proposal_id)
    if p is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    actor = _actor(x_goog_authenticated_user_email)
    now = datetime.now()
    p.stage = payload.stage
    p.history.append(ProposalHistory(at=now, stage=payload.stage, by=actor))
    repo.upsert_proposal(p)
    write_audit(
        repo, actor=actor, action="proposal.stage",
        target=proposal_id, detail=payload.stage.value,
    )
    return p
