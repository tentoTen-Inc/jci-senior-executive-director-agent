"""データレイクのワーカー（docs/datalake-design.md §4.2 / §4.4）のテスト。"""
import base64
import io
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import lake, lake_maintenance, line_worker, main
from app.deps import set_repo
from app.repository import InMemoryRepository

client = TestClient(main.app)
NOW = datetime(2026, 10, 6, 12, 0)
ENDPOINT = "https://agent.example/pubsub/line-worker"
PUSH_SA = "pubsub-push@jci-sed-agent.iam.gserviceaccount.com"


def _event(message: dict | None = None, *, type_: str = "message", source: dict | None = None,
           **extra) -> dict:
    payload = {
        "type": type_,
        "timestamp": 1791241260000,
        "webhookEventId": f"evt-{type_}-{(message or {}).get('id', 'x')}",
        "source": source or {"type": "group", "groupId": "G1", "userId": "U2"},
        **extra,
    }
    if message is not None:
        payload["message"] = message
    return lake.envelope("webhook", payload, id=payload["webhookEventId"])


PDF_FILE = {"type": "file", "id": "m2", "fileName": "第1号議案.pdf", "fileSize": 1234}


class Fakes:
    def __init__(self) -> None:
        self.line: dict[str, tuple[int, dict, bytes]] = {}
        self.line_calls: list[str] = []
        self.uploads: dict[str, tuple[bytes, str, dict]] = {}
        self.published: list[dict] = []

    def line_get(self, url, limit=None):
        self.line_calls.append(url)
        for key, value in self.line.items():
            if key in url:
                status, headers, raw = value
                return status, headers, raw if limit is None else raw[: limit + 1]
        return 404, {}, b""

    def upload(self, name, raw, content_type, metadata):
        self.uploads[name] = (raw, content_type, metadata)

    def delete_prefix(self, prefix):
        names = [n for n in self.uploads if n.startswith(prefix)]
        for n in names:
            del self.uploads[n]
        return len(names)

    def content(self) -> list[dict]:
        return [e["payload"] for e in self.published if e["kind"] == "content"]


@pytest.fixture
def repo():
    r = InMemoryRepository()
    set_repo(r)
    line_worker._known_groups.clear()
    yield r
    set_repo(None)
    line_worker._known_groups.clear()


@pytest.fixture
def fakes(monkeypatch):
    f = Fakes()
    monkeypatch.setenv("LINE_CONTENT_BUCKET", "lake-bucket")
    monkeypatch.setenv("LINE_EVENTS_TOPIC", "line-events")
    monkeypatch.setenv("PUBSUB_PUSH_SA", PUSH_SA)
    monkeypatch.setenv("PUBSUB_PUSH_AUDIENCE", ENDPOINT)
    monkeypatch.setattr(line_worker, "_line_get", f.line_get)
    monkeypatch.setattr(line_worker, "_gcs_upload", f.upload)
    monkeypatch.setattr(line_worker, "_gcs_delete_prefix", f.delete_prefix)
    monkeypatch.setattr(lake, "publish", lambda envs: f.published.extend(envs) or True)
    f.line["/group/G1/summary"] = (
        200, {}, json.dumps({"groupId": "G1", "groupName": "理事会", "pictureUrl": "p"}).encode()
    )
    return f


def _pdf_bytes() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# ファイル
# --------------------------------------------------------------------------- #
def test_file_is_stored_with_metadata(repo, fakes):
    raw = _pdf_bytes()
    fakes.line["/m2/content"] = (200, {"Content-Type": "application/pdf"}, raw)

    assert line_worker.handle_envelope(repo, _event(PDF_FILE), NOW) == "stored"

    [(name, (body, ctype, meta))] = fakes.uploads.items()
    assert name == "line/content/m2/第1号議案.pdf"
    assert body == raw and ctype == "application/pdf"
    assert meta["group_id"] == "G1" and meta["user_id"] == "U2" and meta["message_type"] == "file"
    [record] = fakes.content()
    assert record["status"] == "stored"
    assert record["gcs_uri"] == "gs://lake-bucket/line/content/m2/第1号議案.pdf"
    assert record["size"] == len(raw) and len(record["sha256"]) == 64
    assert record["text"] is None  # 空ページの PDF は抽出テキストなし（例外にしない）
    assert [e["id"] for e in fakes.published if e["kind"] == "content"] == ["content_m2_stored"]


