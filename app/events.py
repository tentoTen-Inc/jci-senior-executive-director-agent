"""イベントと対象者解決（docs/mvp-design.md §3/§4.2）。"""
from __future__ import annotations

from .models import Event, Member, TargetScope, TargetScopeKind
from .repository import Repository


def resolve_targets(repo: Repository, event: Event) -> list[Member]:
    """イベントの対象範囲から配信対象の会員一覧を返す（配信可能な会員のみ）。"""
    return resolve_scope(repo, event.target_scope)


def resolve_scope(repo: Repository, scope: TargetScope) -> list[Member]:
    """対象範囲（全員/委員会/役職/任意指定）から配信対象の会員一覧を返す。

    イベント以外（対外連絡の配信など）からも使う。
    """
    members = [m for m in repo.list_members() if m.is_deliverable]

    if scope.kind == TargetScopeKind.all:
        return members
    if scope.kind == TargetScopeKind.committee:
        return [m for m in members if m.committee in scope.value]
    if scope.kind == TargetScopeKind.officer:
        return [m for m in members if m.officer_role in scope.value]
    if scope.kind == TargetScopeKind.custom:
        return [m for m in members if m.member_id in scope.value]
    return members
