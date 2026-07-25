"""JCI 専務理事エージェント — M0: LINE Webhook 疎通 PoC.

エンドポイント:
- GET  /              : 稼働確認
- GET  /healthz       : ヘルスチェック（Cloud Run用）
- POST /line/webhook  : LINE Messaging API の Webhook 受信
    - 署名検証（channel secret）
    - follow（友だち追加）→ 歓迎メッセージ
    - message（テキスト）→ オウム返し（疎通確認用）

設計方針:
- シークレットは関数経由で遅延解決（テスト時に環境変数で差し替え可能）。
- チャネルアクセストークン未設定でも Webhook は 200 を返し受信ログを残す（返信のみスキップ）。
"""
import logging
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    Message,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    FollowEvent,
    MessageEvent,
    PostbackEvent,
    TextMessageContent,
)

from . import config, line_push
from .admin_api import router as admin_router
from .delivery import execute_delivery
from .deps import get_repo
from .invite import verify_and_link
from .line_messages import apply_postback, build_attendance_request
from .member_menu import handle_member_text
from .notices_api import router as notices_router
from .proposals_api import router as proposals_router
from .reminders import plan_reminders

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jci-agent")

app = FastAPI(title="JCI SED Agent", version="0.1.0")

# /admin・/api・/tasks は非公開。/line/webhook(署名検証)・/・/health のみ公開。
PROTECTED_PREFIXES = ("/admin", "/api", "/tasks")


def _is_authorized(request: Request) -> bool:
    # IAP 経由（Cloud Run 前段で認証済み）
    if request.headers.get("X-Goog-Authenticated-User-Email"):
        return True
    # 共有シークレット。Cloud Run は Authorization: Bearer を IAM 認証として横取りする
    # ため、独自ヘッダ X-Admin-Token を用いる（Cloud Scheduler/管理ツール向け）。
    secret = config.admin_api_secret()
    if secret and request.headers.get("X-Admin-Token") == secret:
        return True
    return False


@app.middleware("http")
async def access_guard(request: Request, call_next):
    path = request.url.path
    if path.startswith(PROTECTED_PREFIXES) and not _is_authorized(request):
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return await call_next(request)


app.include_router(admin_router, prefix="/admin")  # 後方互換
app.include_router(admin_router, prefix="/api")  # SPA(管理ダッシュボード)用
app.include_router(proposals_router, prefix="/admin")
app.include_router(proposals_router, prefix="/api")
app.include_router(notices_router, prefix="/admin")
app.include_router(notices_router, prefix="/api")


@app.get("/dashboard", include_in_schema=False)
def legacy_dashboard():
    """旧・最小HTMLダッシュボードは SPA(/app) へ移行済み（design §10 確定5）。

    既存ブックマーク救済のためリダイレクトのみ残す。
    """
    return RedirectResponse(url="/app/", status_code=308)


def _mount_spa() -> None:
    """ビルド済み SPA (web/dist) があれば /app で静的配信する。

    Cloud Run(管理サービス)のイメージに dist を同梱する。dist が無いローカル/テスト
    環境では何もしない（API・webhook は通常通り動作）。
    """
    import os

    from fastapi.staticfiles import StaticFiles

    dist = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web", "dist")
    if os.path.isdir(dist):
        # html=True で SPA のクライアントルーティング(index.html フォールバック)に対応
        app.mount("/app", StaticFiles(directory=dist, html=True), name="spa")
        logger.info("SPA を /app で配信します: %s", dist)


_mount_spa()

WELCOME_TEXT = (
    "友だち追加ありがとうございます！\n"
    "猪苗代JC 専務理事エージェントです。\n"
    "登録のため、事務局から配布された招待コードを入力してください。"
)


def get_parser() -> WebhookParser | None:
    """channel secret から Webhook パーサを構築（未設定なら None）。"""
    secret = config.line_channel_secret()
    return WebhookParser(secret) if secret else None


def _access_token_ready(token: str | None) -> bool:
    return bool(token and token != "PLACEHOLDER_SET_ME")


def reply_messages(reply_token: str, messages: list[Message]) -> bool:
    """LINE へ任意のメッセージ列を返信。アクセストークン未設定時はスキップ。"""
    token = config.line_channel_access_token()
    if not _access_token_ready(token):
        logger.warning("access token 未設定のため返信スキップ (%d件)", len(messages))
        return False
    configuration = Configuration(access_token=token)
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=messages)
        )
    return True


def reply(reply_token: str, text: str) -> bool:
    """テキスト1通を返信するショートカット。"""
    return reply_messages(reply_token, [TextMessage(text=text)])


