"""LOM の公式アカウントとしてドライブにフォルダ・文書を作る（docs/proposal-ledger-design.md §6）。

サービスアカウント（doc-editor）はマイドライブにファイルを持てないため、新しく作るものは
LOM 公式 Gmail（inawashiro.jc）の OAuth（drive スコープ）で作る。
作ったものは LOM のものとして年度をまたいで残る。
既存の文書の読み書きは引き続き doc-editor（committee_docs）で行う。
"""
from __future__ import annotations

import urllib.parse

from . import committee_docs, gmail

FOLDER = committee_docs.FOLDER
DRIVE_FILES = committee_docs.DRIVE_FILES


def _token() -> str:
    """LOM 公式アカウントのアクセストークン（Gmail の取込と同じ refresh token・要 drive）。"""
    return gmail._access_token()


def _quote(name: str) -> str:
    return name.replace("\\", "\\\\").replace("'", "\\'")


def find_folder(name: str, parent: str, token: str) -> str | None:
    params = {
        "q": f"'{parent}' in parents and name = '{_quote(name)}' "
             f"and mimeType = '{FOLDER}' and trashed = false",
        "fields": "files(id)",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    files = committee_docs._get_json(f"{DRIVE_FILES}?{urllib.parse.urlencode(params)}",
                                     token).get("files", [])
    return files[0]["id"] if files else None


def ensure_folder(name: str, parent: str, token: str) -> str:
    """親フォルダの中に同じ名前のフォルダがあればそれを、無ければ作って返す。"""
    found = find_folder(name, parent, token)
    if found:
        return found
    created = committee_docs._post_json(
        f"{DRIVE_FILES}?supportsAllDrives=true",
        {"name": name, "mimeType": FOLDER, "parents": [parent]}, token)
    return created["id"]


def copy(file_id: str, name: str, parent: str, token: str) -> str:
    """ファイルをコピーして名前を付け、指定のフォルダに置く。新しいファイルIDを返す。"""
    created = committee_docs._post_json(
        f"{DRIVE_FILES}/{file_id}/copy?supportsAllDrives=true",
        {"name": name, "parents": [parent]}, token)
    return created["id"]
