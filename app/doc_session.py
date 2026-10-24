"""資料の作業セッション（docs/doc-session-design.md §4）。

Claude との会話のセッションに近い。違いは、1つのセッションが1つの資料に結びつくこと。

［資料を作る・直す］→ 資料を選ぶ（postback `dss|open|<doc_id>`）→ セッション開始
  作業中は、送った文をすべてその資料への指示として扱う（単語での判定はしない）
  AI は「書き換えの案」「聞き返し」「回答」のどれかで答える
  書き換えの案 →［書き込む］（`dse|<session_id>|<op_id>|yes`）で初めて書き込む
  書き込みの後は［さっきの変更を戻す］（`dsu|<session_id>|<op_id>`）→ 確認 → 元に戻す
［作業を終える］・30分操作なし・別の資料を選ぶ → 終了（質問モードへ）

保存先（Firestore）: chatStates/{member_id}・docSessions/{id}・docSessions/{id}/turns・
docSessions/{id}/edits（変更前と変更後）。
"""
from __future__ import annotations

import logging
import urllib.error
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
from .audit import write_audit
from .doc_edit_intent import (
    CONFLICT_TEXT,
    FAILED_TEXT,
    NOT_BOARD_TEXT,
    RETRY_TEXT,
    clip,
    fields_text,
    is_summary,
    knowledge,
)
from .inference import record_inference
from .llm import draft_doc_edit, model_for
from .models import ChatState, DocEdit, DocSession, DocTurn, InferenceUsage, Member
from .repository import Repository

logger = logging.getLogger("jci-agent.doc_session")

ACTION = "dss"  # dss|open|<doc_id> / dss|end / dss|switch / dss|new
ACTION_EDIT = "dse"  # dse|<session_id>|<op_id>|yes|no
ACTION_UNDO = "dsu"  # dsu|<session_id>|<op_id>
SESSION_TTL = timedelta(minutes=30)
OP_TTL = timedelta(minutes=30)
#: AI に渡す直近のやり取り（10往復）
HISTORY_TURNS = 20
#: 選べる資料の数（クイックリプライの上限 13 から［＋新しい事業計画書］の分を引く）
MAX_DOC_CHOICES = 11
PLAN_PREFIX = "【計画書】"

DISABLED_TEXT = "資料の作業は、まだ準備中です。専務にお問い合わせください。"
ENDED_TEXT = "作業を終えました。ふだんのトーク（質問への回答のみ）に戻ります。"
NOT_EDITABLE_TEXT = "この資料は作業できません（更新できる委員会の資料ではありません）。"
START_HINT = (
    "直したい内容を送ってください。案を作り、書き込む前に確認します。\n"
    "例:「背景と目的を書いて」「次第を19:00開始・20:30終了で考えて」\n"
    "資料を変えずに質問することもできます（「去年の4月例会はどんな内容だった？」）。"
)


# --------------------------------------------------------------------------- #
# 表示
# --------------------------------------------------------------------------- #
def header(session: DocSession) -> str:
    return f"📝 編集中：{session.doc_title}"


def _item(label: str, data: str) -> QuickReplyItem:
    return QuickReplyItem(action=PostbackAction(label=label[:20], data=data,
                                                display_text=label[:20]))


def _session_items() -> list[QuickReplyItem]:
    return [_item("作業を終える", f"{ACTION}|end"), _item("別の資料", f"{ACTION}|switch")]


def _reply(session: DocSession, text: str,
           items: list[QuickReplyItem] | None = None) -> TextMessage:
    return TextMessage(text=f"{header(session)}\n\n{text}",
                       quick_reply=QuickReply(items=items or _session_items()))


def doc_label(doc: dict) -> str:
    """ボタンに出す資料の短い名前（20字まで）。"""
    name = doc["name"]
    if is_summary(doc):
        label = f"{doc['committee'].removesuffix('委員会')} サマリー"
    elif name.startswith(PLAN_PREFIX):
        label = f"{name.removeprefix(PLAN_PREFIX).removesuffix('事業計画書')} 計画書"
    else:
        label = name
    return label if len(label) <= 20 else label[:19] + "…"


