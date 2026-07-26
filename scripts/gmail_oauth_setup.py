#!/usr/bin/env python3
"""専用Gmail（個人アカウント）の refresh token を取得するワンタイム・スクリプト。

対外連絡の取込（F5-1）は個人Gmail運用のためドメイン全体委任が使えない。
本人が一度だけ同意して refresh token を発行し、Secret Manager に登録する。

方式は **loopback（http://localhost:<port>/）**。
Google は OOB（`urn:ietf:wg:oauth:2.0:oob`＝コードを画面表示する方式）を 2022 年に
廃止しているため使えない。ブラウザの戻り先をこのスクリプトが一時的に受け取る。

## 事前準備（GCPコンソール）
1. Gmail API を有効化: `gcloud services enable gmail.googleapis.com`
2. OAuth 同意画面: User type=External。**テストユーザーに専用Gmailを追加**。
   ※ 公開ステータスが「テスト」のままだと **refresh token が7日で失効**するため、
     継続運用するなら「本番」に切り替える（§下部の注意）。
3. 認証情報 → OAuth クライアント ID を作成
   - **デスクトップ アプリ**: 追加入力なし（推奨）
   - ウェブ アプリケーション: 「承認済みのリダイレクト URI」に
     `http://localhost:8765/` を登録（JavaScript 生成元は空欄でよい）

## 実行
    # ダウンロードしたクライアントJSONを渡す（秘密情報を端末履歴に残さない）
    python scripts/gmail_oauth_setup.py --client-secret-file client_secret_....json \
        --write-secrets
    # 個別指定も可
    python scripts/gmail_oauth_setup.py --client-id XXX --client-secret YYY

ブラウザが開く → 専用Gmail（inawashiro.jc@gmail.com）で許可 → 自動で戻ってくる。
最後に Secret Manager への登録コマンドを出力する。

スコープは gmail.readonly のみ（送信・削除はしない）。
"""
from __future__ import annotations

import argparse
import http.server
import json
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
DEFAULT_PORT = 8765
#: 未確認アプリの警告画面を通る時間も見て余裕をとる
DEFAULT_TIMEOUT = 900

_DONE_HTML = """<!doctype html><meta charset="utf-8">
<p>認可が完了しました。ターミナルに戻ってください。</p>"""


def redirect_uri(port: int) -> str:
    return f"http://localhost:{port}/"


def auth_url(client_id: str, port: int) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(port),
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",  # 毎回 refresh token を返させる
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


class _CodeHandler(http.server.BaseHTTPRequestHandler):
    """認可コードを1回だけ受け取るハンドラ。"""

    code: str | None = None
    error: str | None = None

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler の規約
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        code = (params.get("code") or [None])[0]
        error = (params.get("error") or [None])[0]
        if code:
            _CodeHandler.code = code
        if error:
            _CodeHandler.error = error
        if not (code or error):
            # /favicon.ico 等は無視して待ち続ける
            self.send_response(204)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_DONE_HTML.encode())

    def log_message(self, *args) -> None:  # ローカルサーバのアクセスログは出さない
        return


def wait_for_code(port: int, timeout: int) -> str:
    """ブラウザからの戻りを待って認可コードを返す。

    ブラウザは /favicon.ico など無関係なリクエストも投げてくるため、
    `code` か `error` を受け取るまで受付を続ける（1回で打ち切らない）。
    """
    server = http.server.HTTPServer(("localhost", port), _CodeHandler)
    server.timeout = 1  # handle_request のポーリング間隔
    deadline = time.monotonic() + timeout

    def _serve() -> None:
        while _CodeHandler.code is None and _CodeHandler.error is None:
            if time.monotonic() > deadline:
                return
            server.handle_request()  # timeout=1 秒で戻る

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    thread.join(timeout + 2)
    server.server_close()

    if _CodeHandler.error:
        raise SystemExit(f"認可が拒否されました: {_CodeHandler.error}")
    if not _CodeHandler.code:
        raise SystemExit(
            f"認可コードを受け取れませんでした（{timeout}秒でタイムアウト）。"
            "--timeout を延ばすか、--no-server で手動貼り付けに切り替えてください。"
        )
    return _CodeHandler.code


def exchange(client_id: str, client_secret: str, code: str, port: int) -> dict:
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri(port),
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:  # 原因が分かるよう本文を出す
        body = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"トークン交換に失敗しました: {body}") from exc


