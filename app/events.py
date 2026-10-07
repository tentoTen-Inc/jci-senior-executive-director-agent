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
        wanted = set(scope.value)
        return [m for m in members if m.committee_names & wanted]  # 兼務を含む
    if scope.kind == TargetScopeKind.officer:
        return [m for m in members if m.officer_role in scope.value]
    if scope.kind == TargetScopeKind.custom:
        return [m for m in members if m.member_id in scope.value]
    return members


def in_operation(event: Event, operation_start) -> bool:
    """運用開始日（年度の始まり）以降のイベントか。未設定なら常に True。

    運用開始日より前のデータは消さずに残し、一覧・集計・カレンダー取込の対象から外す。
    """
    return operation_start is None or event.datetime_start.date() >= operation_start


def operating_events(repo: Repository, events: list[Event]) -> list[Event]:
    start = repo.get_settings().operation_start
    return [e for e in events if in_operation(e, start)]
