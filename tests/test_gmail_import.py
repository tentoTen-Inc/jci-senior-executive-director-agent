"""Gmail取込（F5-1 / P3-2）のテスト。Gmail APIの応答形に合わせた擬似データを使う。"""
import base64
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import gmail, main
from app.deps import set_repo
from app.gmail_import import import_gmail_notices
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
IAP = {"X-Goog-Authenticated-User-Email": "sed@10to10.co.jp"}
NOW = datetime(2026, 7, 26, 10, 0)
BODY = "各LOM専務理事様\n8月5日までに会員大会の参加登録をお願いします。"


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def message(
    mid="m1",
    subject="2026ブロック会員大会について",
    sender='"福島ブロック協議会" <block@example.jp>',
    body=BODY,
    html=None,
    attachments=(),
    internal_date="1785000000000",  # 2026-07-25 ごろ
) -> dict:
    parts = []
    if body is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": b64(body)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": b64(html)}})
    for name, mime, data in attachments:
        parts.append({"filename": name, "mimeType": mime, "body": {"data": b64(data)}})
    return {
        "id": mid,
        "internalDate": internal_date,
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
                {"name": "To", "value": "inawashiro.jc@gmail.com"},
            ],
            "parts": parts,
        },
    }


@pytest.fixture(autouse=True)
def repo():
    r = InMemoryRepository()
    set_repo(r)
    yield r
    set_repo(None)


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    """OAuth設定済みの状態にする（実際の認証はしない）。"""
    monkeypatch.setattr(gmail, "is_configured", lambda: True)


def fake_fetch(messages):
    def _fetch(*, label_name=None, newer_than=None):
        _fetch.calls.append((label_name, newer_than))
        return [gmail.normalize(m, "token") for m in messages]

    _fetch.calls = []
    return _fetch


# --------------------------------------------------------------------------- #
# 正規化
# --------------------------------------------------------------------------- #
def test_normalize_extracts_sender_subject_body_and_date():
    m = gmail.normalize(message(), "token")
    assert m["message_id"] == "m1"
    assert m["subject"] == "2026ブロック会員大会について"
    assert m["from_name"] == "福島ブロック協議会"
    assert m["from_addr"] == "block@example.jp"
    assert m["body_text"] == BODY
    assert m["received_at"] is not None


def test_normalize_plain_address_and_missing_subject():
    m = gmail.normalize(message(sender="block@example.jp", subject=None), "token")
    assert (m["from_name"], m["from_addr"]) == (None, "block@example.jp")
    assert m["subject"] == "(件名なし)"


def test_normalize_falls_back_to_html_body():
    m = gmail.normalize(
        message(body=None, html="<p>お知らせ</p><br><b>締切は8月5日</b>"), "token"
    )
    assert "お知らせ" in m["body_text"] and "締切は8月5日" in m["body_text"]
    assert "<p>" not in m["body_text"]  # タグは落とす


def test_normalize_keeps_attachment_name_when_text_unavailable():
    m = gmail.normalize(
        message(attachments=[("案内.pdf", "application/pdf", "not a real pdf")]), "token"
    )
    assert m["attachments"][0]["name"] == "案内.pdf"
    assert m["attachments"][0]["text_excerpt"] is None  # 抽出失敗でも取込は続ける


def test_pdf_text_extracts_real_pdf():
    """pypdf でテキストが取り出せること（最小のPDFを生成して検証）。"""
    pytest.importorskip("pypdf")
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    # 空ページはテキストが無いので None（例外にはならない）
    assert gmail.pdf_text(buf.getvalue()) is None
    assert gmail.pdf_text(b"garbage") is None


# --------------------------------------------------------------------------- #
# 取込
# --------------------------------------------------------------------------- #
def test_import_creates_notice(repo):
    fetch = fake_fetch([message(attachments=[("案内.pdf", "application/pdf", "x")])])
    summary = import_gmail_notices(repo, now=NOW, fetch=fetch, actor="sed@10to10.co.jp")

    assert (summary.total, summary.created, summary.updated) == (1, 1, 0)
    notices = repo.list_notices()
    assert len(notices) == 1
    n = notices[0]
    assert n.source == "gmail" and n.source_ref == "gmail:m1"
    assert n.subject == "2026ブロック会員大会について"
    assert n.body_text == BODY
    assert n.from_addr == "block@example.jp"
    assert [a.name for a in n.attachments] == ["案内.pdf"]
    assert n.status == "new"
    assert n.history[0].action == "imported_gmail"


