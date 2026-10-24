"""連携済みメンバーの対話応答（docs/mvp-design.md §4.3・docs/doc-session-design.md）。

**原則: リッチメニューのボタンで作業を始めない限り、チャットは検索・回答のみで書き込まない。**

- メニューのボタン（postback `menu|<label>`）: 次回予定・自分の出欠状況・出欠回答・事務局連絡、
  理事・五役の［資料を作る・直す］［予定を登録・変更］。書き込みはボタンから始めた流れの中だけ。
- 自由文: 質問として答える。書き込みの依頼に見える文には、その作業を始めるボタンを返すだけ。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from linebot.v3.messaging import (
    Message,
    PostbackAction,
    QuickReply,
    QuickReplyItem,
    TextMessage,
)

from . import committee_docs
from .assistant import answer_member_question
from .calendar_intent import HELP_TEXT as CALENDAR_HELP_TEXT
from .calendar_intent import can_edit_calendar, looks_like_calendar_write, try_calendar_intent
from .doc_edit_intent import NOT_BOARD_TEXT, looks_like_doc_edit
from .events import resolve_targets
from .jpdate import month_day_time
from .line_messages import build_attendance_request
from .models import (
    AttendanceStatus,
    ChatState,
    Escalation,
    Event,
    EventStatus,
    Member,
)
from .proposal_intent import looks_like_create
from .repository import Repository

# メニュー項目（label, postback action 値）
MENU_SCHEDULE = "次回の予定"
MENU_MY_ATT = "自分の出欠状況"
MENU_ANSWER = "出欠を回答"
MENU_CONTACT = "事務局に連絡"
MENU_ITEMS = [MENU_SCHEDULE, MENU_MY_ATT, MENU_ANSWER, MENU_CONTACT]
# 立場ごとのリッチメニューにだけ出るボタン（docs/doc-session-design.md §3）
MENU_DOCS = "資料を作る・直す"
MENU_CALENDAR = "予定を登録・変更"
MENU_HELP = "使い方"


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
    return month_day_time(dt)


#: これ以下の長さの入力はコマンド（「次回の予定」「出欠確認」等）とみなし、キーワードで定型応答する
SHORT_COMMAND_MAX = 12

#: 終了時刻が未設定のイベントは、開始からこの時間までを「開催中」とみなす
DEFAULT_DURATION = timedelta(hours=2)


def _upcoming_events(repo: Repository, now: datetime) -> list[Event]:
    """これから開催（開催中を含む）の受付中イベントを開催順に返す。

    過去のイベントは返さない。以前は未来が無いと過去を新しい順に返しており、
    「次回以降の予定」に終わった会議が出たり、過去のイベントの出欠回答を促したりしていた。
    """
    events = [
        e for e in repo.list_events(status=EventStatus.open)
        if (e.datetime_end or e.datetime_start + DEFAULT_DURATION) >= now
    ]
    return sorted(events, key=lambda e: e.datetime_start)


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


#: ［予定を登録・変更］の後、予定の指示として読む時間（その後の1通だけ）
CALENDAR_TTL = timedelta(minutes=10)
ATTENDANCE_WORDS = ("出欠を回答", "回答", "出席", "欠席")
CONTACT_WORDS = ("事務局", "連絡", "問い合わせ", "問合せ")

DOCS_GUIDE = (
    "資料の作成・更新は、メニューの［資料を作る・直す］から始めてください。"
    "チャットの文だけでは資料を書き換えません。"
)
CALENDAR_GUIDE = (
    "予定の登録・変更は、［予定を登録・変更］を押してから送ってください。"
    "チャットの文だけでは予定を書き換えません。"
)
ATTENDANCE_GUIDE = "出欠の回答は、下の［出欠を回答］から行えます。"
CONTACT_GUIDE = "事務局への連絡は、下の［事務局に連絡］を押すと受け付けます。"
HELP_GUIDE = (
    "【専務AIの使い方】\n"
    "・ふだんのトークは、質問への回答だけを行います（資料や予定を書き換えることはありません）。\n"
    "・出欠の回答、事務局への連絡は、メニューのボタンからどうぞ。\n"
    "・資料の作成・更新は［資料を作る・直す］から。対象の資料を選んでから指示を送ります。"
)
DOCS_NOT_BOARD = NOT_BOARD_TEXT
DOCS_COMING = "資料の作業は準備中です。次の更新で、資料を選んでから指示を送れるようになります。"
CALENDAR_NOT_OFFICER = "予定の登録・変更は、五役・監事・事務局のみご利用いただけます。"
CALENDAR_START = (
    "登録・変更・中止したい予定を、1通で送ってください（10分以内）。\n"
    "例:「10/20 19時から理事会を登録 会場は体験交流館」\n"
    "　　「11月例会を18日19時半に変更」「第8回理事会を中止」"
)


def _button(label: str, text: str) -> TextMessage:
    """作業を始めるボタン（押すとメニューのボタンと同じ流れになる）だけを付けた案内。"""
    return TextMessage(text=text, quick_reply=QuickReply(items=[QuickReplyItem(
        action=PostbackAction(label=label, data=f"menu|{label}", display_text=label))]))


def _active_state(repo: Repository, member: Member, now: datetime) -> ChatState | None:
    state = repo.get_chat_state(member.member_id)
    if state is None or state.mode == "question":
        return None
    if state.expires_at is None or state.expires_at <= now:
        return None
    return state


def _start_calendar(repo: Repository, member: Member, now: datetime) -> list[Message]:
    if not can_edit_calendar(member):
        return [TextMessage(text=CALENDAR_NOT_OFFICER)]
    repo.save_chat_state(ChatState(member_id=member.member_id, mode="calendar",
                                   expires_at=now + CALENDAR_TTL, updated_at=now))
    return [TextMessage(text=CALENDAR_START)]


def _calendar_turn(repo: Repository, member: Member, text: str, now: datetime) -> list[Message]:
    """［予定を登録・変更］の後の1通を予定の指示として読む（確認してから反映するのは従来どおり）。"""
    repo.save_chat_state(ChatState(member_id=member.member_id, mode="question", updated_at=now))
    return try_calendar_intent(repo, member, text, now=now, explicit=True) or [
        TextMessage(text=CALENDAR_HELP_TEXT)]


def _menu(repo: Repository, member: Member, label: str, now: datetime) -> list[Message]:
    """リッチメニューのボタン。書き込みが始まるのはここからだけ。"""
    if label == MENU_SCHEDULE:
        return [next_schedule_message(repo, now)]
    if label == MENU_MY_ATT:
        return [my_attendance_message(repo, member, now)]
    if label == MENU_ANSWER:
        return [answer_prompt_message(repo, member, now)]
    if label == MENU_CONTACT:
        return [record_contact(repo, member, now)]
    if label == MENU_CALENDAR:
        return _start_calendar(repo, member, now)
    if label == MENU_DOCS:
        if not committee_docs.is_board_member(member):
            return [TextMessage(text=DOCS_NOT_BOARD)]
        return [TextMessage(text=DOCS_COMING)]
    if label == MENU_HELP:
        return [TextMessage(text=HELP_GUIDE)]
    return [build_menu()]


def _with_button(messages: list[Message], label: str) -> list[Message]:
    """回答の最後のメッセージに、作業を始めるボタンを足す（既存のクイックリプライは残す）。"""
    last = messages[-1]
    if isinstance(last, TextMessage):
        item = QuickReplyItem(action=PostbackAction(label=label, data=f"menu|{label}",
                                                    display_text=label))
        items = list(last.quick_reply.items) if last.quick_reply else []
        last.quick_reply = QuickReply(items=[*items, item][:13])
    return messages


def handle_member_text(
    repo: Repository, member: Member, text: str, *, now: datetime, in_group: bool = False,
) -> list[Message]:
    """連携済みメンバーのテキスト/メニュー選択に応答する（in_group: グループでのメンション）。

    自由文では書き込まない（docs/doc-session-design.md §1）。書き込みはメニューのボタン
    （`menu|<label>`）と、そのボタンで始めた状態（ChatState）の中だけ。
    """
    t = text.strip()
    if t.startswith("menu|"):
        return _menu(repo, member, t.split("|", 1)[1], now)

    state = None if in_group else _active_state(repo, member, now)
    if state is not None and state.mode == "calendar":
        return _calendar_turn(repo, member, t, now)

    # ---- ここから自由文: 検索・回答のみ（書き込みなし） ----
    if committee_docs.is_board_member(member) and (looks_like_create(t) or looks_like_doc_edit(t)):
        return [_button(MENU_DOCS, DOCS_GUIDE)]
    if can_edit_calendar(member) and looks_like_calendar_write(t):
        return [_button(MENU_CALENDAR, CALENDAR_GUIDE)]

    # キーワードの定型応答は短い文（コマンド的な入力）のときだけ。いずれも読むだけ。
    command = len(t) <= SHORT_COMMAND_MAX
    if command and any(k in t for k in ("予定", "次回", "いつ")):
        return [next_schedule_message(repo, now)]
    if command and any(k in t for k in ("自分の出欠", "出欠状況", "確認")):
        return [my_attendance_message(repo, member, now)]
    if command and any(k in t for k in ATTENDANCE_WORDS):
        # 出欠の案内（出席・欠席のボタン）を見せるだけ。登録はボタンを押したとき
        return [answer_prompt_message(repo, member, now)]
    if command and any(k in t for k in CONTACT_WORDS):
        return [_button(MENU_CONTACT, CONTACT_GUIDE)]
    if command and any(k in t for k in ("メニュー", "menu", "ヘルプ", "help", "使い方")):
        return [build_menu()]

    # 自LOM情報を根拠にした回答（F8, assistant）。出欠の文なら［出欠を回答］のボタンも添える
    attendance = any(k in t for k in ATTENDANCE_WORDS)
    messages = answer_member_question(repo, member, t, now=now)
    if messages is not None:
        return _with_button(messages, MENU_ANSWER) if attendance else messages
    if attendance:
        return [_button(MENU_ANSWER, ATTENDANCE_GUIDE)]
    return [build_menu(f"「{text}」について、以下からお選びください。")]
