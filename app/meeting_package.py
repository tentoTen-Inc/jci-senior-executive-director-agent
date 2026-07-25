"""会議単位の事前共有パッケージ生成（F6-7）。

上程先イベントに紐づく議案を、目次付きの Markdown 1枚にまとめる。
AI生成物（要約・論点）は「参考」と明示し、原本は Drive リンク（`storage_uri`）を示す。
"""
from __future__ import annotations

from datetime import datetime

from .models import Event, Proposal
from .repository import Repository

APPROVAL_LABEL = {"pending": "未確認", "approved": "承認", "returned": "差戻し"}


def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def _anchor(index: int) -> str:
    return f"議案{index}"


def _proposal_section(index: int, p: Proposal, names: dict[str, str]) -> list[str]:
    heading = f"## {_anchor(index)}. {p.number + ' ' if p.number else ''}{p.title}"
    lines = [heading]

    meta = [f"委員会: {p.committee or '未設定'}"]
    if p.owner_member_id:
        meta.append(f"担当: {names.get(p.owner_member_id, p.owner_member_id)}")
    meta.append(f"ステージ: {p.stage.value}")
    meta.append(f"専務確認: {APPROVAL_LABEL.get(p.sed_approval.status, p.sed_approval.status)}")
    lines.append(" / ".join(meta))

    if p.deadlines.submit:
        lines.append(f"- 提出締切: {_fmt_dt(p.deadlines.submit)}")
    if p.storage_uri:
        lines.append(f"- 原本: {p.storage_uri}")

    if p.format_check is not None:
        if p.format_check.passed:
            lines.append("- 形式チェック: OK")
        else:
            lines.append(f"- 形式チェック: 要修正（{len(p.format_check.issues)}件）")
            lines += [f"    - {issue}" for issue in p.format_check.issues]

    if p.llm_review is not None:
        lines.append("- 要約（AI・参考）: " + p.llm_review.summary)
        if p.llm_review.points:
            lines.append("- 審議のポイント（AI・参考）")
            lines += [f"    - {x}" for x in p.llm_review.points]
        if p.llm_review.concerns:
            lines.append("- 要確認点（AI・参考）")
            lines += [f"    - {x}" for x in p.llm_review.concerns]

    if p.sed_approval.comment:
        lines.append(f"- 専務コメント: {p.sed_approval.comment}")
    lines.append("")
    return lines


def build_package(repo: Repository, event: Event, *, now: datetime) -> str:
    """イベントに上程される議案の事前共有パッケージ（Markdown）を返す。"""
    proposals = sorted(
        (p for p in repo.list_proposals() if p.event_id == event.event_id),
        key=lambda p: (p.number or "", p.proposal_id),
    )
    names = {m.member_id: m.name for m in repo.list_members()}

    lines = [
        f"# {event.title} 事前共有資料",
        "",
        f"- 開催: {_fmt_dt(event.datetime_start)}"
        + (f" / {event.location}" if event.location else ""),
        f"- 議案数: {len(proposals)}件",
        f"- 作成: {_fmt_dt(now)}",
        "",
    ]

    if not proposals:
        lines.append("この会議に上程される議案は登録されていません。")
        return "\n".join(lines)

    lines.append("## 目次")
    for i, p in enumerate(proposals, start=1):
        label = f"{p.number} " if p.number else ""
        lines.append(f"{i}. {label}{p.title}（{p.committee or '委員会未設定'}）")
    lines.append("")

    for i, p in enumerate(proposals, start=1):
        lines += _proposal_section(i, p, names)

    lines.append("---")
    lines.append(
        "※ 「AI・参考」と記した要約・論点はエージェントの助言です。最終判断は人が行います。"
    )
    return "\n".join(lines)
