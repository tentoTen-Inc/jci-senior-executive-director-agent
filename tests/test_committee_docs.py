"""委員会資料の項目の読み書き（docs/committee-docs-edit-design.md P1）。"""
import json
from pathlib import Path

import pytest

from app import committee_docs
from app.models import CommitteeSeat, Member

DOC = json.loads((Path(__file__).parent / "fixtures" / "committee_summary_doc.json").read_text())


def _member(officer=None, seats=(), supervised=()):
    return Member(member_id="m", name="会員", officer_role=officer,
                  committees=[CommitteeSeat(committee=c, role=r) for c, r in seats],
                  supervised_committees=list(supervised))


# --------------------------------------------------------------------------- #
# 権限
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("member, committee, allowed", [
    (_member("理事長"), "総務委員会", True),
    (_member("専務理事"), "地域未来創造委員会", True),
    (_member("副理事長", supervised=["総務委員会"]), "総務委員会", True),
    (_member("副理事長", supervised=["総務委員会"]), "地域未来創造委員会", False),
    (_member(seats=[("組織未来推進委員会", "委員長")]), "組織未来推進委員会", True),
    (_member(seats=[("組織未来推進委員会", "委員長")]), "総務委員会", False),
    # 委員長は兼務先の委員会も更新できる（理事メンバーかつ所属）
    (_member(seats=[("総務委員会", "委員長"), ("地域未来創造委員会", "委員")]),
     "地域未来創造委員会", True),
    # 理事メンバーでなければ所属委員会でも不可
    (_member(seats=[("総務委員会", "副委員長")]), "総務委員会", False),
    (_member(seats=[("総務委員会", "委員")]), "総務委員会", False),
    (_member("監事"), "総務委員会", False),
    (_member("直前理事長"), "総務委員会", False),
])
def test_can_edit(member, committee, allowed):
    assert committee_docs.can_edit(member, committee) is allowed


def test_board_member():
    assert committee_docs.is_board_member(_member("直前理事長"))
    assert committee_docs.is_board_member(_member(seats=[("総務委員会", "委員長")]))
    assert not committee_docs.is_board_member(_member(seats=[("総務委員会", "副委員長")]))
    assert not committee_docs.is_board_member(_member("監事"))


def test_editable_committees_keeps_order():
    names = ["地域未来創造委員会", "総務委員会", "組織未来推進委員会"]
    assert committee_docs.editable_committees(_member("専務理事"), names) == names
    vice = _member("副理事長", supervised=["組織未来推進委員会"])
    assert committee_docs.editable_committees(vice, names) == ["組織未来推進委員会"]


# --------------------------------------------------------------------------- #
# 探索
# --------------------------------------------------------------------------- #
def test_committee_name_strips_order_prefix():
    assert committee_docs.committee_name("03-組織未来推進委員会") == "組織未来推進委員会"
    assert committee_docs.committee_name("01ｰ地域未来創造委員会") == "地域未来創造委員会"
    assert committee_docs.committee_name("総務委員会") == "総務委員会"


def test_find_docs_walks_subfolders(monkeypatch):
    tree = {
        "c1": [{"id": "f_plan", "name": "00-年間計画（サマリー）",
                "mimeType": committee_docs.FOLDER},
               {"id": "s1", "name": "表", "mimeType": "application/vnd.google-apps.spreadsheet"}],
        "f_plan": [{"id": "d1", "name": "2027_summary_総務委員会",
                    "mimeType": committee_docs.GOOGLE_DOC, "modifiedTime": "2026-10-24T00:00:00Z"}],
    }
    monkeypatch.setattr(committee_docs, "_children", lambda parent, token: tree.get(parent, []))
    docs = committee_docs.find_docs("c1", "tok")
    assert docs == [{"id": "d1", "name": "2027_summary_総務委員会",
                     "path": "00-年間計画（サマリー）", "modified_at": "2026-10-24T00:00:00Z"}]


def test_committee_folders(monkeypatch):
    monkeypatch.setenv("COMMITTEE_DOCS_FOLDER", "root")
    monkeypatch.setattr(committee_docs, "_children", lambda parent, token: [
        {"id": "c3", "name": "03-組織未来推進委員会", "mimeType": committee_docs.FOLDER},
        {"id": "x", "name": "メモ", "mimeType": committee_docs.GOOGLE_DOC},
    ])
    assert committee_docs.committee_folders("tok") == {"組織未来推進委員会": "c3"}


