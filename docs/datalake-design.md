# LINEメッセージ・データレイク 詳細設計書 v1.0

最終更新: 2026-10-06
対象LOM: 一般社団法人 猪苗代青年会議所 / GCP `jci-sed-agent`
前提: LINE Webhook（`app/main.py`）、グループ応答ルール（F2-5）、自然文応答 `docs/nl-assistant-design.md`

---

## 1. 目的・スコープ

公式LINE「猪苗代専務AI」が受け取る**すべてのやり取り（グループ・個別、挨拶を含むテキスト、PDF等のファイル、
画像、ボットの返信）を欠かさず保存**し、将来の AI の精度改善（評価データ・RAG・傾向分析）に使える形で蓄積する。

### 現状の問題（2026-10-06 本番ログ調査）
- ボットは5グループに参加し、直近30日でグループのテキスト54件・テキスト以外39件を受信しているが、**すべて破棄**している。
- ファイル（PDF等）は LINE からメッセージIDで取得する仕組みで、**LINE 側は一定期間で削除する**。IDを残していないため過去分は取り戻せない。
- 取込を始めるまで、毎日データを失い続けている。

### 確定事項（2026-10-06 専務確認）
| 項目 | 決定 |
|---|---|
| 取込範囲 | **案3: 本文もファイルも取り込む**（グループ・個別トーク、挨拶等も含む全メッセージ） |
| 目的 | 過去のやり取りをデータとして残し、AI の精度改善等に活用する |
| 規模の想定 | 今後膨大になる前提で設計する |

### 設計の芯
1. **受信を止めない・落とさない**。Webhook は受け取った生イベントを Pub/Sub に投げるだけ（数十ms）。保存・加工は非同期。
2. **生データは加工せず全部残す（lossless）**。LINE のイベント JSON をそのまま保存し、解釈はビュー（SQL）で後から変えられるようにする。
3. **サーバーレス・従量課金・コード最小**。Pub/Sub → BigQuery は**公式の BigQuery サブスクリプション**で直結し、取込用のプログラムを書かない。
   1日数件でも数百万件でも同じ構成で動き、少量のうちはほぼ無料。
4. **バイナリ（ファイル）とイベントを分ける**。イベント＝BigQuery、ファイル＝Cloud Storage。
5. **プライバシーを最初から組み込む**。送信取消は尊重（AI用ビューから除外・ファイル削除・本文消去）、アクセスは専務・管理者のみ、保存期間を設定。

---

## 2. 全体構成

```
                    ┌──────────────────────── jci-sed-agent (Cloud Run) ────────────────────────┐
LINE ──Webhook──►   │ /line/webhook: 署名検証 → ① 生イベントを Pub/Sub へ publish → ② 従来の応答処理 │
                    │ 返信・プッシュ送信時: 送ったメッセージも publish（P6-3）                         │
                    └───────────────┬────────────────────────────────────────────────────────────┘
                                    ▼
                          Pub/Sub topic: line-events  ──(失敗)──► line-events-dlq
                           │                      │
     BigQuery サブスクリプション（コード不要）    Push サブスクリプション（OIDC認証）
                           ▼                      ▼
        BigQuery line_lake.events_raw     jci-sed-agent /pubsub/line-worker
        （全イベントの生JSON・日付パーティション）   ├ ファイル/画像/動画/音声 → LINE から取得 → Cloud Storage
                           ▲                      ├ PDF はテキスト抽出（AI 用）
                           │                      ├ 送信取消 → ファイル削除
                           │                      └ 未知のグループ → グループ名を取得
                           └──── 結果も publish（content / group_profile イベント）────┘

        BigQuery ビュー: v_messages / v_messages_for_ai / v_files / v_groups / v_conversations
                           ▼
        AI 活用（P6-4）: 評価データ・RAG（ベクトル検索）・傾向分析、管理画面での閲覧
```

