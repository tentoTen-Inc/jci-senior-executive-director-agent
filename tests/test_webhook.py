"""M0 LINE Webhook 疎通のユニットテスト。"""
import base64
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import main
from tests.conftest import TEST_CHANNEL_SECRET

client = TestClient(main.app)


def sign(body: str) -> str:
    digest = hmac.new(TEST_CHANNEL_SECRET.encode(), body.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _event_envelope(event: dict) -> str:
    return json.dumps({"destination": "Xdummy", "events": [event]})


FOLLOW_EVENT = {
    "type": "follow",
    "mode": "active",
    "timestamp": 1700000000000,
    "source": {"type": "user", "userId": "U_test_user"},
    "replyToken": "reply-token-follow",
    "webhookEventId": "evt-1",
    "deliveryContext": {"isRedelivery": False},
    "follow": {"isUnblocked": True},  # LINE SDK v3 で FollowEvent に必須
}

MESSAGE_EVENT = {
    "type": "message",
    "mode": "active",
    "timestamp": 1700000000000,
    "source": {"type": "user", "userId": "U_test_user"},
    "replyToken": "reply-token-msg",
    "webhookEventId": "evt-2",
    "deliveryContext": {"isRedelivery": False},
    "message": {"type": "text", "id": "msg-1", "text": "こんにちは", "quoteToken": "qt-1"},
}


def test_root():
    res = client.get("/")
    assert res.status_code == 200
    assert res.json()["service"] == "jci-sed-agent"


def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["channel_secret_loaded"] is True
    # conftest はトークンを PLACEHOLDER にしているため未ロード扱い
    assert body["access_token_loaded"] is False


def test_healthz_still_works():
    assert client.get("/healthz").status_code == 200


def test_webhook_rejects_invalid_signature():
    body = _event_envelope(FOLLOW_EVENT)
    res = client.post(
        "/line/webhook",
        content=body,
        headers={"X-Line-Signature": "invalid"},
    )
    assert res.status_code == 400


def test_webhook_follow_triggers_welcome(monkeypatch):
    captured = []
    monkeypatch.setattr(main, "reply", lambda token, text: captured.append((token, text)) or True)

    body = _event_envelope(FOLLOW_EVENT)
    res = client.post(
        "/line/webhook",
        content=body,
        headers={"X-Line-Signature": sign(body)},
    )
    assert res.status_code == 200
    assert len(captured) == 1
    token, text = captured[0]
    assert token == "reply-token-follow"
    assert "招待コード" in text


def test_webhook_message_linked_member_gets_menu(monkeypatch):
    from app.models import Member
    from app.repository import InMemoryRepository

    repo = InMemoryRepository()
    repo.upsert_member(Member(member_id="m1", name="猪苗代 太郎", line_user_id="U_test_user"))
    monkeypatch.setattr(main, "get_repo", lambda: repo)

    captured = []
    monkeypatch.setattr(
        main, "reply_messages", lambda token, msgs: captured.append((token, msgs)) or True
    )

    body = _event_envelope(MESSAGE_EVENT)  # text="こんにちは" → 未知入力→メニュー誘導
    res = client.post(
        "/line/webhook",
        content=body,
        headers={"X-Line-Signature": sign(body)},
    )
    assert res.status_code == 200
    assert len(captured) == 1
    token, msgs = captured[0]
    assert token == "reply-token-msg"
    assert msgs[0].quick_reply is not None  # メニュー(QuickReply)が返る


def test_webhook_message_unlinked_user_treated_as_code(monkeypatch):
    from app.repository import InMemoryRepository

    repo = InMemoryRepository()  # コード未登録 → 無効扱い
    monkeypatch.setattr(main, "get_repo", lambda: repo)

    captured = []
    monkeypatch.setattr(
        main, "reply_messages", lambda token, msgs: captured.append((token, msgs)) or True
    )

    body = _event_envelope(MESSAGE_EVENT)
    res = client.post(
        "/line/webhook",
        content=body,
        headers={"X-Line-Signature": sign(body)},
    )
    assert res.status_code == 200
    assert "無効" in captured[0][1][0].text


def _group_message_event(text: str, *, mention: dict | None = None) -> dict:
    message = {"type": "text", "id": "msg-g", "text": text, "quoteToken": "qt-g"}
    if mention is not None:
        message["mention"] = mention
    return {
        "type": "message",
        "mode": "active",
        "timestamp": 1700000000000,
        "source": {"type": "group", "groupId": "G_test", "userId": "U_test_user"},
        "replyToken": "reply-token-group",
        "webhookEventId": "evt-g",
        "deliveryContext": {"isRedelivery": False},
        "message": message,
    }


SELF_MENTION = {
    "mentionees": [
        {"index": 0, "length": 8, "type": "user", "userId": "U_bot", "isSelf": True}
    ]
}


def _post_event(event: dict):
    body = _event_envelope(event)
    return client.post(
        "/line/webhook",
        content=body,
        headers={"X-Line-Signature": sign(body)},
    )


def test_group_message_without_mention_is_ignored(monkeypatch):
    """グループでメンションが無い発言には一切応答しない（招待コード照合もしない）。"""
    from app.repository import InMemoryRepository

    monkeypatch.setattr(main, "get_repo", lambda: InMemoryRepository())
    captured = []
    monkeypatch.setattr(
        main, "reply_messages", lambda token, msgs: captured.append((token, msgs)) or True
    )
    monkeypatch.setattr(main, "reply", lambda token, text: captured.append((token, text)) or True)

    res = _post_event(_group_message_event("おはようございます"))
    assert res.status_code == 200
    assert captured == []


def test_group_message_with_mention_unlinked_user_is_not_invite_code(monkeypatch):
    """グループでメンションされても未連携ユーザーには招待コード無効を返さない。"""
    from app.repository import InMemoryRepository

    monkeypatch.setattr(main, "get_repo", lambda: InMemoryRepository())
    captured = []
    monkeypatch.setattr(
        main, "reply_messages", lambda token, msgs: captured.append((token, msgs)) or True
    )

    res = _post_event(_group_message_event("@猪苗代専務AI こんにちは", mention=SELF_MENTION))
    assert res.status_code == 200
    text = captured[0][1][0].text
    assert "無効" not in text
    assert "個別トーク" in text


def test_group_message_with_mention_linked_member_is_handled(monkeypatch):
    """連携済みメンバーからのメンションはメンション部分を除いた本文で処理する。"""
    from app.models import Member
    from app.repository import InMemoryRepository

    repo = InMemoryRepository()
    repo.upsert_member(Member(member_id="m1", name="猪苗代 太郎", line_user_id="U_test_user"))
    monkeypatch.setattr(main, "get_repo", lambda: repo)

    captured = []
    monkeypatch.setattr(
        main, "reply_messages", lambda token, msgs: captured.append((token, msgs)) or True
    )

    res = _post_event(_group_message_event("@猪苗代専務AI 次回の予定は？", mention=SELF_MENTION))
    assert res.status_code == 200
    token, msgs = captured[0]
    assert token == "reply-token-group"
    assert msgs  # 次回予定の応答が返る


def test_strip_self_mentions_removes_mention_text():
    from linebot.v3.webhooks import TextMessageContent

    message = TextMessageContent.from_dict(
        _group_message_event("@猪苗代専務AI 次回の予定は？", mention=SELF_MENTION)["message"]
    )
    assert main._strip_self_mentions(message) == "次回の予定は？"


def test_reply_skips_when_token_placeholder():
    # PLACEHOLDER のときは送信せず False
    assert main.reply("rt", "test") is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))


