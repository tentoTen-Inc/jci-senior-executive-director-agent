"""チャットは検索のみ・書き込みはボタンから（docs/doc-session-design.md §1・§2・§5）。

自由文では、どの立場・どの文でも書き込み（資料・議案・予定・出欠・連絡票・取次ぎ）が起きないことを
網羅的に確かめる。書き込みはメニューのボタン（`menu|<label>`）から始めた流れの中だけ。
"""
import json
from datetime import datetime, timedelta

import pytest

from app import committee_docs, lake_knowledge, llm, lom_drive
from app.member_menu import handle_member_text
from app.models import (
    CommitteeSeat,
    Event,
    EventStatus,
    EventType,
    Member,
    MemberType,
)
from app.repository import InMemoryRepository

NOW = datetime(2026, 10, 25, 20, 0)

#: 自由文で呼ばれてはいけない保存（会話の記憶・AI の利用記録は記録なので除く）
WRITE_METHODS = (
    "upsert_member", "save_invite_code", "save_link_state", "upsert_event", "upsert_attendance",
    "upsert_policy", "save_settings", "save_gcal_sync_state", "save_calendar_op",
    "save_chat_state", "save_doc_edit", "save_proposal_create", "save_line_group",
    "save_delivery_job", "save_delivery_log", "save_escalation", "save_audit", "upsert_proposal",
    "upsert_notice", "upsert_notice_action", "upsert_survey", "save_poll", "save_poll_response",
    "save_link_request", "save_scheduled_post", "save_doc_session", "add_doc_turn",
)

MEMBERS = {
    "会員": Member(member_id="m_reg", name="一般 会員", line_user_id="U_reg",
                 committees=[CommitteeSeat(committee="総務委員会", role="委員")]),
    "委員長": Member(member_id="m_chair", name="委員長 太郎", line_user_id="U_chair",
                  committees=[CommitteeSeat(committee="総務委員会", role="委員長")]),
    "副理事長": Member(member_id="m_vice", name="副理事長 次郎", line_user_id="U_vice",
                   officer_role="副理事長", supervised_committees=["総務委員会"]),
    "専務理事": Member(member_id="m_sed", name="専務 三郎", line_user_id="U_sed",
                   officer_role="専務理事", supervised_committees=["組織未来推進委員会"]),
    "理事長": Member(member_id="m_pres", name="理事長 四郎", line_user_id="U_pres",
                  officer_role="理事長"),
    "監事": Member(member_id="m_aud", name="監事 五郎", line_user_id="U_aud",
                 officer_role="監事", member_type=MemberType.external_auditor),
    "事務局": Member(member_id="m_off", name="事務局 花子", line_user_id="U_off",
                  member_type=MemberType.office),
}

TEXTS = [
    # 議案・資料
    "3月例会の事業計画書を作って",
    "組織未来推進委員会の4月例会の事業計画書を作って",
    "サマリーを更新して",
    "4月例会は各会員がお互いの仕事や人柄を深く知れる機会を提供したいです。背景と目的を書いて。",
    "事業概要と具体的手法、次第を書いて",
    "例会の目的はすでに事業計画書に記載されています。次第も19:00開始で考えて。",
    # 予定
    "10/20 19時から理事会を登録 会場は体験交流館",
    "11月例会を18日19時半に変更",
    "第8回理事会を中止",
    "27日19時に総務委員会の予定を追加して",
    # 出欠
    "来週の例会は欠席で",
    "欠席",
    "出席します",
    "11月例会、出席でお願いします",
    # 連絡・取次ぎ
    "事務局に連絡",
    "連絡",
    "会費を分割で払いたいので相談させてください",
    # 質問
    "次回の予定",
    "福島ブロックの予定を教えて",
    "去年の4月例会はどんな内容でしたか",
]


class GuardRepo(InMemoryRepository):
    """有効にした後の書き込みを記録する（テストでは何も書かれないことを確かめる）。"""

    guarding = False
    writes: list[str]

    def __getattribute__(self, name):
        attr = super().__getattribute__(name)
        if name in WRITE_METHODS and object.__getattribute__(self, "guarding"):
            def record(*args, **kwargs):
                object.__getattribute__(self, "writes").append(name)
                return attr(*args, **kwargs)
            return record
        return attr


@pytest.fixture
def repo(monkeypatch):
    r = GuardRepo()
    r.writes = []
    for m in MEMBERS.values():
        r.upsert_member(m)
    r.upsert_event(Event(event_id="ev_nov", title="11月例会", type=EventType.例会,
                         datetime_start=NOW + timedelta(days=20), status=EventStatus.open))
    # 書き込みに至る処理（AI の解釈・ドライブ）は呼ばれてもいけない
    for name in ("generate_calendar_intent", "generate_attendance_intent", "generate_doc_edit"):
        monkeypatch.setattr(llm, name, lambda *a, _n=name, **k: pytest.fail(f"{_n} を呼んだ"))
    monkeypatch.setattr(committee_docs, "_token", lambda: pytest.fail("ドライブに触れた"))
    monkeypatch.setattr(lom_drive, "copy", lambda *a, **k: pytest.fail("文書を作った"))
    # 質問への回答は「根拠なし」にして、自動の取次ぎが起きないことも確かめる
    monkeypatch.setattr(lake_knowledge, "search", lambda *a, **k: [])
    monkeypatch.setattr(llm, "generate_answer", lambda *a, **k: llm.Generation(text=json.dumps(
        {"answer": "確認できませんでした。", "grounded": False, "needs_human": True},
        ensure_ascii=False)))
    monkeypatch.setenv("COMMITTEE_DOCS_FOLDER", "root")
    monkeypatch.setenv("PLAN_TEMPLATE_DOC", "tmpl")
    r.guarding = True
    return r


