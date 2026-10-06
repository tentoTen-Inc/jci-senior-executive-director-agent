"""LINE の自然文で予定を登録・変更・中止する（F3-2 / docs/calendar-design.md §5.2）。

役員（五役）と事務局だけが使える。「10/20 19時から理事会を登録 会場は体験交流館」のような文を
Gemini で解釈し、**確認メッセージ（はい／いいえ）を返すだけ**にする。
本人が「はい」を押したとき（postback `cal|<op_id>|yes`）に初めて反映する。

- LINE で作った予定は `draft`。会員への出欠依頼は管理画面で「受付中」にしてから。
- 反映は管理画面と同じくカレンダーにも行う（calendar_sync.push_event）。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from linebot.v3.messaging import (
    Message,
    PostbackAction,
    QuickReply,
    QuickReplyItem,
    TextMessage,
)

from .audit import write_audit
from .calendar_sync import calendar_title, push_event
from .inference import record_inference
from .llm import CalendarIntent, model_name, parse_calendar_intent
from .models import (
    CalendarOpFields,
    Event,
    EventOrigin,
    EventStatus,
    EventType,
    GcalSyncState,
    InferenceUsage,
    Member,
    MemberType,
    PendingCalendarOp,
)
from .repository import Repository
from .summary import OFFICER_ROLES

logger = logging.getLogger("jci-agent.calendar_intent")

ACTION_CAL = "cal"
OP_TTL = timedelta(minutes=30)
MAX_CANDIDATES = 10
#: 書き込み系の動詞。これを含む文だけ Gemini に回す（通常の問い合わせのコストを増やさない）
WRITE_WORDS = (
    "登録", "追加", "入れて", "作成", "作って", "予約",
    "変更", "変えて", "ずらし", "移動", "延期", "修正",
    "中止", "キャンセル", "削除", "取り消", "取りやめ",
)
HELP_TEXT = (
    "予定の内容をうまく読み取れませんでした。\n"
    "例:「10/20 19時から理事会を登録 会場は体験交流館」\n"
    "　　「11月例会を18日19時半に変更」\n"
    "　　「第8回理事会を中止」"
)
PAST_TEXT = "過去の日時になっています。日付をもう一度お知らせください。"
END_BEFORE_START_TEXT = "終了が開始より前になっています。時刻をもう一度お知らせください。"
_WEEKDAYS = "月火水木金土日"


# --------------------------------------------------------------------------- #
# 判定
# --------------------------------------------------------------------------- #
def can_edit_calendar(member: Member) -> bool:
    """予定を操作できる会員か（五役と事務局）。"""
    return member.officer_role in OFFICER_ROLES or member.member_type == MemberType.office


def looks_like_calendar_write(text: str) -> bool:
    return any(word in text for word in WRITE_WORDS)


def _candidates(repo: Repository, now: datetime) -> list[Event]:
    """変更・中止の対象になりうる、これから開催の予定（開催順）。"""
    events = [
        e for e in repo.list_events()
        if e.status != EventStatus.cancelled and e.datetime_start >= now - timedelta(days=1)
    ]
    return sorted(events, key=lambda e: e.datetime_start)[:MAX_CANDIDATES]


def _events_text(events: list[Event]) -> str:
    return "\n".join(
        f"- id={e.event_id} / {e.type} / {e.title} / "
        f"{e.datetime_start.strftime('%Y-%m-%d %H:%M')} / {e.location or '場所未定'}"
        for e in events
    )


# --------------------------------------------------------------------------- #
# 表示
# --------------------------------------------------------------------------- #
def _when(start: datetime, end: datetime | None) -> str:
    text = f"{start.month}月{start.day}日({_WEEKDAYS[start.weekday()]}) {start:%H:%M}"
    if end:
        text += f"〜{end:%H:%M}" if end.date() == start.date() else f"〜{end:%-m/%-d %H:%M}"
    return text


def _describe(kind: EventType, title: str, start: datetime, end: datetime | None,
              location: str | None) -> str:
    probe = Event(event_id="_", type=kind, title=title, datetime_start=start)
    lines = [calendar_title(probe), _when(start, end)]
    lines.append(f"場所: {location}" if location else "場所: 未定")
    return "\n".join(lines)


def _confirm(op: PendingCalendarOp, text: str) -> TextMessage:
    qr = QuickReply(items=[
        QuickReplyItem(action=PostbackAction(
            label="はい", data=f"{ACTION_CAL}|{op.op_id}|yes", display_text="はい",
        )),
        QuickReplyItem(action=PostbackAction(
            label="いいえ", data=f"{ACTION_CAL}|{op.op_id}|no", display_text="いいえ",
        )),
    ])
    return TextMessage(text=text, quick_reply=qr)


# --------------------------------------------------------------------------- #
# 解釈 → 確認
# --------------------------------------------------------------------------- #
def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _parse_type(value: str | None) -> EventType | None:
    try:
        return EventType(value) if value else None
    except ValueError:
        return None


def _ask(intent: CalendarIntent) -> list[Message]:
    return [TextMessage(text=intent.question or HELP_TEXT)]


def _new_op(member: Member, action: str, now: datetime, *, event_id: str | None = None,
            fields: CalendarOpFields | None = None) -> PendingCalendarOp:
    return PendingCalendarOp(
        op_id=f"cop_{uuid.uuid4().hex[:10]}",
        member_id=member.member_id,
        action=action,
        event_id=event_id,
        fields=fields or CalendarOpFields(),
        created_at=now,
        expires_at=now + OP_TTL,
    )


def _plan_create(member: Member, intent: CalendarIntent, now: datetime):
    start = _parse_dt(intent.start)
    kind = _parse_type(intent.type) or EventType.イベント
    title = intent.title or (intent.type if _parse_type(intent.type) else None)
    if start is None or not title:
        return None, _ask(intent)
    if start < now:
        return None, [TextMessage(text=PAST_TEXT)]
    end = _parse_dt(intent.end)
    if end is not None and end <= start:
        end = None
    fields = CalendarOpFields(
        type=kind, title=title, datetime_start=start, datetime_end=end, location=intent.location,
    )
    op = _new_op(member, "create", now, fields=fields)
    text = (
        "次の予定を登録します。よろしいですか？\n\n"
        + _describe(kind, title, start, end, intent.location)
        + "\n\n※ 下書きとして登録し、Googleカレンダーにも反映します。"
    )
    return op, [_confirm(op, text)]


def _plan_update(member: Member, intent: CalendarIntent, event: Event, now: datetime):
    fields = CalendarOpFields(
        type=_parse_type(intent.type),
        title=intent.title,
        datetime_start=_parse_dt(intent.start),
        datetime_end=_parse_dt(intent.end),
        location=intent.location,
    )
    changes = {k: v for k, v in fields.model_dump().items() if v is not None}
    changes = {k: v for k, v in changes.items() if getattr(event, k) != v}
    if not changes:
        return None, _ask(intent)
    start = changes.get("datetime_start", event.datetime_start)
    end = changes.get("datetime_end", event.datetime_end)
    if "datetime_start" in changes and "datetime_end" not in changes and event.datetime_end:
        # 開始だけ動かしたら、元の長さを保って終了も動かす
        end = start + (event.datetime_end - event.datetime_start)
        fields.datetime_end = end
    if end is not None and end <= start:
        return None, [TextMessage(text=END_BEFORE_START_TEXT)]
    op = _new_op(member, "update", now, event_id=event.event_id, fields=fields)
    after = _describe(
        changes.get("type", event.type), changes.get("title", event.title), start, end,
        changes.get("location", event.location),
    )
    before = _describe(event.type, event.title, event.datetime_start, event.datetime_end,
                       event.location)
    text = (
        "予定を次のように変更します。よろしいですか？\n\n"
        f"【変更前】\n{before}\n\n【変更後】\n{after}"
    )
    return op, [_confirm(op, text)]


def _plan_cancel(member: Member, event: Event, now: datetime):
    op = _new_op(member, "cancel", now, event_id=event.event_id)
    text = (
        "次の予定を中止にします。よろしいですか？\n\n"
        + _describe(event.type, event.title, event.datetime_start, event.datetime_end,
                    event.location)
        + "\n\n※ Googleカレンダーからも削除します（出欠データは残ります）。"
    )
    return op, [_confirm(op, text)]


def try_calendar_intent(
    repo: Repository, member: Member, text: str, *, now: datetime
) -> list[Message] | None:
    """予定操作の文なら確認メッセージ（または聞き返し）を返す。対象外なら None。"""
    if not can_edit_calendar(member) or not looks_like_calendar_write(text):
        return None
    candidates = _candidates(repo, now)
    outcome = parse_calendar_intent(text, _events_text(candidates), now)
    if outcome is None:
        record_inference(
            repo, kind="calendar_intent", usage=InferenceUsage(model=model_name()),
            now=now, target=member.member_id, ok=False, error="generation_failed",
        )
        return None
    record_inference(
        repo, kind="calendar_intent", usage=outcome.usage, now=now, target=member.member_id
    )
    intent = outcome.intent
    if intent.action not in ("create", "update", "cancel"):
        return None  # 出欠回答など予定操作ではない → 既存の応答へ
    if not intent.confident:
        return _ask(intent)

    if intent.action == "create":
        op, messages = _plan_create(member, intent, now)
    else:
        event = next((e for e in candidates if e.event_id == intent.event_id), None)
        if event is None:
            return [TextMessage(
                text="対象の予定が見つかりませんでした。予定名と日付を添えてもう一度お送りください。"
            )]
        if intent.action == "update":
            op, messages = _plan_update(member, intent, event, now)
        else:
            op, messages = _plan_cancel(member, event, now)
    if op is not None:
        repo.save_calendar_op(op)
    return messages


# --------------------------------------------------------------------------- #
# 確認 → 反映
# --------------------------------------------------------------------------- #
def _gcal_note(event: Event) -> str:
    if event.gcal_sync_state == GcalSyncState.synced:
        return "\n📅 Googleカレンダーにも反映しました。"
    if event.gcal_sync_state == GcalSyncState.error:
        return "\n📅 カレンダーへの反映に失敗したため、自動で再試行します。"
    return ""


def _apply(repo: Repository, op: PendingCalendarOp, now: datetime) -> list[Message]:
    actor = f"line:{op.member_id}"
    f = op.fields
    if op.action == "create":
        event = Event(
            event_id=f"ev_{uuid.uuid4().hex[:10]}",
            type=f.type or EventType.イベント,
            title=f.title or str(f.type or EventType.イベント),
            datetime_start=f.datetime_start,
            datetime_end=f.datetime_end,
            location=f.location,
            status=EventStatus.draft,
            origin=EventOrigin.line,
            updated_at=now,
        )
        repo.upsert_event(event)
        write_audit(repo, actor=actor, action="event.create", target=event.event_id,
                    detail=event.title)
        event = push_event(repo, event)
        return [TextMessage(
            text=f"「{calendar_title(event)}」を下書きで登録しました。"
            + _gcal_note(event)
            + "\n会員への出欠依頼は、管理画面で「受付中」にすると配信されます。"
        )]

    event = repo.get_event(op.event_id) if op.event_id else None
    if event is None or event.status == EventStatus.cancelled:
        return [TextMessage(text="対象の予定が見つからないか、すでに中止されています。")]

    if op.action == "update":
        changes = {k: v for k, v in f.model_dump().items() if v is not None}
        updated = event.model_copy(update={**changes, "updated_at": now})
        repo.upsert_event(updated)
        write_audit(repo, actor=actor, action="event.update", target=event.event_id,
                    detail=",".join(sorted(changes)))
        updated = push_event(repo, updated)
        return [TextMessage(text=f"「{calendar_title(updated)}」を変更しました。"
                            + _gcal_note(updated))]

    cancelled = event.model_copy(update={"status": EventStatus.cancelled, "updated_at": now})
    repo.upsert_event(cancelled)
    write_audit(repo, actor=actor, action="event.cancel", target=event.event_id,
                detail=event.title)
    cancelled = push_event(repo, cancelled)
    if cancelled.gcal_sync_state == GcalSyncState.synced and event.gcal_event_id:
        note = "\n📅 Googleカレンダーからも削除しました。"
    elif cancelled.gcal_sync_state == GcalSyncState.error:
        note = _gcal_note(cancelled)
    else:
        note = ""
    return [TextMessage(text=f"「{calendar_title(event)}」を中止にしました。" + note)]


def handle_calendar_postback(
    repo: Repository, member: Member, data: str, *, now: datetime
) -> list[Message]:
    """確認メッセージの「はい／いいえ」（`cal|<op_id>|yes|no`）を処理する。"""
    parts = data.split("|")
    if len(parts) != 3:
        return [TextMessage(text="この確認は無効です。")]
    _, op_id, answer = parts
    op = repo.get_calendar_op(op_id)
    # 確認は依頼した本人だけが押せる（グループで他の人が押しても反映しない）
    if op is None or op.member_id != member.member_id:
        return [TextMessage(text="この確認は無効です。")]
    if op.status != "pending":
        return [TextMessage(text="この操作はすでに処理済みです。")]
    if now > op.expires_at:
        repo.save_calendar_op(op.model_copy(update={"status": "expired"}))
        return [TextMessage(text="確認の有効期限（30分）が切れました。もう一度お送りください。")]
    if answer != "yes":
        repo.save_calendar_op(op.model_copy(update={"status": "declined"}))
        return [TextMessage(text="取りやめました。")]

    # 二重押しで2回反映しないよう、先に処理済みにする
    repo.save_calendar_op(op.model_copy(update={"status": "done"}))
    return _apply(repo, op, now)
