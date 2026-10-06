"""管理SPAの静的配信（画面URLの直接アクセス）のテスト。"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import SpaStaticFiles


def _client(tmp_path) -> TestClient:
    (tmp_path / "index.html").write_text("<!doctype html><title>spa</title>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    app = FastAPI()
    app.mount("/app", SpaStaticFiles(directory=tmp_path, html=True), name="spa")
    return TestClient(app)


def test_root_serves_index(tmp_path):
    res = _client(tmp_path).get("/app/")
    assert res.status_code == 200
    assert "spa" in res.text


def test_client_route_falls_back_to_index(tmp_path):
    """/app/events 等を直接開いても（再読み込みしても）画面が出る。"""
    client = _client(tmp_path)
    for path in ("/app/events", "/app/members", "/app/settings/nested"):
        res = client.get(path)
        assert res.status_code == 200, path
        assert "spa" in res.text


def test_existing_asset_is_served(tmp_path):
    res = _client(tmp_path).get("/app/assets/app.js")
    assert res.status_code == 200
    assert "console.log" in res.text


def test_missing_asset_stays_404(tmp_path):
    """壊れたアセット参照を index.html で隠さない。"""
    assert _client(tmp_path).get("/app/assets/missing.js").status_code == 404
