"""管理画面「LINEグループ」用のAPI（docs/datalake-design.md §8 P6-3）。

データレイク（BigQuery のビュー）から、グループ一覧・最近のメッセージ・取り込んだファイルを返す。
ファイルのダウンロードは Cloud Storage から管理APIを経由して返す（署名付きURLの鍵を持たないため）。
専務・管理者向け（IAP 保護の /api 配下）。会員向けには公開しない。

`lake_query.query` と `_gcs_download` がテストのモック境界。
"""
from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter, HTTPException, Response

from . import config, lake, lake_query
from .deps import get_repo

router = APIRouter()

MAX_LIMIT = 500


def _view(name: str) -> str:
    return f"`{config.PROJECT_ID}.{lake_query.dataset()}.{name}`"


def _run(sql: str, params: dict | None = None) -> list[dict]:
    if not lake.is_enabled():
        raise HTTPException(
            status_code=503, detail="データレイクが未設定です（LINE_EVENTS_TOPIC）。"
        )
    try:
        return lake_query.query(sql, params or {})
    except lake_query.QueryError as exc:
        raise HTTPException(
            status_code=502, detail=f"BigQuery の読み取りに失敗しました: {exc}"
        ) from exc


@router.get("/line/groups")
def line_groups():
    """ボットが参加しているグループ（件数・最終発言・名前）。"""
    rows = _run(f"SELECT * FROM {_view('v_groups')} ORDER BY last_event_at DESC")
    # 名前は BigQuery（group_profile）に無ければ Firestore の台帳で補う
    ledger = {g.group_id: g for g in get_repo().list_line_groups()}
    for row in rows:
        group = ledger.get(row["group_id"])
        if group and not row.get("group_name"):
            row["group_name"] = group.group_name
    return rows


def _limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


@router.get("/line/messages")
def line_messages(group_id: str | None = None, user_id: str | None = None, limit: int = 100):
    """最近の会話（受信＋ボットの送信）。送信取消されたものは含まない。"""
    where, params = [], {"limit": _limit(limit)}
    if group_id:
        where.append("COALESCE(group_id, room_id) = @group_id")
        params["group_id"] = group_id
    if user_id:
        where.append("user_id = @user_id")
        params["user_id"] = user_id
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return _run(
        f"SELECT * FROM {_view('v_conversations')} {clause} "
        "ORDER BY occurred_at DESC LIMIT @limit",
        params,
    )


@router.get("/line/files")
def line_files(group_id: str | None = None, limit: int = 100):
    """取り込んだファイル（新しい順）。抽出テキストは長いので一覧には先頭だけ返す。"""
    params = {"limit": _limit(limit)}
    clause = ""
    if group_id:
        clause = "WHERE group_id = @group_id"
        params["group_id"] = group_id
    return _run(
        f"SELECT * EXCEPT (text), LEFT(text, 200) AS text_excerpt FROM {_view('v_files')} "
        f"{clause} ORDER BY sent_at DESC LIMIT @limit",
        params,
    )


def _gcs_download(gcs_uri: str) -> tuple[bytes, str]:
    """gs://bucket/name を読み出して (本体, content-type) を返す。"""
    bucket, _, name = gcs_uri.removeprefix("gs://").partition("/")
    url = (f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/"
           f"{urllib.parse.quote(name, safe='')}?alt=media")
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {lake._token()}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read(), resp.headers.get("Content-Type", "application/octet-stream")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise HTTPException(status_code=404, detail="ファイルが見つかりません。") from exc
        raise HTTPException(status_code=502, detail=f"Cloud Storage {exc.code}") from exc


@router.get("/line/files/{message_id}/download")
def download_line_file(message_id: str):
    rows = _run(
        f"SELECT status, gcs_uri, file_name FROM {_view('v_files')} WHERE message_id = @id",
        {"id": message_id},
    )
    if not rows or rows[0].get("status") != "stored" or not rows[0].get("gcs_uri"):
        raise HTTPException(status_code=404, detail="保存済みのファイルがありません。")
    raw, content_type = _gcs_download(rows[0]["gcs_uri"])
    filename = rows[0].get("file_name") or message_id
    disposition = f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}"
    return Response(content=raw, media_type=content_type,
                    headers={"Content-Disposition": disposition})
