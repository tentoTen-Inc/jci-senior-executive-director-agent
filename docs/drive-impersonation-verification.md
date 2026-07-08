# Drive サービスアカウント Impersonation 検証手順・期待値

最終更新: 2026-07-09
対象タスク: t_50e91c51 [gtb-jci-senior-executive-director-agent] Drive サービスアカウントimpersonation検証

## 状況（2026-07-09 追記: 検証完了）

2026-07-08 時点ではこのワークツリー（ヘッドレスなコーディングエージェント環境）から `gcloud` を
実行したところ、キャッシュ済みの3アカウントすべてが `Reauthentication failed: cannot prompt
during non-interactive execution` でブロックされ検証不可だった。

2026-07-09、ユーザー側で `admin@10to10.co.jp` の対話的 `gcloud auth login` が完了し、
`jci-sed-agent` プロジェクトへのアクセス権限が確認されたため、このワークツリーで
`gcloud auth list` の ACTIVE アカウントが `admin@10to10.co.jp` に切り替わっており、
以下の §1・§2 の検証コマンドを実際に実行し結果を確認できた。**検証完了・すべて期待どおり。**

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

### 実行結果（2026-07-09、`admin@10to10.co.jp` にて実施）

`gcloud iam service-accounts get-iam-policy drive-reader@jci-sed-agent.iam.gserviceaccount.com`:
```json
{
  "bindings": [
    {
      "members": [
        "serviceAccount:app-runtime@jci-sed-agent.iam.gserviceaccount.com",
        "user:admin@10to10.co.jp"
      ],
      "role": "roles/iam.serviceAccountTokenCreator"
    }
  ]
}
```
→ **期待どおり**: `app-runtime` が `drive-reader` に対して `roles/iam.serviceAccountTokenCreator` を保持している（鍵レスimpersonationの前提条件を満たす）。加えて `admin@10to10.co.jp` にも同ロールが付与されており、人間側で代行検証できる構成になっている。

`gcloud run services describe jci-sed-agent --region asia-northeast1 --format="value(spec.template.spec.serviceAccountName)"`:
```
app-runtime@jci-sed-agent.iam.gserviceaccount.com
```
→ **期待どおり**: Cloud Run の実行SAは `app-runtime`。

`gcloud projects get-iam-policy jci-sed-agent ...`（app-runtime / drive-reader 関連ロールの俯瞰）:
```
ROLE                                MEMBERS
roles/aiplatform.user               serviceAccount:app-runtime@...
roles/cloudtasks.enqueuer           serviceAccount:app-runtime@...
roles/datastore.user                serviceAccount:app-runtime@...
roles/logging.logWriter             serviceAccount:app-runtime@...
roles/secretmanager.secretAccessor  serviceAccount:app-runtime@...
```
→ **期待どおり**: `docs/mvp-design.md` の期待ロール一覧と一致（`aiplatform.user` も付与済み）。`drive-reader` はプロジェクトレベルのロールなし（Drive側フォルダ共有のみで権限付与、設計どおり）。`logging.logWriter` は設計書に明記はないが実運用上の標準ロールで問題なし。

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

### 実行結果（2026-07-09、`admin@10to10.co.jp` にて `drive-reader` impersonate で実施）

`drive-reader` へのimpersonationトークン取得 → `drive/v3/about` で本人確認:
```json
{"user": {"displayName": "drive-reader@jci-sed-agent.iam.gserviceaccount.com", "me": true, "emailAddress": "drive-reader@jci-sed-agent.iam.gserviceaccount.com"}}
```
→ **期待どおり**: impersonationが機能し、`drive-reader` としてDrive APIを呼び出せている。

実フォルダ「計画書/00_議案書」（`folder_id=1VaqSxICRISjEI95IDpqmWrgTGU5iz5L4`、
`list_docs` と同じクエリ: `mimeType='application/vnd.google-apps.document' and trashed=false`）:
```json
{
  "files": [
    {"id": "1bKwAO2kzE72i23OmUQOPGfKkeKDuhnRzog26nxDkOaY", "name": "【事業計画書書式】2026_9月通常総会事業計画書_v2", "modifiedTime": "2026-07-08T09:56:52.971Z"},
    {"id": "1YmIfYm3HAXr8MVw6JgbfFstJqqgIbqPmo2qyGpQhmN0", "name": "【事業計画書書式】2026_9月通常総会事業計画書_v1", "modifiedTime": "2026-06-16T10:21:54.221Z"}
  ]
}
```
→ **期待どおり**: 200が返り、`id`/`name`/`modifiedTime` を取得できた。このフォルダは全ファイルがGoogleドキュメント形式（後述§3参照）。

