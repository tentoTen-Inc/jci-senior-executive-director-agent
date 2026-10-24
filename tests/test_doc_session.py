"""資料の作業セッション（docs/doc-session-design.md §4）。"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app import committee_docs, doc_session, lake_knowledge, llm, main
from app.doc_edit_intent import CONFLICT_TEXT, NOT_BOARD_TEXT
from app.llm import Generation
from app.member_menu import handle_member_text
from app.models import CommitteeSeat, Member
from app.repository import InMemoryRepository

NOW = datetime(2026, 10, 25, 20, 0)
FIXTURES = Path(__file__).parent / "fixtures"
SUMMARY = json.loads((FIXTURES / "committee_summary_doc.json").read_text())
PLAN = json.loads((FIXTURES / "business_plan_doc.json").read_text())

CHAIR = Member(member_id="m13", name="櫻井 駿太", line_user_id="U_chair",
               committees=[CommitteeSeat(committee="総務委員会", role="委員長")])
MEMBER = Member(member_id="m15", name="加藤 律樹", line_user_id="U_member",
                committees=[CommitteeSeat(committee="総務委員会", role="委員")])

DOCS = {
    "c2": [
        {"id": "doc_summary", "name": "2027_summary_総務委員会", "path": "00-年間計画（サマリー）",
         "modified_at": "2026-10-24T00:00:00Z"},
        {"id": "doc_plan", "name": "【計画書】3月例会事業計画書",
         "path": "01-例会・事業/2703_3月例会", "modified_at": "2026-10-25T00:00:00Z"},
    ],
    "c3": [{"id": "doc_other", "name": "2027_summary_組織未来推進委員会", "path": "",
            "modified_at": "2026-10-25T01:00:00Z"}],
}


@pytest.fixture
def drive(monkeypatch):
    monkeypatch.setenv("COMMITTEE_DOCS_FOLDER", "root")
    state = {"docs": {"doc_summary": json.loads(json.dumps(SUMMARY)),
                      "doc_plan": json.loads(json.dumps(PLAN))},
             "applied": [], "prompts": [], "reply": None}
    monkeypatch.setattr(committee_docs, "_token", lambda: "tok")
    monkeypatch.setattr(committee_docs, "committee_folders",
                        lambda token: {"総務委員会": "c2", "組織未来推進委員会": "c3"})
    monkeypatch.setattr(committee_docs, "find_docs", lambda folder, token: list(DOCS[folder]))
    monkeypatch.setattr(committee_docs, "read_doc", lambda doc_id, token: state["docs"][doc_id])
    monkeypatch.setattr(committee_docs, "apply", lambda doc_id, rev, requests, token:
                        state["applied"].append((doc_id, rev, requests)) or {})
    monkeypatch.setattr(lake_knowledge, "search", lambda *a, **k: [])

    def fake(prompt, **kwargs):
        state["prompts"].append(prompt)
        return Generation(text=json.dumps(state["reply"], ensure_ascii=False))
    monkeypatch.setattr(llm, "generate_doc_edit", fake)
    return state


def _edit(**edits):
    return {"kind": "edit", "message": "スローガンと現状分析を書きます。",
            "edits": [{"field": k, "text": v} for k, v in edits.items()]}


def _open(repo, doc_id="doc_summary", member=CHAIR):
    return doc_session.handle_postback(repo, member, f"dss|open|{doc_id}", NOW)


def _buttons(message):
    return {i.action.label: i.action.data for i in message.quick_reply.items}


# --------------------------------------------------------------------------- #
# 資料を選ぶ・開始
# --------------------------------------------------------------------------- #
def test_menu_lists_editable_docs(drive):
    [msg] = handle_member_text(InMemoryRepository(), CHAIR, "menu|資料を作る・直す", now=NOW)
    buttons = _buttons(msg)
    # 自分の委員会の資料だけ、新しく更新された順。最後に［＋新しい事業計画書］
    assert list(buttons) == ["3月例会 計画書", "総務 サマリー", "＋新しい事業計画書"]
    assert buttons["総務 サマリー"] == "dss|open|doc_summary"


def test_menu_for_non_board_member(drive):
    [msg] = handle_member_text(InMemoryRepository(), MEMBER, "menu|資料を作る・直す", now=NOW)
    assert msg.text == NOT_BOARD_TEXT


def test_open_starts_session(drive):
    repo = InMemoryRepository()
    [msg] = _open(repo)
    assert msg.text.startswith("📝 編集中：2027_summary_総務委員会")
    state = repo.get_chat_state("m13")
    assert state.mode == "doc" and state.expires_at == NOW + timedelta(minutes=30)
    session = repo.get_doc_session(state.session_id)
    assert (session.doc_id, session.committee, session.status) == ("doc_summary", "総務委員会",
                                                                    "active")


def test_cannot_open_other_committee_doc(drive):
    repo = InMemoryRepository()
    [msg] = _open(repo, "doc_other")
    assert "作業できません" in msg.text and repo.get_chat_state("m13") is None


def test_switching_doc_closes_previous(drive):
    repo = InMemoryRepository()
    _open(repo)
    first = repo.get_chat_state("m13").session_id
    _open(repo, "doc_plan")
    assert repo.get_doc_session(first).closed_reason == "switched"
    assert repo.get_doc_session(repo.get_chat_state("m13").session_id).doc_id == "doc_plan"


# --------------------------------------------------------------------------- #
# 作業中のメッセージ → 案 → 書き込み
# --------------------------------------------------------------------------- #
def test_turn_proposes_then_writes(drive):
    repo = InMemoryRepository()
    _open(repo)
    drive["reply"] = _edit(**{"スローガン": "自ら学び、未来を築く", "（参考）LOM日程・3月": "x"})
    [msg] = handle_member_text(repo, CHAIR, "若手が自ら学ぶ委員会にしたい", now=NOW)
    assert msg.text.startswith("📝 編集中：2027_summary_総務委員会")
    assert "【スローガン】\n自ら学び、未来を築く" in msg.text and "（参考）" not in msg.text
    assert drive["applied"] == []  # 確認までは書かない
    yes = _buttons(msg)["書き込む"]

    [done] = doc_session.handle_postback(repo, CHAIR, yes, NOW)
    assert "書き込みました（1項目）" in done.text
    [(doc_id, rev, requests)] = drive["applied"]
    assert (doc_id, rev) == ("doc_summary", "rev1")
    session_id = repo.get_chat_state("m13").session_id
    [edit] = repo.list_doc_edits(session_id)
    assert edit.status == "applied" and edit.before == {"スローガン": ""}
    assert repo.list_audit()[0].action == "doc.edit"
    assert "さっきの変更を戻す" in _buttons(done)
    # 二重押しでは書き込まない
    doc_session.handle_postback(repo, CHAIR, yes, NOW)
    assert len(drive["applied"]) == 1


def test_turn_uses_history_and_pending(drive):
    repo = InMemoryRepository()
    _open(repo, "doc_plan")
    drive["reply"] = _edit(**{"6.事業内容(目的達成のための手法)・次第・1行目・時間": "19:00"})
    handle_member_text(repo, CHAIR, "次第を19:00開始で考えて", now=NOW)
    drive["reply"] = _edit(**{"6.事業内容(目的達成のための手法)・次第・1行目・時間": "19:30"})
    handle_member_text(repo, CHAIR, "さっきの次第、開始を19:30にして", now=NOW)
    second = drive["prompts"][1]
    assert "本人: 次第を19:00開始で考えて" in second  # これまでのやり取り
    assert "次第・1行目・時間: 19:00" in second  # 確認待ちの案
    # 新しい案を出したら古い案は取り下げ
    edits = repo.list_doc_edits(repo.get_chat_state("m13").session_id)
    assert [e.status for e in edits] == ["declined", "proposed"]


def test_answer_does_not_propose(drive):
    repo = InMemoryRepository()
    _open(repo)
    drive["reply"] = {"kind": "answer", "edits": [],
                      "message": "去年は日進月歩がスローガンでした。"}
    [msg] = handle_member_text(repo, CHAIR, "去年のスローガンは？", now=NOW)
    assert "日進月歩" in msg.text and "書き込む" not in _buttons(msg)
    assert repo.list_doc_edits(repo.get_chat_state("m13").session_id) == []


def test_undo_restores_previous_content(drive):
    repo = InMemoryRepository()
    _open(repo)
    drive["reply"] = _edit(**{"スローガン": "自ら学び、未来を築く"})
    [msg] = handle_member_text(repo, CHAIR, "スローガンを考えて", now=NOW)
    [done] = doc_session.handle_postback(repo, CHAIR, _buttons(msg)["書き込む"], NOW)
    [confirm] = doc_session.handle_postback(repo, CHAIR, _buttons(done)["さっきの変更を戻す"], NOW)
    assert "（空欄に戻す）" in confirm.text
    doc_session.handle_postback(repo, CHAIR, _buttons(confirm)["元に戻す"], NOW)
    # もとが空欄なので、中身を消すだけ（挿入しない）
    requests = drive["applied"][-1][2]
    assert [list(r) for r in requests] == [["deleteContentRange"]]
    edits = repo.list_doc_edits(repo.get_chat_state("m13").session_id)
    assert [(e.kind, e.status) for e in edits] == [("edit", "reverted"), ("revert", "applied")]


def test_conflict_when_doc_changed(drive):
    repo = InMemoryRepository()
    _open(repo)
    drive["reply"] = _edit(**{"スローガン": "自ら学び"})
    [msg] = handle_member_text(repo, CHAIR, "スローガンを考えて", now=NOW)
    drive["docs"]["doc_summary"]["revisionId"] = "rev2"  # 確認のあとに誰かが編集した
    [reply] = doc_session.handle_postback(repo, CHAIR, _buttons(msg)["書き込む"], NOW)
    assert CONFLICT_TEXT in reply.text and drive["applied"] == []


def test_only_requester_and_expiry(drive):
    repo = InMemoryRepository()
    _open(repo)
    drive["reply"] = _edit(**{"スローガン": "自ら学び"})
    [msg] = handle_member_text(repo, CHAIR, "スローガンを考えて", now=NOW)
    yes = _buttons(msg)["書き込む"]
    other = Member(member_id="m2", name="専務", officer_role="専務理事")
    assert doc_session.handle_postback(repo, other, yes, NOW)[0].text == "この確認は無効です。"
    late = doc_session.handle_postback(repo, CHAIR, yes, NOW + timedelta(minutes=31))
    assert "有効期限" in late[0].text and drive["applied"] == []


# --------------------------------------------------------------------------- #
# 終了・期限切れ・グループ
# --------------------------------------------------------------------------- #
def test_end_returns_to_question_mode(drive, monkeypatch):
    repo = InMemoryRepository()
    _open(repo)
    doc_session.handle_postback(repo, CHAIR, "dss|end", NOW)
    assert repo.get_chat_state("m13").mode == "question"
    monkeypatch.setattr("app.member_menu.answer_member_question",
                        lambda *a, **k: [main.TextMessage(text="質問への回答")])
    msgs = handle_member_text(repo, CHAIR, "去年の4月例会はどんな内容でしたか", now=NOW)
    assert msgs[0].text == "質問への回答" and drive["prompts"] == []


def test_session_expires_after_30_minutes(drive, monkeypatch):
    repo = InMemoryRepository()
    _open(repo)
    monkeypatch.setattr("app.member_menu.answer_member_question",
                        lambda *a, **k: [main.TextMessage(text="質問への回答")])
    msgs = handle_member_text(repo, CHAIR, "去年の4月例会はどんな内容でしたか",
                              now=NOW + timedelta(minutes=31))
    assert msgs[0].text == "質問への回答" and drive["prompts"] == []


def test_group_message_is_not_a_session_turn(drive, monkeypatch):
    repo = InMemoryRepository()
    _open(repo)
    monkeypatch.setattr("app.member_menu.answer_member_question",
                        lambda *a, **k: [main.TextMessage(text="質問への回答")])
    msgs = handle_member_text(repo, CHAIR, "去年の4月例会はどんな内容でしたか", now=NOW,
                              in_group=True)
    assert msgs[0].text == "質問への回答" and drive["prompts"] == []


def test_lost_permission_ends_session(drive):
    repo = InMemoryRepository()
    _open(repo)
    demoted = CHAIR.model_copy(update={"committees": [
        CommitteeSeat(committee="総務委員会", role="委員")]})
    [msg] = handle_member_text(repo, demoted, "スローガンを考えて", now=NOW)
    assert msg.text == NOT_BOARD_TEXT and repo.get_chat_state("m13").mode == "question"


def test_postback_routing_in_main(drive, monkeypatch):
    repo = InMemoryRepository()
    repo.upsert_member(CHAIR)
    monkeypatch.setattr(main, "get_repo", lambda: repo)
    reply = main.handle_postback("U_chair", "dss|open|doc_summary")
    assert reply[0].text.startswith("📝 編集中：")
