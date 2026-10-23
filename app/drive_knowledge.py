"""Google ドライブの資料を専務AIのナレッジに取り込む（docs/drive-knowledge-design.md）。

`DRIVE_KNOWLEDGE_FOLDERS`（フォルダIDのカンマ区切り。例: 2026年度・2027年度のフォルダ）の配下を
たどり、本文を BigQuery の `drive_documents` に入れる（tick から毎時・更新されたものだけ）。
ベクトル化と検索は LINE の資料と同じ `knowledge_chunks`（lake_knowledge）で行う。

- 個人情報（名簿・住所録など）と 00-事務局 は取り込まない。
- 01-執行部・02-五役会 の資料は五役・監事への回答にだけ使う（access=officers）。それ以外は会員全員。
- ドライブは drive-reader（読み取り専用）を権限借用して読む。対象フォルダを共有しておく。

`_list_children` / `_extract` がテストのモック境界。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import time
import urllib.parse
from datetime import UTC, datetime

from . import config, drive, lake_query

logger = logging.getLogger("jci-agent.drive_knowledge")

FOLDER = "application/vnd.google-apps.folder"
GOOGLE_DOC = "application/vnd.google-apps.document"
GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"
GOOGLE_SLIDES = "application/vnd.google-apps.presentation"
PDF = "application/pdf"
TEXT_LIMIT = 40000  # 1ファイルあたりの本文の上限（文字）
SYNC_BATCH = 40  # 1回の tick で本文を取り直すファイル数の上限（残りは次回）
SYNC_SECONDS = 60  # 本文の取り出しに使う時間の上限（tick 全体を Scheduler の期限内に収める）

#: 取り込まないもの（個人情報）。フォルダ名・ファイル名のどちらかに含まれていたら除外
EXCLUDE_PATTERNS = re.compile(r"名簿|住所録|入会|認承証|個人情報|事務局")
#: 五役・監事にだけ使うフォルダ（パスの一部に含まれていたら officers）
OFFICER_FOLDERS = re.compile(r"執行部|五役会")
OFFICER_ROLES = ("理事長", "副理事長", "専務理事", "監事", "直前理事長")


def folders() -> list[str]:
    raw = os.environ.get("DRIVE_KNOWLEDGE_FOLDERS", "")
    return [f.strip() for f in raw.split(",") if f.strip()]


def is_enabled() -> bool:
    return bool(folders())


def _table() -> str:
    return f"`{config.PROJECT_ID}.{lake_query.dataset()}.drive_documents`"


def access_for(path: str) -> str:
    return "officers" if OFFICER_FOLDERS.search(path) else "members"


def scopes_for(member) -> list[str]:
    """会員が答えに使ってよいドライブ資料の範囲。"""
    if member is None:
        return []
    return ["members", "officers"] if member.officer_role in OFFICER_ROLES else ["members"]


def excluded(path: str, title: str) -> bool:
    return bool(EXCLUDE_PATTERNS.search(path) or EXCLUDE_PATTERNS.search(title))


# --------------------------------------------------------------------------- #
# ドライブ
# --------------------------------------------------------------------------- #
def _list_children(folder_id: str, token: str) -> list[dict]:
    """フォルダ直下（ページ送りあり）。テストでモックする境界。"""
    out, page = [], None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed=false",
            "pageSize": 1000,
            "fields": "nextPageToken,files(id,name,mimeType,modifiedTime)",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if page:
            params["pageToken"] = page
        url = "https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(params)
        data = json.loads(drive._get(url, token))
        out += data.get("files", [])
        page = data.get("nextPageToken")
        if not page:
            return out


def _folder_name(folder_id: str, token: str) -> str:
    url = (f"https://www.googleapis.com/drive/v3/files/{folder_id}?"
           + urllib.parse.urlencode({"fields": "name", "supportsAllDrives": "true"}))
    return json.loads(drive._get(url, token)).get("name", "")


def crawl(token: str) -> list[dict]:
    """対象フォルダ配下のファイル（フォルダ以外）を、置き場所つきで返す。"""
    files: list[dict] = []
    seen: set[str] = set()
    for root in folders():
        stack = [(root, _folder_name(root, token))]
        while stack:
            folder_id, path = stack.pop()
            if folder_id in seen:
                continue
            seen.add(folder_id)
            for f in _list_children(folder_id, token):
                if f["mimeType"] == FOLDER:
                    stack.append((f["id"], f"{path}/{f['name']}"))
                else:
                    files.append({**f, "path": path})
    return files


def _extract(f: dict, token: str) -> str | None:
    """本文をテキストで取り出す（対応していない種類は None）。テストでモックする境界。"""
    file_id, mime = f["id"], f["mimeType"]
    base = f"https://www.googleapis.com/drive/v3/files/{file_id}"
    if mime in (GOOGLE_DOC, GOOGLE_SLIDES):
        return drive._get(f"{base}/export?mimeType=text/plain", token).decode("utf-8", "replace")
    if mime == GOOGLE_SHEET:
        raw = drive._get(f"{base}/export?mimeType=text/csv", token).decode("utf-8", "replace")
        # 空のセルが多い表をそのまま読むと区切りだらけになるので、空でないセルだけを並べる
        rows = [" | ".join(c for c in row if c.strip()) for row in csv.reader(io.StringIO(raw))]
        return "\n".join(r for r in rows if r)
    if mime == PDF:
        from .gmail import pdf_text

        return pdf_text(drive._get(f"{base}?alt=media&supportsAllDrives=true", token))
    return None


# --------------------------------------------------------------------------- #
# 取込
# --------------------------------------------------------------------------- #
UPSERT_SQL = """
MERGE {table} AS t
USING (
  SELECT file_id, @titles[OFFSET(i)] AS title, @paths[OFFSET(i)] AS path,
         @mime_types[OFFSET(i)] AS mime_type, TIMESTAMP(@modified_ats[OFFSET(i)]) AS modified_at,
         @accesses[OFFSET(i)] AS access, @texts[OFFSET(i)] AS text
  FROM UNNEST(@file_ids) AS file_id WITH OFFSET i
) AS s
ON t.file_id = s.file_id
WHEN MATCHED THEN UPDATE SET title = s.title, path = s.path, mime_type = s.mime_type,
  modified_at = s.modified_at, access = s.access, text = s.text, synced_at = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT (file_id, title, path, mime_type, modified_at, access, text, synced_at)
  VALUES (s.file_id, s.title, s.path, s.mime_type, s.modified_at, s.access, s.text,
          CURRENT_TIMESTAMP())
