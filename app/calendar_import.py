"""Googleカレンダー → システムの差分取込（F3-2 / docs/calendar-design.md §4.2〜§4.3）。

tick（毎時）から呼ぶ。`events.list(syncToken)` で前回からの差分だけを取り、
- 未リンクの時刻あり予定 → `draft` のイベントとして取込（勝手に配信・催促させない）
- リンク済み予定の変更 → etag が同じなら自分の反映の折り返しなので無視。
  違えば後勝ち（カレンダーの updated とシステムの updated_at を比較）
- リンク済み予定の削除 → イベントを `cancelled` に（出欠データは保持）
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import gcal
from .audit import write_audit
from .calendar_sync import JST, LINK_KEY, push_event
from .events import in_operation
from .models import (
    Event,
    EventOrigin,
    EventStatus,
    EventType,
    GcalSyncState,
)
from .repository import Repository

logger = logging.getLogger("jci-agent.calendar_import")

#: 初回（同期トークン無し）に遡る日数。リンク済み予定の状態を拾うため少し過去から見る。
INITIAL_LOOKBACK = timedelta(days=30)
ACTOR = "gcal"
#: 【】が無いタイトルはこの種別だけキーワードで推定する（「個別相談会」等を個別・対外にしない）
_TYPE_KEYWORDS = [
    EventType.例会, EventType.理事会, EventType.五役会, EventType.委員会, EventType.総会,
]
#: 【】内の種別名。旧名「イベント」（命名規則の変更前に作った予定）は行事として読む
_TITLE_TYPES = {**{t.value: t for t in EventType}, "イベント": EventType.行事}
#: 翌年度の予定の年度前置（「27【対外】…」）
_FISCAL_PREFIX = re.compile(r"^(\d{2})\s*(?=【)")


@dataclass
class PullSummary:
    fetched: int = 0
    created: int = 0
    updated: int = 0
    cancelled: int = 0
    pushed_back: int = 0
    skipped: int = 0
    full_resync: bool = False
    created_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "fetched": self.fetched, "created": self.created, "updated": self.updated,
            "cancelled": self.cancelled, "pushed_back": self.pushed_back,
            "skipped": self.skipped, "full_resync": self.full_resync,
        }


# --------------------------------------------------------------------------- #
# 変換
# --------------------------------------------------------------------------- #
def _to_local(value: str) -> datetime:
    """RFC3339 を JST の naive datetime に（システム内の日時表現に揃える）。"""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(JST).replace(tzinfo=None)


def parse_title(summary: str) -> tuple[EventType, str, int | None]:
    """「27【対外】渡部邦/遠藤孝 第2回次年度会員会議所会議」
    → (対外, 渡部邦/遠藤孝 第2回次年度会員会議所会議, 2027)。

    年度の前置が無ければ年度は None。【種別】が無ければキーワードで推定し、無ければ行事。
    """
    text = (summary or "").strip() or "（無題）"
    fiscal_year = None
    if m := _FISCAL_PREFIX.match(text):
        fiscal_year = 2000 + int(m.group(1))
        text = text[m.end():]
    if text.startswith("【") and "】" in text:
        head, rest = text[1:].split("】", 1)
        rest = rest.strip() or head
        kind = _TITLE_TYPES.get(head)
        if kind is not None:
            return kind, rest, fiscal_year
    for kind in _TYPE_KEYWORDS:
        if kind in text:
            return kind, text, fiscal_year
    return EventType.行事, text, fiscal_year


def _times(item: dict) -> tuple[datetime, datetime | None] | None:
    """時刻あり予定の開始・終了。終日予定（date のみ）は None。"""
    start = (item.get("start") or {}).get("dateTime")
    if not start:
        return None
    end = (item.get("end") or {}).get("dateTime")
    return _to_local(start), (_to_local(end) if end else None)


def _calendar_fields(item: dict) -> dict | None:
    times = _times(item)
    if times is None:
        return None
    kind, title, fiscal_year = parse_title(item.get("summary", ""))
    start, end = times
    return {
        "type": kind,
        "title": title,
        "datetime_start": start,
        "datetime_end": end,
        "location": (item.get("location") or "").strip() or None,
        # 開催年と同じ年度の前置（「26【例会】…」を2026年に置いた等）は前置なしと同じ扱い
        "fiscal_year": fiscal_year if fiscal_year and fiscal_year > start.year else None,
    }


def _jci_id(item: dict) -> str | None:
    return ((item.get("extendedProperties") or {}).get("private") or {}).get(LINK_KEY)


def _find_linked(repo: Repository, item: dict) -> Event | None:
    """カレンダー予定にリンクしたイベント。複製された予定の誤リンクは避ける。"""
    gid = item.get("id")
    jci_id = _jci_id(item)
    if jci_id:
        event = repo.get_event(jci_id)
        # 予定を複製すると jci_event_id ごとコピーされる。別の予定IDなら別物として扱う。
        if event is not None and event.gcal_event_id in (None, gid):
            return event
    for event in repo.list_events():
        if event.gcal_event_id == gid:
            return event
    return None


# --------------------------------------------------------------------------- #
# 反映
# --------------------------------------------------------------------------- #
def _apply_deleted(repo: Repository, item: dict, now: datetime, summary: PullSummary) -> None:
    event = next((e for e in repo.list_events() if e.gcal_event_id == item.get("id")), None)
    if event is None:
        # システム側で中止して消した予定の折り返し、または未取込の予定
        summary.skipped += 1
        return
    cancelled = event.model_copy(update={
        "status": EventStatus.cancelled, "gcal_event_id": None, "gcal_etag": None,
        "gcal_sync_state": GcalSyncState.synced, "gcal_synced_at": now, "gcal_error": None,
        "updated_at": now,
    })
    repo.upsert_event(cancelled)
    write_audit(repo, actor=ACTOR, action="event.cancel_from_gcal",
                target=event.event_id, detail=event.title)
    summary.cancelled += 1


def _apply_changed(repo: Repository, event: Event, item: dict, fields: dict,
                   now: datetime, summary: PullSummary) -> None:
    if item.get("etag") and item.get("etag") == event.gcal_etag:
        summary.skipped += 1  # 自分の反映の折り返し
        return
    cal_updated = _to_local(item["updated"]) if item.get("updated") else now
    if event.updated_at is not None and event.updated_at > cal_updated:
        # システムの方が新しい → カレンダーをシステムの内容で上書きし直す（後勝ち）
        push_event(repo, event)
        summary.pushed_back += 1
        return
    changed = {k: v for k, v in fields.items() if getattr(event, k) != v}
    updated = event.model_copy(update={
        **changed,
        "gcal_event_id": item.get("id"), "gcal_etag": item.get("etag"),
        "gcal_sync_state": GcalSyncState.synced, "gcal_synced_at": now, "gcal_error": None,
        "updated_at": cal_updated,
    })
    repo.upsert_event(updated)
    if changed:
        write_audit(repo, actor=ACTOR, action="event.update_from_gcal",
                    target=event.event_id, detail=",".join(sorted(changed)))
        summary.updated += 1
    else:
        summary.skipped += 1


def _import_new(repo: Repository, item: dict, fields: dict, now: datetime,
                summary: PullSummary) -> None:
    if fields["type"] == EventType.個別:
        # 個人の打合せ等は出欠を取る会の予定ではないので、システムのイベントにしない
        summary.skipped += 1
        return
    end = fields["datetime_end"] or fields["datetime_start"]
    if end < now - timedelta(days=1):
        summary.skipped += 1  # 終わった予定は取り込まない（初回の遡り分など）
        return
    event = Event(
        event_id=f"ev_{uuid.uuid4().hex[:10]}",
        status=EventStatus.draft,
        origin=EventOrigin.gcal,
        updated_at=_to_local(item["updated"]) if item.get("updated") else now,
        gcal_event_id=item.get("id"),
        gcal_etag=item.get("etag"),
        gcal_sync_state=GcalSyncState.synced,
        gcal_synced_at=now,
        **fields,
    )
    if not in_operation(event, repo.get_settings().operation_start):
        # 運用開始日（年度の始まり）より前の予定は取り込まない（翌年度の「27【…】」は取り込む）
        summary.skipped += 1
        return
    repo.upsert_event(event)
    write_audit(repo, actor=ACTOR, action="event.import_from_gcal",
                target=event.event_id, detail=event.title)
    summary.created += 1
    summary.created_ids.append(event.event_id)


def apply_item(repo: Repository, item: dict, now: datetime, summary: PullSummary) -> None:
    """カレンダー予定1件をシステムへ反映する。"""
    if item.get("status") == "cancelled":
        _apply_deleted(repo, item, now, summary)
        return
    fields = _calendar_fields(item)
    if fields is None:
        summary.skipped += 1  # 終日予定は対象外（祝日カレンダー等の混入防止）
        return
    event = _find_linked(repo, item)
    if event is None:
        _import_new(repo, item, fields, now, summary)
    else:
        _apply_changed(repo, event, item, fields, now, summary)


# --------------------------------------------------------------------------- #
# 差分取得
# --------------------------------------------------------------------------- #
def _fetch(sync_token: str | None, now: datetime) -> tuple[list[dict], str | None]:
    """全ページを取得して (予定, nextSyncToken) を返す。"""
    time_min = None
    if not sync_token:
        time_min = (now - INITIAL_LOOKBACK).replace(tzinfo=JST).isoformat()
    items: list[dict] = []
    page_token = None
    while True:
        res = gcal.list_events(sync_token=sync_token, time_min=time_min, page_token=page_token)
        items.extend(res.get("items", []))
        page_token = res.get("nextPageToken")
        if not page_token:
            return items, res.get("nextSyncToken")


def pull_changes(repo: Repository, now: datetime) -> dict | None:
    """カレンダーの差分を取り込む（tick から呼ぶ）。連携無効なら None。"""
    if not gcal.is_configured():
        return None
    state = repo.get_gcal_sync_state()
    summary = PullSummary()
    try:
        try:
            items, next_token = _fetch(state.sync_token, now)
        except gcal.GcalError as exc:
            if exc.status != 410 or not state.sync_token:
                raise
            # 同期トークン失効 → 全件取得からやり直す
            summary.full_resync = True
            items, next_token = _fetch(None, now)
        summary.fetched = len(items)
        for item in items:
            apply_item(repo, item, now, summary)
    except Exception as exc:  # noqa: BLE001 - tick 本体（催促）を止めない
        logger.warning("カレンダー取込に失敗: %s", exc)
        repo.save_gcal_sync_state(state.model_copy(update={"last_error": str(exc)[:300]}))
        return {"error": "gcal_pull_failed"}

    repo.save_gcal_sync_state(state.model_copy(update={
        "sync_token": next_token or state.sync_token,
        "last_pulled_at": now,
        "last_error": None,
    }))
    return summary.as_dict()
