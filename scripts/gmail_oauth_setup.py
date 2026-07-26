#!/usr/bin/env python3
"""専用Gmail（個人アカウント）の refresh token を取得するワンタイム・スクリプト。

対外連絡の取込（F5-1）は個人Gmail運用のためドメイン全体委任が使えない。
本人が一度だけ同意して refresh token を発行し、Secret Manager に登録する。

使い方:
  1. GCP コンソール → 「API とサービス」→ OAuth 同意画面を構成し、
     認証情報で **デスクトップ アプリ** の OAuth クライアントを作成する。
     （Gmail API を有効化しておく: gcloud services enable gmail.googleapis.com）
  2. 発行された client_id / client_secret を渡して本スクリプトを実行:

       python scripts/gmail_oauth_setup.py --client-id XXX --client-secret YYY

  3. 表示されたURLをブラウザで開き、**専用Gmail（inawashiro.jc@gmail.com）** で
     ログインして許可 → 表示されたコードを貼り付ける。
  4. 出力された refresh token を Secret Manager に登録（コマンドも表示される）。

スコープは gmail.readonly のみ（送信・削除はしない）。
"""
from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
# デスクトップアプリ向けの手動コピー用リダイレクト
REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"


def auth_url(client_id: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",  # 毎回 refresh token を返させる
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange(client_id: str, client_secret: str, code: str) -> dict:
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail の refresh token を取得する")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    parser.add_argument("--project", default="jci-sed-agent")
    args = parser.parse_args()

    print("\n次のURLをブラウザで開き、専用Gmailアカウントで許可してください:\n")
    print(auth_url(args.client_id))
    code = input("\n表示された認可コードを貼り付けてください: ").strip()
    if not code:
        raise SystemExit("認可コードが入力されませんでした。")

    tokens = exchange(args.client_id, args.client_secret, code)
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise SystemExit(
            "refresh_token が返りませんでした。既に同意済みの場合は "
            "https://myaccount.google.com/permissions で"
            "アクセス権を削除してから再実行してください。"
        )

    print("\n取得できました。次の3つを Secret Manager に登録してください:\n")
    for secret_id, value in (
        ("gmail-oauth-client-id", args.client_id),
        ("gmail-oauth-client-secret", args.client_secret),
        ("gmail-oauth-refresh-token", refresh_token),
    ):
        print(
            f"  printf %s '{value}' | gcloud secrets create {secret_id} "
            f"--data-file=- --project {args.project} 2>/dev/null || \\\n"
            f"  printf %s '{value}' | gcloud secrets versions add {secret_id} "
            f"--data-file=- --project {args.project}"
        )
    print(
        "\n登録後、Cloud Run（jci-sed-agent / jci-sed-admin）に "
        "--update-secrets GMAIL_OAUTH_CLIENT_ID=gmail-oauth-client-id:latest,"
        "GMAIL_OAUTH_CLIENT_SECRET=gmail-oauth-client-secret:latest,"
        "GMAIL_OAUTH_REFRESH_TOKEN=gmail-oauth-refresh-token:latest を適用するか、"
        "実行SAに roles/secretmanager.secretAccessor を付与してください。"
    )


if __name__ == "__main__":
    main()
