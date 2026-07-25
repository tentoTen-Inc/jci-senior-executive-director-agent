"""Google Forms 参照（鍵レス impersonation, docs/survey-design.md §2）。

Cloud Run の実行SA(app-runtime)が drive-reader を権限借用して Forms API を読む。
対象フォームは drive-reader に共有済みであること。fetch_form がテストのモック境界。
"""
from __future__ import annotations

import json
import logging
import urllib.request

logger = logging.getLogger("jci-agent.forms")

DRIVE_READER_SA_ENV = "DRIVE_READER_SA"
DEFAULT_SA = "drive-reader@jci-sed-agent.iam.gserviceaccount.com"
SCOPES = [
    "https://www.googleapis.com/auth/forms.body.readonly",
    "https://www.googleapis.com/auth/forms.responses.readonly",
]
API_BASE = "https://forms.googleapis.com/v1/forms"


def _reader_sa() -> str:
    import os

    return os.environ.get(DRIVE_READER_SA_ENV, DEFAULT_SA)


def _token() -> str:
    """drive-reader を impersonate したアクセストークンを取得する。"""
    from google.auth import default, impersonated_credentials
    from google.auth.transport.requests import Request

    source, _ = default()
    target = impersonated_credentials.Credentials(
        source_credentials=source,
        target_principal=_reader_sa(),
        target_scopes=SCOPES,
    )
    target.refresh(Request())
    return target.token


def _get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def fetch_form(form_id: str) -> dict:
    """フォームの構造と回答をまとめて返す（モック境界）。

    戻り値: ``{"form": <GET /forms/{id}>, "responses": [<responses[]>]}``
    """
    token = _token()
    form = _get(f"{API_BASE}/{form_id}", token)
    responses: list[dict] = []
    url = f"{API_BASE}/{form_id}/responses"
    while True:
        page = _get(url, token)
        responses.extend(page.get("responses", []))
        next_token = page.get("nextPageToken")
        if not next_token:
            break
        url = f"{API_BASE}/{form_id}/responses?pageToken={next_token}"
    return {"form": form, "responses": responses}