@pytest.mark.parametrize("role", list(MEMBERS))
@pytest.mark.parametrize("text", TEXTS)
@pytest.mark.parametrize("in_group", [False, True])
def test_free_text_never_writes(repo, role, text, in_group):
    messages = handle_member_text(repo, MEMBERS[role], text, now=NOW, in_group=in_group)
    assert messages, "何も返さない"
    assert repo.writes == [], f"{role}「{text}」で書き込み: {repo.writes}"


@pytest.mark.parametrize("role, text, button", [
    ("委員長", "3月例会の事業計画書を作って", "資料を作る・直す"),
    ("専務理事", "サマリーを更新して", "資料を作る・直す"),
    ("専務理事", "10/20 19時から理事会を登録", "予定を登録・変更"),
    ("事務局", "第8回理事会を中止", "予定を登録・変更"),
    ("会員", "事務局に連絡", "事務局に連絡"),
])
def test_write_like_text_returns_entry_button(repo, role, text, button):
    messages = handle_member_text(repo, MEMBERS[role], text, now=NOW)
    assert messages[0].quick_reply.items[0].action.data == f"menu|{button}"


def test_attendance_free_text_answers_and_offers_button(repo):
    # 短い文（「欠席」）は出欠の案内（出席・欠席のボタン）を見せるだけ。長い文は回答＋ボタン
    short = handle_member_text(repo, MEMBERS["会員"], "来週の例会は欠席で", now=NOW)
    assert [i.action.label for i in short[0].quick_reply.items] == ["出席", "Web出席", "欠席"]
    messages = handle_member_text(repo, MEMBERS["会員"], "11月例会は仕事のため欠席でお願いします",
                                  now=NOW)
    labels = [i.action.label for i in messages[-1].quick_reply.items]
    assert "予定と出欠" in labels and "事務局に取り次ぐ" in labels


def test_non_board_write_like_text_is_just_a_question(repo):
    messages = handle_member_text(repo, MEMBERS["会員"], "3月例会の事業計画書を作って", now=NOW)
    assert "確認できませんでした" in messages[0].text  # 質問として答えるだけ


# --------------------------------------------------------------------------- #
# ボタンから始めた流れ（ここでだけ書き込む）
# --------------------------------------------------------------------------- #
def test_contact_button_writes(repo):
    handle_member_text(repo, MEMBERS["会員"], "menu|事務局に連絡", now=NOW)
    assert repo.writes == ["save_escalation"]


def test_calendar_button_reads_only_the_next_message(repo, monkeypatch):
    calls = []
    monkeypatch.setattr("app.member_menu.try_calendar_intent",
                        lambda r, m, t, *, now, explicit=False: calls.append((t, explicit)) or None)
    sed = MEMBERS["専務理事"]
    start = handle_member_text(repo, sed, "menu|予定を登録・変更", now=NOW)
    assert "1通で送ってください" in start[0].text
    handle_member_text(repo, sed, "10/20に理事会", now=NOW + timedelta(minutes=1))
    assert calls == [("10/20に理事会", True)]  # 書き込みの語が無くても予定の指示として読む
    # 次の文はふつうの質問に戻る
    handle_member_text(repo, sed, "10/21に五役会", now=NOW + timedelta(minutes=2))
    assert len(calls) == 1


def test_calendar_button_expires(repo, monkeypatch):
    monkeypatch.setattr("app.member_menu.try_calendar_intent",
                        lambda *a, **k: pytest.fail("期限切れなのに予定として読んだ"))
    sed = MEMBERS["専務理事"]
    handle_member_text(repo, sed, "menu|予定を登録・変更", now=NOW)
    messages = handle_member_text(repo, sed, "10/20 19時から理事会を登録",
                                  now=NOW + timedelta(minutes=11))
    assert messages[0].quick_reply.items[0].action.data == "menu|予定を登録・変更"


def test_calendar_button_requires_officer(repo):
    messages = handle_member_text(repo, MEMBERS["委員長"], "menu|予定を登録・変更", now=NOW)
    assert "五役" in messages[0].text
    assert repo.get_chat_state("m_chair") is None


def test_docs_button_for_non_board(repo):
    assert "理事メンバー" in handle_member_text(repo, MEMBERS["会員"], "menu|資料を作る・直す",
                                            now=NOW)[0].text