| 部品 | GCP サービス | 名前 | 役割 |
|---|---|---|---|
| 受信 | Cloud Run（既存） | `jci-sed-agent` | 署名検証済みの生イベントを publish |
| バス | Pub/Sub | `line-events`（保持7日）／`line-events-dlq` | 非同期化・再送・リプレイ |
| 生データ | BigQuery | `line_lake.events_raw` | 全イベントの生 JSON（日付パーティション） |
| 取込 | Pub/Sub BigQuery サブスクリプション | `line-events-to-bq` | トピック→テーブル直結（プログラム無し） |
| ファイル | Cloud Storage | `jci-sed-agent-line-content` | PDF・画像等の実体 |
| ワーカー | Cloud Run（既存）＋ Push サブスクリプション | `line-events-worker` → `/pubsub/line-worker` | ファイル取得・送信取消・グループ名 |
| 加工 | BigQuery ビュー | `line_lake.v_*` | 用途別の見やすい形（SQLで随時変更可） |

**リージョンはすべて `asia-northeast1`（東京）**。データは国内に置く。

### なぜこの構成か（代替案との比較）
| 代替案 | 採らない理由 |
|---|---|
| Firestore に全メッセージを保存 | 書き込み課金が件数比例。分析・AI 用の集計（SQL）が苦手。運用データ（会員・イベント）と混ざる |
| Webhook から BigQuery に直接書く | BigQuery の一時障害で受信が遅れる／落ちる。Pub/Sub を挟めば7日間保持され、再送・リプレイできる |
| 生イベントを Cloud Storage にも二重保存 | BigQuery の長期保存は Nearline 並みに安く、二重化は削除依頼への対応を難しくする。障害復旧は BigQuery のタイムトラベル（7日）と Pub/Sub 保持で足りる |
| 専用のワーカーサービスを新設 | 現規模では既存サービスの1エンドポイントで十分。負荷が増えたら同じイメージで `ROLE=worker` のサービスに切り出せる設計にする |

---

## 3. データモデル

### 3.1 イベント封筒（Pub/Sub メッセージ本文）
全イベントを同じ形に包んで publish する。`payload` は LINE の生 JSON（webhook）か、アプリが作る記録。

```json
{
  "v": 1,
  "id": "webhookEventId または uuid（重複排除キー）",
  "kind": "webhook | outbound | content | group_profile",
  "received_at": "2026-10-06T12:34:56.789+09:00",
  "destination": "Uxxxxxxxx（ボットのユーザーID）",
  "payload": { ... }
}
```

Pub/Sub 属性（サブスクリプションのフィルタ用）: `kind` / `event_type`（message, join, unsend…）/ `message_type`（text, file, image…）/ `source_type`（user, group, room）

### 3.2 `line_lake.events_raw`（BigQuery サブスクリプションの書き込み先）
| 列 | 型 | 内容 |
|---|---|---|
| `subscription_name` | STRING | 書き込んだサブスクリプション |
| `message_id` | STRING | Pub/Sub メッセージID |
| `publish_time` | TIMESTAMP | **パーティション列（日単位）** |
| `data` | JSON | 封筒（3.1） |
| `attributes` | JSON | 属性 |

- Pub/Sub は「少なくとも1回」配信なので重複があり得る。ビューで `id` ごとに1件に絞る。
- パーティションの保存期間（既定5年）で古いデータを自動削除できる（7章）。

### 3.3 ビュー（`infra/bigquery/views/*.sql` で管理）
| ビュー | 粒度 | 主な列 |
|---|---|---|
| `v_events` | イベント | 重複排除済みの封筒、`kind`、`event_type`、受信時刻 |
| `v_messages` | 受信メッセージ | 送信時刻、`source_type`、`group_id`、`user_id`、`message_id`、`message_type`、`text`、`file_name`、`mentions_bot`、`quoted_message_id`、`unsent`（送信取消済み） |
| `v_messages_for_ai` | 同上 | **送信取消を除外**したもの。AI 用途はこちらだけを使う |
| `v_files` | ファイル | `message_id`、`gcs_uri`、`content_type`、`size`、`sha256`、`file_name`、`status`、`text`（PDF抽出） |
| `v_groups` | グループ | グループ名、参加/退出、最初/最後のメッセージ時刻、件数 |
| `v_conversations` | 受信＋送信 | 受信メッセージとボットの返信を時系列で並べたもの（P6-3） |

### 3.4 ファイルの置き場所
`gs://jci-sed-agent-line-content/line/content/{messageId}/{ファイル名}`