# --------------------------------------------------------------------------- #
# 返信の非同期化（Pub/Sub → /pubsub/line-reply）と重複除去
# --------------------------------------------------------------------------- #
PUSH_SA = "pubsub-push@test.iam.gserviceaccount.com"
ENDPOINT = "https://agent.example/pubsub/line-worker"


@pytest.fixture
def replies(monkeypatch):
    from app.deps import set_repo
    from app.repository import InMemoryRepository

    set_repo(InMemoryRepository())
    captured = []
    monkeypatch.setattr(main, "reply", lambda token, text: captured.append(token) or True)
    yield captured
    set_repo(None)


@pytest.fixture
def async_on(monkeypatch):
    monkeypatch.setenv("LINE_ASYNC_REPLY", "1")
    monkeypatch.setenv("LINE_CONTENT_BUCKET", "lake-bucket")
    monkeypatch.setenv("PUBSUB_PUSH_SA", PUSH_SA)
    monkeypatch.setenv("PUBSUB_PUSH_AUDIENCE", ENDPOINT)
    monkeypatch.setattr(main.line_worker, "_verify_jwt",
                        lambda token, audience: {"email": PUSH_SA, "email_verified": True})


def _post_webhook(event: dict):
    body = _event_envelope(event)
    return client.post("/line/webhook", content=body, headers={"X-Line-Signature": sign(body)})


