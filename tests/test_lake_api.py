"""ボットの送信記録・データレイク読み取り・管理画面API（docs/datalake-design.md §4.3 / P6-3）。"""
import pytest
from fastapi.testclient import TestClient
from linebot.v3.messaging import TextMessage

from app import config, lake, lake_api, lake_query, line_push, main
from app.deps import set_repo
from app.models import LineGroup
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)


@pytest.fixture
def published(monkeypatch):
    monkeypatch.setenv("LINE_EVENTS_TOPIC", "line-events")
    envs: list[dict] = []
    monkeypatch.setattr(lake, "publish", lambda e: envs.extend(e) or True)
    return envs


@pytest.fixture
def repo():
    r = InMemoryRepository()
    set_repo(r)
    yield r
    set_repo(None)


# --------------------------------------------------------------------------- #
# ボットの送信の記録
# --------------------------------------------------------------------------- #
def test_record_outbound_serializes_messages(published):
    lake.record_outbound("reply", [TextMessage(text="おはようございます！")], reply_token="rt1")
    [env] = published
    assert env["kind"] == "outbound"
    assert env["payload"]["channel"] == "reply"
    assert env["payload"]["reply_token"] == "rt1"
    assert env["payload"]["messages"][0]["text"] == "おはようございます！"


def test_record_outbound_disabled(monkeypatch):
    monkeypatch.delenv("LINE_EVENTS_TOPIC", raising=False)
    called = []
    monkeypatch.setattr(lake, "publish", lambda e: called.append(e))
    lake.record_outbound("push", [TextMessage(text="x")], to="U1")
    assert called == []


class _FakeApi:
    def __init__(self, *_a, **_k):
        pass

    def reply_message(self, req):
        pass

    def push_message(self, req):
        pass


def test_reply_and_push_are_recorded(published, monkeypatch):
    monkeypatch.setattr(config, "line_channel_access_token", lambda: "real-token")
    monkeypatch.setattr(main, "MessagingApi", _FakeApi)
    monkeypatch.setattr(line_push, "MessagingApi", _FakeApi)

    assert main.reply_messages("rt9", [TextMessage(text="返信です")])
    assert line_push.push_messages("U7", [TextMessage(text="お知らせです")])

    reply, push = published
    assert reply["payload"]["reply_token"] == "rt9" and reply["payload"]["to"] is None
    assert push["payload"]["channel"] == "push" and push["payload"]["to"] == "U7"


def test_reply_not_recorded_when_not_sent(published):
    # conftest のトークンは PLACEHOLDER なので送信しない → 記録もしない
    assert main.reply_messages("rt", [TextMessage(text="x")]) is False
    assert published == []


# --------------------------------------------------------------------------- #
# BigQuery 読み取り
# --------------------------------------------------------------------------- #
def test_query_uses_named_parameters(monkeypatch):
    seen = {}

    def fake_post(body):
        seen.update(body)
        return {
            "jobComplete": True,
            "schema": {"fields": [
                {"name": "n", "type": "INTEGER"}, {"name": "ok", "type": "BOOLEAN"},
                {"name": "at", "type": "TIMESTAMP"}, {"name": "s", "type": "STRING"},
            ]},
            "rows": [{"f": [{"v": "3"}, {"v": "true"}, {"v": "1791241200.0"}, {"v": None}]}],
        }

    monkeypatch.setattr(lake_query, "_post", fake_post)
    rows = lake_query.query("SELECT @g, @n", {"g": "G1", "n": 5})
    assert rows == [{"n": 3, "ok": True, "at": "2026-10-05T23:00:00+00:00", "s": None}]
    assert seen["parameterMode"] == "NAMED"
    assert seen["queryParameters"] == [
        {"name": "g", "parameterType": {"type": "STRING"}, "parameterValue": {"value": "G1"}},
        {"name": "n", "parameterType": {"type": "INT64"}, "parameterValue": {"value": "5"}},
    ]
    assert seen["location"] == "asia-northeast1"


def test_query_incomplete_raises(monkeypatch):
    monkeypatch.setattr(lake_query, "_post", lambda body: {"jobComplete": False})
    with pytest.raises(lake_query.QueryError):
        lake_query.query("SELECT 1")


# --------------------------------------------------------------------------- #
# 管理画面API
# --------------------------------------------------------------------------- #
@pytest.fixture
def bq(monkeypatch):
    monkeypatch.setenv("LINE_EVENTS_TOPIC", "line-events")
    calls: list[tuple[str, dict]] = []
    results: dict[str, list[dict]] = {}

    def fake_query(sql, params=None):
        calls.append((sql, params or {}))
        for key, rows in results.items():
            if key in sql:
                return rows
        return []

    monkeypatch.setattr(lake_query, "query", fake_query)
    return calls, results


def test_api_503_when_lake_disabled(repo, monkeypatch):
    monkeypatch.delenv("LINE_EVENTS_TOPIC", raising=False)
    assert client.get("/api/line/groups").status_code == 503


def test_api_requires_admin_auth(repo, bq):
    assert TestClient(main.app).get("/api/line/groups").status_code == 401


def test_groups_fill_names_from_ledger(repo, bq):
    calls, results = bq
    results["v_groups"] = [
        {"group_id": "G1", "group_name": None, "messages": 3},
        {"group_id": "G2", "group_name": "理事会", "messages": 1},
    ]
    repo.save_line_group(LineGroup(group_id="G1", group_name="総務委員会"))
    rows = client.get("/api/line/groups").json()
    assert [r["group_name"] for r in rows] == ["総務委員会", "理事会"]
    assert "`test-project.line_lake.v_groups`" in calls[0][0]


def test_messages_filter_by_group_with_parameters(repo, bq):
    calls, _ = bq
    client.get("/api/line/messages", params={"group_id": "G1", "limit": 9999})
    sql, params = calls[0]
    assert "v_conversations" in sql and "@group_id" in sql and "G1" not in sql
    assert params == {"limit": lake_api.MAX_LIMIT, "group_id": "G1"}


def test_files_list_returns_excerpt_only(repo, bq):
    calls, _ = bq
    client.get("/api/line/files", params={"group_id": "G1"})
    sql, params = calls[0]
    assert "EXCEPT (text)" in sql and "LEFT(text, 200) AS text_excerpt" in sql
    assert params["group_id"] == "G1"


def test_download_requires_stored_file(repo, bq):
    _, results = bq
    results["v_files"] = [{"status": "deleted_unsent", "gcs_uri": None, "file_name": "a.pdf"}]
    assert client.get("/api/line/files/m2/download").status_code == 404


def test_download_streams_file(repo, bq, monkeypatch):
    _, results = bq
    results["v_files"] = [{"status": "stored", "gcs_uri": "gs://b/line/content/m2/第1号議案.pdf",
                           "file_name": "第1号議案.pdf"}]
    monkeypatch.setattr(lake_api, "_gcs_download",
                        lambda uri: (b"%PDF-1.7", "application/pdf"))
    res = client.get("/api/line/files/m2/download")
    assert res.status_code == 200
    assert res.content == b"%PDF-1.7"
    assert res.headers["content-type"] == "application/pdf"
    assert "UTF-8''%E7%AC%AC1%E5%8F%B7" in res.headers["content-disposition"]


def test_query_error_becomes_502(repo, monkeypatch):
    monkeypatch.setenv("LINE_EVENTS_TOPIC", "line-events")

    def boom(sql, params=None):
        raise lake_query.QueryError("403: denied")

    monkeypatch.setattr(lake_query, "query", boom)
    assert client.get("/api/line/files").status_code == 502
