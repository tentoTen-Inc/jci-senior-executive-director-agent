#!/usr/bin/env python3
"""委員会サマリーの議案に、提出締切と上程先（日程調整中の会議）を設定する CLI。

既定は確認だけ（何も書き込まない）。--apply で書き込む（docs/proposal-ledger-design.md §10）。

使い方（リポジトリ直下・direnv 有効な状態で）:
  .venv/bin/python scripts/set_summary_deadline.py                       # 対象と変更点の確認
  .venv/bin/python scripts/set_summary_deadline.py --apply               # 書き込み

- 対象: 台帳の open の議案のうち、種類が「年間計画（サマリー）」のもの（proposal_sync が作る）。
- 既定値: 提出締切 2026-10-31 23:59、上程先 = 日程調整 poll_08649098fc（第2回五役予定者会議）。
- 認証は gcloud のログイン（案件ストア）のアクセストークンを使う。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.proposal_sync import SUMMARY_TYPE  # noqa: E402

PROJECT = "jci-sed-agent"
DEFAULT_DEADLINE = "2026-10-31T23:59"
DEFAULT_POLL = "poll_08649098fc"


def _repo():
    from google.cloud import firestore
    from google.oauth2.credentials import Credentials

    from app.firestore_repo import FirestoreRepository

    token = subprocess.run(
        ["gcloud", "auth", "print-access-token"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    return FirestoreRepository(client=firestore.Client(project=PROJECT,
                                                       credentials=Credentials(token)))


def plan(repo, deadline: datetime, poll_id: str) -> list:
    """設定する議案（変更後の値）の一覧。上程先の日程調整が無ければ止める。"""
    if repo.get_poll(poll_id) is None:
        raise SystemExit(f"日程調整 {poll_id} が見つかりません。")
    targets = [p for p in repo.list_proposals(status="open") if p.doc_type == SUMMARY_TYPE]
    return [p.model_copy(update={
        "deadlines": p.deadlines.model_copy(update={"submit": deadline}),
        "meeting_poll_id": poll_id,
    }) for p in targets]


def main(argv: list[str] | None = None, repo=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--deadline", default=DEFAULT_DEADLINE, help="提出締切（日本時間）")
    ap.add_argument("--poll", default=DEFAULT_POLL, help="上程先の会議の日程調整 ID")
    ap.add_argument("--apply", action="store_true", help="書き込む（無ければ確認だけ）")
    args = ap.parse_args(argv)
    repo = repo or _repo()
    deadline = datetime.fromisoformat(args.deadline)
    poll = repo.get_poll(args.poll)
    updates = plan(repo, deadline, args.poll)
    print(f"上程先: {poll.title}（{args.poll}）  提出締切: {deadline:%Y-%m-%d %H:%M}")
    if not updates:
        print("対象の議案（年間計画（サマリー））がありません。同期（tick）が済んでいるか確かめてください。")
        return 1
    for p in updates:
        before = repo.get_proposal(p.proposal_id)
        print(f"- {p.title}（{p.proposal_id}・{p.stage}）"
              f" 締切 {before.deadlines.submit} → {p.deadlines.submit}"
              f" / 上程先 {before.meeting_poll_id} → {p.meeting_poll_id}")
    if not args.apply:
        print("\n確認だけです。書き込むときは --apply を付けてください。")
        return 0
    for p in updates:
        repo.upsert_proposal(p)
    print(f"\n{len(updates)} 件に設定しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
