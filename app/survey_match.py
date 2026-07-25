"""アンケート回答と会員の突き合わせ（docs/survey-design.md §4, F7-3）。

優先順: ①回答者メール = Member.email ②氏名一致（空白除去）③照合不能。
照合不能は件数を可視化し、催促判定には使わない（誤催促を避ける）。
"""
from __future__ import annotations

from pydantic import BaseModel

from .events import resolve_scope
from .models import Member, Survey, TargetScope
from .repository import Repository


def normalize_name(name: str | None) -> str:
    """氏名の表記ゆれ（空白・全角空白）を除去して比較用に正規化する。"""
    if not name:
        return ""
    return name.replace(" ", "").replace("　", "").strip()


class MatchResult(BaseModel):
    matched: dict[str, str] = {}  # response_id -> member_id
    unmatched_response_ids: list[str] = []


def match_responses(repo: Repository, survey: Survey) -> MatchResult:
    """回答を会員に突き合わせる。"""
    members = repo.list_members()
    by_email = {
        (m.contact.email or "").strip().lower(): m.member_id
        for m in members
        if (m.contact.email or "").strip()
    }
    by_name: dict[str, str] = {}
    for m in members:
        key = normalize_name(m.name)
        if key:
            by_name.setdefault(key, m.member_id)

    matched: dict[str, str] = {}
    unmatched: list[str] = []
    for r in survey.responses:
        email = (r.respondent_email or "").strip().lower()
        member_id = by_email.get(email) if email else None
        if member_id is None:
            member_id = by_name.get(normalize_name(r.respondent_name))
        if member_id is None:
            unmatched.append(r.response_id)
        else:
            matched[r.response_id] = member_id
    return MatchResult(matched=matched, unmatched_response_ids=unmatched)


class PendingResult(BaseModel):
    total_targets: int
    answered: int  # 照合できた回答者数（実人数）
    unmatched: int  # 会員に紐づけられなかった回答数
    pending_member_ids: list[str]
    pending: list[dict]  # {member_id, name}


def survey_targets(repo: Repository, survey: Survey) -> list[Member]:
    """アンケートの対象者。イベントが紐づいていればその対象範囲、無ければ配信可能な全会員。"""
    if survey.event_id:
        event = repo.get_event(survey.event_id)
        if event is not None:
            return resolve_scope(repo, event.target_scope)
    return resolve_scope(repo, TargetScope())


def pending_members(repo: Repository, survey: Survey) -> PendingResult:
    """未提出者（対象者 − 照合できた回答者）を返す。"""
    result = match_responses(repo, survey)
    answered_ids = set(result.matched.values())
    targets = survey_targets(repo, survey)
    pending = [m for m in targets if m.member_id not in answered_ids]
    return PendingResult(
        total_targets=len(targets),
        answered=len(answered_ids),
        unmatched=len(result.unmatched_response_ids),
        pending_member_ids=[m.member_id for m in pending],
        pending=[{"member_id": m.member_id, "name": m.name} for m in pending],
    )


def apply_matches(repo: Repository, survey: Survey) -> Survey:
    """照合結果を Survey に書き戻して保存する（画面表示・再集計用）。"""
    result = match_responses(repo, survey)
    for r in survey.responses:
        r.member_id = result.matched.get(r.response_id)
    repo.upsert_survey(survey)
    return survey
