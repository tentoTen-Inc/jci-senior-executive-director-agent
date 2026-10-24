"""LINE の依頼で議案（事業計画書）を作る（docs/proposal-ledger-design.md L3）。"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app import committee_docs, lom_drive
from app.doc_edit_intent import GROUP_TEXT, NOT_BOARD_TEXT
from app.member_menu import handle_member_text
from app.models import CommitteeSeat, Event, EventStatus, EventType, Member
from app.proposal_intent import (
    DISABLED_TEXT,
    WHICH_EVENT_TEXT,
    find_event,
    handle_proposal_create_postback,
    looks_like_create,
    replacements,
    try_proposal_create_intent,
)
from app.repository import InMemoryRepository

NOW = datetime(2026, 11, 1, 20, 0)
PLAN = json.loads((Path(__file__).parent / "fixtures" / "business_plan_doc.json").read_text())

CHAIR = Member(member_id="m13", name="櫻井 駿太", line_user_id="U_chair",
               committees=[CommitteeSeat(committee="総務委員会", role="委員長")])
VICE = Member(member_id="m6", name="小檜山 祥", officer_role="副理事長",
              supervised_committees=["総務委員会"])
MEMBER = Member(member_id="m15", name="加藤 律樹", line_user_id="U_member",
                committees=[CommitteeSeat(committee="総務委員会", role="委員")])


def _repo():
    repo = InMemoryRepository()
    for m in (CHAIR, VICE, MEMBER):
        repo.upsert_member(m)
    for event_id, title, kind, start in [
        ("ev_mar", "3月例会（時刻未定）", EventType.例会, datetime(2027, 3, 23, 19, 0)),
        ("ev_board", "3月理事会", EventType.理事会, datetime(2027, 3, 2, 19, 0)),
        ("ev_dec", "納会（時刻未定）", EventType.行事, datetime(2027, 12, 17, 19, 0)),
        ("ev_past", "10月例会", EventType.例会, datetime(2026, 10, 27, 19, 0)),
    ]:
        repo.upsert_event(Event(event_id=event_id, title=title, type=kind,
                                datetime_start=start, status=EventStatus.draft))
    return repo


@pytest.fixture
def drive(monkeypatch):
    monkeypatch.setenv("COMMITTEE_DOCS_FOLDER", "root")
    monkeypatch.setenv("PLAN_TEMPLATE_DOC", "tmpl")
    state = {"folders": [], "copies": [], "applied": []}
    monkeypatch.setattr(committee_docs, "_token", lambda: "sa")
    monkeypatch.setattr(lom_drive, "_token", lambda: "lom")
    monkeypatch.setattr(committee_docs, "committee_folders", lambda token: {"総務委員会": "c2"})

    def ensure(name, parent, token):
        assert token == "lom"  # 作成は LOM 公式アカウント
        state["folders"].append((parent, name))
        return f"f:{name}"
    monkeypatch.setattr(lom_drive, "ensure_folder", ensure)
    monkeypatch.setattr(lom_drive, "copy", lambda file_id, name, parent, token:
                        state["copies"].append((file_id, name, parent)) or "doc_new")
    monkeypatch.setattr(committee_docs, "read_doc", lambda doc_id, token: PLAN)
    monkeypatch.setattr(committee_docs, "apply", lambda doc_id, rev, requests, token:
                        state["applied"].append((doc_id, rev, requests)) or {})
    return state


def _ask(repo, member=CHAIR, text="3月例会の事業計画書を作って", **kw):
    return try_proposal_create_intent(repo, member, text, now=NOW, **kw)


def _op_id(messages):
    return messages[0].quick_reply.items[0].action.data.split("|")[1]


def test_looks_like_create():
    assert looks_like_create("3月例会の事業計画書を作って")
    assert looks_like_create("納会の議案を作成したい")
    assert not looks_like_create("事業計画書の目的を書いて")
    assert not looks_like_create("サマリーを作って")


def test_find_event():
    repo = _repo()
    assert find_event(repo, "3月例会の計画書を作って", NOW).event_id == "ev_mar"
    assert find_event(repo, "3月の例会の計画書", NOW).event_id == "ev_mar"  # 理事会ではなく例会
    assert find_event(repo, "納会の議案を作って", NOW).event_id == "ev_dec"
    assert find_event(repo, "10月例会の計画書", NOW) is None  # 終わった予定は対象外
    assert find_event(repo, "計画書を作って", NOW) is None


def test_refusals(drive):
    repo = _repo()
    assert _ask(repo, MEMBER)[0].text == NOT_BOARD_TEXT
    assert _ask(repo, in_group=True)[0].text == GROUP_TEXT
    assert _ask(repo, text="事業計画書を作って")[0].text == WHICH_EVENT_TEXT


def test_disabled_without_template(drive, monkeypatch):
    monkeypatch.delenv("PLAN_TEMPLATE_DOC")
    assert _ask(_repo())[0].text == DISABLED_TEXT


def test_confirm_then_create(drive):
    repo = _repo()
    messages = _ask(repo)
    assert "総務委員会の「3月例会」（3/23）" in messages[0].text
    assert "01-例会・事業/2703_3月例会" in messages[0].text
    assert drive["copies"] == []  # 確認までは何も作らない

    reply = handle_proposal_create_postback(repo, CHAIR, f"prc|{_op_id(messages)}|yes", now=NOW)
    assert "「【計画書】3月例会事業計画書」を作りました" in reply[0].text
    assert drive["folders"] == [("c2", "01-例会・事業"), ("f:01-例会・事業", "2703_3月例会")]
    assert drive["copies"] == [("tmpl", "【計画書】3月例会事業計画書", "f:2703_3月例会")]

    [(doc_id, rev, requests)] = drive["applied"]
    inserted = [r["insertText"]["text"] for r in requests if "insertText" in r]
    assert {"【計画書】3月例会事業計画書", "総務委員会", "小檜山 祥", "櫻井 駿太",
            "委員長/櫻井 駿太", "2026年11月1日"} <= set(inserted)
    replaced = {r["replaceAllText"]["containsText"]["text"]: r["replaceAllText"]["replaceText"]
                for r in requests if "replaceAllText" in r}
    assert replaced["2026年◯月◯日(土)"] == "2027年3月23日(火)"
    assert replaced["2026年度様式"] == "2027年度様式"

    [p] = repo.list_proposals()
    assert (p.committee, p.event_id, p.doc_id, p.fiscal_year) == ("総務委員会", "ev_mar",
                                                                 "doc_new", 2027)
    assert repo.list_audit()[0].action == "proposal.create"
    # 二重押しでは作らない
    again = handle_proposal_create_postback(repo, CHAIR, f"prc|{_op_id(messages)}|yes", now=NOW)
    assert "処理済み" in again[0].text and len(drive["copies"]) == 1


def test_existing_plan_is_not_duplicated(drive):
    repo = _repo()
    handle_proposal_create_postback(repo, CHAIR, f"prc|{_op_id(_ask(repo))}|yes", now=NOW)
    reply = _ask(repo)
    assert "すでにあります" in reply[0].text and "doc_new" in reply[0].text


def test_only_requester_and_not_expired(drive):
    repo = _repo()
    op_id = _op_id(_ask(repo))
    assert handle_proposal_create_postback(repo, VICE, f"prc|{op_id}|yes",
                                           now=NOW)[0].text == "この確認は無効です。"
    late = NOW + timedelta(minutes=31)
    assert "有効期限" in handle_proposal_create_postback(repo, CHAIR, f"prc|{op_id}|yes",
                                                        now=late)[0].text
    assert drive["copies"] == []


def test_replacements_use_event_year():
    from app.models import PendingProposalCreate
    op = PendingProposalCreate(op_id="o", member_id="m", committee="総務委員会", event_id="e",
                               event_start=datetime(2027, 12, 17, 19), title="納会",
                               folder_name="2712_納会", doc_name="【計画書】納会事業計画書",
                               created_at=NOW, expires_at=NOW)
    texts = [r["replaceAllText"]["replaceText"] for r in replacements(op)]
    assert texts == ["2027年12月17日(金)", "2027年度様式", "2027年　月　日"]


def test_member_text_routes_create_before_doc_edit(drive):
    messages = handle_member_text(_repo(), CHAIR, "3月例会の事業計画書を作って", now=NOW)
    assert "作ります。よろしいですか" in messages[0].text