`export_text` 相当（`GET .../export?mimeType=text/plain`）を上記ファイルの1つに実行:
```
【事業計画書書式】（2026年度様式）
一般社団法人猪苗代青年会議所
事業計画書(案)
事業名  2026年度9月通常総会
◯ファイル名  【事業計画書書式】2026_9月通常総会事業計画書_v２
◯委員会名  総務委員会
...
```
→ **期待どおり**: `list_docs` → `export_text` の一連の実データパイプラインが正常動作することを確認。

---

## 3. フォルダ構成の期待値（ドキュメント化）と実データによる形式確認

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
　実データでも「07-組織運営室 / 05-総務委員会 / 06-各種フォーマット / 04-コト創り委員会」の並びを
　実フォルダ一覧（drive-reader 共有スコープ）で確認済み。

### 議案取り込み対象（`import_drive_folder` の想定）
- 対象: `03_理事会/04_資料` のような、当該回の議案（事業計画書）が格納されたフォルダ。
- ファイル形式: Google ドキュメント（`mimeType='application/vnd.google-apps.document'`）のみ。
  PDF/Excel/Wordはこの実装ではスキップされる（`list_docs` のクエリで mimeType 固定）
- 冪等化キー: Drive の `file.id` を `Proposal.storage_uri` に保存し突合（`drive_import.py:48`）。

### 実データでの形式混在確認（2026-07-09 実施・結論あり）

3つの実フォルダを比較調査した結果、**「議案書」名を冠する厳選フォルダはGoogleドキュメント形式に
統一されているが、委員会の作業フォルダ全般ではPDF/docx/画像/スプレッドシートが混在する**ことを確認：

| フォルダ | 内容 | 結果 |
|---|---|---|
| 計画書/00_議案書（総務委員会） | 事業計画書(案) x2 | 全て `application/vnd.google-apps.document`（100%） |
| 事業計画書/01-議案書（コト創り委員会） | テンプレート/記載例 x3 | 全て `application/vnd.google-apps.document`（100%） |
| 05-総務委員会/.../01_1月通常総会/審議資料 | 御礼状・予算書・見積書・写真等 x15 | Googleドキュメント7、PDF2、docx3、jpg2、スプレッドシート1、フォーム1（**混在**） |
| 05-総務委員会/.../03_9月通常総会/02_審議資料 | 総会資料一式 x7 | Googleドキュメント3、docx3、PDF1（**混在**） |

→ **結論**: `import_drive_folder` の運用上、**呼び出し側が「議案書」そのものが格納された
狭いフォルダ（例: `00_議案書`、`01-議案書`）を `folder_id` に指定する運用であれば、
現行のGoogleドキュメント限定フィルタで実用上問題ない**（実際にそれらのフォルダは100%
Googleドキュメント形式だった）。一方、委員会の作業フォルダ全体や「審議資料」フォルダを
直接指定すると、docx/PDFの議案・関連資料が取りこぼされる。
運用ガイド（`web/src/pages/Proposals.tsx` の注記や利用者向けヘルプ）に
「取り込み対象フォルダは議案書そのものを格納した狭いフォルダを選ぶこと（作業フォルダ全体は不可）」
という注意書きを追記することを推奨する。mimeTypeフィルタのdocx/PDF対応拡張は、
現状の運用（狭いフォルダ選択）で回避可能なため、緊急性の高い別タスク化は不要と判断。

### フォルダ命名規約（`drive-analysis.md` §7）
- 会議資料: `YYMMDD_第N回理事会資料` / `YYMMDD_第N回理事会アジェンダ`
- 理事会フォルダ機能分割: `01_理事会案内 / 02_理事会アジェンダ / 03_議事録 / 04_資料`

---

## 4. 検証結果まとめ（完了）

2026-07-09、`admin@10to10.co.jp` の対話ログイン完了後、本ワークツリーから以下すべてを実施・確認した:

1. §1 IAM確認: `app-runtime` に `drive-reader` への `roles/iam.serviceAccountTokenCreator` が
   付与されていることを確認（期待どおり）。Cloud Runの実行SAも `app-runtime` で一致。
2. §2 実Driveフォルダスキャン: `drive-reader` impersonationで実フォルダ「00_議案書」から
   Googleドキュメント2件を一覧取得し、`export_text` でテキスト抽出まで成功（実データパイプライン動作確認）。
3. §3 ファイル形式混在確認: 「議案書」を冠する狭いフォルダはGoogleドキュメントに統一されている一方、
   委員会の作業フォルダ全体や「審議資料」フォルダにはPDF/docx/画像が混在することを実データで確認。
   運用ガイドへの注意書き追記を推奨（緊急のコード変更は不要）。

本タスクの検証範囲は完了。フォローアップとして、Proposals.tsx のDrive取込UI付近に
「取り込み対象は議案書そのものが格納された狭いフォルダを選ぶこと」という運用注意書きを
追記するドキュメント/UIタスクを別途起票することを推奨する。

