"""LINE の依頼で委員会資料を更新する（docs/committee-docs-edit-design.md §4）。

理事メンバーが個別トークで「〇〇な委員会にしたいのでサマリーを更新して」と送ると、
対象の委員会と資料を決めて AI が表の項目の案を作り、
**確認メッセージ（更新する／やめる）を返すだけ**にする。
本人が「更新する」を押したとき（postback `doc|<op_id>|yes`）に初めて書き込む。

- 更新できる委員会は committee_docs.can_edit（理事長・専務は全部、副理事長は担当、委員長は所属）。
- 読んだときの版から誰かが編集していたら書き込まない（requiredRevisionId）。
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

from . import committee_docs, drive_knowledge, lake_knowledge
from .audit import write_audit
from .inference import record_inference
from .llm import draft_doc_edit, model_for
from .models import InferenceUsage, Member, PendingDocEdit
from .repository import Repository

logger = logging.getLogger("jci-agent.doc_edit_intent")

ACTION_DOC = "doc"
OP_TTL = timedelta(minutes=30)
#: 資料を指す語と、書き換えを頼む語の両方があるときだけ依頼とみなす（通常の質問を巻き込まない）
DOC_WORDS = ("サマリー", "計画書", "事業計画", "年間計画", "資料", "ドキュメント",
             "スローガン", "現状分析", "目指すべき状態", "事業概要", "事業手法")
EDIT_WORDS = ("更新", "書いて", "書き換", "直して", "修正", "記入", "反映", "変えて", "変更",
              "作って", "作成", "埋めて", "入れて", "追記", "足して", "加えて", "まとめて")
#: サマリー（年間計画）を指す語
SUMMARY_WORDS = ("サマリー", "年間計画", "スローガン", "現状分析", "目指すべき状態",
                 "事業概要", "事業手法")
#: 確認メッセージで1項目あたりに見せる文字数（LINE の上限 5,000 字に収める）
PREVIEW_CHARS = 300
KNOWLEDGE_CHUNKS = 5
KNOWLEDGE_CHARS = 600

GROUP_TEXT = "資料の更新は、専務AIとの個別トークでお送りください。"
NOT_BOARD_TEXT = (
    "資料の更新は、理事メンバー（理事長・副理事長・専務理事・委員長）のみご利用いただけます。"
)
RETRY_TEXT = "少し時間をおいて、もう一度お送りください。"
DISABLED_TEXT = "資料の更新は、まだ準備中です。専務にお問い合わせください。"
NO_COMMITTEE_TEXT = "更新できる委員会の資料が見つかりませんでした。"
FAILED_TEXT = "資料の読み込みに失敗しました。少し時間をおいて、もう一度お送りください。"
CONFLICT_TEXT = (
    "確認のあとに資料が編集されたため、書き込みを取りやめました。"
    "最新の内容で案を作り直しますので、もう一度お送りください。"
)


def looks_like_doc_edit(text: str) -> bool:
    return any(w in text for w in DOC_WORDS) and any(w in text for w in EDIT_WORDS)


# --------------------------------------------------------------------------- #
# 対象の委員会・資料を決める
# --------------------------------------------------------------------------- #
def _short(name: str) -> str:
    return name.removesuffix("委員会")


def choose_committee(member: Member, text: str, candidates: list[str]) -> str | list[str]:
    """文に出てくる委員会 → 本人の所属・担当 → 候補が1つ、の順に決める。決まらなければ候補一覧。"""
    named = [c for c in candidates if c in text or _short(c) in text]
    if len(named) == 1:
        return named[0]
    pool = named or candidates
    own = [c for c in pool if c in committee_docs.own_committees(member)]
    if len(own) == 1:
        return own[0]
    if len(pool) == 1:
        return pool[0]
    return pool


def _is_summary(doc: dict) -> bool:
    name = doc["name"].lower()
    return "summary" in name or "サマリー" in name or "年間計画" in doc.get("path", "")


def choose_doc(text: str, docs: list[dict]) -> dict | list[dict]:
    """文に出てくる資料名 → サマリーの依頼ならサマリー → 資料が1つ、の順に決める。"""
    named = [d for d in docs if d["name"] in text]
    if len(named) == 1:
        return named[0]
    if any(w in text for w in SUMMARY_WORDS):
        summaries = [d for d in docs if _is_summary(d)]
        if len(summaries) == 1:
            return summaries[0]
    if len(docs) == 1:
        return docs[0]
    return named or docs


def _which_committee(candidates: list[str]) -> list[Message]:
    names = "\n".join(f"・{c}" for c in candidates)
    return [TextMessage(text=(
        f"どの委員会の資料ですか？委員会名を添えて、もう一度お送りください。\n\n{names}"
    ))]


def _which_doc(committee: str, docs: list[dict]) -> list[Message]:
    if not docs:
        return [TextMessage(
            text=f"{committee}のフォルダに Google ドキュメントが見つかりませんでした。")]
    names = "\n".join(f"・{d['name']}" for d in docs[:10])
    return [TextMessage(text=(
        f"{committee}のどの資料を更新しますか？資料名を添えて、もう一度お送りください。\n\n{names}"
    ))]


# --------------------------------------------------------------------------- #
# 案 → 確認
# --------------------------------------------------------------------------- #
def _fields_text(fields: list[committee_docs.DocField]) -> str:
    lines = []
    for f in fields:
        mark = "" if f.editable else " [読み取り専用]"
        lines.append(f"- {f.name}{mark}: {f.text or '(空欄)'}")
    return "\n".join(lines)


def _knowledge(member: Member, committee: str, text: str) -> str:
    chunks = lake_knowledge.search(member.line_user_id, f"{committee} {text}",
                                   drive_knowledge.scopes_for(member))
    return "\n---\n".join(c.get("content", "")[:KNOWLEDGE_CHARS]
                          for c in chunks[:KNOWLEDGE_CHUNKS])


def _clip(text: str) -> str:
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "…"


def _confirm(op: PendingDocEdit) -> TextMessage:
    parts = [f"「{op.doc_title}」（{op.committee}）を次のように更新します。よろしいですか？"]
    if op.summary:
        parts.append(op.summary)
    parts += [f"【{name}】\n{_clip(text)}" for name, text in op.edits.items()]
    parts.append("※ 更新したあとも、ドキュメントの「版の履歴」から元に戻せます。")
    qr = QuickReply(items=[
        QuickReplyItem(action=PostbackAction(
            label="更新する", data=f"{ACTION_DOC}|{op.op_id}|yes", display_text="更新する",
        )),
        QuickReplyItem(action=PostbackAction(
            label="やめる", data=f"{ACTION_DOC}|{op.op_id}|no", display_text="やめる",
        )),
    ])
    return TextMessage(text="\n\n".join(parts), quick_reply=qr)


def try_doc_edit_intent(
    repo: Repository, member: Member, text: str, *, now: datetime, in_group: bool = False,
) -> list[Message] | None:
    """資料の更新の依頼なら、確認メッセージ（または聞き返し）を返す。対象外なら None。"""
    if not looks_like_doc_edit(text):
        return None
    if not committee_docs.is_board_member(member):
        return [TextMessage(text=NOT_BOARD_TEXT)]
    if in_group:
        return [TextMessage(text=GROUP_TEXT)]
    if not committee_docs.is_enabled():
        return [TextMessage(text=DISABLED_TEXT)]

    try:
        token = committee_docs._token()
        folders = committee_docs.committee_folders(token)
        candidates = committee_docs.editable_committees(member, list(folders))
        if not candidates:
            return [TextMessage(text=NO_COMMITTEE_TEXT)]
        committee = choose_committee(member, text, candidates)
        if isinstance(committee, list):
            return _which_committee(committee)
        doc = choose_doc(text, committee_docs.find_docs(folders[committee], token))
        if isinstance(doc, list):
            return _which_doc(committee, doc)
        content = committee_docs.read_doc(doc["id"], token)
    except Exception:  # noqa: BLE001 - ドライブの不調で会話全体を止めない
        logger.exception("委員会資料の読み込みに失敗しました")
        return [TextMessage(text=FAILED_TEXT)]

    fields = committee_docs.extract_fields(content)
    editable = {f.name for f in fields if f.editable}
    if not editable:
        return [TextMessage(text=(
            f"「{doc['name']}」には、書き換えられる表の項目が見つかりませんでした。"
            "（いまは「項目名｜内容」の表のある資料に対応しています）"
        ))]

    outcome = draft_doc_edit(text, committee, doc["name"], _fields_text(fields),
                             _knowledge(member, committee, text))
    if outcome is None:
        record_inference(
            repo, kind="doc_edit", usage=InferenceUsage(model=model_for("doc_edit")), now=now,
            target=member.member_id, ok=False, error="generation_failed",
        )
        return [TextMessage(text=f"案を作れませんでした。{RETRY_TEXT}")]
    record_inference(repo, kind="doc_edit", usage=outcome.usage, now=now,
                     target=member.member_id)
    draft = outcome.draft
    edits = {name: body for name, body in draft.edits.items() if name in editable}
    if not edits:
        return [TextMessage(text=draft.question or (
            "どの項目をどう変えるかを読み取れませんでした。"
            "例:「地域の担い手を育てる委員会にしたいのでサマリーを更新して」"
        ))]

    op = PendingDocEdit(
        op_id=f"dop_{uuid.uuid4().hex[:10]}",
        member_id=member.member_id,
        committee=committee,
        doc_id=doc["id"],
        doc_title=doc["name"],
        revision_id=content.get("revisionId", ""),
        edits=edits,
        summary=draft.summary,
        created_at=now,
        expires_at=now + OP_TTL,
    )
    repo.save_doc_edit(op)
    return [_confirm(op)]


# --------------------------------------------------------------------------- #
# 確認 → 書き込み
# --------------------------------------------------------------------------- #
def _apply(repo: Repository, member: Member, op: PendingDocEdit) -> list[Message]:
    try:
        token = committee_docs._token()
        content = committee_docs.read_doc(op.doc_id, token)
        if content.get("revisionId") != op.revision_id:
            return [TextMessage(text=CONFLICT_TEXT)]
        requests = committee_docs.build_requests(committee_docs.extract_fields(content), op.edits)
        if not requests:
            return [TextMessage(text="書き換える項目が見つかりませんでした。")]
        committee_docs.apply(op.doc_id, op.revision_id, requests, token)
    except urllib.error.HTTPError as exc:
        if exc.code == 400:  # 読んだあとに版が変わった（requiredRevisionId の不一致）など
            logger.warning("委員会資料の書き込みを拒否されました: %s", exc)
            return [TextMessage(text=CONFLICT_TEXT)]
        logger.exception("委員会資料の書き込みに失敗しました")
        return [TextMessage(text=f"書き込みに失敗しました。{RETRY_TEXT}")]
    except Exception:  # noqa: BLE001
        logger.exception("委員会資料の書き込みに失敗しました")
        return [TextMessage(text=f"書き込みに失敗しました。{RETRY_TEXT}")]

    write_audit(repo, actor=f"line:{member.member_id}", action="doc.edit", target=op.doc_id,
                detail=f"{op.committee} / {op.doc_title} / {'・'.join(op.edits)}")
    return [TextMessage(text=(
        f"「{op.doc_title}」を更新しました。\n{committee_docs.doc_url(op.doc_id)}\n\n"
        "元に戻すときは、ドキュメントの「版の履歴」から戻せます。"
    ))]


def handle_doc_edit_postback(
    repo: Repository, member: Member, data: str, *, now: datetime
) -> list[Message]:
    """確認メッセージの「更新する／やめる」（`doc|<op_id>|yes|no`）を処理する。"""
    parts = data.split("|")
    if len(parts) != 3:
        return [TextMessage(text="この確認は無効です。")]
    _, op_id, answer = parts
    op = repo.get_doc_edit(op_id)
    # 確認は依頼した本人だけが押せる
    if op is None or op.member_id != member.member_id:
        return [TextMessage(text="この確認は無効です。")]
    if op.status != "pending":
        return [TextMessage(text="この操作はすでに処理済みです。")]
    if now > op.expires_at:
        repo.save_doc_edit(op.model_copy(update={"status": "expired"}))
        return [TextMessage(text="確認の有効期限（30分）が切れました。もう一度お送りください。")]
    if answer != "yes":
        repo.save_doc_edit(op.model_copy(update={"status": "declined"}))
        return [TextMessage(text="取りやめました。")]
    # 確認のあとに役職が変わっていないか、押した時点の権限で確かめる
    if not committee_docs.can_edit(member, op.committee):
        return [TextMessage(text=NOT_BOARD_TEXT)]

    # 二重押しで2回書き込まないよう、先に処理済みにする
    repo.save_doc_edit(op.model_copy(update={"status": "done"}))
    return _apply(repo, member, op)
