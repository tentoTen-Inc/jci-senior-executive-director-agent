"""意見箱（docs/feedback-box-design.md）。

- 会員は LIFF「意見箱」から送る（種類・内容・匿名）。匿名なら会員ID を保存しない。
- AI が答えられなかった質問は、［意見箱に送る］を押したときだけ
  種類「AIが答えられなかった」で保存する。
- tick（`process`）で AI が分類（種類の補正・対象の機能・緊急度・一行要約）し、
  専務理事に LINE で知らせる。静音時間は既存の配信ルール（guardrails）に従い、明けてから知らせる。
- チャットの自由文では保存しない（docs/doc-session-design.md §1）。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from linebot.v3.messaging import QuickReply, QuickReplyItem, TextMessage, URIAction

from . import schedule_poll as sp
from .inference import record_inference
from .llm import digest_feedback, model_for, triage_feedback
from .models import (
    Feedback,
    FeedbackDigest,
    FeedbackKind,
    FeedbackStatus,
    InferenceUsage,
    Member,
)
from .repository import Repository
from .sed_notice import notify_sed

logger = logging.getLogger("jci-agent.feedback")

KIND_LABELS = {
    FeedbackKind.bug: "不具合", FeedbackKind.feature: "ほしい機能",
    FeedbackKind.usability: "使いにくい点", FeedbackKind.other: "その他",
    FeedbackKind.unanswered: "AIが答えられなかった",
}
#: LIFF で送る人が選べる種類（「AIが答えられなかった」は［意見箱に送る］のボタンからだけ）
SELECTABLE = (FeedbackKind.bug, FeedbackKind.feature, FeedbackKind.usability, FeedbackKind.other)
STATUS_LABELS = {
    FeedbackStatus.new: "新規", FeedbackStatus.in_progress: "対応中",
    FeedbackStatus.done: "完了", FeedbackStatus.declined: "見送り",
}
TEXT_MAX = 2000
#: 1回の tick で分類する件数（Pro は1件に時間がかかるので抑える）
TRIAGE_PER_TICK = 5
#: 分類に失敗したものを次に試すまでの間隔
TRIAGE_RETRY = timedelta(hours=1)
#: 傾向のまとめに使う件数（新しい順）
DIGEST_MAX = 200
ADMIN_FEEDBACK_URL = "https://admin.647jc.org/app/feedback"

LABEL = "意見箱"
GUIDE_TEXT = ("専務AIへのご意見（不具合・ほしい機能・使いにくい点など）は、"
              "［意見箱］から送ってください。\n急ぎの用件は、専務に直接 LINE してください。")
UNANSWERED_SAVED = "意見箱に送りました。ありがとうございます。答えられるよう改善に役立てます。"


class FeedbackError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def liff_url() -> str:
    return f"https://liff.line.me/{sp.liff_id()}?view=feedback"


def guide_message() -> TextMessage:
    """意見箱の案内（LIFF を開くボタン付き）。書き込みはしない。"""
    if not sp.is_configured():
        return TextMessage(text=GUIDE_TEXT)
    return TextMessage(text=GUIDE_TEXT, quick_reply=QuickReply(items=[
        QuickReplyItem(action=URIAction(label=LABEL, uri=liff_url()))]))


def kind_label(item: Feedback) -> str:
    kind = (item.triage.kind if item.triage and item.triage.kind else None) or item.kind
    return KIND_LABELS.get(kind, str(kind))


# --------------------------------------------------------------------------- #
# 受け付け
# --------------------------------------------------------------------------- #
def submit(repo: Repository, member: Member, kind: str, text: str, anonymous: bool,
           now: datetime) -> Feedback:
    """LIFF の［送る］。匿名なら会員ID を保存しない。"""
    try:
        value = FeedbackKind(kind)
    except ValueError:
        raise FeedbackError(400, "種類を選んでください。") from None
    if value not in SELECTABLE:
        raise FeedbackError(400, "種類を選んでください。")
    body = (text or "").strip()
    if not body:
        raise FeedbackError(400, "内容を入力してください。")
    if len(body) > TEXT_MAX:
        raise FeedbackError(400, f"内容は {TEXT_MAX} 字以内にしてください。")
    item = Feedback(feedback_id=f"fb_{uuid.uuid4().hex[:10]}", kind=value, text=body,
                    anonymous=bool(anonymous),
                    member_id=None if anonymous else member.member_id,
                    source="liff", created_at=now)
    repo.save_feedback(item)
    return item


def save_unanswered(repo: Repository, member: Member, question: str, now: datetime) -> Feedback:
    """［意見箱に送る］（AI が答えられなかった質問）。同じ質問の二重押しでは2件にしない。"""
    for item in repo.list_feedback():
        if (item.kind == FeedbackKind.unanswered and item.member_id == member.member_id
                and item.text == question):
            return item
    item = Feedback(feedback_id=f"fb_{uuid.uuid4().hex[:10]}", kind=FeedbackKind.unanswered,
                    text=question, member_id=member.member_id, source="chat", created_at=now)
    repo.save_feedback(item)
    return item


# --------------------------------------------------------------------------- #
# tick: AI の分類と専務理事への通知
# --------------------------------------------------------------------------- #
def _triage(repo: Repository, item: Feedback, now: datetime) -> Feedback:
    outcome = triage_feedback(KIND_LABELS[item.kind], item.text)
    if outcome is None:
        record_inference(repo, kind="feedback_triage",
                         usage=InferenceUsage(model=model_for("feedback_triage")), now=now,
                         target=item.feedback_id, ok=False, error="generation_failed")
        item = item.model_copy(update={"triage_failed_at": now})
    else:
        record_inference(repo, kind="feedback_triage", usage=outcome.usage, now=now,
                         target=item.feedback_id)
        triage = outcome.triage.model_copy(update={"classified_at": now})
        item = item.model_copy(update={"triage": triage, "triage_failed_at": None})
    repo.save_feedback(item)
    return item


def _needs_triage(item: Feedback, now: datetime) -> bool:
    if item.triage is not None:
        return False
    return item.triage_failed_at is None or now - item.triage_failed_at >= TRIAGE_RETRY


def notify_text(repo: Repository, items: list[Feedback]) -> str:
    names = {m.member_id: m.name for m in repo.list_members()}

    def line(item: Feedback) -> str:
        summary = (item.triage.summary if item.triage and item.triage.summary
                   else " ".join(item.text.split())[:40])
        who = "匿名" if item.anonymous or not item.member_id else names.get(item.member_id, "会員")
        return f"【{kind_label(item)}】{summary}（{who}）"

    has_bug = any(kind_label(i) == KIND_LABELS[FeedbackKind.bug] for i in items)
    head = "【不具合】" if has_bug else ""
    if len(items) == 1:
        text = f"{head}意見箱に新しい意見が届きました。\n{line(items[0])}"
    else:
        text = (f"{head}意見箱に新しい意見が {len(items)} 件届きました。\n"
                + "\n".join(f"・{line(i)}" for i in items))
    return f"{text}\n\n{ADMIN_FEEDBACK_URL}"


def _notify(repo: Repository, items: list[Feedback], now: datetime) -> int:
    """専務理事に知らせる。静音時間は送らず次の tick に回す。知らせた件数を返す。"""
    if not items or not notify_sed(repo, notify_text(repo, items), now, "feedback_notice"):
        return 0
    for item in items:
        repo.save_feedback(item.model_copy(update={"notified_at": now}))
    return len(items)


def process(repo: Repository, now: datetime) -> dict:
    """tick: 新しい意見を AI で分類し、専務理事に知らせる。"""
    items = repo.list_feedback()
    triaged = 0
    for item in [i for i in items if _needs_triage(i, now)][:TRIAGE_PER_TICK]:
        _triage(repo, item, now)
        triaged += 1
    pending = sorted((i for i in repo.list_feedback() if i.notified_at is None),
                     key=lambda i: i.created_at)
    return {"triaged": triaged, "notified": _notify(repo, pending, now)}


# --------------------------------------------------------------------------- #
# 管理画面
# --------------------------------------------------------------------------- #
def admin_view(repo: Repository) -> dict:
    names = {m.member_id: m.name for m in repo.list_members()}
    rows = []
    for item in repo.list_feedback():
        rows.append({
            **item.model_dump(mode="json"),
            "kind_label": KIND_LABELS.get(item.kind, str(item.kind)),
            "effective_kind_label": kind_label(item),
            "status_label": STATUS_LABELS.get(item.status, str(item.status)),
            "member_name": None if item.anonymous else names.get(item.member_id or ""),
        })
    digest = repo.get_feedback_digest()
    return {"items": rows, "digest": digest.model_dump(mode="json") if digest else None,
            "kinds": {k.value: v for k, v in KIND_LABELS.items()},
            "statuses": {k.value: v for k, v in STATUS_LABELS.items()}}


def set_status(repo: Repository, feedback_id: str, status: str, actor: str,
               now: datetime) -> Feedback:
    item = repo.get_feedback(feedback_id)
    if item is None:
        raise FeedbackError(404, "意見が見つかりません。")
    try:
        value = FeedbackStatus(status)
    except ValueError:
        raise FeedbackError(400, "状態の指定が正しくありません。") from None
    item = item.model_copy(update={"status": value, "updated_at": now, "updated_by": actor})
    repo.save_feedback(item)
    return item


def build_digest(repo: Repository, now: datetime) -> FeedbackDigest:
    """AI による傾向のまとめ（似た意見をまとめた論点と件数）。見送りは除く。"""
    items = [i for i in repo.list_feedback() if i.status != FeedbackStatus.declined][:DIGEST_MAX]
    if not items:
        raise FeedbackError(400, "まとめる意見がありません。")
    outcome = digest_feedback([(i.feedback_id, kind_label(i), i.text) for i in items])
    if outcome is None:
        record_inference(repo, kind="feedback_digest",
                         usage=InferenceUsage(model=model_for("feedback_digest")), now=now,
                         ok=False, error="generation_failed")
        raise FeedbackError(503, "傾向をまとめられませんでした。時間をおいてやり直してください。")
    record_inference(repo, kind="feedback_digest", usage=outcome.usage, now=now)
    digest = outcome.digest.model_copy(update={"generated_at": now})
    repo.save_feedback_digest(digest)
    return digest
