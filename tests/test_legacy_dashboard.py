"""旧 /dashboard（最小HTML）廃止後の挙動（docs/dashboard-design.md §10 確定5）。"""
from fastapi.testclient import TestClient

from app import main

client = TestClient(main.app)  # 認証ヘッダなし


def test_dashboard_redirects_to_spa():
    res = client.get("/dashboard", follow_redirects=False)
    assert res.status_code == 308
    assert res.headers["location"] == "/app/"


def test_admin_api_still_guarded():
    assert client.get("/admin/members").status_code == 401
