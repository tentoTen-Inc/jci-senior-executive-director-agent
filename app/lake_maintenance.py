"""データレイクの定期メンテナンス（docs/datalake-design.md §4.4）。

送信取消されたメッセージの本文（とファイルから抽出したテキスト）を `events_raw` から消す。
tick（毎時）から呼ぶ。
- LINE の送信取消は送信から24時間以内なので、直近数日のパーティションだけを対象にして安く済ませる。
- 書き込まれた直後の行は DML で変更できないことがあるため、1時間より前の行だけを対象にする
  （それまでの間も、AI 用ビュー `v_messages_for_ai` からは即座に除外されている）。

`_bq_query` がテストのモック境界。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from . import config, lake

logger = logging.getLogger("jci-agent.lake_maintenance")

LOOKBACK_DAYS = 3
SETTLE_HOURS = 1


def dataset() -> str:
    return os.environ.get("LAKE_DATASET", "line_lake")


def redact_unsent_sql() -> str:
    """送信取消されたメッセージの本文と、そのファイルから抽出したテキストを消す UPDATE 文。"""
    table = f"`{config.PROJECT_ID}.{dataset()}.events_raw`"
    return f"""
UPDATE {table}
SET data = JSON_REMOVE(data, '$.payload.message.text', '$.payload.text')
WHERE publish_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {LOOKBACK_DAYS} DAY)
  AND publish_time < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {SETTLE_HOURS} HOUR)
  AND COALESCE(
    JSON_VALUE(data, '$.payload.message.text'), JSON_VALUE(data, '$.payload.text')
  ) IS NOT NULL
  AND COALESCE(
    JSON_VALUE(data, '$.payload.message.id'), JSON_VALUE(data, '$.payload.message_id')
  ) IN (
    SELECT JSON_VALUE(data, '$.payload.unsend.messageId')
    FROM {table}
    WHERE publish_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {LOOKBACK_DAYS + 1} DAY)
      AND JSON_VALUE(data, '$.kind') = 'webhook'
      AND JSON_VALUE(data, '$.payload.type') = 'unsend'
  )
""".strip()


def _bq_query(sql: str) -> dict:
    """BigQuery の jobs.query を同期実行する（テストでモックする境界）。"""
    req = urllib.request.Request(
        f"https://bigquery.googleapis.com/bigquery/v2/projects/{config.PROJECT_ID}/queries",
        data=json.dumps({
            "query": sql, "useLegacySql": False,
            "location": os.environ.get("LAKE_LOCATION", "asia-northeast1"),
            "timeoutMs": 30000,
        }).encode(),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {lake._token()}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"BigQuery query {exc.code}: {detail}") from exc


def redact_unsent() -> dict | None:
    """送信取消された本文を消去する。取込が無効なら None。"""
    if not lake.is_enabled():
        return None
    try:
        res = _bq_query(redact_unsent_sql())
    except Exception as exc:  # noqa: BLE001 - tick 本体を止めない
        logger.warning("送信取消の本文消去に失敗: %s", exc)
        return {"error": "redact_failed"}
    return {"redacted": int(res.get("numDmlAffectedRows", 0) or 0)}