def load_client_json(path: str) -> tuple[str, str, list[str]]:
    """GCPからダウンロードしたクライアントJSONを読む（web / installed の両方に対応）。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    for key in ("web", "installed"):
        if key in data:
            client = data[key]
            return (
                client["client_id"],
                client["client_secret"],
                client.get("redirect_uris") or [],
            )
    raise SystemExit("クライアントJSONの形式が不明です（web / installed が見つかりません）。")


def write_secret(secret_id: str, value: str, project: str) -> None:
    """Secret Manager に登録（無ければ作成、あれば新バージョン追加）。"""
    create = subprocess.run(
        ["gcloud", "secrets", "create", secret_id, "--data-file=-", "--project", project],
        input=value.encode(), capture_output=True,
    )
    if create.returncode == 0:
        print(f"  作成: {secret_id}")
        return
    add = subprocess.run(
        ["gcloud", "secrets", "versions", "add", secret_id, "--data-file=-",
         "--project", project],
        input=value.encode(), capture_output=True,
    )
    if add.returncode != 0:
        raise SystemExit(
            f"{secret_id} の登録に失敗しました: "
            f"{(add.stderr or create.stderr).decode('utf-8', 'replace')}"
        )
    print(f"  更新: {secret_id}")


def print_secret_commands(project: str, values: dict[str, str]) -> None:
    print("\n取得できました。次の3つを Secret Manager に登録してください:\n")
    for secret_id, value in values.items():
        print(
            f"  printf %s '{value}' | gcloud secrets create {secret_id} "
            f"--data-file=- --project {project} 2>/dev/null || \\\n"
            f"  printf %s '{value}' | gcloud secrets versions add {secret_id} "
            f"--data-file=- --project {project}"
        )
    print(
        "\n実行SA(app-runtime@)にはプロジェクトレベルで secretmanager.secretAccessor が"
        "付与済みのため、Cloud Run の再設定は不要です（次の tick から取込が始まります）。"
    )
    print(
        "\n※ OAuth同意画面の公開ステータスが「テスト」のままだと refresh token は"
        "7日で失効します。継続運用するなら「本番」に切り替えてください。"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail の refresh token を取得する")
    parser.add_argument("--client-id")
    parser.add_argument("--client-secret")
    parser.add_argument(
        "--client-secret-file",
        help="GCPからダウンロードしたクライアントJSON（--client-id/--secret の代わり）",
    )
    parser.add_argument(
        "--write-secrets", action="store_true",
        help="取得した refresh token を表示せず Secret Manager に直接登録する",
    )
    parser.add_argument("--project", default="jci-sed-agent")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT,
        help=f"認可を待つ秒数（既定 {DEFAULT_TIMEOUT}）",
    )
    parser.add_argument(
        "--no-server", action="store_true",
        help="ローカルサーバを立てず、戻り先URLの code= を手動で貼り付ける",
    )
    args = parser.parse_args()

    if args.client_secret_file:
        client_id, client_secret, registered = load_client_json(args.client_secret_file)
        expected = redirect_uri(args.port)
        if registered and expected not in registered:
            raise SystemExit(
                f"このクライアントに登録されたリダイレクトURIは {registered} です。"
                f"--port を合わせるか、コンソールに {expected} を追加してください。"
            )
    elif args.client_id and args.client_secret:
        client_id, client_secret = args.client_id, args.client_secret
    else:
        raise SystemExit(
            "--client-secret-file か、--client-id と --client-secret を指定してください。"
        )

    url = auth_url(client_id, args.port)
    print("\n次のURLをブラウザで開き、専用Gmailアカウントで許可してください:\n", flush=True)
    print(url, flush=True)
    print(f"\n（戻り先: {redirect_uri(args.port)}）", flush=True)

    if args.no_server:
        print(
            "\n許可後、ブラウザのアドレスバーに出る "
            f"{redirect_uri(args.port)}?code=... の code の値を貼り付けてください。"
        )
        code = input("code: ").strip()
        if not code:
            raise SystemExit("認可コードが入力されませんでした。")
    else:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - 開けなければ手動で開いてもらう
            pass
        print(
            f"\nブラウザの許可を待っています…（最大{args.timeout // 60}分）", flush=True
        )
        code = wait_for_code(args.port, args.timeout)

    tokens = exchange(client_id, client_secret, code, args.port)
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise SystemExit(
            "refresh_token が返りませんでした。既に同意済みの場合は "
            "https://myaccount.google.com/permissions で"
            "アクセス権を削除してから再実行してください。"
        )

    secrets = {
        "gmail-oauth-client-id": client_id,
        "gmail-oauth-client-secret": client_secret,
        "gmail-oauth-refresh-token": refresh_token,
    }
    if args.write_secrets:
        print("\nSecret Manager に登録します:")
        for secret_id, value in secrets.items():
            write_secret(secret_id, value, args.project)
        print(
            "\n完了しました。実行SA(app-runtime@)にはプロジェクトレベルで "
            "secretmanager.secretAccessor が付与済みのため、Cloud Run の再設定は不要です"
            "（次の tick から取込が始まります）。"
        )
        print(
            "\n※ OAuth同意画面の公開ステータスが「テスト」のままだと refresh token は"
            "7日で失効します。継続運用するなら「本番」に切り替えてください。"
        )
    else:
        print_secret_commands(args.project, secrets)


if __name__ == "__main__":
    main()
