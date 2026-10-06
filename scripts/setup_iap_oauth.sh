#!/usr/bin/env bash
# 管理画面（jci-sed-admin）の IAP に自前の OAuth クライアントを設定する（1回だけ）。
#
# 背景: Cloud Run の IAP は既定で「Google 管理の OAuth クライアント」を使うが、これは
#       組織（10to10.co.jp）内のユーザーしか通さない。@gmail.com の役員・専務を
#       許可リスト（roles/iap.httpsResourceAccessor）どおりに通すには自前のクライアントが要る。
#       https://docs.cloud.google.com/iap/docs/custom-oauth-configuration
#
# 事前作業（Cloud コンソール・手動）:
#   1. 「Google Auth Platform」→「ブランディング/対象」でユーザーの種類が「外部」になっていること。
#      公開ステータスが「テスト」なら、ログインする gmail アカウントをテストユーザーに追加する。
#   2. 「認証情報」→「OAuth クライアント ID を作成」→ 種類「ウェブ アプリケーション」で作成。
#   3. 作成したクライアントを開き、承認済みのリダイレクト URI に次を追加して保存:
#        https://iap.googleapis.com/v1/oauth/clientIds/<クライアントID>:handleRedirect
#
# 使い方:
#   ./scripts/setup_iap_oauth.sh                      # クライアントID/シークレットを対話入力（表示しない）
#   ./scripts/setup_iap_oauth.sh client_secret_XXX.json  # コンソールでダウンロードした JSON から読む
set -euo pipefail

PROJECT="${GCP_PROJECT_ID:-jci-sed-agent}"
REGION="${GCP_REGION:-asia-northeast1}"
SERVICE="${IAP_SERVICE:-jci-sed-admin}"

if [[ $# -ge 1 ]]; then
  # ダウンロードした client_secret_*.json（web 型）から読む。値は画面に出さない
  CLIENT_ID="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(next(iter(d.values()))["client_id"])' "$1")"
  CLIENT_SECRET="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(next(iter(d.values()))["client_secret"])' "$1")"
  echo ">> ${1} からクライアント ${CLIENT_ID} を読み込みました"
else
  read -r -p "OAuth クライアント ID: " CLIENT_ID
  read -r -s -p "OAuth クライアント シークレット（入力は表示されません）: " CLIENT_SECRET
  echo
fi
if [[ -z "${CLIENT_ID}" || -z "${CLIENT_SECRET}" ]]; then
  echo "クライアント ID とシークレットの両方が必要です。" >&2
  exit 1
fi

# シークレットをディスクに残さないよう、一時ファイルは終了時に必ず消す
TMP="$(mktemp)"
trap 'rm -f "${TMP}"' EXIT
chmod 600 "${TMP}"
cat > "${TMP}" <<EOF
accessSettings:
  oauthSettings:
    clientId: ${CLIENT_ID}
    clientSecret: ${CLIENT_SECRET}
EOF

echo ">> ${SERVICE} の IAP に OAuth クライアントを設定"
gcloud iap settings set "${TMP}" \
  --project="${PROJECT}" \
  --resource-type=cloud-run \
  --region="${REGION}" \
  --service="${SERVICE}" >/dev/null

echo ">> 設定後の状態（シークレットは表示しない）"
gcloud iap settings get \
  --project="${PROJECT}" \
  --resource-type=cloud-run \
  --region="${REGION}" \
  --service="${SERVICE}" \
  --format="value(accessSettings.oauthSettings.clientId)"

cat <<EOF

完了しました。ブラウザでいったんログアウト（またはシークレットウィンドウ）してから
  https://jci-sed-admin-6momralspq-an.a.run.app/app/events
を開き、許可リストの gmail アカウントでログインできるか確認してください。

リダイレクト URI（クライアントに登録が必要）:
  https://iap.googleapis.com/v1/oauth/clientIds/${CLIENT_ID}:handleRedirect
EOF
