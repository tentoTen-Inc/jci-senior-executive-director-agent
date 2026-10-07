"""年度の組織図（会員・役職・委員会・出向）の取込（2027年度の切り替え）。

組織図を JSON にしたもの（`data/roster-2027.json` 等・リポジトリには入れない）を読み、
既存の会員と**氏名で突き合わせて**更新する。突き合わせた会員は member_id と LINE 連携を引き継ぐ。

- 委員会は兼務を含めて `committees` に入れ、先頭を主たる所属（`committee`/`committee_role`）にする。
- 名簿に無い既存会員は、指定があれば `inactive` にする（削除はしない）。
- 実際の書き込みは呼び出し側（scripts/import_org.py）。ここは差分の計算だけ。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from .models import CommitteeSeat, Member, MemberStatus, MemberType, Secondment


class RosterEntry(BaseModel):
    name: str
    kana: str | None = None
    member_type: MemberType = MemberType.regular
    officer_role: str | None = None
    committees: list[CommitteeSeat] = Field(default_factory=list)
    secondments: list[Secondment] = Field(default_factory=list)


@dataclass
class Change:
    action: str  # create | update | unchanged | deactivate
    member: Member
    fields: list[str] = field(default_factory=list)


def name_key(name: str) -> str:
    """突き合わせ用の氏名（空白の有無・全角半角の違いを無視）。"""
    return re.sub(r"\s+", "", name.replace("　", " "))


def _next_ids(existing: list[Member]):
    numbers = [int(m.member_id[1:]) for m in existing if re.fullmatch(r"m\d+", m.member_id)]
    n = max(numbers, default=0)
    while True:
        n += 1
        yield f"m{n}"


def _apply(member: Member, entry: RosterEntry) -> list[str]:
    """名簿の内容を会員に反映し、変わった項目名を返す（LINE 連携・連絡先は触らない）。"""
    primary = entry.committees[0] if entry.committees else None
    target = {
        "name": entry.name,
        "member_type": entry.member_type,
        "status": MemberStatus.active,
        "officer_role": entry.officer_role,
        "committee": primary.committee if primary else None,
        "committee_role": primary.role if primary else None,
        "committees": entry.committees,
        "secondments": entry.secondments,
    }
    if entry.kana:
        target["kana"] = entry.kana
    changed = []
    for key, value in target.items():
        if getattr(member, key) != value:
            setattr(member, key, value)
            changed.append(key)
    return changed


def plan(existing: list[Member], entries: list[RosterEntry], *,
         deactivate_missing: bool = False) -> list[Change]:
    """名簿を既存の会員に当てたときの変更点を計算する（書き込みはしない）。"""
    by_name = {name_key(m.name): m for m in existing}
    seen: set[str] = set()
    ids = _next_ids(existing)
    changes: list[Change] = []
    for entry in entries:
        key = name_key(entry.name)
        if key in seen:
            raise ValueError(f"名簿に同じ氏名が2回あります: {entry.name}")
        seen.add(key)
        current = by_name.get(key)
        if current is None:
            member = Member(member_id=next(ids), name=entry.name)
            _apply(member, entry)
            changes.append(Change("create", member))
        else:
            member = current.model_copy(deep=True)
            fields = _apply(member, entry)
            changes.append(Change("update" if fields else "unchanged", member, fields))
    if deactivate_missing:
        for m in existing:
            if name_key(m.name) not in seen and m.status != MemberStatus.inactive:
                member = m.model_copy(update={"status": MemberStatus.inactive})
                changes.append(Change("deactivate", member, ["status"]))
    return changes