def _push(event: dict):
    env = {"v": 1, "id": event["webhookEventId"], "kind": "webhook", "payload": event}
    data = base64.b64encode(json.dumps(env).encode()).decode()
    return client.post("/pubsub/line-reply", json={"message": {"data": data}},
                       headers={"Authorization": "Bearer t"})


def test_async_webhook_returns_without_replying(replies, async_on, monkeypatch):
    monkeypatch.setattr(main.lake, "ingest_webhook", lambda body: True)
    assert _post_webhook(FOLLOW_EVENT).status_code == 200
    assert replies == []  # 返信は Pub/Sub 側で作る
    assert _push(FOLLOW_EVENT).status_code == 204
    assert replies == ["reply-token-follow"]


def test_async_falls_back_inline_when_publish_fails(replies, async_on, monkeypatch):
    monkeypatch.setattr(main.lake, "ingest_webhook", lambda body: False)
    assert _post_webhook(FOLLOW_EVENT).status_code == 200
    assert replies == ["reply-token-follow"]  # 取りこぼさない
    # あとでリプレイされても二重に返信しない
    assert _push(FOLLOW_EVENT).status_code == 204
    assert replies == ["reply-token-follow"]


def test_duplicate_delivery_replies_once(replies, async_on):
    assert _push(FOLLOW_EVENT).status_code == 204
    redelivered = {**FOLLOW_EVENT, "deliveryContext": {"isRedelivery": True}}
    assert _push(redelivered).status_code == 204
    assert replies == ["reply-token-follow"]


def test_line_redelivery_inline_replies_once(replies, monkeypatch):
    monkeypatch.delenv("LINE_ASYNC_REPLY", raising=False)
    _post_webhook(FOLLOW_EVENT)
    _post_webhook({**FOLLOW_EVENT, "deliveryContext": {"isRedelivery": True}})
    assert replies == ["reply-token-follow"]


def test_reply_endpoint_ignored_while_async_off(replies, async_on, monkeypatch):
    monkeypatch.delenv("LINE_ASYNC_REPLY", raising=False)
    assert _push(FOLLOW_EVENT).status_code == 204
    assert replies == []  # 無効の間は Webhook の中で返信済み


def test_reply_endpoint_requires_push_token(replies, async_on, monkeypatch):
    def reject(token, audience):
        raise ValueError("bad")

    monkeypatch.setattr(main.line_worker, "_verify_jwt", reject)
    assert _push(FOLLOW_EVENT).status_code == 401
    assert replies == []


def test_reply_failure_is_not_retried(replies, async_on, monkeypatch):
    def boom(event):
        raise RuntimeError("LINE API down")

    monkeypatch.setattr(main, "handle_event", boom)
    assert _push(FOLLOW_EVENT).status_code == 204  # 再送すると二重返信になりうるため ack


def test_claim_store_outage_is_retried(replies, async_on, monkeypatch):
    class Down:
        def claim_line_event(self, event_id):
            raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(main, "get_repo", lambda: Down())
    assert _push(FOLLOW_EVENT).status_code == 500  # 返信前なので再送してよい
    assert replies == []
