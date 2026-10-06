#!/usr/bin/env bash
# LINEメッセージ・データレイクの GCP 側セットアップ（冪等。何度実行してもよい）。
# 設計: docs/datalake-design.md §2 / §5 / §9
#
#   LINE → Webhook(jci-sed-agent) → Pub/Sub(line-events)
#            ├ BigQuery サブスクリプション → line_lake.events_raw（コード不要）
#            └ （P6-2）Push サブスクリプション → ワーカー → Cloud Storage
#
# 前提: gcloud / bq がプロジェクトのオーナー相当で認証済み。
#       案件ごとの認証分離のため、このリポジトリ直下で direnv 有効な状態で実行すること。
# 使い方: ./scripts/setup_datalake.sh
set -euo pipefail

PROJECT="${GCP_PROJECT_ID:-jci-sed-agent}"
REGION="${GCP_REGION:-asia-northeast1}"
DATASET="${LAKE_DATASET:-line_lake}"
TOPIC="${LINE_EVENTS_TOPIC_NAME:-line-events}"
DLQ="${TOPIC}-dlq"
BQ_SUB="${TOPIC}-to-bq"
RETENTION_DAYS="${LAKE_RETENTION_DAYS:-1825}"  # 生イベントの保存期間（既定5年）
RUNTIME_SA="app-runtime@${PROJECT}.iam.gserviceaccount.com"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
PUBSUB_AGENT="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"

echo ">> project=${PROJECT} dataset=${DATASET} topic=${TOPIC} retention=${RETENTION_DAYS}d"

echo ">> API を有効化"
gcloud services enable pubsub.googleapis.com bigquery.googleapis.com \
  bigquerystorage.googleapis.com --project "${PROJECT}"

# --------------------------------------------------------------------------- #
# Pub/Sub トピック（7日保持＝障害時のリプレイ用）
# --------------------------------------------------------------------------- #
for t in "${TOPIC}" "${DLQ}"; do
  if ! gcloud pubsub topics describe "${t}" --project "${PROJECT}" >/dev/null 2>&1; then
    gcloud pubsub topics create "${t}" --message-retention-duration=7d --project "${PROJECT}"
  else
    echo "   既に存在: topic ${t}"
  fi
done
# DLQ に落ちたメッセージを7日間保持して調査できるようにする
if ! gcloud pubsub subscriptions describe "${DLQ}-hold" --project "${PROJECT}" >/dev/null 2>&1; then
  gcloud pubsub subscriptions create "${DLQ}-hold" --topic "${DLQ}" \
    --message-retention-duration=7d --expiration-period=never --project "${PROJECT}"
fi

# --------------------------------------------------------------------------- #
# BigQuery: データセット・生テーブル・ビュー
# --------------------------------------------------------------------------- #
if ! bq --project_id="${PROJECT}" show --dataset "${PROJECT}:${DATASET}" >/dev/null 2>&1; then
  bq --project_id="${PROJECT}" --location="${REGION}" mk --dataset \
    --description "LINE メッセージのデータレイク（docs/datalake-design.md）" \
    "${PROJECT}:${DATASET}"
else
  echo "   既に存在: dataset ${DATASET}"
fi

if ! bq --project_id="${PROJECT}" show "${PROJECT}:${DATASET}.events_raw" >/dev/null 2>&1; then
  bq --project_id="${PROJECT}" --location="${REGION}" mk --table \
    --schema "${ROOT}/infra/bigquery/events_raw.json" \
    --time_partitioning_field publish_time \
    --time_partitioning_type DAY \
    --time_partitioning_expiration "$((RETENTION_DAYS * 86400))" \
    --description "全 LINE イベントの生 JSON（Pub/Sub BigQuery サブスクリプションが書き込む）" \
    "${PROJECT}:${DATASET}.events_raw"
else
  echo "   既に存在: table events_raw"
fi

echo ">> ビューを作成・更新"
for f in "${ROOT}"/infra/bigquery/views/*.sql; do
  sql="$(sed -e "s/\${PROJECT}/${PROJECT}/g" -e "s/\${DATASET}/${DATASET}/g" "${f}")"
  bq --project_id="${PROJECT}" --location="${REGION}" query --use_legacy_sql=false \
    --quiet "${sql}" >/dev/null
  echo "   $(basename "${f}")"
done

# --------------------------------------------------------------------------- #
# 権限
# --------------------------------------------------------------------------- #
echo ">> 権限付与"
# アプリ（Webhook）がトピックへ publish できる
gcloud pubsub topics add-iam-policy-binding "${TOPIC}" \
  --member "serviceAccount:${RUNTIME_SA}" --role roles/pubsub.publisher \
  --project "${PROJECT}" >/dev/null
# Pub/Sub が生テーブルへ書き込める（BigQuery サブスクリプション）。テーブル単位の最小権限
bq --project_id="${PROJECT}" add-iam-policy-binding \
  --member="serviceAccount:${PUBSUB_AGENT}" --role=roles/bigquery.dataEditor \
  "${PROJECT}:${DATASET}.events_raw" >/dev/null
# Pub/Sub が DLQ へ退避できる
gcloud pubsub topics add-iam-policy-binding "${DLQ}" \
  --member "serviceAccount:${PUBSUB_AGENT}" --role roles/pubsub.publisher \
  --project "${PROJECT}" >/dev/null

# --------------------------------------------------------------------------- #
# BigQuery サブスクリプション（トピック → events_raw を直結）
# --------------------------------------------------------------------------- #
if ! gcloud pubsub subscriptions describe "${BQ_SUB}" --project "${PROJECT}" >/dev/null 2>&1; then
  gcloud pubsub subscriptions create "${BQ_SUB}" \
    --topic "${TOPIC}" \
    --bigquery-table "${PROJECT}:${DATASET}.events_raw" \
    --write-metadata \
    --dead-letter-topic "${DLQ}" \
    --max-delivery-attempts 5 \
    --expiration-period=never \
    --project "${PROJECT}"
else
  echo "   既に存在: subscription ${BQ_SUB}"
fi
gcloud pubsub subscriptions add-iam-policy-binding "${BQ_SUB}" \
  --member "serviceAccount:${PUBSUB_AGENT}" --role roles/pubsub.subscriber \
  --project "${PROJECT}" >/dev/null

cat <<EOF

データレイク（取込部分）の準備が完了しました。

次に Webhook からの取込を有効化します（既存の設定は保持されます）:

  gcloud run services update jci-sed-agent --region ${REGION} --project ${PROJECT} \\
    --update-env-vars LINE_EVENTS_TOPIC=${TOPIC}

確認（数分後）:
  bq query --use_legacy_sql=false \\
    'SELECT sent_at, source_type, message_type, text FROM \`${PROJECT}.${DATASET}.v_messages\` ORDER BY sent_at DESC LIMIT 20'

※ 取込開始前に、各グループへ周知文（docs/datalake-design.md §7）を投稿してください。
EOF
