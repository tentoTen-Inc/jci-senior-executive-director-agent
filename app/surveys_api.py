"""アンケートAPI（docs/survey-design.md §5, F7）。

配信は現行の Google フォームを維持し、本APIは取込・集計・要約を担う。
未提出者の催促（P4-2）とレポート通知（P4-3）は後続フェーズ。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from . import forms
from .audit import write_audit
from .deps import get_repo
from .inference import record_inference
from .llm import digest_survey
from .llm import model_name as llm_model_name
from .models import InferenceUsage
from .survey_agg import aggregate_survey, free_texts
from .survey_import import build_survey

router = APIRouter(tags=["surveys"])

SURVEY_KINDS = ("internal", "external")


def _actor(email: str | None) -> str:
    return email or "unknown"


class SurveyImport(BaseModel):
    form_id: str
    kind: str = "internal"
    event_id: str | None = None
    dry_run: bool = False


@router.post("/surveys/import-form")
def import_form(
    payload: SurveyImport,
    x_goog_authenticated_user_email: str | None = Header(default=None),
):
    """GoogleフォームIDから設問・回答を取込む。同じ form_id は更新（冪等）。"""
    if payload.kind not in SURVEY_KINDS:
        raise HTTPException(status_code=400, detail="kind は internal か external です。")
    repo = get_repo()
    try:
        raw = forms.fetch_form(payload.form_id)
    except Exception as exc:  # noqa: BLE001 - 権限/接続失敗は502で返す
        raise HTTPException(
            status_code=502,
            detail=f"フォームの取得に失敗しました（drive-readerへの共有を確認）: {exc}",
        ) from exc

    existing = repo.get_survey_by_form_id(payload.form_id)
    survey = build_survey(
        raw,
        form_id=payload.form_id,
        kind=payload.kind,
        event_id=payload.event_id,
        now=datetime.now(),
        existing=existing,
    )
    if payload.dry_run:
        return {
            "dry_run": True,
            "title": survey.title,
            "questions": len(survey.questions),
            "responses": len(survey.responses),
        }

    repo.upsert_survey(survey)
    write_audit(
        repo, actor=_actor(x_goog_authenticated_user_email), action="survey.import",
        target=survey.survey_id,
        detail=f"{survey.title} responses={len(survey.responses)}",
    )
    return survey


@router.get("/surveys")
def list_surveys(kind: str | None = None):
    return get_repo().list_surveys(kind=kind)


@router.get("/surveys/{survey_id}")
def get_survey(survey_id: str):
    """設問・回答と集計値を返す。"""
    survey = get_repo().get_survey(survey_id)
    if survey is None:
        raise HTTPException(status_code=404, detail="survey not found")
    return {"survey": survey, "aggregate": aggregate_survey(survey)}


@router.post("/surveys/{survey_id}/sync")
def sync_survey(
    survey_id: str,
    x_goog_authenticated_user_email: str | None = Header(default=None),
):
    """回答を再取得する（設問の変更も取り込む）。"""
    repo = get_repo()
    survey = repo.get_survey(survey_id)
    if survey is None:
        raise HTTPException(status_code=404, detail="survey not found")
    return import_form(
        SurveyImport(form_id=survey.form_id, kind=survey.kind, event_id=survey.event_id),
        x_goog_authenticated_user_email,
    )


@router.post("/surveys/{survey_id}/digest")
def build_digest(
    survey_id: str,
    x_goog_authenticated_user_email: str | None = Header(default=None),
):
    """自由記述を Gemini で要約・分類する（F7-2）。"""
    repo = get_repo()
    survey = repo.get_survey(survey_id)
    if survey is None:
        raise HTTPException(status_code=404, detail="survey not found")

    texts = free_texts(aggregate_survey(survey))
    if not texts:
        raise HTTPException(status_code=400, detail="自由記述の回答がありません。")

    now = datetime.now()
    outcome = digest_survey(texts)
    if outcome is None:
        record_inference(
            repo, kind="survey_digest", usage=InferenceUsage(model=llm_model_name()),
            now=now, target=survey_id, ok=False, error="generation_failed",
        )
        raise HTTPException(
            status_code=503, detail="要約を生成できませんでした（LLM未設定/失敗）。"
        )
    record_inference(
        repo, kind="survey_digest", usage=outcome.usage, now=now, target=survey_id
    )

    digest = outcome.digest
    digest.generated_at = now
    survey.digest = digest
    repo.upsert_survey(survey)
    write_audit(
        repo, actor=_actor(x_goog_authenticated_user_email), action="survey.digest",
        target=survey_id, detail=f"texts={len(texts)}",
    )
    return survey
