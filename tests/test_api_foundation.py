"""管理サービス基盤(/api・監査ログ・SPA配信)のテスト。"""
import pytest
from fastapi.testclient import TestClient

from app import main
from app.deps import set_repo
from app.models import Member
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
IAP = {"X-Goog-Authenticated-User-Email": "sed@10to10.co.jp"}


@pytest.fixture(autouse=True)
def repo():
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="m1", name="太郎", line_user_id="U1"))
    set_repo(r)
    yield r
    set_repo(None)


def test_api_alias_works():
    # 同じハンドラが /api でも /admin でも応答
    assert client.get("/api/members").status_code == 200
    assert client.get("/admin/members").status_code == 200


def test_api_is_protected():
    noauth = TestClient(main.app)
    assert noauth.get("/api/members").status_code == 401


def test_iap_header_authorizes_behind_iap(monkeypatch):
    """IAP で守られた管理サービス（ROLE=admin）では IAP のヘッダで認可する。"""
    monkeypatch.setenv("ROLE", "admin")
    iap_client = TestClient(main.app)
    assert iap_client.get("/api/members", headers=IAP).status_code == 200


def test_spoofed_iap_header_rejected_on_public_service(monkeypatch):
    """公開サービスでは IAP のヘッダを偽装しても入れない（2026-10-12 本番で 200 を確認し修正）。"""
    monkeypatch.delenv("ROLE", raising=False)
    public = TestClient(main.app)
    spoofed = {"X-Goog-Authenticated-User-Email": "accounts.google.com:attacker@example.com"}
    for path in ("/api/members", "/api/gcal/status", "/admin/events", "/tasks/tick"):
        res = public.request("POST" if path == "/tasks/tick" else "GET", path, headers=spoofed)
        assert res.status_code == 401, path


def test_admin_token_works_on_both_services(monkeypatch):
    for role in ("admin", ""):
        monkeypatch.setenv("ROLE", role)
        assert TestClient(main.app).get("/api/members", headers=ADMIN_AUTH).status_code == 200
        wrong = {"X-Admin-Token": "wrong"}
        assert TestClient(main.app).get("/api/members", headers=wrong).status_code == 401


def test_settings_update_writes_audit(repo):
    body = {
        "kill_switch": True,
        "quiet_hours": {"start": "21:00", "end": "08:00", "tz": "Asia/Tokyo"},
        "rate_limit": {"per_member_per_day": 3, "global_per_min": 30},
    }
    res = client.put("/api/settings", json=body, headers=IAP)
    assert res.status_code == 200
    logs = repo.list_audit()
    assert any(a.action == "settings.update" and a.actor == "sed@10to10.co.jp" for a in logs)
    # 監査API
    assert client.get("/api/audit-logs").status_code == 200
