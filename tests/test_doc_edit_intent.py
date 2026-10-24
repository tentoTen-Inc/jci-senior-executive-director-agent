"""LINE の依頼で委員会資料を更新する（docs/committee-docs-edit-design.md P2）。"""
import json
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app import committee_docs, lake_knowledge, llm
from app.doc_edit_intent import (
    CONFLICT_TEXT,
    GROUP_TEXT,
    NOT_BOARD_TEXT,
    choose_committee,
    choose_doc,
    handle_doc_edit_postback,
    looks_like_doc_edit,
    try_doc_edit_intent,
)
from app.llm import Generation
from app.member_menu import handle_member_text
from app.models import CommitteeSeat, Member
from app.repository import InMemoryRepository

NOW = datetime(2026, 10, 24, 20, 0)
DOC = json.loads((Path(__file__).parent / "fixtures" / "committee_summary_doc.json").read_text())
FOLDERS = {"地域未来創造委員会": "c1", "総務委員会": "c2", "組織未来推進委員会": "c3"}
SUMMARY = {"id": "doc_summary", "name": "2027_summary_総務委員会",
           "path": "00-年間計画（サマリー）"}

CHAIR = Member(member_id="m13", name="櫻井 駿太", line_user_id="U_chair",
               committees=[CommitteeSeat(committee="総務委員会", role="委員長")])
SED = Member(member_id="m2", name="遠藤 孝行", line_user_id="U_sed", officer_role="専務理事",
             supervised_committees=["組織未来推進委員会"])
PRESIDENT = Member(member_id="m3", name="渡部 邦祐", line_user_id="U_pres", officer_role="理事長")
VICE_CHAIR = Member(member_id="m14", name="村尾 碧", line_user_id="U_vice",
                    committees=[CommitteeSeat(committee="総務委員会", role="副委員長")])
REQUEST = "若手が自ら学ぶ総務委員会にしたいのでサマリーを更新して"


@pytest.fixture
def drive(monkeypatch):
    """ドライブ・ドキュメント・ナレッジ検索・AI をモックする。"""
    monkeypatch.setenv("COMMITTEE_DOCS_FOLDER", "root")
    monkeypatch.setenv("DOC_EDIT_FROM_TEXT", "1")
    state = {"doc": json.loads(json.dumps(DOC)), "applied": [], "prompts": []}
    monkeypatch.setattr(committee_docs, "_token", lambda: "tok")
    monkeypatch.setattr(committee_docs, "committee_folders", lambda token: dict(FOLDERS))
    monkeypatch.setattr(committee_docs, "find_docs", lambda folder, token: [
        SUMMARY, {"id": "d2", "name": "【計画書】3月例会事業計画書", "path": "01-例会・事業"}])
    monkeypatch.setattr(committee_docs, "read_doc", lambda doc_id, token: state["doc"])
    monkeypatch.setattr(committee_docs, "apply", lambda doc_id, rev, requests, token:
                        state["applied"].append((doc_id, rev, requests)) or {})
    monkeypatch.setattr(lake_knowledge, "search", lambda uid, q, scopes=None: [
        {"content": "2026_summary_総務委員会: 日進月歩〜JCスキルを高め、基礎を築く〜"}])

    def fake(prompt, **kwargs):
        state["prompts"].append(prompt)
        return Generation(text=json.dumps({
            "edits": [
                {"field": "スローガン", "text": "自ら学び、未来を築く"},
                {"field": "現状分析", "text": "若手メンバーが多く、基礎の習得が課題です。"},
                {"field": "（参考）LOM日程・3月", "text": "書き換えない"},
                {"field": "存在しない項目", "text": "無視"},
            ],
            "summary": "若手の主体的な学びを軸に、スローガンと現状分析を書きました。",
            "question": None,
        }, ensure_ascii=False), input_tokens=10, output_tokens=20)
    monkeypatch.setattr(llm, "generate_doc_edit", fake)
    return state


def _ask(repo, member, text=REQUEST, **kwargs):
    return try_doc_edit_intent(repo, member, text, now=NOW, **kwargs)


def _op_id(messages):
    return messages[0].quick_reply.items[0].action.data.split("|")[1]


# --------------------------------------------------------------------------- #
# 判定と権限
# --------------------------------------------------------------------------- #
def test_looks_like_doc_edit():
    assert looks_like_doc_edit("サマリーを更新して")
    assert looks_like_doc_edit("スローガンを変えて")
    assert not looks_like_doc_edit("サマリーの締切はいつ？")
    assert not looks_like_doc_edit("理事会を登録して")


def test_not_doc_edit_returns_none(drive):
    assert _ask(InMemoryRepository(), CHAIR, "次の例会はいつ？") is None


def test_non_board_member_is_refused(drive):
    assert _ask(InMemoryRepository(), VICE_CHAIR)[0].text == NOT_BOARD_TEXT
    assert drive["prompts"] == []