- メッセージIDで一意に決まるので、送信取消のときにすぐ消せる。
- グループID・送信者・送信日時はオブジェクトのメタデータと `v_files` に持つ。
- バケットは**一般公開禁止**・均一アクセス制御。保管クラスは90日で Nearline、1年で Coldline に自動で移す（コスト最適化）。

### 3.5 Firestore `lineGroups/{groupId}`（運用用の小さな台帳）
グループ名・アイコン・参加日時・退出日時。管理画面のグループ一覧と、ワーカーの「未知のグループか」判定に使う。

---

## 4. 処理仕様

### 4.1 Webhook（P6-1）
1. 署名検証（既存）。
2. リクエスト本文の `events[]` を**そのまま**封筒に包み、まとめて1回 publish（REST、追加ライブラリ不要）。
3. 従来の応答処理（メンション判定・招待コード・メニュー等）。

- publish に失敗しても応答処理は止めない。失敗した生イベントは `LINE_INGEST_FALLBACK` という目印付きで Cloud Logging に残し、
  `scripts/replay_ingest_fallback.py` で後から再投入できるようにする。
- `LINE_EVENTS_TOPIC` が未設定なら取込は無効（従来どおり動く）。

### 4.2 ワーカー（P6-2）
Push サブスクリプション `line-events-worker`（フィルタ `attributes.kind = "webhook"`）が `/pubsub/line-worker` を呼ぶ。

| 受け取ったイベント | 処理 |
|---|---|
| file / image / video / audio メッセージ | LINE（`api-data.line.me/v2/bot/message/{id}/content`）から取得 → Cloud Storage に保存 → `content` イベントを publish。PDF はテキストを抽出（上限あり）して同梱 |
| 外部URLの動画・音声（`contentProvider=external`） | 取得せず `status=external` で記録 |
| 50MB 超 | 取得せず `status=too_large` で記録（上限は環境変数で変更可） |
| 動画・音声の変換待ち | 失敗応答 → Pub/Sub が間隔を空けて再送 |
| unsend（送信取消） | 該当ファイルを削除し `status=deleted_unsent` を publish |
| join / 未知のグループからのイベント | グループ名・アイコンを取得 → `lineGroups` に保存し `group_profile` を publish |
| leave | `lineGroups` に退出日時を記録 |

- 認証: Pub/Sub が付ける **OIDC トークンを検証**（発行者・宛先URL・専用SA `pubsub-push@` のメール）。トークンが無い・不正なら 401。
- 冪等: 同じメッセージが再送されても、保存先が同じなので上書きになるだけ。
- 失敗が5回続いたら `line-events-dlq` に退避（後で調査・再処理）。

### 4.3 送信メッセージの記録（P6-3）
返信（reply）・プッシュ（push）の直後に `outbound` イベントを publish（宛先、送った内容、返信先のイベントID、どの機能が送ったか）。
これで「質問 → ボットの回答」の組がそろい、AI の回答品質を評価・改善できる。

### 4.4 送信取消の本文消去（P6-2）
LINE では送信から24時間以内に取り消せる。tick（毎時）で、取り消されたメッセージの本文を `events_raw` から消す
（`JSON_REMOVE`、直近3日のパーティションだけを対象にして安く済ませる）。ビュー `v_messages_for_ai` は即座に除外する。

---

## 5. 権限・セキュリティ

| 主体 | 権限 |
|---|---|
| `app-runtime`（Cloud Run） | `line-events` への publish、コンテンツバケットの読み書き削除、`line_lake` の編集とクエリ実行（本文消去用） |
| Pub/Sub サービスエージェント | `line_lake` への書き込み（BigQuery サブスクリプション）、DLQ への publish、`pubsub-push@` のトークン発行 |
| `pubsub-push@`（新規SA） | Push の OIDC トークンの発行元。権限は持たない |
| 専務・管理者 | BigQuery とバケットの閲覧（プロジェクトのオーナー権限で既に可） |

- 会員向けの画面やAPIからは一切参照しない（会員向け画面は保留中）。
- 将来 AI 開発者など外部の人が使うときは、`v_messages_for_ai` だけを見せる「承認済みビュー」と仮名化（user_id のハッシュ化）を追加する。

