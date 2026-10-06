"""LINEイベントのデータレイク取込（docs/datalake-design.md §3.1 / §4.1）のテスト。"""
import base64
import json
import logging

import pytest
from fastapi.testclient import TestClient

from app import lake, main
from scripts.replay_ingest_fallback import extract_envelopes
from tests.test_webhook import _event_envelope, sign

client = TestClient(main.app)

GROUP_TEXT = {
    "type": "message",
    "mode": "active",
    "timestamp": 1791241200000,
    "source": {"type": "group", "groupId": "G1", "userId": "U1"},
    "replyToken": "rt-g",
    "webhookEventId": "evt-g1",
    "deliveryContext": {"isRedelivery": False},
    "message": {"type": "text", "id": "m1", "text": "おはようございます", "quoteToken": "q1"},
}
GROUP_FILE = {
    "type": "message",
    "mode": "active",
    "timestamp": 1791241260000,
    "source": {"type": "group", "groupId": "G1", "userId": "U2"},
    "replyToken": "rt-f",
    "webhookEventId": "evt-g2",
    "deliveryContext": {"isRedelivery": False},
    "message": {"type": "file", "id": "m2", "fileName": "議案.pdf", "fileSize": 12345},
}


@pytest.fixture
def published(monkeypatch):
    monkeypatch.setenv("LINE_EVENTS_TOPIC", "line-events")
    calls: list[tuple[str, list[dict]]] = []
    monkeypatch.setattr(lake, "_publish_raw", lambda topic, msgs: calls.append((topic, msgs)))
    return calls


def _decode(msg: dict) -> dict:
    return json.loads(base64.b64decode(msg["data"]))


# --------------------------------------------------------------------------- #
# 封筒・属性
# --------------------------------------------------------------------------- #
def test_topic_path_accepts_short_and_full(monkeypatch):
    monkeypatch.setenv("LINE_EVENTS_TOPIC", "line-events")
    assert lake.topic_path() == "projects/test-project/topics/line-events"
    monkeypatch.setenv("LINE_EVENTS_TOPIC", "projects/p/topics/t")
    assert lake.topic_path() == "projects/p/topics/t"
    monkeypatch.setenv("LINE_EVENTS_TOPIC", "  ")
    assert lake.topic_path() is None


def test_webhook_envelopes_keep_raw_event():
    body = json.dumps({"destination": "Ubot", "events": [GROUP_TEXT, GROUP_FILE]})
    envs = lake.webhook_envelopes(body)
    assert [e["id"] for e in envs] == ["evt-g1", "evt-g2"]  # webhookEventId が重複排除キー
    assert envs[0]["kind"] == "webhook"
    assert envs[0]["destination"] == "Ubot"
    assert envs[0]["payload"] == GROUP_TEXT  # 生イベントを加工しない
    assert envs[0]["received_at"].endswith("+09:00")


def test_attributes_for_filters():
    [text_env, file_env] = lake.webhook_envelopes(
        json.dumps({"destination": "U", "events": [GROUP_TEXT, GROUP_FILE]})
    )
    assert lake.attributes_for(text_env) == {
        "kind": "webhook", "event_type": "message", "message_type": "text", "source_type": "group",
    }
    assert lake.attributes_for(file_env)["message_type"] == "file"
    join = lake.envelope("webhook", {"type": "join", "source": {"type": "group", "groupId": "G"}})
    assert lake.attributes_for(join) == {
        "kind": "webhook", "event_type": "join", "source_type": "group",
    }  # 空の属性は入れない
    profile = lake.envelope("group_profile", {"groupId": "G"})
    assert lake.attributes_for(profile) == {"kind": "group_profile"}
    assert profile["id"].startswith("group_profile_")


# --------------------------------------------------------------------------- #
# publish
# --------------------------------------------------------------------------- #
def test_disabled_without_topic(monkeypatch):
    monkeypatch.delenv("LINE_EVENTS_TOPIC", raising=False)
    called = []
    monkeypatch.setattr(lake, "_publish_raw", lambda *a: called.append(a))
    lake.ingest_webhook(json.dumps({"events": [GROUP_TEXT]}))
    assert called == []


def test_ingest_publishes_all_events_in_one_call(published):
    lake.ingest_webhook(json.dumps({"destination": "Ubot", "events": [GROUP_TEXT, GROUP_FILE]}))
    [(topic, msgs)] = published
    assert topic == "projects/test-project/topics/line-events"
    assert [_decode(m)["payload"]["message"]["id"] for m in msgs] == ["m1", "m2"]
    assert msgs[1]["attributes"]["message_type"] == "file"


def test_publish_failure_logs_fallback(monkeypatch, caplog):
    monkeypatch.setenv("LINE_EVENTS_TOPIC", "line-events")

    def boom(topic, msgs):
        raise RuntimeError("Pub/Sub publish 503")

    monkeypatch.setattr(lake, "_publish_raw", boom)
    with caplog.at_level(logging.ERROR, logger="jci-agent.lake"):
        ok = lake.publish(lake.webhook_envelopes(json.dumps({"events": [GROUP_TEXT]})))
    assert ok is False
    fallback = [r.getMessage() for r in caplog.records if lake.FALLBACK_MARKER in r.getMessage()]
    assert len(fallback) == 1
    assert "おはようございます" in fallback[0]


def test_bad_body_does_not_raise(published):
    lake.ingest_webhook("not json")
    assert published == []


# --------------------------------------------------------------------------- #
# Webhook 経由
# --------------------------------------------------------------------------- #
def _post(event: dict):
    body = _event_envelope(event)
    return client.post("/line/webhook", content=body, headers={"X-Line-Signature": sign(body)})


def test_webhook_ingests_ignored_group_message(published, monkeypatch):
    """メンションが無く応答しないグループ発言も、データとしては全部残す。"""
    replies = []
    monkeypatch.setattr(main, "reply_messages", lambda *a: replies.append(a) or True)
    res = _post(GROUP_TEXT)
    assert res.status_code == 200
    assert replies == []  # 応答はしない（F2-5）
    [(_, [msg])] = published
    assert _decode(msg)["payload"]["message"]["text"] == "おはようございます"


def test_webhook_ingests_file_message(published):
    assert _post(GROUP_FILE).status_code == 200
    [(_, [msg])] = published
    assert _decode(msg)["payload"]["message"]["fileName"] == "議案.pdf"


def test_webhook_still_200_when_publish_fails(monkeypatch):
    monkeypatch.setenv("LINE_EVENTS_TOPIC", "line-events")
    monkeypatch.setattr(lake, "_publish_raw", lambda *a: (_ for _ in ()).throw(OSError("down")))
    assert _post(GROUP_TEXT).status_code == 200


def test_webhook_rejects_bad_signature_without_ingest(published):
    body = _event_envelope(GROUP_TEXT)
    res = client.post("/line/webhook", content=body, headers={"X-Line-Signature": "bad"})
    assert res.status_code == 400
    assert published == []  # 署名が不正なものは取り込まない


# --------------------------------------------------------------------------- #
# 退避ログからの再投入
# --------------------------------------------------------------------------- #
def test_extract_envelopes_from_logs():
    env = lake.webhook_envelopes(json.dumps({"events": [GROUP_TEXT]}))[0]
    line = f"ERROR:jci-agent.lake:{lake.FALLBACK_MARKER} {json.dumps(env, ensure_ascii=False)}"
    entries = [
        {"textPayload": line},
        {"textPayload": line},  # 重複
        {"textPayload": "INFO: something else"},
        {"textPayload": f"{lake.FALLBACK_MARKER} {{broken"},
    ]
    assert extract_envelopes(entries) == [env]
