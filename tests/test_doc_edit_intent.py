"""資料の作成・更新で使う共通の判定（docs/doc-session-design.md）。

資料の書き換えそのものは作業セッション（tests/test_doc_session.py）で確かめる。
"""
from app.doc_edit_intent import choose_committee, clip, is_summary, looks_like_doc_edit
from app.models import CommitteeSeat, Member

FOLDERS = ["地域未来創造委員会", "総務委員会", "組織未来推進委員会"]
CHAIR = Member(member_id="m13", name="櫻井 駿太",
               committees=[CommitteeSeat(committee="総務委員会", role="委員長")])
SED = Member(member_id="m2", name="遠藤 孝行", officer_role="専務理事",
             supervised_committees=["組織未来推進委員会"])
PRESIDENT = Member(member_id="m3", name="渡部 邦祐", officer_role="理事長")


def test_looks_like_doc_edit():
    assert looks_like_doc_edit("サマリーを更新して")
    assert looks_like_doc_edit("背景と目的を書いて")
    assert looks_like_doc_edit("具体的手法は交流が深まる設えを考えて記載してほしい")
    assert not looks_like_doc_edit("サマリーの締切はいつ？")
    assert not looks_like_doc_edit("理事会を登録して")


def test_choose_committee():
    named = choose_committee(PRESIDENT, "地域未来創造のサマリー", FOLDERS)
    assert named == "地域未来創造委員会"
    assert choose_committee(SED, "サマリー", FOLDERS) == "組織未来推進委員会"
    assert choose_committee(PRESIDENT, "サマリー", FOLDERS) == FOLDERS
    assert choose_committee(CHAIR, "サマリー", ["総務委員会"]) == "総務委員会"


def test_is_summary_and_clip():
    assert is_summary({"name": "2027_summary_総務委員会", "path": ""})
    assert is_summary({"name": "メモ", "path": "00-年間計画（サマリー）"})
    assert not is_summary({"name": "【計画書】3月例会事業計画書", "path": "01-例会・事業"})
    assert clip("あ" * 400).endswith("…") and clip("短い") == "短い"
