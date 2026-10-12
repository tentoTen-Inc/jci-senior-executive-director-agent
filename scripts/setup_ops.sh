#!/usr/bin/env bash
# 本番運用の保全設定（冪等。何度実行してもよい）
#   1. Firestore のバックアップ（7日間の時点復元・毎日のバックアップ4週分・削除保護）
#   2. 障害通知（Cloud Monitoring → Slack）
#   3. 月額予算のアラート（→ Slack）
#
# 前提（手動・1回だけ）: Cloud Monitoring に Slack の通知先を作っておく。
#   コンソール → Monitoring → アラート → 「通知チャンネルを編集」→ Slack →「新規追加」
#   → Slack にサインインして許可 → 通知先チャンネルを選ぶ（表示名は任意）
#   Slack の通知先が複数あるときは SLACK_CHANNEL_DISPLAY_NAME に表示名かチャンネル名を指定する
# 使い方: ./scripts/setup_ops.sh   （direnv 有効な状態で）
set -euo pipefail

PROJECT="${GCP_PROJECT_ID:-jci-sed-agent}"
REGION="${GCP_REGION:-asia-northeast1}"
CHANNEL_NAME="${SLACK_CHANNEL_DISPLAY_NAME:-jci-sed 障害通知}"
BUDGET="${MONTHLY_BUDGET:-5000}"  # 月額予算（請求先アカウントの通貨。日本円なら 5000 = 5,000円）

echo ">> project=${PROJECT}"
gcloud services enable monitoring.googleapis.com billingbudgets.googleapis.com \
  --project "${PROJECT}"

# --------------------------------------------------------------------------- #
# 1. Firestore の保全
# --------------------------------------------------------------------------- #
echo ">> Firestore: 時点復元（7日）と削除保護"
gcloud firestore databases update --database='(default)' --project "${PROJECT}" \
  --enable-pitr --delete-protection --quiet >/dev/null
if [[ -z "$(gcloud firestore backups schedules list --database='(default)' \
      --project "${PROJECT}" --format='value(name)' 2>/dev/null)" ]]; then
  echo ">> Firestore: 毎日のバックアップ（4週間保持）"
  gcloud firestore backups schedules create --database='(default)' --project "${PROJECT}" \
    --recurrence=daily --retention=4w >/dev/null
else
  echo "   既に存在: バックアップのスケジュール"
fi

# --------------------------------------------------------------------------- #
# 2. 障害通知（Slack）
# --------------------------------------------------------------------------- #
TOKEN="$(gcloud auth print-access-token)"
CHANNEL_FILE="$(mktemp)"
trap 'rm -f "${CHANNEL_FILE}"' EXIT
python3 - "${PROJECT}" "${CHANNEL_NAME}" "${TOKEN}" "${CHANNEL_FILE}" <<'PY'
import json, sys, urllib.request
project, channel_name, token, channel_file = sys.argv[1:5]
api = f"https://monitoring.googleapis.com/v3/projects/{project}"

def call(method, url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                 method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or b"{}")

channels = call("GET", f"{api}/notificationChannels").get("notificationChannels", [])
slack = [c for c in channels if c.get("type") == "slack" and c.get("enabled", True)]
wanted = channel_name.lstrip("#")
# 表示名・Slack のチャンネル名のどちらでも一致すればよい。Slack の通知先が1つだけならそれを使う
match = [c for c in slack if c.get("displayName") == channel_name
         or c.get("labels", {}).get("channel_name", "").lstrip("#") == wanted]
if not match and len(slack) == 1:
    match = slack
if not match:
    names = ", ".join(f"{c.get('displayName')}（{c.get('labels', {}).get('channel_name')}）"
                      for c in slack) or "なし"
    print(f"!! Slack の通知先「{channel_name}」が見つかりません（既存: {names}）。"
          "SLACK_CHANNEL_DISPLAY_NAME に表示名かチャンネル名を指定してください。")
    sys.exit(1)
channel = match[0]["name"]
print(f">> 通知先: {match[0].get('displayName')} → "
      f"{match[0].get('labels', {}).get('channel_name')}（{channel}）")
open(channel_file, "w").write(channel)

