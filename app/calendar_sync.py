"""イベント → Googleカレンダーの反映（F3-2 / docs/calendar-design.md §4.1）。

- 反映は保存の直後に同期実行する。失敗してもイベント保存は成功させ、
  `gcal_sync_state=error` を残して tick（`retry_pending`）で再試行する。
- カレンダー側の予定には `extendedProperties.private.jci_event_id` を刻み、
  カレンダーから取り込むとき（P5-2）にリンク済みかどうかを判定できるようにする。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import gcal
from .models import Event, EventStatus, GcalSyncState
from .repository import Repository

logger = logging.getLogger("jci-agent.calendar_sync")

JST = ZoneInfo(gcal.TIMEZONE)
DEFAULT_DURATION = timedelta(hours=2)
#: カレンダー予定の中身に影響するイベント項目（これ以外の変更では API を呼ばない）
CALENDAR_FIELDS = frozenset({
    "type", "title", "datetime_start", "datetime_end", "location",
    "attendance_deadline", "material_deadline", "target_scope", "status",
})
LINK_KEY = "jci_event_id"
MANAGED_NOTE = "※ 猪苗代JC 専務理事エージェントが管理する予定です。"


def _fmt(dt: datetime) -> str:
    return dt.strftime("%-m/%-d %H:%M")


def _calendar_time(dt: datetime) -> dict:
    """Calendar API の dateTime。naive は JST とみなす。"""
    local = dt.replace(tzinfo=JST) if dt.tzinfo is None else dt.astimezone(JST)
    return {"dateTime": local.isoformat(), "timeZone": gcal.TIMEZONE}


def calendar_title(event: Event) -> str:
    """「【理事会】第8回理事会」。種別とタイトルが同じなら前置しない。"""
    kind = str(event.type)
    if event.title.strip() == kind or event.title.startswith(f"【{kind}】"):
        return event.title
    return f"【{kind}】{event.title}"


def _description(event: Event) -> str:
    lines = []
    if event.attendance_deadline:
        lines.append(f"出欠締切: {_fmt(event.attendance_deadline)}")
    if event.material_deadline:
        lines.append(f"資料締切: {_fmt(event.material_deadline)}")
    scope = event.target_scope
    if scope.kind != "all" and scope.value:
        lines.append(f"対象: {'・'.join(scope.value)}")
    else:
        lines.append("対象: 全員")
    lines.append("")
    lines.append(MANAGED_NOTE)
    return "\n".join(lines)


def to_calendar_body(event: Event) -> dict:
    """イベントをカレンダー予定（events リソース）に変換する。"""
    end = event.datetime_end or (event.datetime_start + DEFAULT_DURATION)
    return {
        "summary": calendar_title(event),
        "location": event.location or "",
        "description": _description(event),
        "start": _calendar_time(event.datetime_start),
        "end": _calendar_time(end),
        "extendedProperties": {"private": {LINK_KEY: event.event_id}},
    }


def touches_calendar(changed_fields: set[str]) -> bool:
    """更新項目にカレンダーへ影響するものが含まれるか。"""
    return bool(changed_fields & CALENDAR_FIELDS)


def _mark(event: Event, state: GcalSyncState, *, error: str | None = None) -> Event:
    update: dict = {"gcal_sync_state": state, "gcal_error": error}
    if state == GcalSyncState.synced:
        update["gcal_synced_at"] = datetime.now()
    return event.model_copy(update=update)


def push_event(repo: Repository, event: Event) -> Event:
    """イベントの現在の状態をカレンダーへ反映し、同期状態を保存して返す。

    - 中止（cancelled）: カレンダー予定を削除しリンクを外す。
    - それ以外: 未リンクなら作成、リンク済みなら更新。
    失敗しても例外は投げず、`gcal_sync_state=error` で保存する。
    """
    if not gcal.is_configured():
        synced = _mark(event, GcalSyncState.disabled)
        repo.upsert_event(synced)
        return synced

    try:
        if event.status == EventStatus.cancelled:
            if event.gcal_event_id:
                gcal.delete_event(event.gcal_event_id)
            synced = event.model_copy(update={"gcal_event_id": None, "gcal_etag": None})
        elif event.gcal_event_id:
            try:
                res = gcal.patch_event(event.gcal_event_id, to_calendar_body(event))
            except gcal.GcalError as exc:
                if exc.status not in (404, 410):
                    raise
                # カレンダー側で消えていた → 作り直す
                res = gcal.insert_event(to_calendar_body(event))
            synced = event.model_copy(
                update={"gcal_event_id": res.get("id"), "gcal_etag": res.get("etag")}
            )
        else:
            res = gcal.insert_event(to_calendar_body(event))
            synced = event.model_copy(
                update={"gcal_event_id": res.get("id"), "gcal_etag": res.get("etag")}
            )
        synced = _mark(synced, GcalSyncState.synced)
    except Exception as exc:  # noqa: BLE001 - 同期失敗でイベント保存を止めない
        logger.warning("カレンダー反映に失敗: event=%s err=%s", event.event_id, exc)
        synced = _mark(event, GcalSyncState.error, error=str(exc)[:300])

    repo.upsert_event(synced)
    return synced


def retry_pending(repo: Repository, now: datetime) -> dict | None:
    """未反映（pending/error）のイベントを再反映する（tick から呼ぶ）。

    過去に終わったイベントは対象外（今さらカレンダーを直しても意味が薄く、API を浪費するため）。
    """
    if not gcal.is_configured():
        return None
    targets = [
        e for e in repo.list_events()
        if e.gcal_sync_state in (GcalSyncState.pending, GcalSyncState.error,
                                 GcalSyncState.disabled)
        and (e.datetime_end or e.datetime_start) >= now - timedelta(days=1)
    ]
    ok = failed = 0
    for event in targets:
        result = push_event(repo, event)
        if result.gcal_sync_state == GcalSyncState.synced:
            ok += 1
        else:
            failed += 1
    return {"retried": len(targets), "synced": ok, "failed": failed}
