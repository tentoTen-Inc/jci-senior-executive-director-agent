"""本番で起きた「自由文に返事できない」不具合の回帰テスト（2026-10-06）。

- 対外連絡の取得が Firestore の複合インデックス不足で FailedPrecondition になり、
  自由文の応答（F8）が例外で止まって返信されなかった。
- Webhook が例外をそのまま漏らして 500 を返し、同じ送信の後続イベントも処理されなかった。
"""
from datetime import datetime

from fastapi.testclient import TestClient

from app import main, rag
from app.firestore_repo import FirestoreRepository
from app.models import Event, EventStatus, EventType, ExternalNotice, Member
from app.repository import InMemoryRepository
from tests.test_webhook import sign

client = TestClient(main.app)


# --------------------------------------------------------------------------- #
# Firestore: 複合インデックスが無くても対外連絡を読める
# --------------------------------------------------------------------------- #
class _Snap:
    def __init__(self, data: dict) -> None:
        self._data = data

    def to_dict(self) -> dict:
        return self._data


class _Query:
    """本番の Firestore と同じく、等価フィルタ＋別フィールドの並べ替えで失敗する。"""

    def __init__(self, docs: list[dict], filters: list | None = None) -> None:
        self.docs = docs
        self.filters = filters or []

    def where(self, field, op, value):
        return _Query(self.docs, [*self.filters, (field, value)])

    def order_by(self, field, direction=None):
        if self.filters and all(f != field for f, _ in self.filters):
            raise RuntimeError("400 The query requires an index.")
        return self

    def stream(self):
        for d in self.docs:
            if all(d.get(f) == v for f, v in self.filters):
                yield _Snap(d)


class _Db:
    def __init__(self, docs: list[dict]) -> None:
        self.docs = docs

    def collection(self, name):
        return _Query(self.docs)


def _notice(nid: str, status: str, day: int) -> dict:
    return ExternalNotice(
        notice_id=nid, received_at=datetime(2026, 10, day, 9, 0), subject=f"連絡{nid}",
        body_text="本文", status=status,
    ).model_dump(mode="json")


def test_list_notices_by_status_needs_no_composite_index():
    repo = FirestoreRepository(client=_Db([
        _notice("a", "delivered", 1), _notice("b", "new", 3), _notice("c", "delivered", 5),
    ]))
    assert [n.notice_id for n in repo.list_notices(status="delivered")] == ["c", "a"]
    assert [n.notice_id for n in repo.list_notices()] == ["c", "b", "a"]


# --------------------------------------------------------------------------- #
# 回答材料の一部が取れなくても応答する
# --------------------------------------------------------------------------- #
class _BrokenNotices(InMemoryRepository):
    def list_notices(self, *, status=None):
        raise RuntimeError("400 The query requires an index.")


def test_build_context_survives_notice_failure():
    repo = _BrokenNotices()
    repo.upsert_event(Event(
        event_id="ev1", type=EventType.例会, title="10月例会",
        datetime_start=datetime(2026, 10, 21, 19, 0), status=EventStatus.open,
    ))
    member = Member(member_id="m1", name="専務 太郎", officer_role="専務理事", line_user_id="U1")
    repo.upsert_member(member)
    context = rag.build_context(repo, member, now=datetime(2026, 10, 6, 12, 0))
    assert "10月例会" in context  # 予定の材料は残る


# --------------------------------------------------------------------------- #
# Webhook: 1件の失敗で他を止めず、必ず 200
# --------------------------------------------------------------------------- #
def _text_event(eid: str, text: str) -> dict:
    return {
        "type": "message", "mode": "active", "timestamp": 1791241200000,
        "source": {"type": "user", "userId": "U_test_user"},
        "replyToken": f"rt-{eid}", "webhookEventId": eid,
        "deliveryContext": {"isRedelivery": False},
        "message": {"type": "text", "id": eid, "text": text, "quoteToken": "q"},
    }


def test_webhook_returns_200_and_continues_after_failure(monkeypatch):
    handled = []

    def flaky(event):
        handled.append(event.message.text)
        if event.message.text == "壊れる":
            raise RuntimeError("400 The query requires an index.")

    monkeypatch.setattr(main, "handle_event", flaky)
    import json

    body = json.dumps({"destination": "U", "events": [
        _text_event("e1", "壊れる"), _text_event("e2", "次回の予定は？"),
    ]})
    res = client.post("/line/webhook", content=body, headers={"X-Line-Signature": sign(body)})
    assert res.status_code == 200
    assert handled == ["壊れる", "次回の予定は？"]  # 2件目も処理される