# --------------------------------------------------------------------------- #
# 項目の読み取り（実際のサマリーの構造）
# --------------------------------------------------------------------------- #
def test_extract_fields_from_summary():
    fields = {f.name: f for f in committee_docs.extract_fields(DOC)}
    for name in ("スローガン", "現状分析", "目指すべき状態", "事業概要", "事業手法"):
        assert fields[name].editable and fields[name].text == ""
    assert fields["例会/事業・3月"].editable
    # 参考の LOM 日程は読み取り専用で、内容は読める
    lom = fields["（参考）LOM日程・3月"]
    assert not lom.editable and lom.text == "例会\n3/23"
    # 現状分析のセル（空行6つ）の中身の範囲（最後の改行は残す）
    assert (fields["現状分析"].start, fields["現状分析"].end) == (96, 107)


# --------------------------------------------------------------------------- #
# 書き込み
# --------------------------------------------------------------------------- #
def test_build_requests_from_the_back_and_keeps_style():
    fields = committee_docs.extract_fields(DOC)
    requests = committee_docs.build_requests(fields, {
        "スローガン": "未来へつなぐ一歩",
        "現状分析": "一行目\n二行目",
        "（参考）LOM日程・3月": "書き換えない",
        "存在しない項目": "無視",
        "事業概要": "  ",
    })
    starts = [r["insertText"]["location"]["index"] for r in requests if "insertText" in r]
    assert starts == [96, 86]  # 後ろ（現状分析）から
    assert requests[0] == {"deleteContentRange": {"range": {"startIndex": 96, "endIndex": 107}}}
    assert requests[1]["insertText"]["text"] == "一行目\n二行目"
    # スローガンの太字を付け直す（範囲は UTF-16 の長さ）
    style = [r["updateTextStyle"] for r in requests if "updateTextStyle" in r][-1]
    assert style["range"] == {"startIndex": 86, "endIndex": 86 + len("未来へつなぐ一歩")}
    assert style["textStyle"]["bold"] is True
    assert style["fields"] == "bold,fontSize,weightedFontFamily"
    # 空欄のセルは書式を持たないので、同じ行の見出しの大きさ・フォントに合わせる（10pt）
    analysis = [r["updateTextStyle"] for r in requests if "updateTextStyle" in r][0]
    assert analysis["range"]["startIndex"] == 96
    assert analysis["textStyle"]["fontSize"] == {"magnitude": 10, "unit": "PT"}
    assert "bold" not in analysis["textStyle"]


def test_apply_sends_required_revision(monkeypatch):
    sent = {}
    monkeypatch.setattr(committee_docs, "_post_json",
                        lambda url, body, token: sent.update(url=url, body=body) or {})
    committee_docs.apply("doc1", "rev9", [{"insertText": {}}], "tok")
    assert sent["url"].endswith("/doc1:batchUpdate")
    assert sent["body"]["writeControl"] == {"requiredRevisionId": "rev9"}


# --------------------------------------------------------------------------- #
# 事業計画書（2027年度様式）の読み取り（docs/proposal-ledger-design.md §3）
# --------------------------------------------------------------------------- #
PLAN = json.loads((Path(__file__).parent / "fixtures" / "business_plan_doc.json").read_text())


def test_business_plan_basics_from_key_value_table():
    fields = {f.name: f for f in committee_docs.extract_fields(PLAN)}
    for name in ("事業名", "ファイル名", "委員会名", "職務分掌", "担当副理事長", "委員長",
                 "文書作成人（役職/氏名）", "文書作成日時"):
        assert name in fields and fields[name].editable
    assert fields["専務理事確認日"].text == "月　日(　)"
    assert fields["審議対象資料(★)"].text == "予算書"