"""


def _known() -> dict[str, str]:
    rows = lake_query.query(
        f"SELECT file_id, FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E3SZ', modified_at) AS m "
        f"FROM {_table()}")
    return {r["file_id"]: r["m"] for r in rows}


def _norm(ts: str) -> str:
    """Drive の modifiedTime（…123Z）を比較できる形にそろえる。"""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def sync() -> dict | None:
    """更新されたファイルの本文を取り込み、消えたファイルを外す（tick から呼ぶ）。"""
    if not is_enabled():
        return None
    try:
        token = drive._token()
        files = [f for f in crawl(token) if not excluded(f["path"], f["name"])]
        known = _known()
        changed = [f for f in files if known.get(f["id"]) != _norm(f["modifiedTime"])]
        rows: list[tuple[dict, str]] = []
        started = time.monotonic()
        for f in changed[:SYNC_BATCH]:
            if time.monotonic() - started > SYNC_SECONDS:
                break
            # 本文が取れない種類（画像・Word 等）も記録して、毎回取り直さないようにする
            rows.append((f, (_extract(f, token) or "").strip()[:TEXT_LIMIT]))
        if rows:
            # 1件ずつの DML は数秒かかるので、まとめて1回で書く
            lake_query.execute(UPSERT_SQL.format(table=_table()), {
                "file_ids": [f["id"] for f, _ in rows],
                "titles": [f["name"] for f, _ in rows],
                "paths": [f["path"] for f, _ in rows],
                "mime_types": [f["mimeType"] for f, _ in rows],
                "modified_ats": [f["modifiedTime"] for f, _ in rows],
                "accesses": [access_for(f["path"]) for f, _ in rows],
                "texts": [text for _, text in rows],
            })
        added = len(rows)
        current = [f["id"] for f in files]
        removed = 0
        gone = [fid for fid in known if fid not in set(current)]
        if gone:
            removed = lake_query.execute(
                f"DELETE FROM {_table()} WHERE file_id IN UNNEST(@ids)", {"ids": gone})
    except Exception as exc:  # noqa: BLE001 - tick 本体を止めない
        logger.warning("ドライブの取込に失敗: %s", exc)
        return {"error": "drive_sync_failed"}
    return {"files": len(files), "updated": added, "pending": len(changed) - added,
            "removed": removed}
