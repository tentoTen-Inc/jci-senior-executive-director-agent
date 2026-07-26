"""Gmail 参照（OAuth refresh token, docs/external-notice-design.md §2）。

専用アドレスは個人Gmail（`inawashiro.jc@gmail.com`）で確定したため、
ドメイン全体委任は使えない。初回同意で得た refresh token を Secret Manager に置き、
`gmail.readonly` で読み取りのみ行う（送信・削除はしない）。

`fetch_messages` がテストのモック境界。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import UTC, datetime

from . import config

logger = logging.getLogger("jci-agent.gmail")

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
#: 取込対象のラベル（Gmail のフィルタで転送メールに付与する）
DEFAULT_LABEL = "対外連絡"
#: 差分取得の範囲。tick は毎時なので2日分見ておけば取りこぼさない。
DEFAULT_NEWER_THAN = "2d"
MAX_MESSAGES = 25
#: 添付から取り出すテキストの上限（要約プロンプトが膨らみすぎないように）
ATTACHMENT_TEXT_LIMIT = 4000


def label() -> str:
    return os.environ.get("GMAIL_LABEL", DEFAULT_LABEL)


def is_configured() -> bool:
    """OAuth の3点セットが揃っているか（未設定なら取込をスキップする）。"""
    return all([
        config.get_secret("GMAIL_OAUTH_CLIENT_ID", "gmail-oauth-client-id"),
        config.get_secret("GMAIL_OAUTH_CLIENT_SECRET", "gmail-oauth-client-secret"),
        config.get_secret("GMAIL_OAUTH_REFRESH_TOKEN", "gmail-oauth-refresh-token"),
    ])


def _access_token() -> str:
    """refresh token をアクセストークンに交換する。"""
    client_id = config.get_secret("GMAIL_OAUTH_CLIENT_ID", "gmail-oauth-client-id")
    client_secret = config.get_secret("GMAIL_OAUTH_CLIENT_SECRET", "gmail-oauth-client-secret")
    refresh_token = config.get_secret("GMAIL_OAUTH_REFRESH_TOKEN", "gmail-oauth-refresh-token")
    if not (client_id and client_secret and refresh_token):
        raise RuntimeError("Gmail の OAuth 情報（client_id/secret/refresh_token）が未設定です。")

    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data)
    with urllib.request.urlopen(req) as resp:
        payload = json.loads(resp.read())
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("アクセストークンを取得できませんでした。")
    return token


def _get(path: str, token: str, params: dict | None = None) -> dict:
    url = f"{API_BASE}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def _decode(data: str | None) -> bytes:
    if not data:
        return b""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _header(headers: list[dict], name: str) -> str | None:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value")
    return None


def _walk(part: dict) -> list[dict]:
    """MIME ツリーを平坦化する。"""
    parts = [part]
    for child in part.get("parts", []) or []:
        parts.extend(_walk(child))
    return parts


def extract_body(payload: dict) -> str:
    """本文（text/plain 優先、無ければ text/html をタグ除去）を返す。"""
    parts = _walk(payload)
    plains = [
        _decode(p.get("body", {}).get("data")).decode("utf-8", "replace")
        for p in parts
        if p.get("mimeType") == "text/plain" and p.get("body", {}).get("data")
    ]
    if plains:
        return "\n".join(t.strip() for t in plains if t.strip())

    htmls = [
        _decode(p.get("body", {}).get("data")).decode("utf-8", "replace")
        for p in parts
        if p.get("mimeType") == "text/html" and p.get("body", {}).get("data")
    ]
    if not htmls:
        return ""
    import re

    text = re.sub(r"<[^>]+>", " ", "\n".join(htmls))
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def pdf_text(raw: bytes) -> str | None:
    """PDF からテキストを抽出する（失敗しても取込は続ける）。"""
    try:
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        text = text.strip()
        return text[:ATTACHMENT_TEXT_LIMIT] if text else None
    except Exception:  # noqa: BLE001 - 抽出できない添付は名前だけ残す
        logger.exception("添付PDFのテキスト抽出に失敗しました")
        return None


def _parse_internal_date(value: str | None) -> datetime | None:
    """internalDate（epoch ミリ秒の文字列）をローカル naive datetime にする。"""
    if not value:
        return None
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC).astimezone().replace(tzinfo=None)


def _attachments(message_id: str, payload: dict, token: str) -> list[dict]:
    out: list[dict] = []
    for part in _walk(payload):
        filename = part.get("filename")
        if not filename:
            continue
        body = part.get("body", {})
        attachment_id = body.get("attachmentId")
        mime = part.get("mimeType")
        raw = b""
        if attachment_id:
            got = _get(f"/messages/{message_id}/attachments/{attachment_id}", token)
            raw = _decode(got.get("data"))
        elif body.get("data"):
            raw = _decode(body["data"])
        text = pdf_text(raw) if (mime == "application/pdf" or filename.lower().endswith(".pdf")) \
            else None
        out.append({"name": filename, "mime": mime, "text_excerpt": text})
    return out


def normalize(message: dict, token: str) -> dict:
    """Gmail の message を取込しやすい形に整える。"""
    payload = message.get("payload", {})
    headers = payload.get("headers", []) or []
    sender = _header(headers, "From") or ""
    name, addr = sender, None
    if "<" in sender and ">" in sender:
        name = sender.split("<", 1)[0].strip().strip('"') or None
        addr = sender.split("<", 1)[1].split(">", 1)[0].strip()
    else:
        name, addr = None, sender.strip() or None

    return {
        "message_id": message.get("id", ""),
        "subject": _header(headers, "Subject") or "(件名なし)",
        "from_name": name,
        "from_addr": addr,
        "received_at": _parse_internal_date(message.get("internalDate")),
        "body_text": extract_body(payload),
        "attachments": _attachments(message.get("id", ""), payload, token),
    }


def fetch_messages(*, label_name: str | None = None, newer_than: str | None = None) -> list[dict]:
    """ラベル付きの新着メールを取得して正規化して返す（モック境界）。"""
    token = _access_token()
    query = f'label:"{label_name or label()}" newer_than:{newer_than or DEFAULT_NEWER_THAN}'
    listed = _get(
        "/messages", token, {"q": query, "maxResults": MAX_MESSAGES},
    )
    out: list[dict] = []
    for ref in listed.get("messages", []) or []:
        message = _get(f"/messages/{ref['id']}", token, {"format": "full"})
        out.append(normalize(message, token))
    return out
