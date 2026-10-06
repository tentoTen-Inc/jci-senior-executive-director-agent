#!/usr/bin/env bash
# Googleカレンダー連携（F3-2）の GCP 側セットアップ（1回だけ実行すればよい）。
# 設計: docs/calendar-design.md §2 / §7
#
# 方式: Drive と同じ鍵レス impersonation。
#   app-runtime SA ──impersonate──► calendar-sync SA ──► inawashiro.jc@gmail.com のカレンダー
#   calendar-sync にはプロジェクトの IAM ロールを付けない（権限はカレンダーの共有設定だけ）。
#
# 前提: gcloud がプロジェクトのオーナー相当で認証済み。
#       案件ごとの認証分離のため、このリポジトリ直下で direnv 有効な状態で実行すること。
# 使い方: ./scripts/setup_calendar_sync.sh
set -euo pipefail

PROJECT="${GCP_PROJECT_ID:-jci-sed-agent}"
REGION="${GCP_REGION:-asia-northeast1}"
CALENDAR_ID="${GCAL_CALENDAR_ID:-inawashiro.jc@gmail.com}"
RUNTIME_SA="app-runtime@${PROJECT}.iam.gserviceaccount.com"
SYNC_SA_ID="calendar-sync"
SYNC_SA="${SYNC_SA_ID}@${PROJECT}.iam.gserviceaccount.com"

echo ">> project=${PROJECT} calendar=${CALENDAR_ID}"

echo ">> Calendar API を有効化"
gcloud services enable calendar-json.googleapis.com iamcredentials.googleapis.com \
  --project "${PROJECT}"

echo ">> カレンダー同期用サービスアカウント"
if ! gcloud iam service-accounts describe "${SYNC_SA}" --project "${PROJECT}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${SYNC_SA_ID}" \
    --display-name "Google Calendar sync (impersonation target)" \
    --project "${PROJECT}"
else
  echo "   既に存在: ${SYNC_SA}"
fi

echo ">> app-runtime が calendar-sync を権限借用できるようにする"
gcloud iam service-accounts add-iam-policy-binding "${SYNC_SA}" \
  --member "serviceAccount:${RUNTIME_SA}" \
  --role roles/iam.serviceAccountTokenCreator \
  --project "${PROJECT}" \
  --quiet >/dev/null

cat <<EOF

GCP 側の準備は完了しました。残りは次の2つです。

1) カレンダーの共有（手動・1回だけ）
   ${CALENDAR_ID} で Google カレンダー（https://calendar.google.com）を開き、
   設定 → 対象のカレンダー → 「特定のユーザーまたはグループと共有する」で
     ${SYNC_SA}
   を「予定の変更」権限で追加してください。

2) 連携を有効化（共有が終わってから）
   両サービスに環境変数 GCAL_CALENDAR_ID を設定します（既存の設定は保持されます）。

   gcloud run services update jci-sed-agent --region ${REGION} --project ${PROJECT} \\
     --update-env-vars GCAL_CALENDAR_ID=${CALENDAR_ID}
   gcloud run services update jci-sed-admin --region ${REGION} --project ${PROJECT} \\
     --update-env-vars GCAL_CALENDAR_ID=${CALENDAR_ID}

   有効化後、管理画面「出欠管理」の「まとめて反映」で、これから開催のイベントを
   カレンダーに一括登録できます（毎時の tick でも自動で反映されます）。
EOF
