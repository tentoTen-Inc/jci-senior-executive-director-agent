"""資料の作成・更新で使う共通の判定と表示（docs/doc-session-design.md）。

資料の書き換えは、リッチメニューの［資料を作る・直す］で始める作業セッション（doc_session）の
中だけで行う。ここには、自由文が資料の依頼に見えるかの判定（入口のボタンを返すため）と、
作業セッション・議案の作成で共通に使う委員会の決め方・AI に渡す材料の作り方を置く。
"""
from __future__ import annotations

from . import committee_docs, drive_knowledge, lake_knowledge
from .models import Member

#: 資料を指す語と、書き換えを頼む語の両方がある文は「資料の依頼」とみなし、入口のボタンを返す
DOC_WORDS = ("サマリー", "計画書", "事業計画", "年間計画", "資料", "ドキュメント",
             "スローガン", "現状分析", "目指すべき状態", "事業概要", "事業手法",
             # 事業計画書の欄の名前（「背景と目的を書いて」）
             "事業名", "背景", "目的", "パートナー", "実施場所", "事業内容", "具体的手法", "次第",
             "実施組織", "期待される効果", "検証方法", "参加員数", "広報戦略", "引き継ぎ",
             "審議のポイント", "ブラッシュアップ", "選定理由", "リスクマネジメント")
EDIT_WORDS = ("更新", "書いて", "書き換", "直して", "修正", "記入", "記載", "反映", "変えて",
              "変更", "作って", "作成", "埋めて", "入れて", "追記", "足して", "加えて", "まとめて",
              "考えて")
#: 確認メッセージで1項目あたりに見せる文字数（LINE の上限 5,000 字に収める）
PREVIEW_CHARS = 300
KNOWLEDGE_CHUNKS = 5
KNOWLEDGE_CHARS = 600

GROUP_TEXT = "資料の更新は、専務AIとの個別トークでお送りください。"
NOT_BOARD_TEXT = (
    "資料の更新は、理事メンバー（理事長・副理事長・専務理事・委員長）のみご利用いただけます。"
)
RETRY_TEXT = "少し時間をおいて、もう一度お送りください。"
FAILED_TEXT = f"資料の読み込みに失敗しました。{RETRY_TEXT}"
CONFLICT_TEXT = (
    "確認のあとに資料が編集されたため、書き込みを取りやめました。"
    "最新の内容で案を作り直しますので、もう一度お送りください。"
)


def looks_like_doc_edit(text: str) -> bool:
    return any(w in text for w in DOC_WORDS) and any(w in text for w in EDIT_WORDS)


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


def is_summary(doc: dict) -> bool:
    name = doc["name"].lower()
    return "summary" in name or "サマリー" in name or "年間計画" in doc.get("path", "")


def fields_text(fields: list[committee_docs.DocField]) -> str:
    """AI に渡す、資料の項目と今の内容の一覧。"""
    lines = []
    for f in fields:
        mark = "" if f.editable else " [読み取り専用]"
        lines.append(f"- {f.name}{mark}: {f.text or '(空欄)'}")
    return "\n".join(lines)


def knowledge(member: Member, committee: str, text: str) -> str:
    """本人が見られる範囲のナレッジ検索の結果（昨年度の資料・理事長所信など）。"""
    chunks = lake_knowledge.search(member.line_user_id, f"{committee} {text}",
                                   drive_knowledge.scopes_for(member))
    return "\n---\n".join(c.get("content", "")[:KNOWLEDGE_CHARS]
                          for c in chunks[:KNOWLEDGE_CHUNKS])


def clip(text: str) -> str:
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "…"
