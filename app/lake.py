"""LINEイベントのデータレイク取込（docs/datalake-design.md）。

Webhook で受けた生イベントを「封筒」に包んで Pub/Sub（`line-events`）へ publish するだけにする。
BigQuery への書き込みは Pub/Sub の BigQuery サブスクリプションが行う（アプリ側にコードは無い）。

- 生イベントは加工しない（解釈は BigQuery のビューで行う）。
- publish に失敗しても Webhook の応答処理は止めない。失敗分は `LINE_INGEST_FALLBACK` の目印付きで
  ログに残し、`scripts/replay_ingest_fallback.py` で再投入できるようにする。
- `LINE_EVENTS_TOPIC` が未設定なら何もしない。

`_publish_raw` がテストのモック境界。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config

logger = logging.getLogger("jci-agent.lake")

ENVELOPE_VERSION = 1
FALLBACK_MARKER = "LINE_INGEST_FALLBACK"
PUBLISH_TIMEOUT = 5  # 秒。Webhook を待たせすぎない
JST = ZoneInfo("Asia/Tokyo")

KIND_WEBHOOK = "webhook"
KIND_OUTBOUND = "outbound"
KIND_CONTENT = "content"
KIND_GROUP_PROFILE = "group_profile"

_creds = None
_creds_lock = threading.Lock()


def topic_path() -> str | None:
    """publish 先のトピック。`line-events` だけでも `projects/…/topics/…` でもよい。"""
    value = os.environ.get("LINE_EVENTS_TOPIC", "").strip()
    if not value:
        return None
    if value.startswith("projects/"):
        return value
    return f"projects/{config.PROJECT_ID}/topics/{value}"


def is_enabled() -> bool:
    return topic_path() is not None


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------- #
# 封筒
# --------------------------------------------------------------------------- #
def envelope(kind: str, payload: dict, *, id: str | None = None,
             destination: str | None = None, received_at: str | None = None) -> dict:
    """全イベント共通の封筒（docs/datalake-design.md §3.1）。"""
    return {
        "v": ENVELOPE_VERSION,
        "id": id or f"{kind}_{uuid.uuid4().hex}",
        "kind": kind,
        "received_at": received_at or now_iso(),
        "destination": destination,
        "payload": payload,
    }


def attributes_for(env: dict) -> dict[str, str]:
    """Pub/Sub 属性（サブスクリプションのフィルタ用）。値は文字列のみ・空は入れない。"""
    payload = env.get("payload") or {}
    attrs = {"kind": env.get("kind", "")}
    if env.get("kind") == KIND_WEBHOOK:
        attrs["event_type"] = payload.get("type") or ""
        attrs["message_type"] = (payload.get("message") or {}).get("type") or ""
        attrs["source_type"] = (payload.get("source") or {}).get("type") or ""
    return {k: v for k, v in attrs.items() if v}


def webhook_envelopes(body: str) -> list[dict]:
    """Webhook の本文（署名検証済み）から、イベントごとの封筒を作る。"""
    data = json.loads(body)
    destination = data.get("destination")
    received_at = now_iso()
    return [
        envelope(KIND_WEBHOOK, event, id=event.get("webhookEventId"),
                 destination=destination, received_at=received_at)
        for event in data.get("events", [])
    ]


# --------------------------------------------------------------------------- #
# publish
# --------------------------------------------------------------------------- #
def _token() -> str:
    """実行SAのアクセストークン（期限切れのときだけ更新してキャッシュ）。"""
    global _creds
    from google.auth import default
    from google.auth.transport.requests import Request

    with _creds_lock:
        if _creds is None:
            _creds, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if not _creds.valid:
            _creds.refresh(Request())
        return _creds.token


def _publish_raw(topic: str, messages: list[dict]) -> None:
    """Pub/Sub REST の publish を1回呼ぶ（テストでモックする境界）。"""
    req = urllib.request.Request(
        f"https://pubsub.googleapis.com/v1/{topic}:publish",
        data=json.dumps({"messages": messages}).encode(),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {_token()}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=PUBLISH_TIMEOUT) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Pub/Sub publish {exc.code}: {detail}") from exc


def to_pubsub_message(env: dict) -> dict:
    raw = json.dumps(env, ensure_ascii=False, separators=(",", ":")).encode()
    return {"data": base64.b64encode(raw).decode(), "attributes": attributes_for(env)}


def publish(envelopes: list[dict]) -> bool:
    """封筒をまとめて publish する。無効なら False、失敗したらログに退避して False。"""
    if not envelopes:
        return True
    topic = topic_path()
    if topic is None:
        return False
    try:
        _publish_raw(topic, [to_pubsub_message(e) for e in envelopes])
        return True
    except Exception as exc:  # noqa: BLE001 - 取込失敗で応答処理を止めない
        logger.error("LINEイベントの publish に失敗（%d件をログに退避）: %s", len(envelopes), exc)
        for env in envelopes:
            # 1行1封筒。scripts/replay_ingest_fallback.py がこの行を拾って再投入する
            logger.error("%s %s", FALLBACK_MARKER, json.dumps(env, ensure_ascii=False))
        return False


def ingest_webhook(body: str) -> None:
    """Webhook 本文の全イベントを取り込む。例外は外に出さない。"""
    if not is_enabled():
        return
    try:
        envelopes = webhook_envelopes(body)
    except Exception:  # noqa: BLE001
        logger.exception("Webhook 本文の解析に失敗しました（取込スキップ）")
        return
    publish(envelopes)


# --------------------------------------------------------------------------- #
# ボットの送信の記録（docs/datalake-design.md §4.3）
# --------------------------------------------------------------------------- #
def _message_dict(message) -> dict:
    if isinstance(message, dict):
        return message
    try:
        return message.to_dict()
    except Exception:  # noqa: BLE001 - 記録用。形が変でも送信は止めない
        return {"type": getattr(message, "type", None), "repr": repr(message)[:500]}


def record_outbound(channel: str, messages: list, *, reply_token: str | None = None,
                    to: str | None = None) -> None:
    """ボットが送ったメッセージを記録する（reply は replyToken で受信イベントと突き合わせる）。"""
    if not is_enabled() or not messages:
        return
    try:
        payload = {
            "channel": channel,
            "reply_token": reply_token,
            "to": to,
            "messages": [_message_dict(m) for m in messages],
        }
        publish([envelope(KIND_OUTBOUND, payload)])
    except Exception:  # noqa: BLE001 - 記録の失敗で送信処理を止めない
        logger.exception("送信メッセージの記録に失敗しました")