@app.get("/")
def root():
    return {"service": "jci-sed-agent", "stage": "M0", "status": "ok"}


@app.get("/favicon.ico")
def favicon():
    # ファビコンは SPA 側で data URI を使うため、直接要求には 204 を返して 404 を防ぐ
    return Response(status_code=204)


def _health_payload():
    return {
        "ok": True,
        "channel_secret_loaded": bool(config.line_channel_secret()),
        "access_token_loaded": _access_token_ready(config.line_channel_access_token()),
    }


# /healthz は Cloud Run 前段の Google Front End に横取りされる(Google 404 を返す)ため、
# 実用のヘルスチェックは /health を使う。/healthz は後方互換で残す。
@app.get("/health")
def health():
    return _health_payload()


@app.get("/healthz")
def healthz():
    return _health_payload()


def handle_text_message(user_id: str, text: str) -> list[Message]:
    """テキストメッセージへの応答メッセージ列を生成。

    未連携ユーザーは招待コードとして照合し本人確認を行う。
    連携済みユーザーはメニュー応答（次回予定・出欠・連絡）に委譲する。
    """
    repo = get_repo()
    member = repo.get_member_by_line_user_id(user_id)
    if member is None:
        result = verify_and_link(repo, user_id, text, now=datetime.now())
        return [TextMessage(text=result.message)]
    return handle_member_text(repo, member, text, now=datetime.now())


def handle_event(event) -> None:
    """単一の LINE イベントを処理する。"""
    if isinstance(event, FollowEvent):
        logger.info("follow event: userId=%s", event.source.user_id)
        reply(event.reply_token, WELCOME_TEXT)
    elif isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
        user_id = event.source.user_id
        text = event.message.text
        logger.info("message event: userId=%s text=%s", user_id, text)
        reply_messages(event.reply_token, handle_text_message(user_id, text))
    elif isinstance(event, PostbackEvent):
        user_id = event.source.user_id
        data = event.postback.data
        logger.info("postback event: userId=%s data=%s", user_id, data)
        messages = handle_postback(user_id, data)
        reply_messages(event.reply_token, messages)
    else:
        logger.info("other event: %s", type(event).__name__)


def handle_postback(user_id: str, data: str) -> list[Message]:
    """postback（出欠ボタン・メニュー）を処理してメッセージ列を返す。"""
    repo = get_repo()
    member = repo.get_member_by_line_user_id(user_id)
    if member is None:
        return [TextMessage(text="先に招待コードで登録をお願いします。")]
    if data.startswith("menu|"):
        return handle_member_text(repo, member, data, now=datetime.now())
    return apply_postback(repo, member, data, now=datetime.now())


def _push_sender(repo, messages: list[Message]):
    """member_id を LINE userId に解決して Push する sender を作る。"""

    def sender(member_id: str) -> bool:
        member = repo.get_member(member_id)
        if member is None or not member.line_user_id:
            return False
        return line_push.push_messages(member.line_user_id, messages)

    return sender


@app.post("/tasks/tick")
def tasks_tick():
    """Cloud Scheduler から定期起動。締切到来ステージの催促を発火する。

    ※ Cloud Run の IAM 認証（Scheduler の OIDC 呼び出し）で保護する前提。公開しない。
    """
    repo = get_repo()
    now = datetime.now()
    jobs = plan_reminders(repo, now)
    results = []
    for job in jobs:
        event = repo.get_event(job.event_id)
        if event is None:
            continue
        message = build_attendance_request(event)
        report = execute_delivery(
            repo, job, message.text, now=now, sender=_push_sender(repo, [message])
        )
        results.append(
            {
                "job_id": job.job_id,
                "stage": job.stage,
                "sent": report.sent,
                "blocked": report.blocked,
                "deferred": report.deferred,
                "failed": report.failed,
                "halted": report.halted,
            }
        )
    return {"planned": len(jobs), "results": results}


@app.post("/line/webhook")
async def line_webhook(request: Request):
    parser = get_parser()
    if parser is None:
        logger.error("channel secret 未設定。Webhook を処理できません。")
        raise HTTPException(status_code=503, detail="channel secret not configured")

    signature = request.headers.get("X-Line-Signature", "")
    body = (await request.body()).decode("utf-8")

    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError as exc:
        logger.warning("署名検証に失敗しました。")
        raise HTTPException(status_code=400, detail="invalid signature") from exc

    for event in events:
        handle_event(event)

    # LINE には常に 200 を返す（個別失敗は内部でログ化）
    return Response(status_code=200)
