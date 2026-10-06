"""データレイクのビューSQLを、サンプルイベントで BigQuery 上で検証する（開発用）。

`infra/bigquery/views/*.sql` の `events_raw` をリテラルの CTE に差し替えた1本のクエリを作る。
データセットが無くても実行でき、読むのはリテラルだけなので課金も発生しない。
ビューを追加・変更したら、セットアップ前にこれで構文と結果を確認する。

使い方（リポジトリ直下・direnv 有効な状態で）:
  .venv/bin/python scripts/check_lake_views.py v_messages          # SQL を表示
  .venv/bin/python scripts/check_lake_views.py v_messages --run    # bq で実行して結果を表示
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIEWS = sorted((ROOT / "infra" / "bigquery" / "views").glob("*.sql"))
PROJECT = "jci-sed-agent"
REGION = "asia-northeast1"

_SOURCE = {"type": "group", "groupId": "G1", "userId": "U1"}


def _webhook(eid: str, payload: dict) -> dict:
    payload = {"webhookEventId": eid, "source": _SOURCE, **payload}
    return {"v": 1, "id": eid, "kind": "webhook", "destination": "Ubot",
            "received_at": "2026-10-06T12:00:00.000+09:00", "payload": payload}


#: ビューの挙動を一通り通すサンプル（重複・メンション・ファイル・送信取消・参加・グループ名）
SAMPLES = [
    _webhook("e1", {"type": "message", "timestamp": 1791241200000,
                    "deliveryContext": {"isRedelivery": False},
                    "message": {"type": "text", "id": "m1", "text": "おはようございます",
                                "mention": {"mentionees": [{"index": 0, "length": 3,
                                                            "type": "user", "isSelf": True}]}}}),
    _webhook("e1", {"type": "message", "timestamp": 1791241200000,
                    "message": {"type": "text", "id": "m1", "text": "重複配信"}}),
    _webhook("e2", {"type": "message", "timestamp": 1791241260000,
                    "message": {"type": "file", "id": "m2", "fileName": "議案.pdf",
                                "fileSize": 12345}}),
    _webhook("e3", {"type": "unsend", "timestamp": 1791241300000, "unsend": {"messageId": "m2"}}),
    _webhook("e4", {"type": "join", "timestamp": 1791241000000,
                    "source": {"type": "group", "groupId": "G1"}}),
    {"v": 1, "id": "gp1", "kind": "group_profile", "received_at": "2026-10-06T12:05:00.000+09:00",
     "payload": {"groupId": "G1", "groupName": "猪苗代JC 理事会", "pictureUrl": "https://x"}},
]


def _view_name(sql: str) -> str:
    return re.search(r"\.(v_\w+)`", sql).group(1)


def _body(sql: str) -> str:
    sql = re.sub(r"^--.*$", "", sql, flags=re.M)
    sql = re.sub(r"CREATE OR REPLACE VIEW `[^`]+` AS", "", sql)
    return re.sub(r"`\$\{PROJECT\}\.\$\{DATASET\}\.(\w+)`", r"\1", sql).strip()


def _row(i: int, env: dict) -> str:
    data = json.dumps(env, ensure_ascii=False).replace("'", "\\'")
    return (f"SELECT 'sub' AS subscription_name, 'm{i}' AS message_id, "
            f"TIMESTAMP_ADD(TIMESTAMP '2026-10-06 03:00:00+00', INTERVAL {i} SECOND) "
            f"AS publish_time, PARSE_JSON('{data}') AS data, JSON '{{}}' AS attributes")


def build_query(target: str, samples: list[dict] = SAMPLES) -> str:
    ctes = ["events_raw AS (" + " UNION ALL ".join(_row(i, e) for i, e in enumerate(samples))
            + ")"]
    for path in VIEWS:
        sql = path.read_text()
        name = _view_name(sql)
        ctes.append(f"{name} AS (\n{_body(sql)}\n)")
        if name == target:
            return "WITH " + ",\n".join(ctes) + f"\nSELECT * FROM {target}"
    raise SystemExit(f"ビューが見つかりません: {target}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("view", help="v_messages など")
    parser.add_argument("--run", action="store_true", help="bq で実行する")
    args = parser.parse_args()
    query = build_query(args.view)
    if not args.run:
        print(query)
        return 0
    return subprocess.run(
        ["bq", f"--project_id={PROJECT}", f"--location={REGION}", "query",
         "--use_legacy_sql=false", "--format=pretty", "--max_rows=50"],
        input=query, text=True, check=False,
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