SERVICES = 'resource.labels.service_name=~"^jci-sed-(agent|admin)$"'
def log_policy(name, filt, doc):
    return {
        "displayName": name, "combiner": "OR", "enabled": True,
        "conditions": [{"displayName": name, "conditionMatchedLog": {"filter": filt}}],
        "alertStrategy": {"notificationRateLimit": {"period": "300s"}, "autoClose": "1800s"},
        "documentation": {"content": doc, "mimeType": "text/markdown"},
        "notificationChannels": [channel],
    }

def metric_policy(name, filt, threshold, duration, doc):
    return {
        "displayName": name, "combiner": "OR", "enabled": True,
        "conditions": [{"displayName": name, "conditionThreshold": {
            "filter": filt, "comparison": "COMPARISON_GT", "thresholdValue": threshold,
            "duration": duration,
            "aggregations": [{"alignmentPeriod": "60s", "perSeriesAligner": "ALIGN_MAX"}],
        }}],
        "alertStrategy": {"autoClose": "1800s"},
        "documentation": {"content": doc, "mimeType": "text/markdown"},
        "notificationChannels": [channel],
    }

policies = [
    log_policy(
        "jci-sed: アプリのエラー",
        f'resource.type="cloud_run_revision" AND {SERVICES} AND severity>=ERROR',
        "LINE ボット・管理画面でエラーが出ました。Cloud Logging で該当時刻のログを確認してください。"
        "（同じ種類の通知は5分に1回まで）",
    ),
    log_policy(
        "jci-sed: 定期実行（tick）の失敗",
        'resource.type="cloud_scheduler_job" AND resource.labels.job_id="jci-tick" AND severity>=ERROR',
        "毎時の定期実行（催促・カレンダー同期・データ取込の保守）が失敗しました。",
    ),
    metric_policy(
        "jci-sed: LINEデータ取込の失敗（DLQ）",
        'resource.type="pubsub_subscription" AND resource.labels.subscription_id="line-events-dlq-hold"'
        ' AND metric.type="pubsub.googleapis.com/subscription/num_undelivered_messages"',
        0, "60s",
        "LINE のメッセージ・ファイルの保存に5回失敗し、退避先（line-events-dlq）に入りました。"
        "原因を直したあと再処理が必要です。",
    ),
    metric_policy(
        "jci-sed: LINEデータ取込の滞留",
        'resource.type="pubsub_subscription"'
        ' AND resource.labels.subscription_id=one_of("line-events-to-bq","line-events-worker")'
        ' AND metric.type="pubsub.googleapis.com/subscription/oldest_unacked_message_age"',
        1800, "300s",
        "LINE のデータ取込が30分以上止まっています（BigQuery への保存かファイル取得のワーカー）。",
    ),
]

existing = {p["displayName"]: p["name"]
            for p in call("GET", f"{api}/alertPolicies").get("alertPolicies", [])}
for policy in policies:
    name = policy["displayName"]
    if name in existing:
        call("PATCH", f"https://monitoring.googleapis.com/v3/{existing[name]}", policy)
        print(f"   更新: {name}")
    else:
        call("POST", f"{api}/alertPolicies", policy)
        print(f"   作成: {name}")
PY

# --------------------------------------------------------------------------- #
# 3. 月額予算のアラート（50% / 90% / 100% で Slack に通知）
# --------------------------------------------------------------------------- #
CHANNEL="$(cat "${CHANNEL_FILE}")"
BILLING="$(gcloud billing projects describe "${PROJECT}" --format='value(billingAccountName)' \
  | sed 's#billingAccounts/##')"
if gcloud billing budgets list --billing-account="${BILLING}" \
     --format='value(displayName)' 2>/dev/null | grep -qx "jci-sed 月額予算"; then
  echo "   既に存在: 月額予算"
else
  echo ">> 月額予算 ${BUDGET}（請求先アカウントの通貨）"
  gcloud billing budgets create --billing-account="${BILLING}" \
    --display-name="jci-sed 月額予算" \
    --budget-amount="${BUDGET}" \
    --filter-projects="projects/${PROJECT}" \
    --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0 \
    --notifications-rule-monitoring-notification-channels="${CHANNEL}" >/dev/null
fi

cat <<EOF

完了しました。
- Firestore: 7日以内の任意の時点に復元可能・毎日のバックアップ（4週間）・削除保護
- 障害通知: アプリのエラー / 定期実行の失敗 / LINE データ取込の失敗・滞留 → Slack
- 月額予算: ${BUDGET}（50%・90%・100% で Slack に通知）
EOF
