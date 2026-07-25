# go-live Runbook — JCI 専務理事エージェント（出欠自動化MVP）

最終更新: 2026-06-21
対象: 一般社団法人 猪苗代青年会議所 / GCP `jci-sed-agent`

本番URL: `https://jci-sed-agent-52910381566.asia-northeast1.run.app`

---

## 1. システム構成（稼働中）

| 要素 | 値 |
|---|---|
| Cloud Run | `jci-sed-agent`（asia-northeast1）/ 実行SA `app-runtime@…` |
| データ | Firestore (Native) |
| 定期実行 | Cloud Scheduler `jci-tick`（毎時8–21時JST）→ `POST /tasks/tick` |
| シークレット | Secret Manager: `line-channel-secret` / `line-channel-access-token` / `admin-api-secret` |
| LINE | 公式アカウント（Channel ID 2010454434）。Webhook 登録済み |
| 管理 | SPA `/app`（IAP・管理サービス `jci-sed-admin`）＋ `/api/*`・`/admin/*`（IAP または `X-Admin-Token`） |

公開: `/`, `/health`, `/line/webhook`（署名検証）。保護: `/admin/*`, `/api/*`, `/tasks/*`。

> 旧・最小HTML `/dashboard` は廃止し、`/app/` へ 308 リダイレクトのみ残置。

---

## 2. go-live チェックリスト

- [x] `/health` が 200(`curl <URL>/health`)— 2026-07-09 確認済み(200)
- [x] LINE「Webhookの利用」が **ON**、「応答メッセージ」が **OFF**(自動応答競合回避)— `GET /v2/bot/info` の `chatMode: "bot"` で確認済み(bot モード = 応答メッセージ機能は使われずWebhookのみで処理)
- [x] Webhook 検証成功(LINE: `POST /v2/bot/channel/webhook/test`)— 2026-07-09 `success:true, statusCode:200` 確認済み
- [x] 既定催促ポリシー投入済み(`POST /admin/policies/seed`)— `GET /admin/policies` で `rp_例会_default` / `rp_理事会_default` 既存確認済み(投入済み・再実行不要)
- [x] リッチメニュー登録済み(`scripts/setup_rich_menu.py`)— `richmenu-d3da97f7f72fd22e4b45c5b6e9934f7c`(名前: jci-default)が全ユーザーのデフォルトに設定済みであることを `GET /v2/bot/richmenu/list` と `GET /v2/bot/user/all/richmenu` で確認済み
- [ ] **実会員名簿を Firestore へ投入**(`scripts/import_roster.py … --upsert`)※実PII — 実ファイル(会員名簿.xlsx)が未提供のため未実施。ご本人にてローカル実行が必要
- [ ] 各会員へ**招待コード発行・配布**(`POST /admin/members/{id}/invite`)— 名簿投入後、対象会員IDが確定してから実施
- [x] 静音時間・レート上限の最終確認(`GET /admin/settings`)— 2026-07-09 確認済み: `kill_switch:false`, `quiet_hours 21:00-08:00 JST`, `rate_limit 3/日/人, 30/分(全体)`
- [ ] テスト会員で 友だち追加→コード→出欠回答→集計 を実機確認 — 物理デバイス操作が必要なためご本人実施
- [ ] テスト用データ(test-endo 等)を削除 — 現状 Firestore に `test-endo`(LINE連携済み)が残存。実機確認完了後に削除推奨

---

## 3. 主要オペレーション

### デプロイ
```bash
./scripts/deploy.sh
```

### 既定催促ポリシー投入
```bash
curl -X POST <URL>/admin/policies/seed -H "X-Admin-Token: <secret>"
```

### 名簿投入（実PII・要注意）
```bash
# プレビュー（投入しない）
python scripts/import_roster.py 会員名簿.xlsx --sheet 2026会員名簿
# 投入
python scripts/import_roster.py 会員名簿.xlsx --sheet 2026会員名簿 --upsert
```

### 招待コード発行
```bash
curl -X POST <URL>/admin/members/<member_id>/invite -H "X-Admin-Token: <secret>"
```

### イベント登録（例会）
```bash
curl -X POST <URL>/admin/events -H "X-Admin-Token: <secret>" -H "Content-Type: application/json" -d '{
  "type":"例会","title":"7月例会","datetime_start":"2026-07-XXT19:00:00",
  "location":"会館","attendance_deadline":"2026-07-XXT23:59:00",
  "target_scope":{"kind":"all","value":[]},"reminder_policy_id":"rp_例会_default","status":"open"}'
```

### 集計確認・クローズ（五役へサマリ通知）
```bash
curl <URL>/admin/events/<event_id>/summary -H "X-Admin-Token: <secret>"
curl -X POST <URL>/admin/events/<event_id>/close -H "X-Admin-Token: <secret>"
```

---

## 4. 緊急停止（キルスイッチ）

全自動配信を即時停止する。

```bash
# 現在値
curl <URL>/admin/settings -H "X-Admin-Token: <secret>"
# 停止（kill_switch=true）
curl -X PUT <URL>/admin/settings -H "X-Admin-Token: <secret>" -H "Content-Type: application/json" \
  -d '{"kill_switch":true,"quiet_hours":{"start":"21:00","end":"08:00","tz":"Asia/Tokyo"},"rate_limit":{"per_member_per_day":3,"global_per_min":30}}'
```
管理SPA `/app/settings` の「キルスイッチ」でも可。

> ON の間、tick もクローズ通知も送信されない（ブロックされた対象は OFF 後の次 tick で再送される）。

---

## 5. ロールバック

```bash
# 直近のリビジョン一覧
gcloud run revisions list --service jci-sed-agent --region asia-northeast1
# 1つ前へ全トラフィックを戻す
gcloud run services update-traffic jci-sed-agent --region asia-northeast1 --to-revisions <REVISION>=100
```
コードは `main` の squash 履歴で `git revert` 可能（PR単位）。

---

## 6. 監視・調査

```bash
# 直近ログ
gcloud run services logs read jci-sed-agent --region asia-northeast1 --limit 100
# tick の手動実行
gcloud scheduler jobs run jci-tick --location asia-northeast1
# 配信ログ・エスカレーションは Firestore: deliveryLogs / escalations
```

---

## 7. 既知事項・制約

- `/healthz` は Google Front End に横取りされ Google 404 を返す → 監視は `/health` を使う。
- `/admin` 認証は共有トークン（`X-Admin-Token`）。恒久的には **IAP** 導入を推奨。
- 静音時間中のブロックは「次 tick で再送」方式（Cloud Tasks の遅延配信は未使用）。
- 対外アンケート（非会員）は Google フォーム維持（F7、後続フェーズ）。
- 組織ポリシー（DRS）は当プロジェクトで `allowAll` に上書き済み（公開ingress用）。
