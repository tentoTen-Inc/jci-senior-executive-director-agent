"""データレイク（BigQuery）の読み取り（docs/datalake-design.md §3.3）。

jobs.query の REST を同期で呼び、行を dict のリストで返す。値は必ずクエリパラメータで渡す
（SQL に文字列を埋め込まない）。追加ライブラリは使わない。

`_post` がテストのモック境界。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from . import config, lake

TIMEOUT_MS = 30000


class QueryError(RuntimeError):
    pass


def dataset() -> str:
    return os.environ.get("LAKE_DATASET", "line_lake")


def _param(name: str, value) -> dict:
    if isinstance(value, bool):
        kind, text = "BOOL", str(value).lower()
    elif isinstance(value, int):
        kind, text = "INT64", str(value)
    else:
        kind, text = "STRING", str(value)
    return {"name": name, "parameterType": {"type": kind}, "parameterValue": {"value": text}}


def _post(body: dict) -> dict:
    """jobs.query を呼ぶ（テストでモックする境界）。"""
    req = urllib.request.Request(
        f"https://bigquery.googleapis.com/bigquery/v2/projects/{config.PROJECT_ID}/queries",
        data=json.dumps(body).encode(),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {lake._token()}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_MS / 1000 + 15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise QueryError(f"{exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise QueryError(str(exc.reason)) from exc


def _cell(value, field: dict):
    if value is None:
        return None
    kind = field.get("type")
    if kind in ("INTEGER", "INT64"):
        return int(value)
    if kind in ("FLOAT", "FLOAT64", "NUMERIC"):
        return float(value)
    if kind in ("BOOLEAN", "BOOL"):
        return value == "true"
    if kind == "TIMESTAMP":
        # BigQuery は epoch 秒（小数）の文字列で返す → ISO 8601（UTC）に揃える
        from datetime import UTC, datetime

        return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
    return value


def to_records(res: dict) -> list[dict]:
    fields = (res.get("schema") or {}).get("fields", [])
    return [
        {f["name"]: _cell(cell.get("v"), f) for f, cell in zip(fields, row.get("f", []),
                                                                 strict=False)}
        for row in res.get("rows", []) or []
    ]


def _run(sql: str, params: dict | None) -> dict:
    body = {
        "query": sql,
        "useLegacySql": False,
        "location": os.environ.get("LAKE_LOCATION", "asia-northeast1"),
        "timeoutMs": TIMEOUT_MS,
        "parameterMode": "NAMED",
        "queryParameters": [_param(k, v) for k, v in (params or {}).items()],
    }
    res = _post(body)
    if not res.get("jobComplete", False):
        raise QueryError("クエリが時間内に終わりませんでした。")
    return res


def query(sql: str, params: dict | None = None) -> list[dict]:
    """クエリを実行して行を返す。完了しなかった・失敗したら QueryError。"""
    return to_records(_run(sql, params))


def execute(sql: str, params: dict | None = None) -> int:
    """DML（INSERT/UPDATE/DELETE）を実行し、変更した行数を返す。"""
    return int(_run(sql, params).get("numDmlAffectedRows", 0) or 0)