---

## 6. コストの見積もり

想定: 受信 2,000件/日（現在の約100倍）、平均 1KB、ファイル 10件/日・平均 1MB

| 項目 | 月額の目安 |
|---|---|
| Pub/Sub（publish＋2サブスクリプション） | 無料枠（10GiB/月）内 |
| BigQuery 保存（年間約0.7GB） | 数円 |
| BigQuery クエリ | 無料枠（1TiB/月）内 |
| Cloud Storage（年間約3.6GB、Nearline/Coldline へ自動移行） | 数円〜十数円 |
| Cloud Run（ワーカーの呼び出し増分） | 無料枠内 |

**月数十円〜数百円程度**。件数が100倍になっても構成は同じで、費用は件数にほぼ比例して増える。

---

## 7. ガバナンス（運用ルール）

| 項目 | 既定値（変更可） |
|---|---|
| 利用目的 | LOM 運営の支援、ボットの応答品質・AI の精度改善。**個人の評価には使わない** |
| 保存期間 | 生イベント・ファイルとも **5年**（`events_raw` のパーティション期限・バケットの削除ルール） |
| 送信取消 | 尊重する（AI用ビューから除外、ファイル削除、本文消去） |
| 削除依頼 | 本人から依頼があれば、user_id で BigQuery の該当行とファイルを削除（手順は runbook に記載） |
| 閲覧できる人 | 専務・システム管理者のみ |

### メンバーへの周知（取込開始前に専務が各グループへ投稿）
ボットからの自動投稿はしない（F2-5 の「メンション無しで発言しない」方針を守る）。以下を専務が投稿する。

> 【お知らせ】このグループには公式LINE「猪苗代専務AI」が参加しています。
> LOM運営の効率化とAIの精度向上のため、このグループのメッセージ・ファイルを記録しています。
> 記録は専務・システム管理者のみが閲覧し、個人の評価には使用しません。保存期間は5年です。
> 送信取消したメッセージは記録からも削除されます。削除のご希望は専務までご連絡ください。

※ LOM の個人情報保護方針（利用目的の公表）にも同じ内容を追記することを推奨。

---

## 8. 実装フェーズ（issue 分割）

| フェーズ | 内容 | 依存 |
|---|---|---|
| **P6-1** | 封筒＋Webhook からの publish（失敗時のログ退避・再投入スクリプト）、BigQuery テーブル・BigQuery サブスクリプション・DLQ、ビュー（events/messages/messages_for_ai/groups の基本）、`scripts/setup_datalake.sh` | なし |
| **P6-2** | ワーカー（OIDC 検証、ファイル取得→GCS、PDFテキスト抽出、送信取消でファイル削除、グループ名取得・`lineGroups`）、本文消去（tick）、`v_files`・`v_groups` 拡充 | P6-1 |
| **P6-3** | 送信メッセージの記録（reply/push）、`v_conversations`、管理画面「LINEグループ」（グループ一覧・最近のファイル・ファイルのダウンロード） | P6-1 |
| **P6-4** | AI 活用: 評価データセット、BigQuery ベクトル検索による過去のやり取りの RAG、応答品質の振り返り画面 | P6-1〜3、データの蓄積 |

---

## 9. 残課題（ユーザー提供・手動が必要）

1. **`scripts/setup_datalake.sh` の実行**（1回だけ）: API 有効化、トピック・DLQ、BigQuery データセット・テーブル・ビュー、
   バケット（公開禁止・ライフサイクル）、SA と権限、BigQuery/Push サブスクリプションを作成する。
2. **Cloud Run の環境変数**（スクリプトが最後にコマンドを表示）: `LINE_EVENTS_TOPIC`、`LINE_CONTENT_BUCKET`、`PUBSUB_PUSH_SA`、`PUBSUB_PUSH_AUDIENCE`。
3. **各グループへの周知投稿**（7章の文面）と、個人情報保護方針への追記。
4. 保存期間（既定5年）を変える場合は指示する。

---

> v1.0。P6-1 から実装に入る。ファイルは LINE 側で消えるため、P6-1・P6-2 を優先して早く稼働させる。
