"""AI の回答に付ける「出典ファイルを開くリンク」（docs/lake-ai-design.md §4.2）。

LINE で共有されたファイル（Cloud Storage・非公開）を、回答を受け取った本人が
タップで開けるようにする。
リンクには「どのファイルを・誰に・いつまで」を入れて HMAC で署名し、改ざんできないようにする。
署名鍵は既存の管理用シークレットから用途別に導出する（新しいシークレットを増やさない）。

- 発行するのは、その人が検索で見られる範囲（docs/lake-ai-design.md §4.1）のファイルだけ。
- 有効期限は30日。LINE のトークを後から見返しても開けるようにしつつ、無期限にはしない。
- `/files/{token}` は公開パスだが、署名と期限が正しいときだけファイルを返す。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
from urllib.parse import urlsplit

from . import config

LINK_TTL_SECONDS = 30 * 24 * 3600


def public_base_url() -> str | None:
    """公開サービス（jci-sed-agent）の URL。未設定なら Push の宛先 URL から求める。"""
    explicit = os.environ.get("PUBLIC_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    audience = os.environ.get("PUBSUB_PUSH_AUDIENCE", "").strip()
    if audience:
        parts = urlsplit(audience)
        return f"{parts.scheme}://{parts.netloc}"
    return None


def _key() -> bytes | None:
    secret = config.admin_api_secret()
    if not secret:
        return None
    return hmac.new(secret.encode(), b"line-file-link-v1", hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(message_id: str, user_id: str, *, now: float | None = None) -> str | None:
    key = _key()
    if key is None:
        return None
    issued = int(now if now is not None else time.time())
    payload = _b64(json.dumps(
        {"m": message_id, "u": user_id, "exp": issued + LINK_TTL_SECONDS},
        separators=(",", ":"),
    ).encode())
    signature = _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def verify_token(token: str, *, now: float | None = None) -> dict | None:
    """署名と期限が正しければ {"m": message_id, "u": user_id, "exp": ...} を返す。"""
    key = _key()
    if key is None or token.count(".") != 1:
        return None
    payload, signature = token.split(".")
    expected = _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        data = json.loads(_unb64(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(data.get("exp", 0)) < (now if now is not None else time.time()):
        return None
    return data


def file_url(message_id: str, user_id: str) -> str | None:
    base = public_base_url()
    token = make_token(message_id, user_id)
    if not base or not token:
        return None
    return f"{base}/files/{urllib.parse.quote(token, safe='.')}"
