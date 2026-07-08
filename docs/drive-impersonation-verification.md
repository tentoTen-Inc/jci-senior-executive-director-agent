# Drive サービスアカウント Impersonation 検証手順・期待値（ドラフト）

最終更新: 2026-07-08
対象タスク: t_50e91c51 [gtb-jci-senior-executive-director-agent] Drive サービスアカウントimpersonation検証

## 状況

このドキュメントは実 GCP/Drive での検証を行うための **手順書と期待値定義**。
このワークツリー（ヘッドレスなコーディングエージェント環境）から `gcloud` を実行したところ、
キャッシュ済みの3アカウント（`admin@10to10.co.jp` / `t.endo@10to10.co.jp` /
`takayuki.endo@education-beyond.org`）すべてが `Reauthentication failed: cannot prompt during
non-interactive execution` でブロックされ、`gcloud auth login --no-launch-browser` も
ブラウザでの認証コード入力が必要なため完遂できなかった。ADC (`gcloud auth application-default
print-access-token` の元になる `application_default_credentials.json`) は
`educationbeyond`（`gtb-jci-senior-executive-director-agent` とは無関係の別プロジェクト）に
紐づいており使えない。

→ 実 IAM 確認・実 Drive フォルダスキャンは、ユーザー側で以下のコマンドを実行し結果を
共有していただくか、このワークツリーで対話的に `gcloud auth login` を完了できる状態に
していただく必要がある（capability blocker としてタスクをブロックする）。

---

## 1. SA権限（GCP IAM）— 期待値と確認コマンド

### 期待される構成（`docs/mvp-design.md` §「サービスID（IAM）」より）

| SA | 用途 | 期待ロール |
|---|---|---|
| `app-runtime@jci-sed-agent.iam.gserviceaccount.com` | Cloud Run 実行SA | `roles/datastore.user`, `roles/cloudtasks.enqueuer`, `roles/secretmanager.secretAccessor`, `roles/aiplatform.user`（任意） |
| `drive-reader@jci-sed-agent.iam.gserviceaccount.com` | Drive読み取り専用（impersonation対象） | プロジェクトレベルのIAMロールなし（Drive側フォルダ共有のみで権限付与、`drive.readonly` スコープ） |

### impersonation の鍵となる権限

`app.drive._token()`（`app/drive.py:22-34`）が `impersonated_credentials.Credentials(source_credentials=<app-runtime実行時の既定認証>, target_principal="drive-reader@...", target_scopes=["https://www.googleapis.com/auth/drive.readonly"])` を使う。
これが機能するには **`app-runtime@jci-sed-agent.iam.gserviceaccount.com` が `drive-reader@jci-sed-agent.iam.gserviceaccount.com` に対して `roles/iam.serviceAccountTokenCreator` を持っている**必要がある（鍵レスimpersonationの前提条件）。

### 確認コマンド（ユーザー側で実行・出力を共有）

```bash
# プロジェクト確認
gcloud config set project jci-sed-agent

# app-runtime が drive-reader を impersonate できるか（Token Creator ロール確認）
gcloud iam service-accounts get-iam-policy \
  drive-reader@jci-sed-agent.iam.gserviceaccount.com \
  --format=json

# 期待: bindings に以下が含まれること
#   role: roles/iam.serviceAccountTokenCreator
#   members: serviceAccount:app-runtime@jci-sed-agent.iam.gserviceaccount.com

# Cloud Run サービスの実行SA確認（app-runtime であること）
gcloud run services describe jci-sed-agent --region asia-northeast1 \
  --format="value(spec.template.spec.serviceAccountName)"

# プロジェクト全体のIAMポリシー（app-runtime / drive-reader の他ロールも俯瞰）
gcloud projects get-iam-policy jci-sed-agent \
  --flatten="bindings[].members" \
  --filter="bindings.members:app-runtime@jci-sed-agent.iam.gserviceaccount.com OR bindings.members:drive-reader@jci-sed-agent.iam.gserviceaccount.com" \
  --format="table(bindings.role,bindings.members)"
```

---

## 2. 実 Drive でのフォルダスキャン確認

### 目的
`app.drive.fetch_folder_docs(folder_id)` が実フォルダに対して動作することを確認する
（`list_docs` → `export_text` の一連。Google ドキュメント形式のみ対象、`mimeType='application/vnd.google-apps.document'`）。

### 前提
- 対象フォルダは `drive-reader@jci-sed-agent.iam.gserviceaccount.com` に共有済みであること
  （`web/src/pages/Proposals.tsx:125` の注記どおり、共有し忘れると 0件 or 403 になる）。
- `docs/drive-analysis.md` §7 の命名規約に沿ったテスト用（または実）フォルダを用意。

