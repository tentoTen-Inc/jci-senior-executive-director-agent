"""会員ごとに立場に合ったリッチメニューを結びつける（docs/liff-menu-design.md §2）。

メニューの ID は Settings.rich_menus（member / board / officer / officer_ops）に入っている
（scripts/setup_rich_menu.py が作成して登録する）。未登録なら何もしない。
LINE 連携の承認・招待コードでの連携・組織図の取込のときに呼ぶ。`_post` がテストのモック境界。
"""
from __future__ import annotations

import logging
import urllib.request

from . import config, roles
from .models import Member
from .repository import Repository
from .rich_menu import BOARD, MEMBER, OFFICER

logger = logging.getLogger("jci-agent.rich_menu_link")

API = "https://api.line.me/v2/bot/user"


def kind_for(member: Member) -> str:
    """会員の立場に合ったメニューの種類。"""
    if roles.is_ops(member):
        return OFFICER  # 五役・監事・事務局（「みんな」「運営」のタブ。1枚目を結ぶ）
    if roles.is_board(member):
        return BOARD  # 委員長・直前理事長（資料の作業・委員会の議案）
    return MEMBER


def _post(url: str, token: str) -> None:
    """LINE API に POST する。テストでモックする境界。"""
    req = urllib.request.Request(url, method="POST",
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10):
        pass


def link(repo: Repository, member: Member) -> str | None:
    """会員の LINE に立場のメニューを結びつけ、種類を返す（未連携・未登録・失敗なら None）。"""
    if not member.line_user_id:
        return None
    kind = kind_for(member)
    menu_id = repo.get_settings().rich_menus.get(kind)
    token = config.line_channel_access_token()
    if not menu_id or not token:
        return None
    try:
        _post(f"{API}/{member.line_user_id}/richmenu/{menu_id}", token)
    except Exception:  # noqa: BLE001 - メニューの付け替えに失敗しても連携・取込は止めない
        logger.warning("リッチメニューを結びつけられませんでした: %s", member.member_id)
        return None
    return kind


def link_all(repo: Repository) -> dict[str, int]:
    """LINE 連携済みの全会員にメニューを結びつけ直す（種類ごとの件数）。"""
    counts: dict[str, int] = {}
    for member in repo.list_members():
        kind = link(repo, member)
        if kind:
            counts[kind] = counts.get(kind, 0) + 1
    return counts
