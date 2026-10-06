# Googleカレンダー連携（F3-2）詳細設計書 v1.0

最終更新: 2026-10-06
対象LOM: 一般社団法人 猪苗代青年会議所 / GCP `jci-sed-agent`
前提: 要件 `docs/requirements.md` §F3、イベント管理 `docs/mvp-design.md`、管理画面 `docs/dashboard-design.md`、
自然文応答 `docs/nl-assistant-design.md`

---

## 1. 目的・スコープ

例会・理事会などの予定を **Googleカレンダー（`inawashiro.jc@gmail.com`）と双方向に同期**し、
管理画面と LINE（役員の自然文）から予定の登録・変更・中止をできるようにする。
専務が「システムに登録して、カレンダーにも手で入れ直す」二重入力をなくす。

| 要件 | 内容 | 優先 | 実装フェーズ |
|---|---|---|---|
| F3-2 | Google Calendar と連携してイベントを取込／反映できる | SHOULD | P5-1（反映）／P5-2（取込） |
| （追加）| 役員が LINE の自然文で予定を登録・変更・中止できる | SHOULD | P5-3 |

### 確定事項（2026-10-06 専務確認）
| 項目 | 決定 |
|---|---|
| 連携先カレンダー | `inawashiro.jc@gmail.com` のカレンダー（Gmail取込と同じ専用アカウント） |
| 同期の向き | **双方向**（システム→カレンダー、カレンダー→システム） |
| 操作の入口 | 管理画面（イベント登録・編集）＋ LINE（役員の自然文） |

### 設計の芯
1. **イベントの正はシステム（Firestore）**。カレンダーは入出力の連携先（要件 §2「正データ」の方針どおり）。
   出欠・催促・定足数などカレンダーに無い情報は常にシステム側だけが持つ。
2. **リンクは双方に刻む**。システム側は `Event.gcal_event_id`、カレンダー側は
   `extendedProperties.private.jci_event_id` を持ち、どちらからでも相手を特定できる。
3. **カレンダー由来の予定は `draft` で取り込む**。個人的な予定や仮押さえで LINE 配信・催促が勝手に動かないよう、
   配信対象にするかは専務が管理画面で `open` にして決める。
4. **削除は消さずに「中止」にする**。カレンダーで消されてもシステムの出欠データは残し、`cancelled` にする（元に戻せる）。
5. **LINE からの書き込みは必ず確認を挟む**。自然文の解釈結果を見せ、本人が「はい」を押したときだけ反映する（F4-7 と同じ思想）。
6. **同期失敗で本体を止めない**。カレンダー API が落ちていてもイベント保存は成功させ、未同期として tick で再試行する。

---

## 2. 認証方式（Drive と同じ鍵レス impersonation）

```
Cloud Run (app-runtime SA) ──impersonate──► calendar-sync SA ──Calendar API──► inawashiro.jc@gmail.com のカレンダー
                                                   ▲
                       カレンダーの「特定のユーザーと共有」で calendar-sync SA に「予定の変更」権限を付与
```

- 新規SA **`calendar-sync@jci-sed-agent.iam.gserviceaccount.com`**（プロジェクトレベルのIAMロールなし）。
  `app-runtime` に、このSAへの `roles/iam.serviceAccountTokenCreator` を付与する（drive-reader と同じ構成）。
- 権限はカレンダー側の共有設定だけで付与する。スコープは `https://www.googleapis.com/auth/calendar.events`。
- **OAuth refresh token 方式（Gmail取込）は採らない**。トークンの失効（同意画面が「テスト」だと7日）や再同意の手間が無く、
  鍵も持たないため。
- 制約: SA からは**招待メールを送れない**（ドメイン全体委任が必要なため）。会員への周知は従来どおり LINE で行う。
  予定の登録・更新・削除・差分取得は問題なく行える。
- 環境変数: `GCAL_CALENDAR_ID`（既定 `inawashiro.jc@gmail.com`）、`GCAL_SYNC_SA`（既定 上記SA）。
  `GCAL_CALENDAR_ID` が空なら連携は無効（従来どおり動く）。

---

## 3. データモデル

