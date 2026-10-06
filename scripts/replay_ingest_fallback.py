"""Pub/Sub への publish に失敗して Cloud Logging に退避した LINE イベントを再投入する。

Webhook は publish に失敗すると、封筒を `LINE_INGEST_FALLBACK {json}` の1行としてログに残す
（app/lake.py）。このスクリプトはその行を拾い、同じ封筒のまま publish し直す。
封筒の id（webhookEventId）が同じなので、二重に入ってもビューで1件にまとまる。

使い方（リポジトリ直下・direnv 有効な状態で）:
  gcloud logging read \
    'resource.labels.service_name="jci-sed-agent" AND textPayload:"LINE_INGEST_FALLBACK"' \
    --project jci-sed-agent --freshness=7d --format=json > fallback.json
  LINE_EVENTS_TOPIC=line-events .venv/bin/python scripts/replay_ingest_fallback.py fallback.json
  （--dry-run で件数だけ確認）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import lake  # noqa: E402

BATCH = 100


def extract_envelopes(entries: list[dict]) -> list[dict]:
    """ログエントリから封筒を取り出す（id で重複を除く）。"""
    found: dict[str, dict] = {}
    for entry in entries:
        text = entry.get("textPayload") or ""
        marker = text.find(lake.FALLBACK_MARKER)
        if marker < 0:
            continue
        try:
            env = json.loads(text[marker + len(lake.FALLBACK_MARKER):].strip())
        except json.JSONDecodeError:
            continue
        if isinstance(env, dict) and env.get("id"):
            found[env["id"]] = env
    return list(found.values())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("log_json", help="gcloud logging read --format=json の出力ファイル")
    parser.add_argument("--dry-run", action="store_true", help="件数を表示するだけ")
    args = parser.parse_args()

    envelopes = extract_envelopes(json.loads(Path(args.log_json).read_text()))
    print(f"再投入対象: {len(envelopes)} 件")
    if args.dry_run or not envelopes:
        return 0
    if not lake.is_enabled():
        print("LINE_EVENTS_TOPIC が未設定です。", file=sys.stderr)
        return 1
    failed = 0
    for i in range(0, len(envelopes), BATCH):
        if not lake.publish(envelopes[i:i + BATCH]):
            failed += len(envelopes[i:i + BATCH])
    print(f"完了: 成功 {len(envelopes) - failed} 件 / 失敗 {failed} 件")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