def test_pdf_text_is_extracted(repo, fakes, monkeypatch):
    fakes.line["/m2/content"] = (200, {"Content-Type": "application/pdf"}, b"%PDF")
    monkeypatch.setattr(line_worker.gmail, "pdf_text", lambda raw: "第1号議案 事業計画")
    line_worker.handle_envelope(repo, _event(PDF_FILE), NOW)
    assert fakes.content()[0]["text"] == "第1号議案 事業計画"


def test_image_gets_id_based_name(repo, fakes):
    fakes.line["/m3/content"] = (200, {"content-type": "image/jpeg"}, b"\xff\xd8jpeg")
    line_worker.handle_envelope(repo, _event({"type": "image", "id": "m3"}), NOW)
    assert list(fakes.uploads) == ["line/content/m3/m3.jpg"]


def test_text_message_does_not_touch_storage(repo, fakes):
    assert line_worker.handle_envelope(
        repo, _event({"type": "text", "id": "m1", "text": "おはよう"}), NOW
    ) == "noop"
    assert fakes.uploads == {}
    assert not any("/content" in u for u in fakes.line_calls)


def test_external_video_is_recorded_not_fetched(repo, fakes):
    msg = {"type": "video", "id": "m4",
           "contentProvider": {"type": "external", "originalContentUrl": "https://v"}}
    assert line_worker.handle_envelope(repo, _event(msg), NOW) == "external"
    assert fakes.uploads == {}
    assert fakes.content()[0]["original_url"] == "https://v"


def test_declared_large_file_is_skipped(repo, fakes, monkeypatch):
    monkeypatch.setenv("LINE_CONTENT_MAX_BYTES", "1000")
    assert line_worker.handle_envelope(repo, _event(PDF_FILE), NOW) == "too_large"
    assert not any("/content" in u for u in fakes.line_calls)


def test_undeclared_large_content_is_cut_off(repo, fakes, monkeypatch):
    monkeypatch.setenv("LINE_CONTENT_MAX_BYTES", "10")
    fakes.line["/m5/content"] = (200, {"Content-Type": "image/jpeg"}, b"x" * 100)
    assert line_worker.handle_envelope(repo, _event({"type": "image", "id": "m5"}), NOW) \
        == "too_large"
    assert fakes.uploads == {}


def test_expired_content_is_recorded_unavailable(repo, fakes):
    fakes.line["/m2/content"] = (404, {}, b"")
    assert line_worker.handle_envelope(repo, _event(PDF_FILE), NOW) == "unavailable"


@pytest.mark.parametrize("status", [202, 500, 429])
def test_transient_errors_retry(repo, fakes, status):
    fakes.line["/m2/content"] = (status, {}, b"")
    with pytest.raises(line_worker.RetryLater):
        line_worker.handle_envelope(repo, _event(PDF_FILE), NOW)


def test_safe_filename_strips_paths():
    assert line_worker.safe_filename({"fileName": "../../etc/passwd"}, None) == "_.._etc_passwd"
    assert line_worker.safe_filename({"type": "image", "id": "9"}, "image/png") == "9.png"