### 3.1 `Event` への追加フィールド
| フィールド | 型 | 内容 |
|---|---|---|
| `gcal_event_id` | `str \| None` | カレンダー側の予定ID |
| `gcal_etag` | `str \| None` | 最後に同期したカレンダー予定の etag（ループ防止・差分判定） |
| `gcal_sync_state` | `synced \| pending \| error \| disabled` | 反映状態。`pending/error` は tick で再試行 |
| `gcal_synced_at` | `datetime \| None` | 最後に同期が成功した時刻 |
| `gcal_error` | `str \| None` | 直近の失敗理由（画面表示用） |
| `origin` | `system \| gcal \| line` | どこで作られたか（画面のバッジ・監査用） |
| `updated_at` | `datetime \| None` | システム側の最終更新時刻（競合判定に使う） |

### 3.2 `EventStatus` に `cancelled` を追加
- `cancelled` のイベントは催促・配信の対象外（`plan_reminders` は `open` のみ対象なので追加改修は不要）。
- カレンダー反映: `cancelled` になったらカレンダー予定を削除する。`open/draft` に戻したら再作成する。

### 3.3 `CalendarSyncState`（新規・シングルトン `settings/gcal_sync`）
| フィールド | 内容 |
|---|---|
| `sync_token` | `events.list` の `nextSyncToken`（差分取得用） |
| `last_pulled_at` | 最後に取込が成功した時刻 |
| `last_error` | 直近の取込失敗理由 |

### 3.4 `PendingCalendarOp`（新規・P5-3）
LINE で解釈した操作を確認待ちで保持する（`calendar_ops/{op_id}`）。
`op_id / member_id / action(create|update|cancel) / event_id / fields / expires_at(30分) / status`。

---

## 4. 同期仕様

### 4.1 システム → カレンダー（P5-1）
| システム側の操作 | カレンダーへの反映 |
|---|---|
| イベント作成（管理画面・LINE） | `events.insert`（`jci_event_id` を付与）。返った id/etag を保存 |
| イベント編集（タイトル・日時・場所・種別） | `events.patch`。出欠締切などカレンダーに無い項目だけの変更なら呼ばない |
| `cancelled` にする | `events.delete` |
| `cancelled` から戻す | `events.insert`（新しい id を保存） |

**予定の中身**
- タイトル: `【理事会】第8回理事会` のように種別を前置（種別とタイトルが同じなら前置しない）
- 日時: `Asia/Tokyo`。終了未設定なら開始＋2時間
- 場所: `location`
- 説明: 出欠締切・資料締切・対象範囲と「猪苗代JC 専務理事エージェントが管理する予定です」の注記

反映は API 呼び出しの直後に同期実行する（体感を良くするため）。失敗時は `gcal_sync_state=error` を残して保存自体は成功させ、
tick（毎時）で `pending/error` を再試行する。

### 4.2 カレンダー → システム（P5-2）
tick に相乗りし、`events.list(syncToken=…)` で差分だけを取る。初回（トークン無し）は `timeMin=今日-30日` から全件取って
`nextSyncToken` を保存する。`410 Gone`（トークン失効）なら初回と同じ全件取得に戻る。

| カレンダー側の変化 | システム側の処理 |
|---|---|
| `jci_event_id` 無しの新しい予定 | **`draft` のイベントとして取込**（`origin=gcal`）。種別はタイトルのキーワード（例会/理事会/五役会/委員会/総会）から推定、無ければ「イベント」 |
| リンク済み予定の変更 | etag が保存値と同じなら**自分の反映の折り返しなので無視**。違えば、カレンダーの `updated` がシステムの `updated_at` より新しい場合だけタイトル・日時・場所を取り込む |
| リンク済み予定の削除 | イベントを `cancelled` にする（出欠データは保持）。監査ログ `event.cancel_from_gcal` |
| 終日予定 | 取込対象外（例会・会議は時刻ありが前提。祝日カレンダー等の混入防止） |
| 繰り返し予定 | `singleEvents=true` で個々の回に展開して扱う |

**競合ルール（両方で編集された場合）**: タイトル・日時・場所は**後勝ち**（カレンダーの `updated` とシステムの `updated_at` を比較）。
出欠・締切・対象範囲などカレンダーに無い項目は常にシステムが正なので競合しない。