# --------------------------------------------------------------------------- #
# 資料を選ぶ・セッションの開始と終了
# --------------------------------------------------------------------------- #
def editable_docs(member: Member, token: str) -> list[dict]:
    """本人が更新できる委員会の資料（新しく更新された順）。"""
    folders = committee_docs.committee_folders(token)
    docs = []
    for committee in committee_docs.editable_committees(member, list(folders)):
        for doc in committee_docs.find_docs(folders[committee], token):
            docs.append({**doc, "committee": committee})
    return sorted(docs, key=lambda d: d.get("modified_at") or "", reverse=True)


def start_menu(repo: Repository, member: Member, now: datetime) -> list[Message]:
    """［資料を作る・直す］: 作業する資料を選ぶ。"""
    if not committee_docs.is_board_member(member):
        return [TextMessage(text=NOT_BOARD_TEXT)]
    if not committee_docs.is_enabled():
        return [TextMessage(text=DISABLED_TEXT)]
    try:
        docs = editable_docs(member, committee_docs._token())
    except Exception:  # noqa: BLE001 - ドライブの不調で会話全体を止めない
        logger.exception("資料の一覧の取得に失敗しました")
        return [TextMessage(text=FAILED_TEXT)]
    shown = docs[:MAX_DOC_CHOICES]
    lines = [f"・{d['name']}（{d['committee']}）" for d in shown]
    listing = "\n".join(lines) if lines else "（資料がまだありません）"
    text = f"どの資料の作業をしますか？\n\n{listing}"
    if len(docs) > len(shown):
        text += f"\n\n※ 最近更新された {len(shown)} 件を表示しています。"
    items = [_item(doc_label(d), f"{ACTION}|open|{d['id']}") for d in shown]
    items.append(_item("＋新しい事業計画書", f"{ACTION}|new"))
    return [TextMessage(text=text, quick_reply=QuickReply(items=items))]


def _active_session(repo: Repository, member: Member) -> DocSession | None:
    state = repo.get_chat_state(member.member_id)
    if state is None or state.mode != "doc" or not state.session_id:
        return None
    session = repo.get_doc_session(state.session_id)
    return session if session and session.status == "active" else None


def close_session(repo: Repository, member: Member, now: datetime, reason: str) -> None:
    session = _active_session(repo, member)
    if session is not None:
        repo.save_doc_session(session.model_copy(update={
            "status": "closed", "closed_at": now, "closed_reason": reason}))
    repo.save_chat_state(ChatState(member_id=member.member_id, mode="question", updated_at=now))


def _touch(repo: Repository, member: Member, session: DocSession, now: datetime) -> None:
    repo.save_doc_session(session.model_copy(update={"last_active_at": now}))
    repo.save_chat_state(ChatState(member_id=member.member_id, mode="doc",
                                   session_id=session.session_id,
                                   expires_at=now + SESSION_TTL, updated_at=now))


def open_session(repo: Repository, member: Member, doc_id: str, title: str, committee: str,
                 now: datetime) -> list[Message]:
    """資料の作業セッションを始める（作業中のものがあれば終えてから）。"""
    close_session(repo, member, now, "switched")
    session = DocSession(session_id=f"dss_{uuid.uuid4().hex[:10]}", member_id=member.member_id,
                         doc_id=doc_id, doc_title=title, committee=committee,
                         started_at=now, last_active_at=now)
    repo.save_doc_session(session)
    _touch(repo, member, session, now)
    url = committee_docs.doc_url(doc_id)
    return [_reply(session, f"作業を始めます。\n{url}\n\n{START_HINT}")]


def _open_by_id(repo: Repository, member: Member, doc_id: str, now: datetime) -> list[Message]:
    try:
        docs = editable_docs(member, committee_docs._token())
    except Exception:  # noqa: BLE001
        logger.exception("資料の一覧の取得に失敗しました")
        return [TextMessage(text=FAILED_TEXT)]
    doc = next((d for d in docs if d["id"] == doc_id), None)
    if doc is None:  # 権限が変わった・資料が移動した
        return [TextMessage(text=NOT_EDITABLE_TEXT)]
    return open_session(repo, member, doc["id"], doc["name"], doc["committee"], now)