# --------------------------------------------------------------------------- #
# 送信取消
# --------------------------------------------------------------------------- #
def test_unsend_deletes_stored_file(repo, fakes):
    fakes.line["/m2/content"] = (200, {"Content-Type": "application/pdf"}, b"%PDF")
    line_worker.handle_envelope(repo, _event(PDF_FILE), NOW)
    assert fakes.uploads

    unsend = _event(type_="unsend", unsend={"messageId": "m2"})
    assert line_worker.handle_envelope(repo, unsend, NOW) == "unsend:1"
    assert fakes.uploads == {}
    assert fakes.content()[-1]["status"] == "deleted_unsent"


def test_unsend_of_text_message_is_noop(repo, fakes):
    unsend = _event(type_="unsend", unsend={"messageId": "m1"})
    assert line_worker.handle_envelope(repo, unsend, NOW) == "unsend:0"
    assert fakes.content() == []


# --------------------------------------------------------------------------- #
# グループ
# --------------------------------------------------------------------------- #
def test_join_fetches_group_name(repo, fakes):
    line_worker.handle_envelope(repo, _event(type_="join"), NOW)
    group = repo.get_line_group("G1")
    assert group.group_name == "理事会" and group.joined_at == NOW and group.left_at is None
    [profile] = [e for e in fakes.published if e["kind"] == "group_profile"]
    assert profile["payload"] == {"groupId": "G1", "groupName": "理事会", "pictureUrl": "p"}


def test_unknown_group_is_profiled_once(repo, fakes):
    text = {"type": "text", "id": "m1", "text": "こんにちは"}
    line_worker.handle_envelope(repo, _event(text), NOW)
    line_worker.handle_envelope(repo, _event(text), NOW + timedelta(minutes=5))
    assert sum("/summary" in u for u in fakes.line_calls) == 1  # 2回目はキャッシュ
    assert repo.get_line_group("G1").group_name == "理事会"


def test_profile_refreshes_after_ttl(repo, fakes):
    text = {"type": "text", "id": "m1", "text": "x"}
    line_worker.handle_envelope(repo, _event(text), NOW)
    line_worker.handle_envelope(repo, _event(text), NOW + timedelta(days=8))
    assert sum("/summary" in u for u in fakes.line_calls) == 2


def test_leave_marks_left_without_fetch(repo, fakes):
    line_worker.handle_envelope(repo, _event(type_="join"), NOW)
    fakes.line_calls.clear()
    line_worker.handle_envelope(repo, _event(type_="leave"), NOW + timedelta(days=1))
    assert repo.get_line_group("G1").left_at == NOW + timedelta(days=1)
    assert fakes.line_calls == []


def test_room_has_no_profile_api(repo, fakes):
    room = {"type": "room", "roomId": "R1", "userId": "U1"}
    line_worker.handle_envelope(repo, _event({"type": "text", "id": "m", "text": "x"},
                                             source=room), NOW)
    assert repo.get_line_group("R1").source_type == "room"
    assert not any("/summary" in u for u in fakes.line_calls)


def test_one_to_one_is_not_a_group(repo, fakes):
    user = {"type": "user", "userId": "U1"}
    line_worker.handle_envelope(repo, _event({"type": "text", "id": "m", "text": "x"},
                                             source=user), NOW)
    assert repo.list_line_groups() == []


def test_non_webhook_kind_is_ignored(repo, fakes):
    env = lake.envelope("content", {"message_id": "m2"})
    assert line_worker.handle_envelope(repo, env, NOW) == "ignored"


# --------------------------------------------------------------------------- #
# Push エンドポイント（認証・再送）
# --------------------------------------------------------------------------- #
def _push_body(env: dict) -> dict:
    data = base64.b64encode(json.dumps(env).encode()).decode()
    return {"message": {"data": data, "messageId": "1"}, "subscription": "s"}


@pytest.fixture
def valid_token(monkeypatch):
    def verify(token, audience):
        assert audience == ENDPOINT
        if token != "good":
            raise ValueError("bad signature")
        return {"email": PUSH_SA, "email_verified": True}

    monkeypatch.setattr(line_worker, "_verify_jwt", verify)


