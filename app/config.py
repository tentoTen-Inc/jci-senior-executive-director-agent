"""環境設定とシークレット解決。

ローカルでは .env / 環境変数、本番(Cloud Run)では Secret Manager から取得する。
Secret Manager は Cloud Run の --set-secrets で環境変数にマウントする運用を基本とし、
未設定時のみ API 経由で取得するフォールバックを持つ。
"""
import functools
import os

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "jci-sed-agent")
TZ = os.environ.get("TZ", "Asia/Tokyo")


def behind_iap() -> bool:
    """IAP で守られたサービス（管理サービス jci-sed-admin、環境変数 ROLE=admin）で動いているか。

    IAP の付ける X-Goog-Authenticated-User-Email を信用してよいのはこのときだけ。
    公開サービス（jci-sed-agent）では誰でも同じヘッダを付けて送れるため信用しない。
    """
    return os.environ.get("ROLE", "").strip() == "admin"


def admin_api_secret() -> str | None:
    """/admin・/tasks 保護用の共有シークレット（環境変数 or Secret Manager）。"""
    return get_secret("ADMIN_API_SECRET", "admin-api-secret")


@functools.lru_cache(maxsize=8)
def _from_secret_manager(secret_id: str) -> str | None:
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
        return client.access_secret_version(name=name).payload.data.decode("utf-8")
    except Exception:  # noqa: BLE001 - 起動時に握りつぶしてヘルスチェックは通す
        return None


def get_secret(env_key: str, secret_id: str) -> str | None:
    """環境変数優先、なければ Secret Manager。"""
    val = os.environ.get(env_key)
    if val:
        return val
    return _from_secret_manager(secret_id)


def line_channel_secret() -> str | None:
    return get_secret("LINE_CHANNEL_SECRET", "line-channel-secret")


def line_channel_access_token() -> str | None:
    return get_secret("LINE_CHANNEL_ACCESS_TOKEN", "line-channel-access-token")
