#!/usr/bin/env bash
# データレイクの AI 活用（P6-4a）の GCP 側セットアップ（冪等。何度実行してもよい）。
# 設計: docs/lake-ai-design.md §2 / §6 / §8
#
#   BigQuery ──(接続 vertex-ai)──► Vertex AI gemini-embedding-001
#   line_lake.embedding_model（リモートモデル）でベクトル化し、line_lake.knowledge_chunks を VECTOR_SEARCH
#
# 前提: ./scripts/setup_datalake.sh 済み。gcloud / bq がオーナー相当で認証済み（direnv 有効な状態で実行）。
# 使い方: ./scripts/setup_lake_ai.sh
set -euo pipefail

PROJECT="${GCP_PROJECT_ID:-jci-sed-agent}"
REGION="${GCP_REGION:-asia-northeast1}"
DATASET="${LAKE_DATASET:-line_lake}"
CONNECTION="${LAKE_AI_CONNECTION:-vertex-ai}"
MODEL="${LAKE_EMBEDDING_MODEL_NAME:-embedding_model}"
ENDPOINT="${LAKE_EMBEDDING_ENDPOINT:-gemini-embedding-001}"
SERVICE="${AGENT_SERVICE:-jci-sed-agent}"
RUNTIME_SA="app-runtime@${PROJECT}.iam.gserviceaccount.com"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo ">> project=${PROJECT} dataset=${DATASET} connection=${CONNECTION} endpoint=${ENDPOINT}"

echo ">> API を有効化"
gcloud services enable aiplatform.googleapis.com bigqueryconnection.googleapis.com \
  --project "${PROJECT}"

# --------------------------------------------------------------------------- #
# BigQuery → Vertex AI の接続
# --------------------------------------------------------------------------- #
CONN_REF="${PROJECT}.${REGION}.${CONNECTION}"
if ! bq --project_id="${PROJECT}" show --connection "${CONN_REF}" >/dev/null 2>&1; then
  bq --project_id="${PROJECT}" mk --connection --location="${REGION}" \
    --connection_type=CLOUD_RESOURCE "${CONNECTION}"
else
  echo "   既に存在: connection ${CONNECTION}"
fi
CONN_SA="$(bq --project_id="${PROJECT}" show --format=json --connection "${CONN_REF}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["cloudResource"]["serviceAccountId"])')"
echo "   接続のサービスアカウント: ${CONN_SA}"
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member "serviceAccount:${CONN_SA}" --role roles/aiplatform.user \
  --condition=None --quiet >/dev/null

# --------------------------------------------------------------------------- #
# リモートモデル（権限の反映待ちで最初は失敗することがあるので再試行）
# --------------------------------------------------------------------------- #
echo ">> リモートモデル ${DATASET}.${MODEL}（${ENDPOINT}）"
MODEL_SQL="CREATE MODEL IF NOT EXISTS \`${PROJECT}.${DATASET}.${MODEL}\`
REMOTE WITH CONNECTION \`${CONN_REF}\`
OPTIONS (ENDPOINT = '${ENDPOINT}')"
for attempt in 1 2 3 4 5 6; do
  if echo "${MODEL_SQL}" | bq --project_id="${PROJECT}" --location="${REGION}" \
      query --use_legacy_sql=false --quiet >/dev/null 2>"${TMPDIR:-/tmp}/lake_ai_model.err"; then
    echo "   作成済み"
    break
  fi
  if [[ "${attempt}" == 6 ]]; then
    cat "${TMPDIR:-/tmp}/lake_ai_model.err" >&2
    echo "リモートモデルを作成できませんでした（上のエラーを確認してください）。" >&2
    exit 1
  fi
  echo "   権限の反映待ち（${attempt}/5）… 20秒後に再試行"
  sleep 20
done

# --------------------------------------------------------------------------- #
# ナレッジ索引のテーブル
# --------------------------------------------------------------------------- #
if ! bq --project_id="${PROJECT}" show "${PROJECT}:${DATASET}.knowledge_chunks" >/dev/null 2>&1; then
  bq --project_id="${PROJECT}" --location="${REGION}" mk --table \
    --schema "${ROOT}/infra/bigquery/knowledge_chunks.json" \
    --clustering_fields scope_type,scope_id \
    --description "LINE の資料・やり取りのナレッジ索引（docs/lake-ai-design.md §3）" \
    "${PROJECT}:${DATASET}.knowledge_chunks"
else
  echo "   既に存在: table knowledge_chunks"
fi

# --------------------------------------------------------------------------- #
# アプリの権限: 接続の利用 + データセットの編集（索引の追加・削除、モデルの利用）
# --------------------------------------------------------------------------- #
echo ">> 権限付与"
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member "serviceAccount:${RUNTIME_SA}" --role roles/bigquery.connectionUser \
  --condition=None --quiet >/dev/null
ACCESS="$(mktemp)"
trap 'rm -f "${ACCESS}"' EXIT
bq --project_id="${PROJECT}" show --format=prettyjson "${PROJECT}:${DATASET}" > "${ACCESS}"
python3 - "${ACCESS}" "${RUNTIME_SA}" <<'PY'
import json, sys
path, sa = sys.argv[1], sys.argv[2]
ds = json.load(open(path))
access = ds.get("access", [])
if not any(a.get("userByEmail") == sa and a.get("role") == "WRITER" for a in access):
    access.append({"role": "WRITER", "userByEmail": sa})
json.dump({"access": access}, open(path, "w"))
PY
bq --project_id="${PROJECT}" update --source "${ACCESS}" "${PROJECT}:${DATASET}" >/dev/null

echo ">> ${SERVICE} で索引と検索を有効化（既存の環境変数は保持）"
gcloud run services update "${SERVICE}" --region "${REGION}" --project "${PROJECT}" \
  --update-env-vars "LAKE_EMBEDDING_MODEL=${MODEL}" --quiet >/dev/null

cat <<EOF

AI 活用（P6-4a）の準備が完了しました。
- 索引は毎時の tick で自動実行されます（PDF本文・20字以上のテキスト）。
- 動作確認: 個別トークで PDF を送ったあと、その内容について自由文で質問してください。
EOF
