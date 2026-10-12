#!/usr/bin/env bash
# 独自ドメイン（Cloud Run のドメインマッピング）の設定（冪等。何度実行してもよい）
#   admin.647jc.org → jci-sed-admin（管理画面・IAP）
#   line.647jc.org  → jci-sed-agent（LINE の受信・AI 回答の資料リンク）
#
# 前提（手動・1回だけ）:
#   1. Xserver の DNS に CNAME を追加済み: admin / line → ghs.googlehosted.com.（設定済み）
#   2. gcloud のアカウント（admin@10to10.co.jp）で 647jc.org の所有確認が済んでいること
#      → `gcloud domains verify 647jc.org` で Search Console が開く。表示された TXT レコードを
#        Xserver の DNS（ホスト名は空欄、種別 TXT）に追加して「確認」を押す。
# 使い方: ./scripts/setup_domains.sh   （direnv 有効な状態で）
set -euo pipefail

PROJECT="${GCP_PROJECT_ID:-jci-sed-agent}"
REGION="${GCP_REGION:-asia-northeast1}"
ROOT_DOMAIN="${ROOT_DOMAIN:-647jc.org}"
ADMIN_DOMAIN="admin.${ROOT_DOMAIN}"
LINE_DOMAIN="line.${ROOT_DOMAIN}"

if ! gcloud domains list-user-verified --format='value(id)' | grep -qx "${ROOT_DOMAIN}"; then
  cat >&2 <<EOF
!! ${ROOT_DOMAIN} の所有確認が済んでいません。次を実行して Search Console で確認してください:
   gcloud domains verify ${ROOT_DOMAIN}
EOF
  exit 1
fi

map() {
  local domain="$1" service="$2"
  if gcloud beta run domain-mappings describe --domain "${domain}" \
       --region "${REGION}" --project "${PROJECT}" >/dev/null 2>&1; then
    echo "   既に存在: ${domain} → ${service}"
  else
    echo ">> ${domain} → ${service}"
    gcloud beta run domain-mappings create --service "${service}" --domain "${domain}" \
      --region "${REGION}" --project "${PROJECT}" >/dev/null
  fi
}
map "${ADMIN_DOMAIN}" jci-sed-admin
map "${LINE_DOMAIN}" jci-sed-agent

ready() {
  gcloud beta run domain-mappings describe --domain "$1" --region "${REGION}" \
    --project "${PROJECT}" \
    --format="value(status.conditions.filter(type:Ready).extract(status).flatten())" \
    | grep -qx True
}

echo ">> 証明書の状態（発行まで 15分〜最大24時間。未発行ならあとでもう一度実行）"
for domain in "${ADMIN_DOMAIN}" "${LINE_DOMAIN}"; do
  if ready "${domain}"; then echo "   ${domain}: 準備完了"; else echo "   ${domain}: 証明書の発行待ち"; fi
done

# 証明書が「発行済み」になっても、Google の配信側が新しい証明書を使い始めるまで
# さらに時間がかかり、その間 HTTPS は接続できない（2026-10-12 本番で発生）。
# 実際に https で応答することを確かめてから資料リンクを切り替える。
https_ok() {
  [[ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://$1/health")" == "200" ]]
}

if ready "${LINE_DOMAIN}" && https_ok "${LINE_DOMAIN}"; then
  echo ">> 資料リンクを https://${LINE_DOMAIN} で発行する（既存の環境変数は保持）"
  gcloud run services update jci-sed-agent --region "${REGION}" --project "${PROJECT}" \
    --update-env-vars "PUBLIC_BASE_URL=https://${LINE_DOMAIN}" --quiet >/dev/null
else
  echo "   https://${LINE_DOMAIN} がまだ応答しないため、資料リンクは切り替えません。"
  echo "   しばらくしてからこのスクリプトを再実行してください。"
fi

cat <<EOF

ドメインマッピングを作成しました。証明書が発行されたら（上で「準備完了」）:
  1. 管理画面を開けるか確認: https://${ADMIN_DOMAIN}/app/
  2. LINE Developers コンソール → Messaging API 設定 → Webhook URL を
     https://${LINE_DOMAIN}/line/webhook に変更し「検証」を押す（成功を確認してから保存）
  ※ 証明書が出る前に Webhook を切り替えると LINE からの受信が止まります。
  ※ 旧URL（*.run.app）も引き続き使えるので、切り替え前に問題は起きません。
EOF
