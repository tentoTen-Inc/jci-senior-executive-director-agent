"""LINE の依頼で議案（事業計画書）を作る（docs/proposal-ledger-design.md §6）。

理事メンバーが個別トークで「3月例会の事業計画書を作って」と送ると、委員会と年間スケジュールの
例会・事業を決めて**確認メッセージ（作成する／やめる）を返すだけ**にする。
本人が「作成する」を押したとき（postback `prc|<op_id>|yes`）に、

1. 委員会フォルダの `01-例会・事業/YYMM_名前/` を作り、テンプレートをコピーして
   `【計画書】名前事業計画書` と名付ける
   （作成は LOM 公式アカウント: lom_drive）
2. 基本情報（ファイル名・委員会名・担当副理事長・委員長・文書作成人・作成日）と実施日を記入し、
   テンプレートの古い年度表記を直す（書き込みは doc-editor: committee_docs）
3. 議案台帳にエントリーとして登録し、年間スケジュールの予定と結ぶ
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timedelta

from linebot.v3.messaging import (
    Message,
    PostbackAction,
    QuickReply,
    QuickReplyItem,
    TextMessage,
)

from . import committee_docs, lom_drive
from .audit import write_audit
from .doc_edit_intent import GROUP_TEXT, NOT_BOARD_TEXT, choose_committee
from .jpdate import weekday
from .models import (
    Event,
    EventStatus,
    EventType,
    Member,
    PendingProposalCreate,
    Proposal,
    ProposalHistory,
    ProposalStage,
)
from .proposal_sync import PLAN_PREFIX
from .repository import Repository

logger = logging.getLogger("jci-agent.proposal_intent")

ACTION_CREATE = "prc"
OP_TTL = timedelta(minutes=30)
PLAN_WORDS = ("事業計画書", "計画書", "議案")
CREATE_WORDS = ("作って", "作成", "用意", "立ち上げ", "新しく", "作りたい")
#: 議案（事業計画書）を作る対象の予定の種別
EVENT_TYPES = (EventType.例会, EventType.行事, EventType.総会)
PLANS_FOLDER = "01-例会・事業"
#: テンプレートの記入例・古い年度表記
TEMPLATE_YEAR = "2026"
_MONTH = re.compile(r"(\d{1,2})月")
_NOTE = re.compile(r"（[^）]*）")

DISABLED_TEXT = "議案の作成は、まだ準備中です。専務にお問い合わせください。"
WHICH_EVENT_TEXT = (
    "どの例会・事業の事業計画書ですか？月を添えて、もう一度お送りください。\n"
    "例:「3月例会の事業計画書を作って」"
)
FAILED_TEXT = "事業計画書を作れませんでした。少し時間をおいて、もう一度お送りください。"


def template_id() -> str:
    """事業計画書のテンプレート（06-各種フォーマット/01-議案書）。"""
    return os.environ.get("PLAN_TEMPLATE_DOC", "").strip()


def is_enabled() -> bool:
    return committee_docs.is_enabled() and bool(template_id())


def looks_like_create(text: str) -> bool:
    return any(w in text for w in PLAN_WORDS) and any(w in text for w in CREATE_WORDS)


# --------------------------------------------------------------------------- #
# 対象の予定と名前
# --------------------------------------------------------------------------- #
def event_name(event: Event) -> str:
    """「3月例会（時刻未定）」→「3月例会」。"""
    return _NOTE.sub("", event.title).strip()


def find_event(repo: Repository, text: str, now: datetime) -> Event | None:
    """文の「〇月」と予定名から、これからの例会・行事・総会を探す（同じ月なら最も近いもの）。"""
    events = [e for e in repo.list_events()
              if e.type in EVENT_TYPES and e.status != EventStatus.cancelled
              and e.datetime_start >= now - timedelta(days=1)]
    named = [e for e in events if event_name(e) and event_name(e) in text]
    if named:
        return min(named, key=lambda e: e.datetime_start)
    months = {int(m) for m in _MONTH.findall(text)}
    if not months:
        return None
    hits = [e for e in events if e.datetime_start.month in months]
    if "例会" in text:
        hits = [e for e in hits if e.type == EventType.例会] or hits
    return min(hits, key=lambda e: e.datetime_start) if hits else None


def _names(event: Event) -> tuple[str, str, str]:
    title = event_name(event)
    return title, f"{event.datetime_start:%y%m}_{title}", f"{PLAN_PREFIX}{title}事業計画書"


def _existing(repo: Repository, committee: str, event_id: str) -> Proposal | None:
    return next((p for p in repo.list_proposals(status="open")
                 if p.committee == committee and p.event_id == event_id), None)


def _confirm(op: PendingProposalCreate) -> TextMessage:
    text = (
        f"{op.committee}の「{op.title}」（{op.event_start:%-m/%-d}）の事業計画書を作ります。よろしいですか？\n\n"
        f"・保存先: 04-委員会/{op.committee}/{PLANS_FOLDER}/{op.folder_name}\n"
        f"・文書名: {op.doc_name}\n"
        "・委員会名・担当副理事長・委員長・文書作成人・実施日は記入しておきます。"
    )
    qr = QuickReply(items=[
        QuickReplyItem(action=PostbackAction(
            label="作成する", data=f"{ACTION_CREATE}|{op.op_id}|yes", display_text="作成する",
        )),
        QuickReplyItem(action=PostbackAction(
            label="やめる", data=f"{ACTION_CREATE}|{op.op_id}|no", display_text="やめる",
        )),
    ])
    return TextMessage(text=text, quick_reply=qr)


def try_proposal_create_intent(
    repo: Repository, member: Member, text: str, *, now: datetime, in_group: bool = False,
) -> list[Message] | None:
    """議案を作る依頼なら確認メッセージ（または聞き返し）を返す。対象外なら None。"""
    if not looks_like_create(text):
        return None
    if not committee_docs.is_board_member(member):
        return [TextMessage(text=NOT_BOARD_TEXT)]
    if in_group:
        return [TextMessage(text=GROUP_TEXT)]
    if not is_enabled():
        return [TextMessage(text=DISABLED_TEXT)]

    try:
        folders = committee_docs.committee_folders(committee_docs._token())
    except Exception:  # noqa: BLE001
        logger.exception("委員会フォルダの読み込みに失敗しました")
        return [TextMessage(text=FAILED_TEXT)]
    candidates = committee_docs.editable_committees(member, list(folders))
    if not candidates:
        return [TextMessage(text="議案を作れる委員会が見つかりませんでした。")]
    committee = choose_committee(member, text, candidates)
    if isinstance(committee, list):
        names = "\n".join(f"・{c}" for c in committee)
        return [TextMessage(text=(
            f"どの委員会の議案ですか？委員会名を添えて、もう一度お送りください。\n\n{names}"))]
    event = find_event(repo, text, now)
    if event is None:
        return [TextMessage(text=WHICH_EVENT_TEXT)]
    found = _existing(repo, committee, event.event_id)
    if found is not None:
        return [TextMessage(text=(
            f"{committee}の「{event_name(event)}」の事業計画書はすでにあります。\n{found.storage_uri}\n\n"
            "内容を書くときは「〇〇な例会にしたいので背景と目的を書いて」のようにお送りください。"
        ))]

    title, folder_name, doc_name = _names(event)
    op = PendingProposalCreate(
        op_id=f"pco_{uuid.uuid4().hex[:10]}", member_id=member.member_id, committee=committee,
        event_id=event.event_id, event_start=event.datetime_start, title=title,
        folder_name=folder_name, doc_name=doc_name, created_at=now, expires_at=now + OP_TTL,
    )
    repo.save_proposal_create(op)
    return [_confirm(op)]


# --------------------------------------------------------------------------- #
# 作成
# --------------------------------------------------------------------------- #
def _role(member: Member, committee: str) -> str:
    seat = next((s.role for s in member.committees if s.committee == committee and s.role), None)
    return member.officer_role or seat or "委員"


def basics(repo: Repository, member: Member, op: PendingProposalCreate,
           now: datetime) -> dict[str, str]:
    """基本情報の記入内容（組織図の会員データから）。"""
    members = repo.list_members()
    vice = next((m.name for m in members if op.committee in m.supervised_committees), None)
    chair = next((m.name for m in members
                  if any(s.committee == op.committee and s.role == committee_docs.CHAIR
                         for s in m.committees)), None)
    values = {
        "ファイル名": op.doc_name,
        "委員会名": op.committee,
        "担当副理事長": vice,
        "委員長": chair,
        "文書作成人（役職/氏名）": f"{_role(member, op.committee)}/{member.name}",
        "文書作成日時": f"{now:%Y年%-m月%-d日}",
    }
    return {k: v for k, v in values.items() if v}


def replacements(op: PendingProposalCreate) -> list[dict]:
    """テンプレートの記入例・古い年度表記を、この議案の年度と実施日に置き換える。"""
    start = op.event_start
    year = str(start.year)
    pairs = [
        (f"{TEMPLATE_YEAR}年◯月◯日(土)", f"{start:%Y年%-m月%-d日}({weekday(start)})"),
        (f"{TEMPLATE_YEAR}年度様式", f"{year}年度様式"),
        (f"{TEMPLATE_YEAR}年　月　日", f"{year}年　月　日"),
    ]
    return [{"replaceAllText": {"replaceText": new, "containsText": {"text": old,
                                                                    "matchCase": True}}}
            for old, new in pairs]


def _create(repo: Repository, member: Member, op: PendingProposalCreate,
            now: datetime) -> list[Message]:
    try:
        sa = committee_docs._token()
        lom = lom_drive._token()
        committee_folder = committee_docs.committee_folders(sa)[op.committee]
        plans = lom_drive.ensure_folder(PLANS_FOLDER, committee_folder, lom)
        folder = lom_drive.ensure_folder(op.folder_name, plans, lom)
        doc_id = lom_drive.copy(template_id(), op.doc_name, folder, lom)
        content = committee_docs.read_doc(doc_id, sa)
        fields = committee_docs.extract_fields(content)
        requests = committee_docs.build_requests(fields, basics(repo, member, op, now))
        committee_docs.apply(doc_id, content["revisionId"], requests + replacements(op), sa)
    except Exception:  # noqa: BLE001
        logger.exception("事業計画書の作成に失敗しました")
        return [TextMessage(text=FAILED_TEXT)]

    url = committee_docs.doc_url(doc_id)
    proposal = Proposal(
        proposal_id=f"prp_{uuid.uuid4().hex[:10]}", title=op.title, committee=op.committee,
        owner_member_id=member.member_id, event_id=op.event_id, doc_id=doc_id, storage_uri=url,
        fiscal_year=op.event_start.year, stage=ProposalStage.entry,
        history=[ProposalHistory(at=now, stage=ProposalStage.entry, by=f"line:{member.member_id}")],
    )
    repo.upsert_proposal(proposal)
    write_audit(repo, actor=f"line:{member.member_id}", action="proposal.create",
                target=proposal.proposal_id, detail=f"{op.committee} / {op.doc_name}")
    return [TextMessage(text=(
        f"「{op.doc_name}」を作りました。\n{url}\n\n"
        "続けて「〇〇な例会にしたいので背景と目的を書いて」のようにお送りいただくと、案を作って記入します。"
    ))]


def handle_proposal_create_postback(
    repo: Repository, member: Member, data: str, *, now: datetime
) -> list[Message]:
    """確認メッセージの「作成する／やめる」（`prc|<op_id>|yes|no`）を処理する。"""
    parts = data.split("|")
    if len(parts) != 3:
        return [TextMessage(text="この確認は無効です。")]
    _, op_id, answer = parts
    op = repo.get_proposal_create(op_id)
    if op is None or op.member_id != member.member_id:
        return [TextMessage(text="この確認は無効です。")]
    if op.status != "pending":
        return [TextMessage(text="この操作はすでに処理済みです。")]
    if now > op.expires_at:
        repo.save_proposal_create(op.model_copy(update={"status": "expired"}))
        return [TextMessage(text="確認の有効期限（30分）が切れました。もう一度お送りください。")]
    if answer != "yes":
        repo.save_proposal_create(op.model_copy(update={"status": "declined"}))
        return [TextMessage(text="取りやめました。")]
    if not committee_docs.can_edit(member, op.committee):
        return [TextMessage(text=NOT_BOARD_TEXT)]
    # 二重押しで2つ作らないよう、先に処理済みにする
    repo.save_proposal_create(op.model_copy(update={"status": "done"}))
    return _create(repo, member, op, now)