# --------------------------------------------------------------------------- #
# 作業中のメッセージ
# --------------------------------------------------------------------------- #
def _history(turns: list[DocTurn]) -> str:
    who = {"user": "本人", "assistant": "AI"}
    return "\n".join(f"{who.get(t.role, t.role)}: {t.text}" for t in turns)


def _pending(repo: Repository, session: DocSession, now: datetime) -> DocEdit | None:
    edits = [e for e in repo.list_doc_edits(session.session_id)
             if e.status == "proposed" and e.expires_at > now]
    return edits[-1] if edits else None


def _confirm(session: DocSession, edit: DocEdit) -> TextMessage:
    parts = [f"次の{len(edit.after)}項目を書き込みます。よろしいですか？"]
    if edit.summary:
        parts.append(edit.summary)
    parts += [f"【{name}】\n{clip(text) or '（空欄に戻す）'}" for name, text in edit.after.items()]
    parts.append("※ 書き込んだあとも［さっきの変更を戻す］で元に戻せます。")
    yes = "元に戻す" if edit.kind == "revert" else "書き込む"
    data = f"{ACTION_EDIT}|{session.session_id}|{edit.op_id}"
    return _reply(session, "\n\n".join(parts), [
        _item(yes, f"{data}|yes"), _item("やめる", f"{data}|no"),
        _item("作業を終える", f"{ACTION}|end")])


def handle_turn(repo: Repository, member: Member, text: str, now: datetime,
                session_id: str) -> list[Message] | None:
    """作業中に届いた文に答える。セッションが無効なら None（呼び側で質問として扱う）。"""
    session = repo.get_doc_session(session_id)
    if session is None or session.status != "active":
        return None
    if not committee_docs.can_edit(member, session.committee):
        close_session(repo, member, now, "permission")
        return [TextMessage(text=NOT_BOARD_TEXT)]

    history = _history(repo.list_doc_turns(session.session_id, limit=HISTORY_TURNS))
    repo.add_doc_turn(session.session_id, DocTurn(at=now, role="user", kind="instruction",
                                                  text=text))
    _touch(repo, member, session, now)
    try:
        content = committee_docs.read_doc(session.doc_id, committee_docs._token())
    except Exception:  # noqa: BLE001
        logger.exception("資料の読み込みに失敗しました")
        return [_reply(session, FAILED_TEXT)]
    fields = committee_docs.extract_fields(content)
    editable = {f.name for f in fields if f.editable}
    pending = _pending(repo, session, now)
    pending_text = "\n".join(f"- {k}: {v}" for k, v in pending.after.items()) if pending else ""

    outcome = draft_doc_edit(text, session.committee, session.doc_title, fields_text(fields),
                             knowledge(member, session.committee, text),
                             history=history, pending=pending_text)
    if outcome is None:
        record_inference(repo, kind="doc_edit", usage=InferenceUsage(model=model_for("doc_edit")),
                         now=now, target=member.member_id, ok=False, error="generation_failed")
        return [_reply(session, f"返答を作れませんでした。{RETRY_TEXT}")]
    record_inference(repo, kind="doc_edit", usage=outcome.usage, now=now, target=member.member_id)
    draft = outcome.draft
    edits = {k: v for k, v in draft.edits.items() if k in editable}

    if draft.kind != "edit" or not edits:
        message = draft.message or "どの項目をどう直すか、もう少し詳しく教えてください。"
        kind = draft.kind if draft.kind in ("ask", "answer") else "ask"
        repo.add_doc_turn(session.session_id, DocTurn(at=now, role="assistant", kind=kind,
                                                      text=message))
        return [_reply(session, message)]

    # 新しい案を出したら、確認待ちだった古い案は取り下げる
    for old in repo.list_doc_edits(session.session_id):
        if old.status == "proposed":
            repo.save_doc_edit(old.model_copy(update={"status": "declined"}))
    edit = DocEdit(op_id=f"dse_{uuid.uuid4().hex[:10]}", session_id=session.session_id,
                   member_id=member.member_id, doc_id=session.doc_id, doc_title=session.doc_title,
                   committee=session.committee, revision_id=content.get("revisionId", ""),
                   after=edits, summary=draft.message, created_at=now, expires_at=now + OP_TTL)
    repo.save_doc_edit(edit)
    summary = "・".join(edits)
    repo.add_doc_turn(session.session_id, DocTurn(
        at=now, role="assistant", kind="edit", op_id=edit.op_id,
        text=f"{draft.message or ''}（案: {summary}）".strip()))
    return [_confirm(session, edit)]


