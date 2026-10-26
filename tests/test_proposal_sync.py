"""事業計画書・委員会サマリーから議案台帳への同期（docs/proposal-ledger-design.md L2・§10）。"""
import json
from datetime import datetime
from pathlib import Path

import pytest

from app import committee_docs, proposal_sync
from app.models import Proposal, ProposalStage, Settings
from app.repository import InMemoryRepository

NOW = datetime(2027, 1, 10, 9, 0)
FIXTURES = Path(__file__).parent / "fixtures"
PLAN = json.loads((FIXTURES / "business_plan_doc.json").read_text())
SUMMARY = json.loads((FIXTURES / "committee_summary_doc.json").read_text())
DOC = {"id": "doc_plan", "name": "【計画書】3月例会事業計画書",
       "path": "01-例会・事業/2703_3月例会", "modified_at": "2027-01-09T10:00:00.000Z"}
SUM = {"id": "sum", "name": "2027_summary_総務委員会", "path": "00-年間計画（サマリー）",
       "modified_at": "2026-10-20T10:00:00.000Z"}
OTHER = {"id": "memo", "name": "打合せメモ", "path": "02-議事録"}


@pytest.fixture
def drive(monkeypatch):
    monkeypatch.setenv("COMMITTEE_DOCS_FOLDER", "root")
    state = {"docs": [DOC, OTHER], "reads": []}
    monkeypatch.setattr(committee_docs, "_token", lambda: "tok")
    monkeypatch.setattr(committee_docs, "committee_folders", lambda token: {"総務委員会": "c2"})
    monkeypatch.setattr(committee_docs, "find_docs", lambda folder, token: list(state["docs"]))
    monkeypatch.setattr(committee_docs, "read_doc",
                        lambda doc_id, token: state["reads"].append(doc_id)
                        or (SUMMARY if doc_id == "sum" else PLAN))
    return state


# --------------------------------------------------------------------------- #
# 項目 → 台帳の値
# --------------------------------------------------------------------------- #
def test_event_date():
    assert proposal_sync.event_date("■実施日\n2027年3月23日(火)\n19:00〜") == "2027-03-23"
    assert proposal_sync.event_date("■実施日\n2026年◯月◯日(土)") is None  # 記入例のまま
    assert proposal_sync.event_date("") is None


def test_apply_fields_maps_sections_basics_and_feedback():
    fields = {
        "事業名": "3月例会「未来を語る会」",
        "委員会名": "総務委員会",
        "委員長": "櫻井 駿太",
        "1.事業実施に至る背景・問題": "若手の発言の機会が少ない",
        "4.実施日と実施前後のスケジュール": "■実施日\n2027年3月23日(火)\n19:00〜21:00",
        "5.実施場所": "■猪苗代町体験交流館\n〒969-3123",
        "10.対象者及び参加員数": "正会員13名",
        "五役会議①・意見1・質問者/回答者": "渡部 邦祐",
        "五役会議①・意見1・内容": "目的をもう少し具体的に",
        "五役会議①・対応1・内容": "目的に数値目標を追記しました",
        "五役会議①・意見2・内容": "",
    }
    p = proposal_sync.apply_fields(
        Proposal(proposal_id="p1", title="仮", stage=ProposalStage.board), fields, "3月例会")
    assert p.title == "3月例会「未来を語る会」"
    assert p.basics == {"委員会名": "総務委員会", "委員長": "櫻井 駿太"}
    assert p.sections["1.事業実施に至る背景・問題"] == "若手の発言の機会が少ない"
    assert (p.event_date, p.fiscal_year) == ("2027-03-23", 2027)
    assert (p.venue, p.participants) == ("猪苗代町体験交流館", "正会員13名")
    assert [(f.meeting, f.kind, f.no, f.person) for f in p.feedback] == [
        ("五役会議①", "意見", 1, "渡部 邦祐"), ("五役会議①", "対応", 1, None)]
    assert p.stage == ProposalStage.board  # 状態は文書から変えない
    assert "【1.事業実施に至る背景・問題】\n若手の発言の機会が少ない" in p.content


# --------------------------------------------------------------------------- #
# 同期
# --------------------------------------------------------------------------- #
def test_sync_creates_proposal_from_plan_only(drive):
    repo = InMemoryRepository()
    result = proposal_sync.sync(repo, NOW)
    assert result == {"docs": 1, "created": 1, "updated": 0, "pending": 0, "closed": 0}
    [p] = repo.list_proposals()
    assert (p.committee, p.doc_id, p.stage) == ("総務委員会", "doc_plan", ProposalStage.entry)
    assert p.title == "3月例会事業計画書"  # 事業名が空欄なら文書名から
    assert "1.事業実施に至る背景・問題の原因" in p.sections
    assert p.storage_uri == "https://docs.google.com/document/d/doc_plan/edit"
    assert p.doc_modified_at == DOC["modified_at"] and p.synced_at == NOW
    assert drive["reads"] == ["doc_plan"]  # 計画書・サマリー以外は読まない


