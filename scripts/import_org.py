#!/usr/bin/env python3
"""年度の組織図（JSON）を会員データに取り込む CLI（既定は確認だけ。--apply で書き込み）。

使い方（リポジトリ直下・direnv 有効な状態で）:
  .venv/bin/python scripts/import_org.py data/roster-2027.json                 # 変更点の確認
  .venv/bin/python scripts/import_org.py data/roster-2027.json --apply \\
      --operation-start 2027-01-01 --deactivate-missing                       # 書き込み

- 既存の会員とは氏名で突き合わせ、member_id と LINE 連携を引き継ぐ（app/org_import.py）。
- --operation-start: 運用開始日。これより前のイベントは一覧・集計・カレンダー取込の
  対象外になる（データは消さない）。
- 認証は gcloud のログイン（案件ストア）のアクセストークンを使う（ADC の設定は不要）。
- 実データの JSON はリポジトリにコミットしない（data/ は .gitignore 済み）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.org_import import RosterEntry, plan  # noqa: E402

PROJECT = "jci-sed-agent"


def _repo():
    from google.cloud import firestore
    from google.oauth2.credentials import Credentials

    from app.firestore_repo import FirestoreRepository

    token = subprocess.run(
        ["gcloud", "auth", "print-access-token"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    return FirestoreRepository(client=firestore.Client(project=PROJECT,
                                                       credentials=Credentials(token)))


def _describe(member) -> str:
    seats = "・".join(f"{s.committee}{f'({s.role})' if s.role else ''}" for s in member.committees)
    parts = [member.member_type.value]
    if member.officer_role:
        parts.append(member.officer_role)
    if seats:
        parts.append(seats)
    if member.secondments:
        parts.append("出向:" + "・".join(f"{s.org} {s.role}" for s in member.secondments))
    if member.line_user_id:
        parts.append("LINE連携済み")
    return " / ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("path", help="組織図の JSON（RosterEntry の配列）")
    ap.add_argument("--apply", action="store_true", help="Firestore に書き込む")
    ap.add_argument("--deactivate-missing", action="store_true",
                    help="名簿に無い既存会員を inactive にする（削除はしない）")
    ap.add_argument("--operation-start", type=date.fromisoformat, default=None,
                    help="運用開始日（例: 2027-01-01）を設定する")
    args = ap.parse_args()

    entries = [RosterEntry.model_validate(x) for x in json.loads(Path(args.path).read_text())]
    repo = _repo()
    changes = plan(repo.list_members(), entries, deactivate_missing=args.deactivate_missing)

    labels = {"create": "新規", "update": "更新", "unchanged": "変更なし", "deactivate": "無効化"}
    for c in changes:
        detail = f"（{', '.join(c.fields)}）" if c.fields else ""
        print(f"[{labels[c.action]}] {c.member.member_id:>4} {c.member.name}: "
              f"{_describe(c.member)} {detail}")
    counts = {k: sum(1 for c in changes if c.action == k) for k in labels}
    print("合計: " + " / ".join(f"{labels[k]} {n}" for k, n in counts.items()))
    if args.operation_start:
        print(f"運用開始日: {args.operation_start}")

    if not args.apply:
        print("\n確認のみです。書き込むには --apply を付けて実行してください。")
        return 0
    for c in changes:
        if c.action != "unchanged":
            repo.upsert_member(c.member)
    if args.operation_start:
        settings = repo.get_settings()
        settings.operation_start = args.operation_start
        repo.save_settings(settings)
    print("\n書き込みました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
