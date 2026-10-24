#!/usr/bin/env bash
# 委員会資料の編集（docs/committee-docs-edit-design.md §7）の GCP 側セットアップ（冪等）。
#
#   Cloud Run (app-runtime) ──権限借用──► doc-editor ──► Docs API（04-委員会 の資料だけ）
#
# 前提: gcloud がオーナー相当で認証済み（direnv 有効な状態で実行）。
# 使い方: ./scripts/setup_doc_editor.sh <04-委員会のフォルダID>
# 実行後: ドライブで 04-委員会 フォルダを doc-editor@<project>.iam.gserviceaccount.com に「編集者」で共有する。
set -euo pipefail

FOLDER_ID="${1:?04-委員会 のフォルダIDを指定してください}"
PROJECT="${GCP_PROJECT_ID:-jci-sed-agent}"
REGION="${GCP_REGION:-asia-northeast1}"
SERVICE="${AGENT_SERVICE:-jci-sed-agent}"
EDITOR_NAME="doc-editor"
EDITOR_SA="${EDITOR_NAME}@${PROJECT}.iam.gserviceaccount.com"
RUNTIME_SA="app-runtime@${PROJECT}.iam.gserviceaccount.com"

echo ">> project=${PROJECT} editor=${EDITOR_SA}"

echo ">> API を有効化（Docs / Drive）"
gcloud services enable docs.googleapis.com drive.googleapis.com --project "${PROJECT}"

echo ">> 書き込み用のサービスアカウント"
if ! gcloud iam service-accounts describe "${EDITOR_SA}" --project "${PROJECT}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${EDITOR_NAME}" --project "${PROJECT}" \
    --display-name "委員会資料の編集（04-委員会 のみ共有）"
else
  echo "   既に存在: ${EDITOR_SA}"
fi

echo ">> app-runtime に権限借用（トークン作成者）を付与"
gcloud iam service-accounts add-iam-policy-binding "${EDITOR_SA}" --project "${PROJECT}" \
  --member "serviceAccount:${RUNTIME_SA}" --role roles/iam.serviceAccountTokenCreator \
  --condition=None --quiet >/dev/null

echo ">> Cloud Run に 04-委員会 のフォルダIDを設定"
gcloud run services update "${SERVICE}" --region "${REGION}" --project "${PROJECT}" \
  --update-env-vars "COMMITTEE_DOCS_FOLDER=${FOLDER_ID}" --quiet >/dev/null

cat <<EOF

完了しました。最後に、ドライブで次の共有をしてください（他のフォルダは共有しない）:
  フォルダ: https://drive.google.com/drive/folders/${FOLDER_ID}
  共有先:   ${EDITOR_SA}（編集者）
EOF
