"""テスト共通設定。

アプリ import 前に LINE シークレットを環境変数で固定し、
Secret Manager への実アクセスを避ける。
"""
import os

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "test_channel_secret")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "PLACEHOLDER_SET_ME")
os.environ.setdefault("GCP_PROJECT_ID", "test-project")
os.environ.setdefault("ADMIN_API_SECRET", "test-admin-secret")

TEST_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
ADMIN_AUTH = {"X-Admin-Token": os.environ["ADMIN_API_SECRET"]}


@pytest.fixture(autouse=True)
def _no_live_firestore():
    """リポジトリを注入していないテストが本物の Firestore に繋ぎに行かないようにする。"""
    from app import deps
    from app.repository import InMemoryRepository

    injected = deps._repo is None
    if injected:
        deps.set_repo(InMemoryRepository())
    yield
    if injected:
        deps.set_repo(None)
