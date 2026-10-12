"""ログを Cloud Logging が読める構造化 JSON で出す（障害通知の前提）。

標準の basicConfig だと、Cloud Logging 上では深刻度が「なし（DEFAULT）」になり、
複数行のメッセージ（例外のトレースバック・API のエラー本文）は行ごとに別のログに分かれていた。
1行1 JSON（severity・message）で出すと、深刻度で通知でき、例外も1件にまとまる。

ローカル開発・テストでは従来の読みやすい形式のまま（環境変数 K_SERVICE は Cloud Run が設定する）。
"""
from __future__ import annotations

import json
import logging
import os
import sys

_SEVERITY = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARNING",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRITICAL",
}


class CloudLoggingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        entry = {
            "severity": _SEVERITY.get(record.levelno, "DEFAULT"),
            "message": message,
            "logger": record.name,
        }
        return json.dumps(entry, ensure_ascii=False)


def configure_logging() -> None:
    if not os.environ.get("K_SERVICE"):
        logging.basicConfig(level=logging.INFO)
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(CloudLoggingFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    # uvicorn の出力（アクセスログ・起動時の例外）も同じ形式にする
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
