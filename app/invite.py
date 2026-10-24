"""招待コードの発行と本人確認（LINEユーザー紐付け）。

docs/mvp-design.md §4.1。会員ごとに使い切り・有効期限つきコードを発行し、
友だち追加後の入力で照合して line_user_id を紐付ける。総当たり対策として
LINEユーザー単位で失敗回数をカウントし、上限超過でロックする。
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import InviteCode, LinkState, Member
from .repository import Repository

# 紛らわしい文字（0/O, 1/I 等）を除いた英数字
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MAX_FAILED = 3
DEFAULT_TTL_DAYS = 30


def generate_code(length: int = 8) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def normalize_code(raw: str) -> str:
    return raw.strip().upper().replace(" ", "").replace("-", "")


def issue_invite(
    repo: Repository,
    member_id: str,
    *,
    now: datetime,
    ttl_days: int = DEFAULT_TTL_DAYS,
    length: int = 8,
) -> InviteCode:
    """会員に新しい招待コードを発行して保存する。"""
    code = generate_code(length)
    invite = InviteCode(
        code=code,
        member_id=member_id,
        expires_at=now + timedelta(days=ttl_days),
    )
    repo.save_invite_code(invite)
    return invite


def invite_url(code: str, line_basic_id: str) -> str:
    """招待コードを添えて友だち追加へ誘導するURL（basic_id は @ を含む想定）。"""
    bid = line_basic_id.lstrip("@")
    return f"https://line.me/R/ti/p/@{bid}?code={code}"


@dataclass
class LinkResult:
    ok: bool
    message: str
    member: Member | None = None
    reason: str | None = None  # already_linked / locked / invalid / used / expired


def verify_and_link(
    repo: Repository,
    line_user_id: str,
    code_input: str,
    *,
    now: datetime,
) -> LinkResult:
    """招待コードを検証し、成功なら会員に line_user_id を紐付ける。"""
    state = repo.get_link_state(line_user_id) or LinkState(line_user_id=line_user_id)

    if state.locked:
        return LinkResult(
            ok=False,
            reason="locked",
            message="試行回数の上限に達したため、ロックされています。事務局までご連絡ください。",
        )

    # 既に紐付け済み
    existing = repo.get_member_by_line_user_id(line_user_id)
    if existing:
        return LinkResult(
            ok=True,
            member=existing,
            reason="already_linked",
            message=f"{existing.name} さんとして既に登録済みです。",
        )

    code = repo.get_invite_code(normalize_code(code_input))
    invalid_reason = _validate_code(code, now)
    if invalid_reason:
        return _register_failure(repo, state, invalid_reason)

    # 紐付け実行
    member = repo.get_member(code.member_id)
    if member is None:
        return _register_failure(repo, state, "invalid")

    member.line_user_id = line_user_id
    member.linked_at = now
    repo.upsert_member(member)

    code.used_at = now
    repo.save_invite_code(code)

    state.failed_attempts = 0
    repo.save_link_state(state)

    # 立場に合ったリッチメニュー（理事・五役は作業のボタン付き）に付け替える
    from . import rich_menu_link

    rich_menu_link.link(repo, member)

    return LinkResult(
        ok=True,
        member=member,
        message=(
            f"{member.name} さん、登録が完了しました！"
            "今後はこちらから出欠連絡などをお送りします。"
        ),
    )


def _validate_code(code: InviteCode | None, now: datetime) -> str | None:
    if code is None:
        return "invalid"
    if code.used_at is not None:
        return "used"
    if code.expires_at < now:
        return "expired"
    return None


def _register_failure(repo: Repository, state: LinkState, reason: str) -> LinkResult:
    state.failed_attempts += 1
    messages = {
        "invalid": "招待コードが無効です。",
        "used": "この招待コードは既に使用済みです。",
        "expired": "この招待コードは有効期限が切れています。",
    }
    msg = messages.get(reason, "招待コードが無効です。")
    if state.failed_attempts >= MAX_FAILED:
        state.locked = True
        repo.save_link_state(state)
        return LinkResult(
            ok=False,
            reason="locked",
            message=msg + "\n試行回数の上限に達したためロックしました。事務局までご連絡ください。",
        )
    repo.save_link_state(state)
    remaining = MAX_FAILED - state.failed_attempts
    return LinkResult(
        ok=False,
        reason=reason,
        message=msg + f"\n事務局から配布されたコードをご確認ください（残り{remaining}回）。",
    )
