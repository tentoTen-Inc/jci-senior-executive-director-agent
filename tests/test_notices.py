"""対外連絡（F5 / P3-1）のテスト。LLM境界はモックする。"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import llm, main
from app.deps import set_repo
from app.models import ExternalNotice
from app.notices_api import manual_source_ref
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
IAP = {"X-Goog-Authenticated-User-Email": "sed@10to10.co.jp"}

FAKE_JSON = (
    '{"summary":"ブロック会員大会の参加者登録依頼。",'
    '"announcement":"8月5日までに会員大会の参加可否をご回答ください。会場は郡山です。",'
    '"audience_hint":"全員",'
    '"deadline":"2026-08-05",'
    '"actions":["参加者登録","会費の納入"]}'
)
BODY = "各LOM専務理事様\n2026ブロック会員大会の参加者登録をお願いします。締切は8月5日です。"


def fake_digest(subject, sender, body, **kw):
    return llm.Generation(text=FAKE_JSON, input_tokens=900, output_tokens=250)


@pytest.fixture(autouse=True)
def repo():
    r = InMemoryRepository()
    set_repo(r)
    yield r
    set_repo(None)


def create(**over):
    payload = {"subject": "2026ブロック会員大会の参加者登録について", "body_text": BODY}
    payload.update(over)
    return client.post("/api/notices", json=payload, headers=IAP)


def test_create_notice_keeps_original_text(repo):
    res = create(from_addr="block@example.jp", from_name="ブロック協議会")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "new"
    assert body["source"] == "manual"
    assert body["body_text"] == BODY  # 原文は不変（F5-6）
    assert body["digest"] is None
    assert body["history"][0]["by"] == "sed@10to10.co.jp"
    assert any(a.action == "notice.create" for a in repo.list_audit())


def test_create_is_idempotent_for_same_content(repo):
    first = create().json()
    second = create().json()
    assert first["notice_id"] == second["notice_id"]
    assert len(repo.list_notices()) == 1
    assert first["source_ref"] == manual_source_ref(first["subject"], BODY)


def test_create_rejects_empty_body():
    assert create(body_text="   ").status_code == 400


def test_list_sorted_desc_and_status_filter(repo):
    repo.upsert_notice(ExternalNotice(
        notice_id="n1", received_at=datetime(2026, 7, 1), subject="古い", body_text="x",
    ))
    repo.upsert_notice(ExternalNotice(
        notice_id="n2", received_at=datetime(2026, 7, 20), subject="新しい", body_text="x",
        status="archived",
    ))
    ids = [n["notice_id"] for n in client.get("/api/notices").json()]
    assert ids == ["n2", "n1"]  # 受信降順
    assert [n["notice_id"] for n in client.get("/api/notices?status=archived").json()] == ["n2"]


def test_digest_generates_and_records_cost(monkeypatch, repo):
    monkeypatch.setattr(llm, "generate_notice_digest", fake_digest)
    nid = create().json()["notice_id"]

    res = client.post(f"/api/notices/{nid}/digest", headers=IAP)
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "reviewed"
    assert body["body_text"] == BODY  # 原文は書き換えない
    d = body["digest"]
    assert "会員大会" in d["summary"]
    assert "8月5日" in d["announcement"]
    assert d["audience_hint"] == "全員"
    assert d["deadline"].startswith("2026-08-05")
    assert d["actions"] == ["参加者登録", "会費の納入"]
    assert d["generated_at"] is not None

    logs = repo.list_inference_logs()
    assert len(logs) == 1
    assert (logs[0].kind, logs[0].target, logs[0].ok) == ("external_notice_digest", nid, True)
    assert (logs[0].input_tokens, logs[0].output_tokens) == (900, 250)
    assert any(a.action == "notice.digest" for a in repo.list_audit())


def test_digest_ignores_unparsable_deadline(monkeypatch, repo):
    """期限が解釈できない形式なら None（日付を捏造しない）。"""
    monkeypatch.setattr(
        llm, "generate_notice_digest",
        lambda *a, **kw: llm.Generation(
            text='{"summary":"s","announcement":"a","deadline":"8月上旬","actions":[]}'
        ),
    )
    nid = create().json()["notice_id"]
    d = client.post(f"/api/notices/{nid}/digest").json()["digest"]
    assert d["deadline"] is None
    assert d["audience_hint"] is None


def test_digest_failure_returns_503_and_records(monkeypatch, repo):
    def boom(*a, **kw):
        raise RuntimeError("vertex unavailable")

    monkeypatch.setattr(llm, "generate_notice_digest", boom)
    nid = create().json()["notice_id"]
    assert client.post(f"/api/notices/{nid}/digest").status_code == 503

    logs = repo.list_inference_logs()
    assert len(logs) == 1 and logs[0].ok is False
    assert repo.get_notice(nid).digest is None
    assert repo.get_notice(nid).status == "new"


def test_digest_keeps_delivered_status(monkeypatch, repo):
    """配信済みの連絡を再要約しても status は戻さない。"""
    monkeypatch.setattr(llm, "generate_notice_digest", fake_digest)
    repo.upsert_notice(ExternalNotice(
        notice_id="n1", received_at=datetime(2026, 7, 20), subject="s", body_text=BODY,
        status="delivered",
    ))
    assert client.post("/api/notices/n1/digest").json()["status"] == "delivered"


def test_archive(repo):
    nid = create().json()["notice_id"]
    res = client.post(f"/api/notices/{nid}/archive", headers=IAP)
    assert res.status_code == 200
    assert res.json()["status"] == "archived"
    assert repo.get_notice(nid).history[-1].action == "archived"
    assert any(a.action == "notice.archive" for a in repo.list_audit())


def test_not_found():
    assert client.get("/api/notices/nope").status_code == 404
    assert client.post("/api/notices/nope/digest").status_code == 404
    assert client.post("/api/notices/nope/archive").status_code == 404


def test_notices_require_auth():
    noauth = TestClient(main.app)
    assert noauth.get("/api/notices").status_code == 401
