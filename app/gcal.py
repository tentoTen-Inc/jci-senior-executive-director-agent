"""Googleカレンダー API（鍵レス impersonation, docs/calendar-design.md §2）。

Meet を付けるには予定の持ち主として呼ぶ必要がある（サービスアカウントでは会議を作れない）。
LOM 公式 Gmail の OAuth（`gmail.is_configured()`。calendar.events と meetings.space.settings を
許可済み）があればそちらで呼び、無ければ従来どおり calendar-sync を権限借用する（§4.5）。

Cloud Run の実行SA(app-runtime)が calendar-sync を権限借用して Calendar API を呼ぶ。
対象カレンダー（`inawashiro.jc@gmail.com`）は calendar-sync に「予定の変更」権限で共有済みの前提。
`GCAL_CALENDAR_ID`（本番は `DEFAULT_CALENDAR_ID`）が未設定なら連携は無効。

`_request` がテストのモック境界。
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("jci-agent.gcal")

DEFAULT_CALENDAR_ID = "inawashiro.jc@gmail.com"
GCAL_SYNC_SA = os.environ.get(
    "GCAL_SYNC_SA", "calendar-sync@jci-sed-agent.iam.gserviceaccount.com"
)
SCOPE = "https://www.googleapis.com/auth/calendar.events"
API_BASE = "https://www.googleapis.com/calendar/v3"
MEET_API_BASE = "https://meet.googleapis.com/v2"
#: 会議を作る・読むときに付ける（無いと conferenceData は無視される）
CONFERENCE_PARAMS = {"conferenceDataVersion": "1"}
#: 持ち主のアクセストークン（1時間有効）を使い回す時間。予定ごとに交換しない
OWNER_TOKEN_TTL = 30 * 60
_owner_token: tuple[str, float] = ("", 0.0)
TIMEZONE = "Asia/Tokyo"


class GcalError(RuntimeError):
    """Calendar API の失敗。`status` は HTTP ステータス（通信失敗などは None）。"""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def calendar_id() -> str | None:
    """連携先カレンダーID。未設定・空文字なら連携無効として None。

    SA の作成とカレンダー共有（docs/calendar-design.md §7）が済むまでは
    失敗し続けるだけなので、明示的に設定されたときだけ有効にする。
    """
    value = os.environ.get("GCAL_CALENDAR_ID", "").strip()
    return value or None


def is_configured() -> bool:
    return calendar_id() is not None


def _token() -> str:
    """予定の持ち主（LOM 公式 Gmail）の OAuth があればそれ、無ければ calendar-sync を借用する。"""
    from . import gmail

    if gmail.is_configured():
        global _owner_token
        token, at = _owner_token
        if not token or time.monotonic() - at > OWNER_TOKEN_TTL:
            _owner_token = (gmail._access_token(), time.monotonic())
        return _owner_token[0]
    from google.auth import default, impersonated_credentials
    from google.auth.transport.requests import Request

    source, _ = default()
    target = impersonated_credentials.Credentials(
        source_credentials=source,
        target_principal=GCAL_SYNC_SA,
        target_scopes=[SCOPE],
    )
    target.refresh(Request())
    return target.token


def _events_url(event_id: str | None = None, params: dict | None = None) -> str:
    cal = urllib.parse.quote(calendar_id() or "", safe="")
    url = f"{API_BASE}/calendars/{cal}/events"
    if event_id:
        url += f"/{urllib.parse.quote(event_id, safe='')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return url


def _request(method: str, url: str, body: dict | None = None) -> dict:
    """Calendar API を呼んで JSON を返す（204 は空 dict）。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_token()}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise GcalError(f"Calendar API {method} {exc.code}: {detail}", exc.code) from exc
    except urllib.error.URLError as exc:
        raise GcalError(f"Calendar API {method} 通信失敗: {exc.reason}") from exc
    return json.loads(raw) if raw else {}


def insert_event(body: dict) -> dict:
    return _request("POST", _events_url(params=CONFERENCE_PARAMS), body)


def patch_event(gcal_event_id: str, body: dict) -> dict:
    return _request("PATCH", _events_url(gcal_event_id, CONFERENCE_PARAMS), body)


def enable_auto_recording(meeting_code: str) -> None:
    """Meet の会議室を「主催者側の誰かが入ったら自動で録画」にする（Meet REST API v2）。"""
    space = _request("GET", f"{MEET_API_BASE}/spaces/{urllib.parse.quote(meeting_code)}")
    mask = "config.artifactConfig.recordingConfig.autoRecordingGeneration"
    _request("PATCH", f"{MEET_API_BASE}/{space['name']}?updateMask={mask}",
             {"config": {"artifactConfig": {"recordingConfig": {
                 "autoRecordingGeneration": "ON"}}}})


def delete_event(gcal_event_id: str) -> None:
    """予定を削除する。既に無い（404/410）場合は成功扱い。"""
    try:
        _request("DELETE", _events_url(gcal_event_id))
    except GcalError as exc:
        if exc.status in (404, 410):
            return
        raise


def list_events(*, sync_token: str | None = None, time_min: str | None = None,
                page_token: str | None = None) -> dict:
    """予定の一覧（差分取得）。繰り返し予定は個々の回に展開する。

    sync_token を渡すと前回からの差分だけ返る（削除は status=cancelled で届く）。
    トークン失効時は 410 の GcalError になるので、呼び出し側で全件取得に戻す。
    """
    params: dict = {"singleEvents": "true", "maxResults": "250"}
    if sync_token:
        params["syncToken"] = sync_token
    else:
        params["showDeleted"] = "false"
        if time_min:
            params["timeMin"] = time_min
    if page_token:
        params["pageToken"] = page_token
    return _request("GET", _events_url(params=params))