def test_endpoint_rejects_missing_or_bad_token(repo, fakes, valid_token):
    body = _push_body(_event({"type": "text", "id": "m1", "text": "x"}))
    assert client.post("/pubsub/line-worker", json=body).status_code == 401
    assert client.post("/pubsub/line-worker", json=body,
                       headers={"Authorization": "Bearer evil"}).status_code == 401


def test_endpoint_rejects_other_service_account(repo, fakes, monkeypatch):
    monkeypatch.setattr(line_worker, "_verify_jwt",
                        lambda t, a: {"email": "attacker@x.iam.gserviceaccount.com",
                                      "email_verified": True})
    body = _push_body(_event({"type": "text", "id": "m1", "text": "x"}))
    assert client.post("/pubsub/line-worker", json=body,
                       headers={"Authorization": "Bearer t"}).status_code == 401


def test_endpoint_processes_and_acks(repo, fakes, valid_token):
    fakes.line["/m2/content"] = (200, {"Content-Type": "application/pdf"}, b"%PDF")
    res = client.post("/pubsub/line-worker", json=_push_body(_event(PDF_FILE)),
                      headers={"Authorization": "Bearer good"})
    assert res.status_code == 204
    assert "line/content/m2/第1号議案.pdf" in fakes.uploads


def test_endpoint_asks_retry_on_transient_error(repo, fakes, valid_token):
    fakes.line["/m2/content"] = (503, {}, b"")
    res = client.post("/pubsub/line-worker", json=_push_body(_event(PDF_FILE)),
                      headers={"Authorization": "Bearer good"})
    assert res.status_code == 500  # Pub/Sub が再送する


def test_endpoint_drops_undecodable_message(repo, fakes, valid_token):
    res = client.post("/pubsub/line-worker", json={"message": {"data": "!!!"}},
                      headers={"Authorization": "Bearer good"})
    assert res.status_code == 204  # 再送しても直らないので ack


def test_endpoint_503_until_configured(repo, monkeypatch):
    monkeypatch.delenv("LINE_CONTENT_BUCKET", raising=False)
    res = client.post("/pubsub/line-worker", json={},
                      headers={"Authorization": "Bearer good"})
    assert res.status_code == 503  # 設定前に届いた分は捨てずに再送させる


def test_pubsub_path_is_not_behind_admin_guard():
    """/pubsub は X-Admin-Token ではなく OIDC で守る（/tasks の共有シークレットは使わない）。"""
    assert not "/pubsub/line-worker".startswith(main.PROTECTED_PREFIXES)


# --------------------------------------------------------------------------- #
# 送信取消の本文消去（tick）
# --------------------------------------------------------------------------- #
def test_redact_disabled_without_topic(monkeypatch):
    monkeypatch.delenv("LINE_EVENTS_TOPIC", raising=False)
    assert lake_maintenance.redact_unsent() is None


def test_redact_runs_update_on_recent_partitions(monkeypatch):
    monkeypatch.setenv("LINE_EVENTS_TOPIC", "line-events")
    seen = []
    monkeypatch.setattr(lake_maintenance, "_bq_query",
                        lambda sql: seen.append(sql) or {"numDmlAffectedRows": "2"})
    assert lake_maintenance.redact_unsent() == {"redacted": 2}
    sql = seen[0]
    assert sql.startswith("UPDATE `test-project.line_lake.events_raw`")
    assert "JSON_REMOVE(data, '$.payload.message.text', '$.payload.text')" in sql
    assert "INTERVAL 3 DAY" in sql and "INTERVAL 1 HOUR" in sql


def test_redact_failure_does_not_raise(monkeypatch):
    monkeypatch.setenv("LINE_EVENTS_TOPIC", "line-events")
    monkeypatch.setattr(lake_maintenance, "_bq_query",
                        lambda sql: (_ for _ in ()).throw(RuntimeError("403")))
    assert lake_maintenance.redact_unsent() == {"error": "redact_failed"}