def test_group_is_refused(drive):
    assert _ask(InMemoryRepository(), CHAIR, in_group=True)[0].text == GROUP_TEXT


def test_disabled_without_folder(drive, monkeypatch):
    monkeypatch.delenv("COMMITTEE_DOCS_FOLDER")
    assert "準備中" in _ask(InMemoryRepository(), CHAIR)[0].text


# --------------------------------------------------------------------------- #
# 委員会・資料の決め方
# --------------------------------------------------------------------------- #
def test_choose_committee():
    names = list(FOLDERS)
    named = choose_committee(PRESIDENT, "地域未来創造のサマリーを更新して", names)
    assert named == "地域未来創造委員会"
    # 専務は全委員会を更新できるが、名指しが無ければ担当の委員会
    assert choose_committee(SED, "サマリーを更新して", names) == "組織未来推進委員会"
    # 理事長は担当が無いので聞き返す
    assert choose_committee(PRESIDENT, "サマリーを更新して", names) == names
    assert choose_committee(CHAIR, "サマリーを更新して", ["総務委員会"]) == "総務委員会"


def test_choose_doc_prefers_summary():
    docs = [SUMMARY, {"id": "d2", "name": "【計画書】3月例会事業計画書", "path": "01-例会・事業"}]
    assert choose_doc("サマリーを更新して", docs) == SUMMARY
    assert choose_doc("【計画書】3月例会事業計画書を直して", docs)["id"] == "d2"
    assert choose_doc("資料を更新して", docs) == docs


def test_president_without_committee_is_asked(drive):
    messages = _ask(InMemoryRepository(), PRESIDENT, "サマリーを更新して")
    assert "どの委員会" in messages[0].text and "総務委員会" in messages[0].text


# --------------------------------------------------------------------------- #
# 案 → 確認 → 書き込み
# --------------------------------------------------------------------------- #
def test_chair_gets_confirmation_with_editable_fields_only(drive):
    repo = InMemoryRepository()
    messages = _ask(repo, CHAIR)
    text = messages[0].text
    assert "「2027_summary_総務委員会」（総務委員会）" in text
    assert "【スローガン】\n自ら学び、未来を築く" in text
    assert "（参考）" not in text and "存在しない項目" not in text
    # AI には今の内容と参考資料（昨年度のサマリー）を渡す
    assert "- （参考）LOM日程・3月 [読み取り専用]: 例会\n3/23" in drive["prompts"][0]
    assert "日進月歩" in drive["prompts"][0]
    op = repo.get_doc_edit(_op_id(messages))
    assert op.edits == {"スローガン": "自ら学び、未来を築く",
                        "現状分析": "若手メンバーが多く、基礎の習得が課題です。"}
    assert op.revision_id == "rev1" and drive["applied"] == []


def test_yes_writes_with_revision_and_audits(drive):
    repo = InMemoryRepository()
    op_id = _op_id(_ask(repo, CHAIR))
    reply = handle_doc_edit_postback(repo, CHAIR, f"doc|{op_id}|yes", now=NOW)
    assert "更新しました" in reply[0].text
    assert "docs.google.com/document/d/doc_summary" in reply[0].text
    [(doc_id, rev, requests)] = drive["applied"]
    assert (doc_id, rev) == ("doc_summary", "rev1")
    inserted = [r["insertText"]["text"] for r in requests if "insertText" in r]
    assert inserted == ["若手メンバーが多く、基礎の習得が課題です。", "自ら学び、未来を築く"]
    assert repo.list_audit()[0].action == "doc.edit"
    # 二重押しでは書き込まない
    again = handle_doc_edit_postback(repo, CHAIR, f"doc|{op_id}|yes", now=NOW)
    assert "処理済み" in again[0].text and len(drive["applied"]) == 1


def test_no_declines(drive):
    repo = InMemoryRepository()
    op_id = _op_id(_ask(repo, CHAIR))
    reply = handle_doc_edit_postback(repo, CHAIR, f"doc|{op_id}|no", now=NOW)
    assert reply[0].text == "取りやめました。"
    assert drive["applied"] == []


def test_only_requester_can_confirm(drive):
    repo = InMemoryRepository()
    op_id = _op_id(_ask(repo, CHAIR))
    reply = handle_doc_edit_postback(repo, SED, f"doc|{op_id}|yes", now=NOW)
    assert reply[0].text == "この確認は無効です。" and drive["applied"] == []


def test_expired(drive):
    repo = InMemoryRepository()
    op_id = _op_id(_ask(repo, CHAIR))
    reply = handle_doc_edit_postback(repo, CHAIR, f"doc|{op_id}|yes",
                                     now=NOW + timedelta(minutes=31))
    assert "有効期限" in reply[0].text and drive["applied"] == []