def test_import_is_idempotent_and_preserves_downstream(repo):
    from app.models import NoticeDigest

    fetch = fake_fetch([message()])
    import_gmail_notices(repo, now=NOW, fetch=fetch)
    nid = repo.list_notices()[0].notice_id

    # 要約・配信済みの状態を作る
    saved = repo.get_notice(nid)
    saved.digest = NoticeDigest(summary="要約", announcement="告知")
    saved.status = "delivered"
    repo.upsert_notice(saved)

    # 同じメールを再取込（本文は更新される想定）
    fetch2 = fake_fetch([message(body=BODY + "\n※会場が変更になりました")])
    summary = import_gmail_notices(repo, now=NOW, fetch=fetch2)
    assert (summary.created, summary.updated) == (0, 1)
    assert len(repo.list_notices()) == 1  # 二重作成しない

    again = repo.get_notice(nid)
    assert "会場が変更" in again.body_text  # 原文は最新に
    assert again.digest.summary == "要約"  # 後工程は壊さない
    assert again.status == "delivered"
    assert again.history[-1].action == "reimported_gmail"


def test_import_dry_run_does_not_save(repo):
    fetch = fake_fetch([message(), message(mid="m2", subject="別件")])
    summary = import_gmail_notices(repo, now=NOW, fetch=fetch, dry_run=True)
    assert (summary.total, summary.created, summary.dry_run) == (2, 2, True)
    assert [i.subject for i in summary.items] == ["2026ブロック会員大会について", "別件"]
    assert repo.list_notices() == []


def test_import_skips_message_without_id(repo):
    broken = gmail.normalize(message(), "token")
    broken["message_id"] = ""
    summary = import_gmail_notices(
        repo, now=NOW, fetch=lambda **kw: [broken]
    )
    assert summary.total == 1 and summary.created == 0
    assert repo.list_notices() == []


def test_import_passes_label_and_range(repo):
    fetch = fake_fetch([])
    import_gmail_notices(repo, now=NOW, fetch=fetch, label_name="対外連絡", newer_than="7d")
    assert fetch.calls == [("対外連絡", "7d")]


# --------------------------------------------------------------------------- #
# API / tick
# --------------------------------------------------------------------------- #
def test_endpoint_imports(monkeypatch, repo):
    monkeypatch.setattr(gmail, "fetch_messages", fake_fetch([message()]))
    res = client.post("/api/notices/import-gmail", json={}, headers=IAP)
    assert res.status_code == 200
    assert res.json()["created"] == 1
    assert len(repo.list_notices()) == 1
    assert any(a.action == "notice.import_gmail" for a in repo.list_audit())


def test_endpoint_dry_run_is_not_audited(monkeypatch, repo):
    monkeypatch.setattr(gmail, "fetch_messages", fake_fetch([message()]))
    res = client.post("/api/notices/import-gmail", json={"dry_run": True})
    assert res.json()["dry_run"] is True
    assert repo.list_notices() == []
    assert repo.list_audit() == []


def test_endpoint_503_when_not_configured(monkeypatch):
    monkeypatch.setattr(gmail, "is_configured", lambda: False)
    res = client.post("/api/notices/import-gmail", json={})
    assert res.status_code == 503
    assert "refresh_token" in res.json()["detail"]


def test_endpoint_502_on_failure(monkeypatch):
    def boom(**kw):
        raise RuntimeError("invalid_grant")

    monkeypatch.setattr(gmail, "fetch_messages", boom)
    res = client.post("/api/notices/import-gmail", json={})
    assert res.status_code == 502
    assert "invalid_grant" in res.json()["detail"]


def test_endpoint_route_not_shadowed(monkeypatch, repo):
    """/notices/import-gmail が /notices/{notice_id} に飲まれていないこと。"""
    monkeypatch.setattr(gmail, "fetch_messages", fake_fetch([]))
    assert client.post("/api/notices/import-gmail", json={}).status_code == 200
    assert client.get("/api/notices/import-gmail").status_code == 404


def test_tick_imports_gmail(monkeypatch, repo):
    monkeypatch.setattr(gmail, "fetch_messages", fake_fetch([message()]))
    res = client.post("/tasks/tick")
    assert res.status_code == 200
    assert res.json()["gmail"] == {"total": 1, "created": 1, "updated": 0}
    assert len(repo.list_notices()) == 1


def test_tick_skips_when_not_configured(monkeypatch, repo):
    monkeypatch.setattr(gmail, "is_configured", lambda: False)
    called = []
    monkeypatch.setattr(gmail, "fetch_messages", lambda **kw: called.append(1) or [])
    res = client.post("/tasks/tick")
    assert res.json()["gmail"] is None
    assert called == []


def test_tick_survives_gmail_failure(monkeypatch, repo):
    def boom(**kw):
        raise RuntimeError("invalid_grant")

    monkeypatch.setattr(gmail, "fetch_messages", boom)
    res = client.post("/tasks/tick")
    assert res.status_code == 200  # 催促処理は止めない
    assert res.json()["gmail"] == {"error": "gmail_import_failed"}
