"""LINE のメニュー・LIFF での立場（docs/liff-menu-design.md §2）。

- 五役（ops）: 理事長・副理事長・専務理事・監事・事務局。運営のタブ（予定の登録・承認待ち等）
- 理事（board）: それ以外の理事メンバー（委員長・直前理事長）。資料の作業・委員会の議案
- 会員: それ以外
"""
from __future__ import annotations

from . import committee_docs
from .models import Member, MemberType

OPS_ROLES = ("理事長", "副理事長", "専務理事", "監事")


def is_ops(member: Member) -> bool:
    """五役・監事・事務局（運営のタブを使う人）。"""
    return member.officer_role in OPS_ROLES or member.member_type == MemberType.office


def is_board(member: Member) -> bool:
    """理事メンバー（資料の作業・委員会の議案を使う人）。五役も含む。"""
    return committee_docs.is_board_member(member)


def proposal_committees(member: Member, committees: list[str]) -> list[str]:
    """委員会の議案を見られる委員会（五役・事務局は全委員会、理事は自分の委員会）。"""
    if is_ops(member):
        return list(committees)
    if not is_board(member):
        return []
    own = committee_docs.own_committees(member)
    return [c for c in committees if c in own]