### 4.3 ループ防止
システムが反映した直後のカレンダー予定は、差分取得で「変更あり」として返ってくる。保存済み `gcal_etag` と一致すれば無視する。

---

## 5. 操作の入口

### 5.1 管理画面（P5-1 / P5-2）
- イベント一覧・詳細に**カレンダー同期バッジ**（同期済み／未同期／エラー＋理由）と「カレンダー由来」バッジ。
- イベント詳細に「**中止にする**」「**再開する**」「**カレンダーに再同期**」ボタン。
- 既存の作成・編集はそのまま。保存時に自動でカレンダーへ反映する。
- API 追加: `POST /api/events/{id}/cancel`、`POST /api/events/{id}/restore`、`POST /api/events/{id}/gcal-sync`、
  `POST /api/gcal/backfill`（これから開催の未同期イベントを一括反映）、
  `GET /api/gcal/status`（連携の有効/無効・最終取込・直近エラー）。

### 5.2 LINE（役員の自然文・P5-3）
- 対象者: 五役（`summary.OFFICER_ROLES` = 理事長/直前理事長/副理事長/専務理事/監事）と事務局（`member_type=office`）。
  それ以外の会員が書き込み系の文を送っても従来の応答（予定の確認など）にフォールバックする。
- 起動条件: 「登録／追加／入れて／変更／変えて／ずらして／中止／キャンセル／削除」などの書き込み動詞を含む文。
  既存の「予定」キーワード（次回予定の表示）より先に判定する。
- 流れ:
  1. Gemini で `{action, type, title, start, end, location, target_event_id}` を抽出（変更・中止は直近の予定候補から対象を選ばせる）。
  2. 「次の内容で登録します。よろしいですか？ 【理事会】10月20日(火) 19:00–21:00 / 体験交流館」を
     **はい／いいえ**のクイックリプライで返す（`PendingCalendarOp` を保存）。
  3. 本人が「はい」を押したら、管理画面と同じサービス関数で作成・更新・中止し、カレンダーに反映して結果を返す。
- 安全策: 確認は依頼した本人の postback だけ有効／30分で失効／解釈できない・日時が曖昧なら登録せず聞き返す。
  LINE で作ったイベントは `draft`（配信・催促は管理画面で `open` にしてから）。監査ログに `actor=line:<member_id>`。
- グループでもメンション時は同じ流れ（F2-5 のメンション必須ルールはそのまま）。

---

## 6. 実装フェーズ（issue分割）

| フェーズ | 内容 | 依存 |
|---|---|---|
| **P5-1** | Calendar クライアント（impersonation）＋ Event 拡張（gcal_*・origin・updated_at・`cancelled`）＋ 作成/編集/中止/再開時の反映 ＋ tick での再試行 ＋ 管理画面のバッジ・ボタン ＋ セットアップスクリプト | なし |
| **P5-2** | カレンダー→システムの差分取込（syncToken・draft取込・後勝ち・削除→中止・ループ防止）＋ tick 結線 ＋ 連携状態API/表示 | P5-1 |
| **P5-3** | LINE 自然文での登録・変更・中止（役員のみ・確認必須・PendingCalendarOp） | P5-1 |

---

## 7. 残課題（ユーザー提供・手動が必要）

1. **SA 作成と権限付与**（1回だけ）: `scripts/setup_calendar_sync.sh` を実行する。
   Calendar API の有効化、`calendar-sync` SA の作成、`app-runtime` への Token Creator 付与を行う。
2. **カレンダーの共有**（1回だけ・手動）: `inawashiro.jc@gmail.com` で Google カレンダーを開き、
   設定 → 対象カレンダー → 「特定のユーザーまたはグループと共有する」に
   `calendar-sync@jci-sed-agent.iam.gserviceaccount.com` を **「予定の変更」** 権限で追加する。
3. **Cloud Run の環境変数**: `GCAL_CALENDAR_ID=inawashiro.jc@gmail.com` を両サービスに設定する（未設定なら連携は無効のまま）。
4. 既存イベントの初回反映: P5-1 デプロイ後、管理画面の「カレンダーに再同期」または `POST /api/gcal/backfill` で、
   これから開催されるイベントをまとめてカレンダーに登録する。

---

> v1.0。P5-1 から実装に入る。
