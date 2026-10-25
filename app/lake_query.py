"""データレイク（BigQuery）の読み取り（docs/datalake-design.md §3.3）。

jobs.query の REST を同期で呼び、行を dict のリストで返す。値は必ずクエリパラメータで渡す
（SQL に文字列を埋め込まない）。追加ライブラリは使わない。

`_post` がテストのモック境界。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, lake

TIMEOUT_MS = 30000


class QueryError(RuntimeError):
    pass


def dataset() -> str:
    return os.environ.get("LAKE_DATASET", "line_lake")


def _param(name: str, value) -> dict:
    # 型を明示しないと BigQuery は STRING として扱い、数値との比較で
    # 「No matching signature for operator <=」になる（本番で発生）
    if isinstance(value, (list, tuple)):
        # 文字列の配列（IN UNNEST(@x) で使う）
        return {"name": name,
                "parameterType": {"type": "ARRAY", "arrayType": {"type": "STRING"}},
                "parameterValue": {"arrayValues": [{"value": str(v)} for v in value]}}
    if isinstance(value, bool):
        kind, text = "BOOL", str(value).lower()
    elif isinstance(value, int):
        kind, text = "INT64", str(value)
    elif isinstance(value, float):
        kind, text = "FLOAT64", repr(value)
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
        raise QueryError(f"{exc.code}: {_error_message(exc.read())}") from exc
    except urllib.error.URLError as exc:
        raise QueryError(str(exc.reason)) from exc


def _error_message(raw: bytes) -> str:
    """BigQuery のエラー応答から message を1行で取り出す（ログが行ごとに分割されないように）。"""
    text = raw.decode("utf-8", "replace")
    try:
        text = json.loads(text)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        pass
    return " ".join(text.split())[:500]


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


def _get_results(job_id: str, location: str, timeout_ms: int) -> dict:
    """jobs.getQueryResults で完了を待つ（テストでモックする境界）。"""
    params = urllib.parse.urlencode({"location": location, "timeoutMs": timeout_ms,
                                     "maxResults": 0})
    req = urllib.request.Request(
        f"https://bigquery.googleapis.com/bigquery/v2/projects/{config.PROJECT_ID}"
        f"/queries/{job_id}?{params}")
    req.add_header("Authorization", f"Bearer {lake._token()}")
    try:
        with urllib.request.urlopen(req, timeout=timeout_ms / 1000 + 15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise QueryError(f"{exc.code}: {_error_message(exc.read())}") from exc
    except urllib.error.URLError as exc:
        raise QueryError(str(exc.reason)) from exc


def _run(sql: str, params: dict | None, timeout_ms: int = TIMEOUT_MS) -> dict:
    location = os.environ.get("LAKE_LOCATION", "asia-northeast1")
    body = {
        "query": sql,
        "useLegacySql": False,
        "location": location,
        "timeoutMs": min(timeout_ms, TIMEOUT_MS),
        "parameterMode": "NAMED",
        "queryParameters": [_param(k, v) for k, v in (params or {}).items()],
    }
    res = _post(body)
    # 長いクエリ（ベクトル化の INSERT 等）は、指定の時間まで完了を待ち続ける。
    # 待たずに打ち切ると、BigQuery 側では処理が続いたまま後続の処理だけが飛ばされる。
    deadline = time.monotonic() + timeout_ms / 1000
    job_id = (res.get("jobReference") or {}).get("jobId")
    while not res.get("jobComplete", False) and job_id and time.monotonic() < deadline:
        wait = int(max(1, min(TIMEOUT_MS / 1000, deadline - time.monotonic())) * 1000)
        res = {**res, **_get_results(job_id, location, wait)}
    if not res.get("jobComplete", False):
        raise QueryError("クエリが時間内に終わりませんでした。")
    return res


def query(sql: str, params: dict | None = None, *, timeout_ms: int = TIMEOUT_MS) -> list[dict]:
    """クエリを実行して行を返す。完了しなかった・失敗したら QueryError。"""
    return to_records(_run(sql, params, timeout_ms))


def execute(sql: str, params: dict | None = None, *, timeout_ms: int = TIMEOUT_MS) -> int:
    """DML（INSERT/UPDATE/DELETE）を実行し、変更した行数を返す。"""
    return int(_run(sql, params, timeout_ms).get("numDmlAffectedRows", 0) or 0)
