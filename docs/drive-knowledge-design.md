# ドライブの資料を専務AIのナレッジにする 詳細設計 v1.0

最終更新: 2026-10-23（v1.1: tick の期限超過への対応）
前提: データレイク `docs/datalake-design.md`、LINE 資料の検索 `docs/lake-ai-design.md`（P6-4a）、ドライブの権限借用 `docs/drive-impersonation-verification.md`

## 1. 目的
会員からの「去年はどうしていた？」「この様式はどこ？」といった質問に、専務本人ではなく専務AIが答えられるようにする。
これまで専務AIが参照できたのは LINE でやり取りした資料と会話だけだったため、Google ドライブ（2026年度・2027年度のフォルダ）の資料も同じ仕組みで検索できるようにする。

## 2. 流れ
```
tick（毎時）
  └ drive_knowledge.sync()
       対象フォルダを再帰的にたどる → 除外ルールを適用
       → 更新日時が変わったファイルだけ本文を取り出す（1回 40 件・60 秒まで、残りは次回）
       → BigQuery line_lake.drive_documents にまとめて1回で MERGE／ドライブから消えたものは DELETE
  └ lake_knowledge.index_knowledge()
       drive_documents を knowledge_chunks に分割・ベクトル化（LINE の資料と同じ処理）
会員の質問
  └ assistant → lake_knowledge.search(user_id, question, drive_scopes)
       LINE の資料＋ドライブの資料から近いものを探して回答。出典に「Google ドライブ・M/D更新」とリンクを付ける
```

## 3. データ
`line_lake.drive_documents`（スキーマ: `infra/bigquery/drive_documents.json`）

| 列 | 内容 |
|---|---|
| file_id | ドライブのファイル ID |
| title / path | ファイル名と置き場所（例: `2027年度/04-委員会/02-総務委員会`） |
| mime_type / modified_at | 種類と更新日時（取り直すかどうかの判定に使う） |
| access | `members`（会員全員）または `officers`（五役・監事のみ） |
| text | 本文（1 ファイル 40,000 文字まで。取り出せない種類は空文字で記録し、毎回取り直さない） |
| synced_at | 取り込んだ日時 |

本文を取り出す種類: Google ドキュメント・スライド（テキスト）、スプレッドシート（CSV を空でないセルだけ並べる）、PDF。

## 4. ルール
- **個人情報は取り込まない**: パスかファイル名に「名簿・住所録・入会・認承証・個人情報・事務局」を含むもの（`00-事務局` フォルダごと）。
- **見せる範囲**: パスに「執行部・五役会」を含む資料は `officers`。理事長・副理事長・専務理事・監事・直前理事長の質問にだけ使う。それ以外は会員全員。
- ドライブは `drive-reader` サービスアカウント（`drive.readonly`）の権限借用で読む。書き込みはしない。
- 取込の失敗は tick を止めない（`{"error": "drive_sync_failed"}` を返してログに残す）。
- tick では予約投稿・催促を先に送り、取込・索引は最後に行う。tick 全体を Cloud Scheduler の期限（180 秒）内に収めるため、本文の取り出しは 60 秒で打ち切り、書き込みは1回の DML にまとめる（1件ずつの DML は1件数秒かかり、40 件で期限を超えた）。
- 回答に使った資料は出典としてファイル名・更新日・リンクを付ける。リンクを開けるかどうかはドライブ側の共有設定に従う。

## 5. 設定
1. 対象フォルダを `drive-reader@jci-sed-agent.iam.gserviceaccount.com` に閲覧者として共有する（2026年度・2027年度のフォルダ）。
2. BigQuery にテーブルを作る:
   `bq mk --table --schema infra/bigquery/drive_documents.json line_lake.drive_documents`
3. jci-sed-agent に環境変数を設定する:
   `DRIVE_KNOWLEDGE_FOLDERS=<2027年度フォルダID>,<2026年度フォルダID>`（カンマ区切り）
   未設定なら取込は動かない（従来どおり LINE の資料だけを検索）。

## 6. テスト
`tests/test_drive_knowledge.py`（ドライブ・BigQuery はモック）: フォルダのたどり方、除外、見せる範囲、更新分だけの取込、件数の上限、削除、失敗時。