def test_business_plan_subheadings_become_fields():
    fields = {f.name: f for f in committee_docs.extract_fields(PLAN)}
    for name in ("1.事業実施に至る背景・問題", "1.事業実施に至る背景・問題の原因",
                 "1.事業実施に至る背景・あるべき姿", "2.目的・対内対象者", "2.目的・対外対象者",
                 "13.前年度（前回からの引き継ぎ事項）・予算面"):
        assert name in fields
    # 見出しの行そのもの（「■問題」）は項目にしない
    assert "1.事業実施に至る背景" not in fields
    # セルの中の見出し（■事業概要・■具体的手法）は見出しごとの項目になり、見出しそのものは含まない
    assert "6.事業内容(目的達成のための手法)・事業概要" in fields
    assert "6.事業内容(目的達成のための手法)・具体的手法" in fields
    assert fields["4.実施日と実施前後のスケジュール・実施日"].text.startswith("2026年◯月◯日")
    # 記入例の「■◯◯◯」は見出しにせず、見出しの前の内容として残す
    assert fields["5.実施場所"].text.startswith("■◯◯◯")


def test_business_plan_feedback_tables_are_named_by_meeting():
    names = [f.name for f in committee_docs.extract_fields(PLAN)]
    assert "五役会議①・意見1・内容" in names
    assert "理事会①・対応1・質問者/回答者" in names
    assert "五役会議②・意見6・内容" in names
    assert not any(n.endswith("(2)") for n in names)  # 会議名で区別できるので番号は付かない


def test_subheading_field_writes_into_the_content_row():
    fields = {f.name: f for f in committee_docs.extract_fields(PLAN)}
    problem = fields["1.事業実施に至る背景・問題"]
    cause = fields["1.事業実施に至る背景・問題の原因"]
    assert problem.start < cause.start  # 小見出しの次の行（内容）を指す
    requests = committee_docs.build_requests(list(fields.values()),
                                             {"1.事業実施に至る背景・問題": "会員が減っている"})
    assert requests[-2]["insertText"] == {"location": {"index": problem.start},
                                          "text": "会員が減っている"}


def test_business_plan_inner_tables_become_row_fields():
    fields = {f.name: f for f in committee_docs.extract_fields(PLAN)}
    plan = "6.事業内容(目的達成のための手法)・次第"
    assert fields[f"{plan}・1行目・時間"].text == "："
    assert f"{plan}・6行目・担当者" in fields
    assert "7.実施組織・実施組織・4行目・職務分掌" in fields
    assert "11.広報戦略・01.広報戦略・3行目・どの媒体で" in fields
    assert fields["4.実施日と実施前後のスケジュール・実施前後のスケジュール・1行目・いつ"].text \
        == "99月99日"
    # 表を含むセル全体を1つの項目として書き換えない（表を壊さない）
    assert "6.事業内容(目的達成のための手法)" not in fields


def test_inner_heading_segment_keeps_heading_and_table():
    fields = committee_docs.extract_fields(PLAN)
    by = {f.name: f for f in fields}
    summary = by["6.事業内容(目的達成のための手法)・事業概要"]
    requests = committee_docs.build_requests(fields, {
        "6.事業内容(目的達成のための手法)・事業概要": "会員同士が仕事を紹介し合う",
        "6.事業内容(目的達成のための手法)・次第・1行目・内容": "開会",
    })
    deletes = [r["deleteContentRange"]["range"] for r in requests if "deleteContentRange" in r]
    # 見出し「■事業概要」（575-581）と次第の表（598-662）は消さない
    assert all(not (d["startIndex"] < 581 and d["endIndex"] > 575) for d in deletes)
    assert all(not (d["startIndex"] < 662 and d["endIndex"] > 598) for d in deletes)
    inserted = [r["insertText"] for r in requests if "insertText" in r]
    assert inserted[-1] == {"location": {"index": summary.start},
                            "text": "会員同士が仕事を紹介し合う"}


def test_heading_without_paragraph_gets_new_line():
    fields = {f.name: f for f in committee_docs.extract_fields(PLAN)}
    reason = fields["5.実施場所・選定理由"]
    assert reason.prefix_newline
    [insert, style] = committee_docs.build_requests(list(fields.values()),
                                                    {"5.実施場所・選定理由": "駅から近い"})
    assert insert["insertText"]["text"] == "\n駅から近い"
    assert style["updateTextStyle"]["range"]["startIndex"] == reason.start + 1
