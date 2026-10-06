"""データレイクのワーカー（docs/datalake-design.md §4.2）。

Pub/Sub の Push サブスクリプション `line-events-worker`（フィルタ kind="webhook"）から呼ばれ、
Webhook の生イベントを1件ずつ処理する。

- file / image / video / audio: LINE からファイルを取得して Cloud Storage に保存し、
  結果を `content` イベントとして publish（PDF はテキストも抽出して同梱）
- unsend（送信取消）: 保存済みのファイルを削除し `deleted_unsent` を publish
- join / 未知のグループ: グループ名を取得して `lineGroups` に保存し `group_profile` を publish
- leave: `lineGroups` に退出日時を記録

例外は「再試行してほしい」の合図（呼び出し側が 500 を返し Pub/Sub が間隔を空けて再送する）。
`_line_get` / `_gcs_upload` / `_gcs_delete_prefix` / `_verify_jwt` がテストのモック境界。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta

from . import config, gmail, lake
from .models import LineGroup
from .repository import Repository

logger = logging.getLogger("jci-agent.line_worker")

LINE_DATA_API = "https://api-data.line.me/v2/bot/message"
LINE_API = "https://api.line.me/v2/bot"
GCS_API = "https://storage.googleapis.com/storage/v1/b"
GCS_UPLOAD = "https://storage.googleapis.com/upload/storage/v1/b"
MEDIA_TYPES = ("file", "image", "video", "audio")
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
PDF_TEXT_LIMIT = 20000
PROFILE_TTL = timedelta(days=7)  # グループ名の再取得間隔（名前の変更を拾う）
_EXT = {"image": ".jpg", "video": ".mp4", "audio": ".m4a"}

#: プロセス内キャッシュ（グループID → 最後にグループ名を確認した時刻）。Firestore 読み取りを減らす
_known_groups: dict[str, datetime] = {}


class RetryLater(RuntimeError):
    """一時的に処理できない（変換待ち・一時障害）。Pub/Sub に再送させる。"""


class ContentGone(RuntimeError):
    """LINE 側にファイルが無い（保存期間切れ等）。再送しても無駄なので記録だけする。"""


# --------------------------------------------------------------------------- #
# 設定
# --------------------------------------------------------------------------- #
def bucket() -> str | None:
    return os.environ.get("LINE_CONTENT_BUCKET", "").strip() or None


def max_bytes() -> int:
    try:
        return int(os.environ.get("LINE_CONTENT_MAX_BYTES", DEFAULT_MAX_BYTES))
    except ValueError:
        return DEFAULT_MAX_BYTES


def is_configured() -> bool:
    return bool(bucket() and os.environ.get("PUBSUB_PUSH_SA")
                and os.environ.get("PUBSUB_PUSH_AUDIENCE"))


# --------------------------------------------------------------------------- #
# Push の認証（Pub/Sub が付ける OIDC トークン）
# --------------------------------------------------------------------------- #
def _verify_jwt(token: str, audience: str) -> dict:
    """Google 署名の ID トークンを検証して claims を返す（テストでモックする境界）。"""
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(token, Request(), audience=audience)


def verify_push(authorization: str | None) -> bool:
    """Push リクエストが自分の Pub/Sub サブスクリプションから来たものか。"""
    audience = os.environ.get("PUBSUB_PUSH_AUDIENCE", "")
    expected_sa = os.environ.get("PUBSUB_PUSH_SA", "")
    if not (audience and expected_sa and authorization):
        return False
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return False
    try:
        claims = _verify_jwt(token, audience)
    except Exception as exc:  # noqa: BLE001 - 不正なトークンは拒否するだけ
        logger.warning("Push トークンの検証に失敗: %s", exc)
        return False
    return claims.get("email") == expected_sa and bool(claims.get("email_verified"))


def decode_push(body: dict) -> dict:
    """Push の本文 {"message": {"data": base64, ...}} から封筒を取り出す。"""
    data = (body.get("message") or {}).get("data") or ""
    return json.loads(base64.b64decode(data))


# --------------------------------------------------------------------------- #
# 外部 API
# --------------------------------------------------------------------------- #
def _line_get(url: str, limit: int | None = None) -> tuple[int, dict, bytes]:
    """LINE API を GET して (status, headers, body) を返す（テストでモックする境界）。

    limit を超える本文は読み切らず、そこで打ち切る（巨大ファイルでメモリを使い切らない）。
    """
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {config.line_channel_access_token()}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read(limit + 1) if limit is not None else resp.read()
            return resp.status, dict(resp.headers), raw
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def _gcs_upload(name: str, raw: bytes, content_type: str, metadata: dict[str, str]) -> None:
    """Cloud Storage にオブジェクトを作る（multipart: メタデータ＋本体を1回で）。"""
    boundary = f"lake-{uuid.uuid4().hex}"
    meta = json.dumps({"name": name, "contentType": content_type, "metadata": metadata})
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
        f"--{boundary}\r\nContent-Type: {content_type}\r\n\r\n"
    ).encode() + raw + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{GCS_UPLOAD}/{bucket()}/o?uploadType=multipart", data=body, method="POST"
    )
    req.add_header("Authorization", f"Bearer {lake._token()}")
    req.add_header("Content-Type", f"multipart/related; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RetryLater(f"GCS upload {exc.code}: {detail}") from exc


def _gcs_delete_prefix(prefix: str) -> int:
    """prefix 配下のオブジェクトをすべて削除し、削除数を返す。"""
    token = lake._token()
    query = urllib.parse.urlencode({"prefix": prefix})
    req = urllib.request.Request(f"{GCS_API}/{bucket()}/o?{query}")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        items = json.loads(resp.read()).get("items", [])
    for item in items:
        name = urllib.parse.quote(item["name"], safe="")
        delete = urllib.request.Request(f"{GCS_API}/{bucket()}/o/{name}", method="DELETE")
        delete.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(delete, timeout=30) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
    return len(items)


# --------------------------------------------------------------------------- #
# ファイル
# --------------------------------------------------------------------------- #
def object_prefix(message_id: str) -> str:
    return f"line/content/{message_id}/"


def safe_filename(message: dict, content_type: str | None) -> str:
    """保存名。ファイルは元の名前（パス区切り等を除去）、それ以外は {id}{拡張子}。"""
    name = message.get("fileName")
    if name:
        name = re.sub(r"[\\/\x00-\x1f]", "_", name).strip(". ") or "file"
        return name[:200]
    ext = _EXT.get(message.get("type", ""), "")
    if content_type == "image/png":
        ext = ".png"
    return f"{message.get('id')}{ext}"


def _source_meta(payload: dict) -> dict:
    source = payload.get("source") or {}
    return {
        "source_type": source.get("type"),
        "group_id": source.get("groupId") or source.get("roomId"),
        "user_id": source.get("userId"),
        "sent_at_ms": payload.get("timestamp"),
    }


def _publish_content(message_id: str, status: str, payload: dict, **extra) -> None:
    record = {"message_id": message_id, "status": status, **_source_meta(payload), **extra}
    lake.publish([lake.envelope(lake.KIND_CONTENT, record, id=f"content_{message_id}_{status}")])


def store_content(payload: dict) -> str:
    """メッセージのファイルを LINE から取得して保存する。結果の status を返す。"""
    message = payload.get("message") or {}
    message_id = message.get("id")
    provider = (message.get("contentProvider") or {}).get("type", "line")
    if provider != "line":
        _publish_content(message_id, "external", payload,
                         original_url=message["contentProvider"].get("originalContentUrl"))
        return "external"
    limit = max_bytes()
    declared = message.get("fileSize")
    if isinstance(declared, int) and declared > limit:
        _publish_content(message_id, "too_large", payload, size=declared,
                         file_name=message.get("fileName"))
        return "too_large"

    status, headers, raw = _line_get(f"{LINE_DATA_API}/{message_id}/content", limit=limit)
    if status == 202:
        raise RetryLater("動画・音声の変換待ち")
    if status in (404, 410):
        _publish_content(message_id, "unavailable", payload, file_name=message.get("fileName"))
        return "unavailable"
    if status >= 500 or status == 429:
        raise RetryLater(f"LINE content {status}")
    if status != 200:
        _publish_content(message_id, "failed", payload, http_status=status,
                         file_name=message.get("fileName"))
        return "failed"
    if len(raw) > limit:
        _publish_content(message_id, "too_large", payload, file_name=message.get("fileName"))
        return "too_large"

    content_type = {k.lower(): v for k, v in headers.items()}.get(
        "content-type", "application/octet-stream"
    ).split(";")[0].strip()
    filename = safe_filename(message, content_type)
    name = object_prefix(message_id) + filename
    meta = {k: str(v) for k, v in _source_meta(payload).items() if v is not None}
    meta["message_type"] = message.get("type", "")
    _gcs_upload(name, raw, content_type, meta)

    text = None
    if content_type == "application/pdf" or filename.lower().endswith(".pdf"):
        text = gmail.pdf_text(raw)
        if text:
            text = text[:PDF_TEXT_LIMIT]
    _publish_content(
        message_id, "stored", payload,
        gcs_uri=f"gs://{bucket()}/{name}", content_type=content_type, size=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(), file_name=filename, text=text,
    )
    return "stored"


def delete_unsent(payload: dict) -> int:
    """送信取消されたメッセージのファイルを削除する。"""
    message_id = (payload.get("unsend") or {}).get("messageId")
    if not message_id:
        return 0
    deleted = _gcs_delete_prefix(object_prefix(message_id))
    if deleted:
        _publish_content(message_id, "deleted_unsent", payload, deleted_objects=deleted)
    return deleted


# --------------------------------------------------------------------------- #
# グループ
# --------------------------------------------------------------------------- #
def _fetch_group_summary(group_id: str) -> dict | None:
    status, _, raw = _line_get(f"{LINE_API}/group/{group_id}/summary")
    if status != 200:
        logger.warning("グループ情報の取得に失敗: %s status=%s", group_id, status)
        return None
    return json.loads(raw)


def track_group(repo: Repository, payload: dict, now: datetime) -> None:
    """グループの参加・退出を記録し、名前が未取得・古ければ取得する。"""
    source = payload.get("source") or {}
    source_type = source.get("type")
    if source_type not in ("group", "room"):
        return
    group_id = source.get("groupId") or source.get("roomId")
    event_type = payload.get("type")
    seen = _known_groups.get(group_id)
    if event_type not in ("join", "leave") and seen and now - seen < PROFILE_TTL:
        return

    group = repo.get_line_group(group_id) or LineGroup(group_id=group_id, source_type=source_type)
    if event_type == "join":
        group.joined_at, group.left_at = now, None
    elif event_type == "leave":
        group.left_at = now
    stale = group.profile_at is None or now - group.profile_at >= PROFILE_TTL
    # 複数人トーク（room）には名前の API が無い。退出後は取得できない
    if source_type == "group" and event_type != "leave" and (stale or event_type == "join"):
        summary = _fetch_group_summary(group_id)
        if summary:
            group.group_name = summary.get("groupName")
            group.picture_url = summary.get("pictureUrl")
            group.profile_at = now
            lake.publish([lake.envelope(
                lake.KIND_GROUP_PROFILE,
                {"groupId": group_id, "groupName": group.group_name,
                 "pictureUrl": group.picture_url},
                id=f"group_profile_{group_id}_{now:%Y%m%d%H}",
            )])
    repo.save_line_group(group)
    _known_groups[group_id] = now


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def handle_envelope(repo: Repository, env: dict, now: datetime) -> str:
    """封筒1件を処理し、何をしたかを返す（ログ・テスト用）。"""
    if env.get("kind") != lake.KIND_WEBHOOK:
        return "ignored"
    payload = env.get("payload") or {}
    track_group(repo, payload, now)
    event_type = payload.get("type")
    if event_type == "message" and (payload.get("message") or {}).get("type") in MEDIA_TYPES:
        return store_content(payload)
    if event_type == "unsend":
        return f"unsend:{delete_unsent(payload)}"
    return "noop"