def test_not_written_when_doc_changed_after_preview(drive):
    repo = InMemoryRepository()
    op_id = _op_id(_ask(repo, CHAIR))
    drive["doc"]["revisionId"] = "rev2"  # 確認のあとに誰かが編集した
    reply = handle_doc_edit_postback(repo, CHAIR, f"doc|{op_id}|yes", now=NOW)
    assert reply[0].text == CONFLICT_TEXT
    assert drive["applied"] == []


def test_rejected_by_google_is_reported_as_conflict(drive, monkeypatch):
    def reject(*args):
        raise urllib.error.HTTPError("u", 400, "revision mismatch", {}, None)
    monkeypatch.setattr(committee_docs, "apply", reject)
    repo = InMemoryRepository()
    op_id = _op_id(_ask(repo, CHAIR))
    reply = handle_doc_edit_postback(repo, CHAIR, f"doc|{op_id}|yes", now=NOW)
    assert reply[0].text == CONFLICT_TEXT


def test_question_when_ai_cannot_draft(drive, monkeypatch):
    monkeypatch.setattr(llm, "generate_doc_edit", lambda prompt, **kw: Generation(text=json.dumps(
        {"edits": [], "summary": None, "question": "どの項目を変えますか？"}, ensure_ascii=False)))
    assert _ask(InMemoryRepository(), CHAIR)[0].text == "どの項目を変えますか？"


# --------------------------------------------------------------------------- #
# 会話の入口
# --------------------------------------------------------------------------- #
def test_member_text_routes_doc_edit_before_calendar(drive, monkeypatch):
    from app import calendar_intent
    monkeypatch.setattr(calendar_intent, "parse_calendar_intent",
                        lambda *a, **k: pytest.fail("予定操作の解釈に回さない"))
    messages = handle_member_text(InMemoryRepository(), SED, "総務委員会のサマリーを作って",
                                  now=NOW)
    # 自由文では資料を書き換えない。［資料を作る・直す］のボタンを返すだけ
    assert messages[0].quick_reply.items[0].action.data == "menu|資料を作る・直す"


# --------------------------------------------------------------------------- #
# 議案書（事業計画書）の欄の名前・議案名での依頼
# --------------------------------------------------------------------------- #
PLAN = json.loads((Path(__file__).parent / "fixtures" / "business_plan_doc.json").read_text())
APRIL = ("4月例会は各会員がお互いの仕事や人柄を深く知れる機会を提供したいです。"
         "背景と目的を書いて。")


def test_field_words_count_as_doc_edit():
    assert looks_like_doc_edit("背景と目的を書いて")
    assert looks_like_doc_edit("広報戦略を埋めて")


def test_plan_named_by_proposal_title(drive, monkeypatch):
    from app.models import Proposal
    repo = InMemoryRepository()
    repo.upsert_proposal(Proposal(proposal_id="p4", title="4月例会", committee="組織未来推進委員会",
                                  doc_id="doc_plan4", status="open"))
    reads = []
    monkeypatch.setattr(committee_docs, "read_doc",
                        lambda doc_id, token: reads.append(doc_id) or PLAN)
    monkeypatch.setattr(committee_docs, "find_docs",
                        lambda folder, token: pytest.fail("議案名で決まるので探さない"))
    monkeypatch.setattr(llm, "generate_doc_edit", lambda prompt, **kw: Generation(text=json.dumps({
        "edits": [{"field": "1.事業実施に至る背景・問題", "text": "会員同士が互いの仕事を知らない"},
                  {"field": "2.目的・対内対象者", "text": "互いの仕事と人柄を深く知る"}],
        "summary": "背景と目的を書きました。", "question": None}, ensure_ascii=False)))

    messages = try_doc_edit_intent(repo, SED, APRIL, now=NOW)
    assert reads == ["doc_plan4"]
    assert "「4月例会の事業計画書」（組織未来推進委員会）" in messages[0].text
    assert "【1.事業実施に至る背景・問題】" in messages[0].text


def test_plan_of_other_committee_is_not_chosen(drive, monkeypatch):
    from app.models import Proposal
    repo = InMemoryRepository()
    repo.upsert_proposal(Proposal(proposal_id="p4", title="4月例会", committee="地域未来創造委員会",
                                  doc_id="doc_plan4", status="open"))
    # 総務委員会の委員長は地域未来創造委員会の議案書を対象にしない（自分の委員会の資料から選ぶ）
    messages = try_doc_edit_intent(repo, CHAIR, APRIL, now=NOW)
    assert "4月例会の事業計画書" not in messages[0].text


def test_paused_by_default(drive, monkeypatch):
    from app.doc_edit_intent import PAUSED_TEXT
    monkeypatch.delenv("DOC_EDIT_FROM_TEXT")
    assert _ask(InMemoryRepository(), CHAIR)[0].text == PAUSED_TEXT
    assert drive["prompts"] == [] and drive["applied"] == []
    # 理事メンバー以外は従来どおり
    assert _ask(InMemoryRepository(), VICE_CHAIR)[0].text == NOT_BOARD_TEXT
