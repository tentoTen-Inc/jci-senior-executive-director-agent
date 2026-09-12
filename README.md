# JCI 専務理事AIエージェント

青年会議所（一般社団法人 猪苗代青年会議所）の専務理事業務をAIエージェント化するプロジェクト。
LINE Messaging API でメンバーとやり取りし、GCP（Cloud Run / Firestore / Vertex AI Gemini）で動作する。

MVP は **出欠自動化**（出欠確認・自動催促・集計）。詳細は `docs/` を参照。

- 要件定義: [docs/requirements.md](docs/requirements.md)
- Drive資料分析: [docs/drive-analysis.md](docs/drive-analysis.md)
- MVP詳細設計: [docs/mvp-design.md](docs/mvp-design.md)
- **go-live Runbook**: [docs/runbook.md](docs/runbook.md)（デプロイ/緊急停止/ロールバック/運用）

## 構成

```
app/
  main.py          # FastAPI: LINE Webhook + ルーティング
  admin_api.py     # 管理API(イベント/出欠/設定/名簿/催促/サマリ)
  models.py        # ドメインモデル(pydantic)
  repository.py    # Repositoryプロトコル + InMemory実装
  firestore_repo.py# Firestore実装
  deps.py          # リポジトリ依存解決
  invite.py        # 招待コード発行・本人確認
  events.py        # 対象者解決
  attendance.py    # 出欠記録・集計
  line_messages.py # LINEメッセージ/ postback
  reminders.py     # 催促ポリシー・スケジューラ
  guardrails.py    # 静音時間/レート/サニティ/キルスイッチ
  delivery.py      # 配信実行+監査ログ
  summary.py       # 集計サマリ・五役通知
  config.py        # 環境/シークレット解決
scripts/import_roster.py  # 会員名簿xlsxの正規化インポート
tests/             # pytest
```

## ローカル開発

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# 検証
.venv/bin/ruff check .
.venv/bin/pytest

# ローカル起動（要 LINE_CHANNEL_SECRET 等; .env 参照）
.venv/bin/uvicorn app.main:app --reload
```

環境変数は `.env.example` を参照（実値は `.env`、コミット禁止）。

## 名簿インポート

```bash
# プレビュー（投入しない・PIIマスク）
.venv/bin/python scripts/import_roster.py path/to/会員名簿.xlsx --sheet 2026会員名簿
# Firestore へ投入
.venv/bin/python scripts/import_roster.py path/to/会員名簿.xlsx --sheet 2026会員名簿 --upsert
```

## デプロイ（手動・概要）

> 本リポジトリでは自動デプロイは行わない。以下は手順の概要。

1. コンテナビルド & Cloud Run デプロイ
   ```bash
   gcloud run deploy jci-sed-agent \
     --source . --region asia-northeast1 \
     --service-account app-runtime@jci-sed-agent.iam.gserviceaccount.com \
     --set-secrets LINE_CHANNEL_SECRET=line-channel-secret:latest,LINE_CHANNEL_ACCESS_TOKEN=line-channel-access-token:latest \
     --set-env-vars GCP_PROJECT_ID=jci-sed-agent,TZ=Asia/Tokyo
   ```
2. LINE Developers コンソールで Webhook URL を `https://<cloud-run-url>/line/webhook` に設定
3. **チャネルアクセストークン**を発行し Secret Manager `line-channel-access-token` を更新
4. Cloud Scheduler で tick エンドポイントを定期起動（催促のスケジューリング）
5. 管理画面/API は IAP で役員アカウントのみ許可

## グループトークでの挙動（F2-5）

- 個別トーク：従来どおり。未連携ユーザーの発言は招待コードとして照合する。
- グループ／複数人トーク：
  - ボット（猪苗代専務AI）宛のメンションがある発言にのみ応答する。それ以外は無反応。
  - 招待コードの照合は行わない（未連携ユーザーには個別トークでの登録を案内する）。
- LINE Developers の Messaging API 設定で「グループ・複数人トークへの参加を許可する」を有効にすると、
  グループ内のメッセージが Webhook に届く（メンション情報 `mention.mentionees[].isSelf` で判定）。

## テスト方針

- LINE/Firestore 等の外部依存はモック/インメモリで分離。CI(GitHub Actions)で ruff + pytest を実行。
- ライブ結合（実 LINE 送信・Firestore 接続）はデプロイ環境で別途確認。
