"""連携済みメンバーの対話応答（docs/mvp-design.md §4.3）。

リッチメニュー/テキストから、次回予定・自分の出欠状況・出欠回答・事務局連絡を扱う。
キーワードで簡易ルーティングする（リッチメニューのテキストアクションにも対応）。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from linebot.v3.messaging import (
    Message,
    PostbackAction,
    QuickReply,
    QuickReplyItem,
    TextMessage,
)

from .assistant import answer_member_question
from .attendance_intent import try_attendance_intent
from .events import resolve_targets
from .line_messages import build_attendance_request
from .models import (
    AttendanceStatus,
    Escalation,
    Event,
    EventStatus,
    Member,
)
from .repository import Repository

# メニュー項目（label, postback action 値）
MENU_SCHEDULE = "次回の予定"
MENU_MY_ATT = "自分の出欠状況"
MENU_ANSWER = "出欠を回答"
MENU_CONTACT = "事務局に連絡"
MENU_ITEMS = [MENU_SCHEDULE, MENU_MY_ATT, MENU_ANSWER, MENU_CONTACT]


def build_menu(prompt: str = "ご用件をお選びください。") -> TextMessage:
    qr = QuickReply(
        items=[
            QuickReplyItem(
                action=PostbackAction(label=label, data=f"menu|{label}", display_text=label)
            )
            for label in MENU_ITEMS
        ]
    )
    return TextMessage(text=prompt, quick_reply=qr)


def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%-m月%-d日(%a) %H:%M")


def _upcoming_events(repo: Repository, now: datetime) -> list[Event]:
    events = repo.list_events(status=EventStatus.open)
    future = [e for e in events if e.datetime_start >= now]
    future.sort(key=lambda e: e.datetime_start)
    if future:
        return future
    # 未来が無ければ開催日時の新しい順
    events.sort(key=lambda e: e.datetime_start, reverse=True)
    return events


def _is_target(repo: Repository, event: Event, member: Member) -> bool:
    return any(m.member_id == member.member_id for m in resolve_targets(repo, event))


def next_schedule_message(repo: Repository, now: datetime) -> TextMessage:
    events = _upcoming_events(repo, now)
    if not events:
        return TextMessage(text="現在、予定されている例会・会議はありません。")
    lines = ["【次回以降の予定】"]
    for e in events[:3]:
        loc = f" / {e.location}" if e.location else ""
        lines.append(f"・{_fmt_dt(e.datetime_start)} {e.title}{loc}")
    return TextMessage(text="\n".join(lines))


def my_attendance_message(repo: Repository, member: Member, now: datetime) -> TextMessage:
    events = _upcoming_events(repo, now)
    mine = [e for e in events if _is_target(repo, e, member)]
    if not mine:
        return TextMessage(text="現在、あなたが対象の予定はありません。")
    lines = ["【あなたの出欠状況】"]
    for e in mine[:5]:
        att = repo.get_attendance(e.event_id, member.member_id)
        status = att.status.value if att else AttendanceStatus.未回答.value
        lines.append(f"・{_fmt_dt(e.datetime_start)} {e.title}: {status}")
    return TextMessage(text="\n".join(lines))


def answer_prompt_message(repo: Repository, member: Member, now: datetime) -> Message:
    events = _upcoming_events(repo, now)
    target = next((e for e in events if _is_target(repo, e, member)), None)
    if target is None:
        return TextMessage(text="現在、回答が必要な出欠はありません。")
    return build_attendance_request(target)


def record_contact(repo: Repository, member: Member, now: datetime) -> TextMessage:
    esc = Escalation(
        escalation_id=f"esc_{uuid.uuid4().hex[:10]}",
        member_id=member.member_id,
        kind="contact",
        created_at=now,
    )
    repo.save_escalation(esc)
    return TextMessage(
        text=(
            "事務局への連絡を受け付けました。担当者から折り返しご連絡します。\n"
            "お急ぎの場合は、案内文のお問い合わせ先へ直接ご連絡ください。"
        )
    )


def handle_member_text(
    repo: Repository, member: Member, text: str, *, now: datetime
) -> list[Message]:
    """連携済みメンバーのテキスト/メニュー選択に応答する。"""
    t = text.strip()
    # postback の "menu|<label>" も同じ値で扱えるよう正規化
    if t.startswith("menu|"):
        t = t.split("|", 1)[1]

    if any(k in t for k in ("予定", "次回", "いつ")):
        return [next_schedule_message(repo, now)]
    if any(k in t for k in ("自分の出欠", "出欠状況", "確認")):
        return [my_attendance_message(repo, member, now)]
    if any(k in t for k in ("出欠を回答", "回答", "出席", "欠席")):
        # 「来週の例会は欠席で」のような自由文は、対象と出欠を解釈して確認する（F4-7）。
        # メニュー由来の定型文や解釈できない場合は従来の出欠依頼を返す。
        if not text.strip().startswith("menu|"):
            intent = try_attendance_intent(repo, member, t, now=now)
            if intent is not None:
                return intent
        return [answer_prompt_message(repo, member, now)]
    if any(k in t for k in ("事務局", "連絡", "問い合わせ", "問合せ")):
        return [record_contact(repo, member, now)]
    if any(k in t for k in ("メニュー", "menu", "ヘルプ", "help")):
        return [build_menu()]

    # 定型に当たらない自由文は自LOM情報を根拠にした応答を試す（F8, assistant）。
    # LLM未設定/失敗時は従来どおりメニューへ誘導する。
    messages = answer_member_question(repo, member, t, now=now)
    if messages is not None:
        return messages
    return [build_menu(f"「{text}」について、以下からお選びください。")]
