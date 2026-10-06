#!/usr/bin/env bash
# JCI 専務理事エージェント — Cloud Run デプロイ（再現可能なワンコマンド）
#
# 前提: gcloud 認証済み、Secret Manager に line-channel-secret /
#       line-channel-access-token / admin-api-secret が存在。
# 使い方: ./scripts/deploy.sh
set -euo pipefail

PROJECT="${GCP_PROJECT_ID:-jci-sed-agent}"
REGION="${GCP_REGION:-asia-northeast1}"
SERVICE="jci-sed-agent"
RUNTIME_SA="app-runtime@${PROJECT}.iam.gserviceaccount.com"

echo ">> Deploying ${SERVICE} to ${PROJECT}/${REGION}"

gcloud run deploy "${SERVICE}" \
  --source . \
  --region "${REGION}" \
  --project "${PROJECT}" \
  --service-account "${RUNTIME_SA}" \
  --set-secrets "LINE_CHANNEL_SECRET=line-channel-secret:latest,LINE_CHANNEL_ACCESS_TOKEN=line-channel-access-token:latest,ADMIN_API_SECRET=admin-api-secret:latest" \
  --update-env-vars "GCP_PROJECT_ID=${PROJECT},TZ=Asia/Tokyo" \
  --allow-unauthenticated

URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" --project "${PROJECT}" --format='value(status.url)')"
echo ">> Deployed: ${URL}"

echo ">> Health check"
curl -fsS "${URL}/health" && echo

cat <<EOF

次の手動作業（必要時）:
  - LINE Webhook URL を ${URL}/line/webhook に設定（API: PUT /v2/bot/channel/webhook/endpoint）
  - LINE 管理画面で「Webhookの利用」を ON、「応答メッセージ」を OFF
  - 既定催促ポリシー投入: curl -X POST ${URL}/admin/policies/seed -H "X-Admin-Token: <secret>"
  - リッチメニュー登録: LINE_CHANNEL_ACCESS_TOKEN=... python scripts/setup_rich_menu.py
EOF
