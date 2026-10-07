"""AI の回答の出典表示とファイルを開くリンク。

2026-10-07 専務の指摘: 回答の [1][2] が何を指すのか LINE 上で見えない。
"""
import json
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import file_links, lake_api, lake_knowledge, line_worker, llm, main
from app.assistant import answer_member_question
from app.deps import set_repo
from app.llm import Generation
from app.models import LineGroup, Member
from app.repository import InMemoryRepository

client = TestClient(main.app)
AUDIENCE = "https://jci-sed-agent-6momralspq-an.a.run.app/pubsub/line-worker"

MSG = {"chunk_id": "message:634944871723434263:0", "source_kind": "message",
       "source_id": "634944871723434263", "scope_type": "user", "scope_id": "U_sed",
       "title": None, "sent_at": "2026-10-06T11:58:59+00:00", "distance": 0.22,
       "content": ("LINEの発言(2026-10-06 20:58)\n↑\n"
                   "福島ブロックの専務LINEグループで流れてきた年スケジュールです")}
PDF0 = {"chunk_id": "file:634944787685835148:0", "source_kind": "file",
        "source_id": "634944787685835148", "scope_type": "user", "scope_id": "U_sed",
        "title": "年内スケジュール.pdf", "sent_at": "2026-10-06T11:58:09+00:00",
        "distance": 0.23, "content": "資料「年内スケジュール.pdf」(2026-10-06)\n本文1"}
PDF1 = {**PDF0, "chunk_id": "file:634944787685835148:1", "content": "資料…\n本文2"}
GROUP = {**MSG, "chunk_id": "message:9:0", "source_id": "9", "scope_type": "group",
         "scope_id": "G1", "sent_at": "2026-10-05T00:00:00+00:00",
         "content": "LINEの発言\n理事会は10月20日です"}


@pytest.fixture
def links(monkeypatch):
    monkeypatch.setenv("PUBSUB_PUSH_AUDIENCE", AUDIENCE)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)


# --------------------------------------------------------------------------- #
# 署名付きリンク
# --------------------------------------------------------------------------- #
def test_token_roundtrip_and_tamper():
    token = file_links.make_token("634944787685835148", "U_sed")
    data = file_links.verify_token(token)
    assert data["m"] == "634944787685835148" and data["u"] == "U_sed"
    payload, sig = token.split(".")
    assert file_links.verify_token(payload + "." + sig[:-2] + "AA") is None  # 署名の改ざん
    forged = file_links._b64(json.dumps({"m": "other", "u": "U_sed",
                                         "exp": 9999999999}).encode())
    assert file_links.verify_token(forged + "." + sig) is None  # 中身の差し替え
    assert file_links.verify_token("garbage") is None


def test_token_expires_after_30_days():
    issued = time.time()
    token = file_links.make_token("m", "U", now=issued)
    assert file_links.verify_token(token, now=issued + 29 * 86400) is not None
    assert file_links.verify_token(token, now=issued + 31 * 86400) is None


def test_no_secret_no_link(monkeypatch):
    monkeypatch.setattr(file_links.config, "admin_api_secret", lambda: None)
    assert file_links.make_token("m", "U") is None


def test_public_base_url_from_push_audience(links):
    assert file_links.public_base_url() == "https://jci-sed-agent-6momralspq-an.a.run.app"


# --------------------------------------------------------------------------- #
# 出典の一覧
# --------------------------------------------------------------------------- #
def test_footer_lists_cited_sources_with_file_link(links):
    repo = InMemoryRepository()
    answer = "共有いただいた資料に記載があります。[1]\n10月8日に引継ぎです。[2][3]"
    footer = lake_knowledge.citation_footer(repo, [MSG, PDF0, PDF1], answer, "U_sed")
    lines = footer.split("\n")
    assert lines[0] == "📎 出典"
    assert lines[1].startswith(
        "[1] あなたとの個別トークでの発言（10/6）「↑ 福島ブロックの専務LINEグループで流れ"
    )
    # 同じファイルの断片は1行にまとめる
    assert lines[2] == "[2][3] 年内スケジュール.pdf（10/6・あなたとの個別トーク）"
    assert lines[3].startswith("https://jci-sed-agent-6momralspq-an.a.run.app/files/")
    token = lines[3].rsplit("/", 1)[1]
    assert file_links.verify_token(token)["m"] == "634944787685835148"


