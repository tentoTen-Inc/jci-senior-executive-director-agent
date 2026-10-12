#!/usr/bin/env bash
# LINE への返信を Webhook の外で作る設定（冪等。何度実行してもよい）
#
#   LINE → Webhook（すぐ 200）→ Pub/Sub(line-events)
#            └ Push サブスクリプション line-events-reply → /pubsub/line-reply → 返信
#
# AI の回答を待ってから 200 を返すと LINE の待ち時間を超えるため（2026-10-12 本番で
# Webhook の検証がタイムアウト）。同じイベントの二重処理は Firestore の lineEventClaims で防ぐ。
#
# 前提: setup_datalake.sh 済み（トピック・Push 用 SA・ワーカー設定）、かつ /pubsub/line-reply を
#       含むコードがデプロイ済み（main へのマージ後に実行する）。
# 使い方: ./scripts/setup_line_reply.sh   （direnv 有効な状態で）
#         元に戻す: gcloud run services update jci-sed-agent --remove-env-vars LINE_ASYNC_REPLY
set -euo pipefail

PROJECT="${GCP_PROJECT_ID:-jci-sed-agent}"
REGION="${GCP_REGION:-asia-northeast1}"
TOPIC="${LINE_EVENTS_TOPIC_NAME:-line-events}"
DLQ="${TOPIC}-dlq"
REPLY_SUB="${TOPIC}-reply"
SERVICE="${AGENT_SERVICE:-jci-sed-agent}"
PUSH_SA="pubsub-push@${PROJECT}.iam.gserviceaccount.com"
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
PUBSUB_AGENT="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"

AGENT_URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" \
  --project "${PROJECT}" --format='value(status.url)')"
# OIDC の audience はワーカーと共通（アプリは PUBSUB_PUSH_AUDIENCE の1つだけを検証する）
AUDIENCE="$(gcloud run services describe "${SERVICE}" --region "${REGION}" --project "${PROJECT}" \
  --format=json | python3 -c 'import json,sys
for e in json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0].get("env", []):
    if e.get("name") == "PUBSUB_PUSH_AUDIENCE": print(e.get("value", ""))')"
if [[ -z "${AUDIENCE}" ]]; then
  echo "!! ${SERVICE} に PUBSUB_PUSH_AUDIENCE がありません。先に setup_datalake.sh を実行してください。" >&2
  exit 1
fi

# 新しいコードがデプロイ済みか（未デプロイだと 404 で DLQ に溜まる）
code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "${AGENT_URL}/pubsub/line-reply")"
if [[ "${code}" == "404" ]]; then
  echo "!! /pubsub/line-reply がまだデプロイされていません。main へのマージ後に実行してください。" >&2
  exit 1
fi

echo ">> Firestore: 処理済みイベントの記録（lineEventClaims）を expire_at で自動削除"
gcloud firestore fields ttls update expire_at --collection-group=lineEventClaims \
  --enable-ttl --project "${PROJECT}" --quiet >/dev/null

if ! gcloud pubsub subscriptions describe "${REPLY_SUB}" --project "${PROJECT}" >/dev/null 2>&1; then
  echo ">> Push サブスクリプション ${REPLY_SUB} → ${AGENT_URL}/pubsub/line-reply"
  gcloud pubsub subscriptions create "${REPLY_SUB}" \
    --topic "${TOPIC}" \
    --message-filter 'attributes.kind = "webhook"' \
    --push-endpoint "${AGENT_URL}/pubsub/line-reply" \
    --push-auth-service-account "${PUSH_SA}" \
    --push-auth-token-audience "${AUDIENCE}" \
    --ack-deadline 300 \
    --min-retry-delay 10s --max-retry-delay 60s \
    --dead-letter-topic "${DLQ}" --max-delivery-attempts 5 \
    --expiration-period=never \
    --project "${PROJECT}" >/dev/null
else
  echo "   既に存在: subscription ${REPLY_SUB}"
fi
gcloud pubsub subscriptions add-iam-policy-binding "${REPLY_SUB}" \
  --member "serviceAccount:${PUBSUB_AGENT}" --role roles/pubsub.subscriber \
  --project "${PROJECT}" >/dev/null

# サブスクリプションができてから有効にする（それまでは Webhook の中で返信している）
echo ">> ${SERVICE}: 返信を Pub/Sub 経由に切り替え（既存の環境変数は保持）"
gcloud run services update "${SERVICE}" --region "${REGION}" --project "${PROJECT}" \
  --update-env-vars LINE_ASYNC_REPLY=1 --quiet >/dev/null

cat <<MSG

完了しました。LINE にはすぐ 200 を返し、返信は ${REPLY_SUB} 経由で作ります。
元に戻す: gcloud run services update ${SERVICE} --region ${REGION} --project ${PROJECT} --remove-env-vars LINE_ASYNC_REPLY
MSG
