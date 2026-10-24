#!/usr/bin/env python3
"""立場ごとのリッチメニューを生成して LINE に登録する（docs/doc-session-design.md §3）。

使い方（リポジトリ直下・direnv 有効な状態で）:
  python scripts/setup_rich_menu.py --image-only out_dir/   # 画像の生成のみ（3種類）
  python scripts/setup_rich_menu.py --create      # 3種類を作成して ID を設定に保存、会員用を既定に
  python scripts/setup_rich_menu.py --link-all    # 連携済みの会員に立場のメニューを結ぶ

- 作成した ID は Firestore の Settings.rich_menus（member / board / officer）に保存する。
  アプリ（LINE 連携の承認・招待コード・組織図の取込）はここを読んで会員ごとに結びつける。
- LINE のチャネルアクセストークンは Secret Manager から読む（gcloud のログインを使う）。
- 登録は LINE Messaging API を直接叩く（line-bot-sdk のバージョン差異を避けるため urllib）。
"""
import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.rich_menu import KINDS, MEMBER, generate_image, rich_menu_definition  # noqa: E402

PROJECT = "jci-sed-agent"
API = "https://api.line.me"
DATA_API = "https://api-data.line.me"


def _req(url: str, token: str, *, method: str, data: bytes | None = None, content_type: str):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if content_type:
        req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req) as resp:
        return resp.status, resp.read().decode()


def _line_token() -> str:
    return subprocess.run(
        ["gcloud", "secrets", "versions", "access", "latest", "--secret",
         "line-channel-access-token", "--project", PROJECT],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _repo():
    from google.cloud import firestore
    from google.oauth2.credentials import Credentials

    from app.firestore_repo import FirestoreRepository

    token = subprocess.run(
        ["gcloud", "auth", "print-access-token"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    return FirestoreRepository(client=firestore.Client(project=PROJECT,
                                                       credentials=Credentials(token)))


def create(token: str, workdir: Path) -> dict[str, str]:
    """3種類のメニューを作成して画像を載せ、{種類: ID} を返す。"""
    ids = {}
    for kind in KINDS:
        image = generate_image(str(workdir / f"rich_menu_{kind}.png"), kind)
        status, body = _req(f"{API}/v2/bot/richmenu", token, method="POST",
                            data=json.dumps(rich_menu_definition(kind=kind)).encode(),
                            content_type="application/json")
        menu_id = json.loads(body)["richMenuId"]
        with open(image, "rb") as f:
            _req(f"{DATA_API}/v2/bot/richmenu/{menu_id}/content", token, method="POST",
                 data=f.read(), content_type="image/png")
        print(f"作成: {kind} = {menu_id} (HTTP {status})")
        ids[kind] = menu_id
    return ids


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image-only", metavar="DIR", default=None, help="画像だけ生成する")
    ap.add_argument("--create", action="store_true", help="3種類を作成して設定に保存する")
    ap.add_argument("--link-all", action="store_true", help="連携済みの会員に結びつける")
    args = ap.parse_args()

    if args.image_only:
        out = Path(args.image_only)
        out.mkdir(parents=True, exist_ok=True)
        for kind in KINDS:
            print("生成:", generate_image(str(out / f"rich_menu_{kind}.png"), kind))
        return 0
    if not (args.create or args.link_all):
        ap.print_help()
        return 1

    repo = _repo()
    if args.create:
        token = _line_token()
        ids = create(token, Path("/tmp"))
        settings = repo.get_settings()
        settings.rich_menus = ids
        repo.save_settings(settings)
        # 会員用を全員の既定にする（個別に結びつけていない人・未連携の人に出る）
        _req(f"{API}/v2/bot/user/all/richmenu/{ids[MEMBER]}", token, method="POST",
             data=None, content_type="")
        print("設定に保存し、会員用を既定にしました。")
    if args.link_all:
        import os

        from app import rich_menu_link

        os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", _line_token())
        print("結びつけ:", rich_menu_link.link_all(repo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