# --------------------------------------------------------------------------- #
# 書き込み・取り消し
# --------------------------------------------------------------------------- #
def _apply(repo: Repository, member: Member, session: DocSession, edit: DocEdit,
           now: datetime) -> list[Message]:
    # 二重押しで2回書き込まないよう、先に処理済みにする
    repo.save_doc_edit(edit.model_copy(update={"status": "applied", "applied_at": now}))
    try:
        token = committee_docs._token()
        content = committee_docs.read_doc(edit.doc_id, token)
        if content.get("revisionId") != edit.revision_id:
            raise _Conflict
        fields = committee_docs.extract_fields(content)
        before = {f.name: f.text for f in fields if f.name in edit.after}
        requests = committee_docs.build_requests(fields, edit.after,
                                                 allow_empty=edit.kind == "revert")
        if not requests:
            repo.save_doc_edit(edit.model_copy(update={"status": "declined"}))
            return [_reply(session, "書き換える項目が見つかりませんでした。")]
        committee_docs.apply(edit.doc_id, edit.revision_id, requests, token)
    except (_Conflict, urllib.error.HTTPError) as exc:
        if isinstance(exc, urllib.error.HTTPError) and exc.code != 400:
            logger.exception("資料の書き込みに失敗しました")
            repo.save_doc_edit(edit.model_copy(update={"status": "declined"}))
            return [_reply(session, f"書き込みに失敗しました。{RETRY_TEXT}")]
        repo.save_doc_edit(edit.model_copy(update={"status": "conflict"}))
        repo.add_doc_turn(session.session_id, DocTurn(at=now, role="assistant", kind="conflict",
                                                      text=CONFLICT_TEXT, op_id=edit.op_id))
        return [_reply(session, CONFLICT_TEXT)]
    except Exception:  # noqa: BLE001
        logger.exception("資料の書き込みに失敗しました")
        repo.save_doc_edit(edit.model_copy(update={"status": "declined"}))
        return [_reply(session, f"書き込みに失敗しました。{RETRY_TEXT}")]

    repo.save_doc_edit(edit.model_copy(update={"status": "applied", "applied_at": now,
                                               "before": before}))
    if edit.kind == "revert" and edit.revert_of:
        original = repo.get_doc_edit(session.session_id, edit.revert_of)
        if original is not None:
            repo.save_doc_edit(original.model_copy(update={"status": "reverted"}))
    write_audit(repo, actor=f"line:{member.member_id}", action="doc.edit", target=edit.doc_id,
                detail=f"{edit.committee} / {edit.doc_title} / {edit.kind} / "
                       f"{'・'.join(edit.after)}")
    done = "元に戻しました" if edit.kind == "revert" else "書き込みました"
    repo.add_doc_turn(session.session_id, DocTurn(
        at=now, role="assistant", kind="reverted" if edit.kind == "revert" else "applied",
        text=f"{done}（{'・'.join(edit.after)}）", op_id=edit.op_id))
    _touch(repo, member, session, now)
    items = [_item("さっきの変更を戻す", f"{ACTION_UNDO}|{session.session_id}|{edit.op_id}"),
             *_session_items()]
    return [_reply(session, f"{done}（{len(edit.after)}項目）。\n"
                            f"{committee_docs.doc_url(edit.doc_id)}", items)]


class _Conflict(Exception):
    """読んだときの版から資料が変わっていた。"""