def test_sync_reads_only_changed_docs(drive):
    repo = InMemoryRepository()
    proposal_sync.sync(repo, NOW)
    assert proposal_sync.sync(repo, NOW)["updated"] == 0 and drive["reads"] == ["doc_plan"]
    drive["docs"][0] = {**DOC, "modified_at": "2027-01-10T08:00:00.000Z"}
    assert proposal_sync.sync(repo, NOW)["updated"] == 1


def test_sync_keeps_management_fields(drive):
    repo = InMemoryRepository()
    proposal_sync.sync(repo, NOW)
    [p] = repo.list_proposals()
    repo.upsert_proposal(p.model_copy(update={"stage": ProposalStage.goyaku, "number": "第3号"}))
    drive["docs"][0] = {**DOC, "modified_at": "2027-01-11T08:00:00.000Z"}
    proposal_sync.sync(repo, NOW)
    [p] = repo.list_proposals()
    assert (p.stage, p.number) == (ProposalStage.goyaku, "第3号")


def test_sync_closes_missing_and_reopens(drive):
    repo = InMemoryRepository()
    proposal_sync.sync(repo, NOW)
    drive["docs"] = []
    assert proposal_sync.sync(repo, NOW)["closed"] == 1
    assert repo.list_proposals()[0].status == "closed"
    drive["docs"] = [DOC]  # ゴミ箱から戻された
    proposal_sync.sync(repo, NOW)
    assert repo.list_proposals()[0].status == "open"


def test_sync_batch_limit(drive, monkeypatch):
    monkeypatch.setattr(proposal_sync, "SYNC_BATCH", 1)
    drive["docs"] = [DOC, {**DOC, "id": "doc2", "name": "【計画書】4月例会事業計画書"}]
    result = proposal_sync.sync(InMemoryRepository(), NOW)
    assert (result["created"], result["pending"]) == (1, 1)


def test_sync_disabled_and_failure(drive, monkeypatch):
    def boom(token):
        raise RuntimeError("drive down")
    monkeypatch.setattr(committee_docs, "committee_folders", boom)
    assert proposal_sync.sync(InMemoryRepository(), NOW) == {"error": "proposal_sync_failed"}
    monkeypatch.delenv("COMMITTEE_DOCS_FOLDER")
    assert proposal_sync.sync(InMemoryRepository(), NOW) is None


# --------------------------------------------------------------------------- #
# 委員会サマリー（§10）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("doc, kind", [
    (DOC, "事業計画書"),
    ({"name": "2027_summary_総務委員会", "path": ""}, "年間計画（サマリー）"),
    ({"name": "総務委員会サマリー", "path": ""}, "年間計画（サマリー）"),
    ({"name": "総務委員会", "path": "00-年間計画（サマリー）"}, "年間計画（サマリー）"),
    (OTHER, None),
])
def test_doc_type(doc, kind):
    assert proposal_sync.doc_type(doc) == kind


def test_sync_creates_summary_with_sections(drive):
    repo = InMemoryRepository()
    repo.save_settings(Settings(operation_start=datetime(2027, 1, 1).date()))
    drive["docs"] = [DOC, SUM]
    assert proposal_sync.sync(repo, NOW)["created"] == 2
    [s] = [p for p in repo.list_proposals() if p.doc_id == "sum"]
    assert s.doc_type == "年間計画（サマリー）" and s.stage == ProposalStage.entry
    assert s.title == "2027年度 総務委員会 年間計画（サマリー）"
    assert list(s.sections)[:5] == list(proposal_sync.SUMMARY_KEYS)
    assert "例会/事業・1月" in s.sections and "（参考）LOM日程・1月" not in s.sections
    assert proposal_sync.blank_count(s) == 17  # 5欄＋12か月すべて空欄
    assert s.event_date is None


def test_closing_missing_plans_does_not_close_summaries(drive):
    repo = InMemoryRepository()
    drive["docs"] = [DOC, SUM]
    proposal_sync.sync(repo, NOW)
    drive["docs"] = [SUM]  # 計画書だけゴミ箱へ
    assert proposal_sync.sync(repo, NOW)["closed"] == 1
    status = {p.doc_id: p.status for p in repo.list_proposals()}
    assert status == {"doc_plan": "closed", "sum": "open"}


def test_blank_count_treats_placeholder_as_blank():
    p = Proposal(proposal_id="p", title="t", sections={
        "a": "", "b": "  ", "c": "◯月◯日", "d": "記入済み", "e": "◯\n本文あり"})
    assert proposal_sync.blank_count(p) == 3