### 確認コマンド（ユーザー側、要 impersonation 権限）

```bash
# drive-reader の一時トークンを取得（app-runtime の代わりに自分のアカウントで代行確認する場合、
# 自分のアカウントにも roles/iam.serviceAccountTokenCreator on drive-reader が必要）
TOKEN=$(gcloud auth print-access-token \
  --impersonate-service-account=drive-reader@jci-sed-agent.iam.gserviceaccount.com \
  --scopes=https://www.googleapis.com/auth/drive.readonly)

# 対象フォルダ直下の Google ドキュメント一覧
FOLDER_ID="<検証用フォルダのID>"
curl -sS "https://www.googleapis.com/drive/v3/files?q='${FOLDER_ID}'+in+parents+and+mimeType='application/vnd.google-apps.document'+and+trashed=false&fields=files(id,name,modifiedTime)&supportsAllDrives=true&includeItemsFromAllDrives=true" \
  -H "Authorization: Bearer ${TOKEN}" | python3 -m json.tool

# 実アプリの import-drive エンドポイント（dry-run）で確認する場合
curl -X POST "<Cloud Run URL>/api/proposals/import-drive" \
  -H "X-Admin-Token: <secret>" -H "Content-Type: application/json" \
  -d "{\"folder_id\": \"${FOLDER_ID}\", \"dry_run\": true}"
```

### 期待結果
- 一覧APIが 200 を返し、対象フォルダ内の Google ドキュメントの `id`/`name`/`modifiedTime` が取得できる。
- 共有されていないフォルダを指定した場合、`files` が空配列（403ではなく空、Drive APIの仕様）になることを確認 —
  `app/drive_import.py` 側でこのケース（0件）がエラーでなく "created=0" として扱われることは
  既存実装・テスト (`tests/test_drive_import.py`) で担保済み。

---

## 3. フォルダ構成の期待値（ドキュメント化）

`docs/drive-analysis.md` §1・§7 に基づき、取り込み対象フォルダの期待構造をここに集約する。

### 年度作業フォルダ直下（会議体・委員会別）
```
01_執行部
02_五役会
03_理事会          ← 理事会（01_理事会案内 / 02_理事会アジェンダ / 03_議事録 / 04_資料）
11_拡大ブランディング委員会
12_人材育成委員会
13_組織力向上委員会
14_会員拡大
15_合同委員会
98_共有書類
```
※ 委員会は年度により改編される（2026年度は「五役 / コト創り委員会 / 総務委員会 / 組織運営室」に変化）。
　`app.drive_import` は特定の委員会名をハードコードせず、`folder_id` を都度指定する設計になっているため
　この年次変動には強い（フォルダ名の変化はエンドポイント呼び出し側の `folder_id` 選択でのみ影響）。

### 議案取り込み対象（`import_drive_folder` の想定）
- 対象: `03_理事会/04_資料` のような、当該回の議案（事業計画書）が格納されたフォルダ。
- ファイル形式: Google ドキュメント（`mimeType='application/vnd.google-apps.document'`）のみ。
  PDF/Excel/Wordはこの実装ではスキップされる（`list_docs` のクエリで mimeType 固定）
  → **要確認**: 実運用フォルダに Google ドキュメント以外（PDF等）の議案が混在する場合、
  取りこぼしが発生する。`drive-analysis.md` の分析時点では
  `98_フォーマット/2024事業計画書_JCI猪苗代` がテンプレートとして言及されているが、
  実際の提出物の形式（Googleドキュメント統一か、PDF/Word混在か）は実データで要確認。
- 冪等化キー: Drive の `file.id` を `Proposal.storage_uri` に保存し突合（`drive_import.py:48`）。

### フォルダ命名規約（`drive-analysis.md` §7）
- 会議資料: `YYMMDD_第N回理事会資料` / `YYMMDD_第N回理事会アジェンダ`
- 理事会フォルダ機能分割: `01_理事会案内 / 02_理事会アジェンダ / 03_議事録 / 04_資料`

---

## 4. 未検証・要ユーザー確認事項（capability blocker）

以下はこのエージェント実行環境（ヘッドレス、対話式GCP認証不可）からは実施できず、
ユーザー本人による実施 or 結果共有が必要:

1. §1 の `gcloud iam service-accounts get-iam-policy drive-reader@...` 実行結果
   （`app-runtime` に `roles/iam.serviceAccountTokenCreator` が付与されているか）
2. §2 の実（またはテスト用）Drive フォルダでの `fetch_folder_docs` 相当の動作確認結果
3. 実運用の議案ファイルが Google ドキュメント形式に統一されているか、PDF/Word混在かの実データ確認
   （混在している場合、`app/drive.py:47` の `mimeType` フィルタ拡張が別タスクとして必要になる可能性）