def _decide(repo: Repository, member: Member, data: str, now: datetime) -> list[Message]:
    """`dse|<session_id>|<op_id>|yes|no`。"""
    parts = data.split("|")
    if len(parts) != 4:
        return [TextMessage(text="この確認は無効です。")]
    _, session_id, op_id, answer = parts
    edit = repo.get_doc_edit(session_id, op_id)
    session = repo.get_doc_session(session_id)
    # 確認は依頼した本人だけが押せる
    if edit is None or session is None or edit.member_id != member.member_id:
        return [TextMessage(text="この確認は無効です。")]
    if edit.status != "proposed":
        return [_reply(session, "この案はすでに処理済みか、新しい案に置き換わっています。")]
    if now > edit.expires_at:
        repo.save_doc_edit(edit.model_copy(update={"status": "expired"}))
        return [_reply(session, "確認の有効期限（30分）が切れました。もう一度お送りください。")]
    if answer != "yes":
        repo.save_doc_edit(edit.model_copy(update={"status": "declined"}))
        repo.add_doc_turn(session_id, DocTurn(at=now, role="assistant", kind="declined",
                                              text="案を取りやめました", op_id=op_id))
        return [_reply(session, "取りやめました。ほかに直したい内容があれば送ってください。")]
    if not committee_docs.can_edit(member, session.committee):
        return [TextMessage(text=NOT_BOARD_TEXT)]
    return _apply(repo, member, session, edit, now)


def _propose_revert(repo: Repository, member: Member, data: str,
                    now: datetime) -> list[Message]:
    """［さっきの変更を戻す］: 書き込み前の内容に戻す案を作り、確認を求める。"""
    parts = data.split("|")
    if len(parts) != 3:
        return [TextMessage(text="この操作は無効です。")]
    _, session_id, op_id = parts
    original = repo.get_doc_edit(session_id, op_id)
    session = repo.get_doc_session(session_id)
    if original is None or session is None or original.member_id != member.member_id:
        return [TextMessage(text="この操作は無効です。")]
    if original.status != "applied" or not original.before:
        return [_reply(session, "この変更は元に戻せません（すでに戻したか、書き込んでいません）。")]
    try:
        content = committee_docs.read_doc(session.doc_id, committee_docs._token())
    except Exception:  # noqa: BLE001
        logger.exception("資料の読み込みに失敗しました")
        return [_reply(session, FAILED_TEXT)]
    edit = DocEdit(op_id=f"dse_{uuid.uuid4().hex[:10]}", session_id=session_id,
                   member_id=member.member_id, doc_id=session.doc_id, doc_title=session.doc_title,
                   committee=session.committee, kind="revert", revert_of=op_id,
                   revision_id=content.get("revisionId", ""), after=dict(original.before),
                   summary="直前の書き込みを、書き込む前の内容に戻します。",
                   created_at=now, expires_at=now + OP_TTL)
    repo.save_doc_edit(edit)
    return [_confirm(session, edit)]


def handle_postback(repo: Repository, member: Member, data: str,
                    now: datetime) -> list[Message]:
    """作業セッションのボタン（dss / dse / dsu）。"""
    if data.startswith(f"{ACTION_EDIT}|"):
        return _decide(repo, member, data, now)
    if data.startswith(f"{ACTION_UNDO}|"):
        return _propose_revert(repo, member, data, now)
    parts = data.split("|")
    command = parts[1] if len(parts) > 1 else ""
    if command == "end":
        close_session(repo, member, now, "user")
        return [TextMessage(text=ENDED_TEXT)]
    if not committee_docs.is_board_member(member):
        return [TextMessage(text=NOT_BOARD_TEXT)]
    if command == "switch":
        return start_menu(repo, member, now)
    if command == "open" and len(parts) == 3:
        return _open_by_id(repo, member, parts[2], now)
    if command == "new":
        from .proposal_intent import start_new_plan

        try:
            folders = committee_docs.committee_folders(committee_docs._token())
        except Exception:  # noqa: BLE001
            logger.exception("委員会フォルダの読み込みに失敗しました")
            return [TextMessage(text=FAILED_TEXT)]
        committees = committee_docs.editable_committees(member, list(folders))
        return start_new_plan(repo, member, committees, now)
    return [TextMessage(text="この操作は無効です。")]