def test_footer_only_cited_numbers(links):
    repo = InMemoryRepository()
    repo.save_line_group(LineGroup(group_id="G1", group_name="2027 猪苗代青年会議所 理事会"))
    footer = lake_knowledge.citation_footer(repo, [MSG, GROUP], "理事会は10/20です[2]。[9]", "U")
    assert "[1]" not in footer  # 使っていない出典は出さない
    assert "[9]" not in footer  # 範囲外の番号は無視
    assert (
        "[2] グループ「2027 猪苗代青年会議所 理事会」での発言（10/5）「理事会は10月20日です」"
        in footer
    )


def test_no_citation_no_footer(links):
    assert lake_knowledge.citation_footer(InMemoryRepository(), [PDF0], "出典なしの回答", "U") == ""


def test_assistant_appends_footer_but_remembers_plain_answer(links, monkeypatch):
    monkeypatch.setenv("LAKE_EMBEDDING_MODEL", "embedding_model")
    monkeypatch.setattr(lake_knowledge, "search", lambda uid, q: [MSG, PDF0])
    monkeypatch.setattr(llm, "generate_answer", lambda *a, **k: Generation(text=json.dumps({
        "answer": "資料によると10月8日に引継ぎがあります。[2]", "grounded": True,
        "needs_human": False})))
    repo = InMemoryRepository()
    member = Member(member_id="m2", name="遠藤 孝行", line_user_id="U_sed")
    [msg] = answer_member_question(repo, member, "福島ブロックの予定を教えて",
                                   now=datetime(2026, 10, 7, 12, 0))
    assert msg.text.startswith(
        "資料によると10月8日に引継ぎがあります。[2]\n\n📎 出典\n[2] 年内スケジュール.pdf"
    )
    assert "/files/" in msg.text
    remembered = repo.get_conversation("m2").turns[-1].text
    assert remembered == "資料によると10月8日に引継ぎがあります。[2]"  # 記憶にリンクは入れない


# --------------------------------------------------------------------------- #
# /files/{token}
# --------------------------------------------------------------------------- #
@pytest.fixture
def storage(monkeypatch, links):
    monkeypatch.setenv("LINE_CONTENT_BUCKET", "jci-sed-agent-line-content")
    objects = {"634944787685835148": "line/content/634944787685835148/年内スケジュール.pdf"}
    monkeypatch.setattr(line_worker, "find_content_object", lambda mid: objects.get(mid))
    monkeypatch.setattr(lake_api, "_gcs_download", lambda uri: (b"%PDF-1.7", "application/pdf"))
    set_repo(InMemoryRepository())
    yield objects
    set_repo(None)


def test_valid_link_opens_file_inline(storage):
    token = file_links.make_token("634944787685835148", "U_sed")
    res = client.get(f"/files/{token}")
    assert res.status_code == 200
    assert res.content == b"%PDF-1.7"
    assert res.headers["content-type"] == "application/pdf"
    assert res.headers["content-disposition"].startswith("inline; filename*=UTF-8''")
    assert res.headers["cache-control"] == "private, no-store"


def test_invalid_link_is_rejected(storage):
    assert client.get("/files/forged.signature").status_code == 403


def test_deleted_file_is_404(storage):
    storage.clear()  # 送信取消で削除済み
    token = file_links.make_token("634944787685835148", "U_sed")
    assert client.get(f"/files/{token}").status_code == 404
