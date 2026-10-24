"""事業計画書（作業文書）から議案台帳への同期（docs/proposal-ledger-design.md §4・§5）。

04-委員会 の各委員会フォルダにある「【計画書】」で始まる Google ドキュメントを、
更新日時が変わったものだけ読み取り、表の項目（committee_docs.extract_fields）を台帳に写す。
台帳に無ければ新しい議案（エントリー）として作る。文書が見つからなくなった議案は closed にする。

文書 → 台帳の一方向。状態・締切・専務確認など文書に無い管理情報は台帳だけが持ち、ここでは触らない。
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import datetime

from . import committee_docs
from .models import Proposal, ProposalFeedback, ProposalHistory, ProposalStage
from .repository import Repository

logger = logging.getLogger("jci-agent.proposal_sync")

PLAN_PREFIX = "【計画書】"
SYNC_BATCH = 20  # 1回の tick で読み取る文書の上限（残りは次回）
SYNC_SECONDS = 30  # 読み取りに使う時間の上限

#: 基本情報（4列の項目表と審議対象資料の表）
BASIC_KEYS = (
    "ファイル名", "委員会名", "職務分掌", "担当副理事長", "委員長", "文書作成人（役職/氏名）",
    "文書作成日時", "担当役員確認日", "専務理事確認日", "審議対象資料(★)", "参考資料（☆）",
)
_SECTION = re.compile(r"^\d+\.")
_FEEDBACK = re.compile(
    r"^(?P<meeting>.+?)・(?P<kind>意見|対応)(?P<no>\d+)・(?P<col>質問者/回答者|内容)$"
)
_DATE = re.compile(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日")
#: テンプレートの記入例（未記入）を表す文字
_PLACEHOLDER = "◯"


def is_plan(doc: dict) -> bool:
    return doc["name"].startswith(PLAN_PREFIX)


# --------------------------------------------------------------------------- #
# 文書の項目 → 台帳の値
# --------------------------------------------------------------------------- #
def _first_line(text: str) -> str | None:
    for line in text.splitlines():
        line = line.strip().lstrip("■").strip()
        if line and _PLACEHOLDER not in line:
            return line
    return None


def event_date(schedule: str) -> str | None:
    """「4.実施日…」の ■実施日 から日付（YYYY-MM-DD）。記入例のままなら None。"""
    match = _DATE.search(schedule or "")
    if match is None:
        return None
    year, month, day = (int(g) for g in match.groups())
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return None


def feedback(fields: dict[str, str]) -> list[ProposalFeedback]:
    """「五役会議①・意見1・内容」などの項目を、会議ごとの意見と対応にまとめる（空欄は除く）。"""
    rows: dict[tuple[str, str, int], dict[str, str]] = {}
    for name, text in fields.items():
        match = _FEEDBACK.match(name)
        if match:
            key = (match["meeting"], match["kind"], int(match["no"]))
            rows.setdefault(key, {})[match["col"]] = text
    out = []
    for (meeting, kind, no), cols in rows.items():
        if cols.get("内容"):
            out.append(ProposalFeedback(meeting=meeting, kind=kind, no=no,
                                        person=cols.get("質問者/回答者") or None,
                                        text=cols["内容"]))
    return out


def apply_fields(proposal: Proposal, fields: dict[str, str], title: str) -> Proposal:
    """読み取った項目を台帳の値に写す（文書に無い管理情報は変えない）。"""
    sections = {k: v for k, v in fields.items() if _SECTION.match(k)}
    schedule = next((v for k, v in sections.items() if k.startswith("4.")), "")
    place = next((v for k, v in sections.items() if k.startswith("5.")), "")
    people = next((v for k, v in sections.items() if k.startswith("10.")), "")
    date = event_date(schedule)
    return proposal.model_copy(update={
        "title": fields.get("事業名") or title,
        "basics": {k: fields[k] for k in BASIC_KEYS if fields.get(k)},
        "sections": sections,
        "feedback": feedback(fields),
        "event_date": date,
        "fiscal_year": int(date[:4]) if date else proposal.fiscal_year,
        "venue": _first_line(place),
        "participants": _first_line(people),
        # 形式チェック・AIレビューは本文（全項目のテキスト）を読む
        "content": "\n\n".join(f"【{k}】\n{v}" for k, v in fields.items() if v),
    })


# --------------------------------------------------------------------------- #
# 同期
# --------------------------------------------------------------------------- #
def _new_proposal(doc: dict, committee: str, now: datetime) -> Proposal:
    return Proposal(
        proposal_id=f"prp_{uuid.uuid4().hex[:10]}",
        title=doc["name"].removeprefix(PLAN_PREFIX),
        committee=committee,
        doc_id=doc["id"],
        storage_uri=committee_docs.doc_url(doc["id"]),
        stage=ProposalStage.entry,
        history=[ProposalHistory(at=now, stage=ProposalStage.entry, by="doc-sync")],
    )


def sync(repo: Repository, now: datetime) -> dict | None:
    """更新された事業計画書を台帳に写す（tick から呼ぶ）。無効なら None。"""
    if not committee_docs.is_enabled():
        return None
    try:
        token = committee_docs._token()
        docs: list[tuple[str, dict]] = []
        for committee, folder in committee_docs.committee_folders(token).items():
            docs += [(committee, d) for d in committee_docs.find_docs(folder, token) if is_plan(d)]
        by_doc = {p.doc_id: p for p in repo.list_proposals() if p.doc_id}
        changed = [(c, d) for c, d in docs
                   if by_doc.get(d["id"]) is None
                   or by_doc[d["id"]].doc_modified_at != d.get("modified_at")
                   or by_doc[d["id"]].status != "open"]
        created = updated = 0
        started = time.monotonic()
        for committee, doc in changed[:SYNC_BATCH]:
            if time.monotonic() - started > SYNC_SECONDS:
                break
            content = committee_docs.read_doc(doc["id"], token)
            fields = {f.name: f.text for f in committee_docs.extract_fields(content)}
            current = by_doc.get(doc["id"])
            if current is None:
                current = _new_proposal(doc, committee, now)
                created += 1
            else:
                updated += 1
            proposal = apply_fields(current, fields, doc["name"].removeprefix(PLAN_PREFIX))
            repo.upsert_proposal(proposal.model_copy(update={
                "committee": committee, "status": "open",
                "doc_modified_at": doc.get("modified_at"), "synced_at": now,
            }))
        # 文書が見つからなくなった（ゴミ箱・委員会フォルダの外へ移動）議案は閉じる
        present = {d["id"] for _, d in docs}
        closed = 0
        for doc_id, proposal in by_doc.items():
            if doc_id not in present and proposal.status == "open":
                repo.upsert_proposal(proposal.model_copy(update={"status": "closed"}))
                closed += 1
    except Exception as exc:  # noqa: BLE001 - tick 本体を止めない
        logger.warning("議案の同期に失敗: %s", exc)
        return {"error": "proposal_sync_failed"}
    return {"docs": len(docs), "created": created, "updated": updated,
            "pending": len(changed) - created - updated, "closed": closed}
