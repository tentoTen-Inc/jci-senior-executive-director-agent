# MVP詳細設計書 — 出欠自動化（ハイブリッド方式）

最終更新: 2026-06-20
対象LOM: 一般社団法人 猪苗代青年会議所（JCI猪苗代, 約21名）
前提: 要件定義 `docs/requirements.md` / 資料分析 `docs/drive-analysis.md`
インフラ: GCP `jci-sed-agent`（APIは有効化済み、Drive参照SA `drive-reader@…` 構築済み）

---

## 1. MVPのゴールとスコープ

### ゴール
「例会・理事会の**出欠確認と未回答者への自動催促**をLINEで完結させ、集計を自動化する」。専務理事の手作業（フォーム作成・回答督促・名寄せ・集計）をゼロにする。

### In Scope（MVP）
1. 会員名簿のDB一元化（xlsxから正規化インポート）
2. LINE友だち追加→**個別招待コードで本人紐付け**
3. イベント（例会/理事会等）の登録・対象者設定
4. **出欠確認のLINE配信＋回答取得**（出席/Web出席/欠席＋遅刻早退・欠席理由）
5. **リアルタイム集計**（出席率・未回答者一覧）
6. **自動催促**（締切基準の逓増リマインド）
7. **全自動配信のガードレール**（静音時間・レート制限・サニティ・キルスイッチ・冪等・監査ログ）
8. **最小限の管理画面**（イベント/出欠/配信ログ/設定）

### Out of Scope（MVP後）
- 会議資料の提出管理・LLMレビュー（F6）
- 対外連絡の伝達（F5）
- アンケート集計（F7、※対外はフォーム維持の方針）
- 自然言語問い合わせ応答（F8、※MVPでは簡易FAQのみ任意）

> ハイブリッド方針：**出欠＝LINEネイティブ**（本MVPの中心）。アンケート（特に対外）はフォーム維持で後続フェーズ。

---

## 2. アーキテクチャ（MVP構成）

```
                         ┌──────────────────────────┐
   LINEメンバー ──────►  │ Cloud Run: line-webhook   │  ← Webhook(署名検証)
        ▲   push         │  - 友だち追加/本人確認     │
        │                │  - 出欠回答の対話処理      │
        │                └─────────┬────────────────┘
        │                          │ (Firestore R/W)
        │                          ▼
        │                ┌──────────────────────────┐
        │                │ Firestore (Native)        │
        │                │ members/events/attendances│
        │                │ deliveryJobs/Logs/policies│
        │                └─────────┬────────────────┘
        │                          ▲
        │ (LINE Push API)          │
   ┌────┴─────────────────┐        │
   │ Cloud Run: worker    │◄───────┤ enqueue (Cloud Tasks)
   │  - 配信ジョブ実行     │        │
   │  - ガードレール適用   │        │
   └────▲─────────────────┘        │
        │ create tasks              │
   ┌────┴─────────────────┐        │
   │ Cloud Run: core-api  │────────┘ (締切判定→ジョブ生成)
   │  - 管理API/画面(IAP)  │
   │  - 名簿インポート     │
   └────▲─────────────────┘
        │ HTTP(OIDC)
   ┌────┴─────────────┐   ┌──────────────────┐
   │ Cloud Scheduler  │   │ Secret Manager   │ LINE secret/token
   │ (毎時tick)        │   │ Drive SA(将来)    │
   └──────────────────┘   └──────────────────┘
```

| コンポーネント | 役割 | 実体 |
|---|---|---|
| `line-webhook` | LINEイベント受信・即時ack・対話処理 | Cloud Run |
| `core-api` | 管理画面/API・締切判定・名簿インポート | Cloud Run（IAP保護） |
| `worker` | Cloud Tasksから配信ジョブ実行・ガードレール適用 | Cloud Run |
| データ | members/events/attendances 他 | Firestore (Native) |
| キュー | 冪等な配信ジョブ | Cloud Tasks |
| 定期実行 | 毎時の締切判定・催促対象抽出 | Cloud Scheduler |
| シークレット | LINEチャネルsecret/access token | Secret Manager |

> MVPでは `line-webhook`/`core-api`/`worker` を**1つのCloud Runサービス内のルーティング**で始め、負荷・権限分離が必要になったら分割してよい（21名規模なら単一サービスで十分）。

### サービスID（IAM）
- Cloud Run実行SA: `app-runtime@jci-sed-agent.iam.gserviceaccount.com`（新規）
  - 付与ロール: `roles/datastore.user`（Firestore）, `roles/cloudtasks.enqueuer`, `roles/secretmanager.secretAccessor`, `roles/aiplatform.user`（Gemini, 任意）
- 既存 `drive-reader@…` は後続フェーズ（資料/フォーム連携）で利用。

---

## 3. データモデル（Firestore コレクション）

```
members/{memberId}
  lomId: "inawashiro"
  name, kana
  memberType: "regular"|"external_auditor"|"office"|"ob"|"support"   # 配信対象の絞り込みに使用
  status: "active"|"inactive"
  committee: "コト創り委員会"|"総務委員会"|"組織運営室"|null
  committeeRole: "委員長"|"副委員長"|"委員"|null
  officerRole: "理事長"|"直前理事長"|"副理事長"|"専務理事"|"監事"|"事務局長"|"事務局員"|null
  secondments: [{org:"日本JC", role:"会員発掘会議 委員"}]   # 出向先
  contact: { mobile, email, ... }                          # 個人情報・最小限
  lineUserId: "U...."|null
  linkedAt: timestamp|null

inviteCodes/{code}            # 本人確認用（使い切り）
  memberId, expiresAt, usedAt|null

events/{eventId}
  type: "例会"|"理事会"|"五役会"|"委員会"|"総会"|"行事"|"対外"|"個別"   # 旧「イベント」は「行事」
  title, datetimeStart, datetimeEnd, location
  targetScope: {kind:"all"|"committee"|"officer"|"custom", value:[...]}
  attendanceDeadline: timestamp     # 出欠締切
  materialDeadline: timestamp|null  # 資料提出締切(後続F6)
  deliveryAt: timestamp|null        # 資料配信(後続F6)
  reminderPolicyId: "rp_例会_default"
  status: "draft"|"open"|"closed"

attendances/{eventId}_{memberId}
  eventId, memberId
  status: "出席"|"Web出席"|"欠席"|"委任"|"未回答"
  lateLeave: "全日程参加"|"遅刻"|"早退"|"遅刻早退"|null
  absenceReasons: ["体調不良"|"家庭の事情"|"仕事"|"冠婚葬祭"]|[]
  freeText: string|null
  respondedAt: timestamp|null
  history: [{at, status, by}]

reminderPolicies/{policyId}
  eventType, stages: [{ offset:"-7d@24:00", audience:"all", template:"req" },
                      { offset:"-3d@19:00", audience:"unanswered", template:"remind" },
                      { offset:"-1d@19:00", audience:"unanswered", template:"final" }]

deliveryJobs/{jobId}
  type:"attendance_request"|"reminder", eventId, stage
  targets:[memberId...], templateId, payloadHash
  scheduledAt, status:"queued"|"sent"|"halted", idempotencyKey

deliveryLogs/{logId}
  jobId, memberId, content, result:"ok"|"blocked"|"failed", reason, sentAt

settings/global
  killSwitch: false
  quietHours: {start:"21:00", end:"08:00", tz:"Asia/Tokyo"}
  rateLimit: {perMemberPerDay: 3, globalPerMin: 30}

auditLogs/{id}  # 全自動配信・設定変更・LLM判断の証跡
  at, actor:"system"|email, action, target, detail
```

> 個人情報は `members.contact` に最小限のみ保持。退会/OBは `status`/`memberType` で配信対象から除外。

---

## 4. LINE 会話フロー

### 4.1 友だち追加 → 本人確認（招待コード）
```
[友だち追加(follow event)]
  → 歓迎メッセージ＋「招待コードを入力してください」
[ユーザー: コード入力]
  → inviteCodes照合（有効期限/未使用）
     OK → members.lineUserId 紐付け, usedAt記録
        → 「○○さん、登録が完了しました」＋リッチメニュー表示
     NG → 「コードが無効です。事務局へご確認ください」(リトライ可, 3回でロック→エスカレーション)
```
- 招待コードは管理画面で会員ごとに発行（QR/URL `https://line.me/R/...?code=XXXX` も生成）。
- なりすまし対策: 使い切り＋有効期限＋失敗回数ロック。

### 4.2 出欠回答（LINEネイティブ・クイックリプライ）
```
[出欠依頼 push]
  Flexメッセージ: イベント名/日時/場所 ＋ ボタン[出席][Web出席][欠席]
[出席/Web出席 タップ]
  → 「遅刻・早退の予定は？」 [全日程参加][遅刻][早退][遅刻早退]
  → 記録 → 「出席で受け付けました。ありがとうございます！」
[欠席 タップ]
  → 「差し支えなければ理由を」 [体調不良][家庭の事情][仕事][冠婚葬祭][その他]
  → (任意)自由記述 → 記録 → 「欠席で承りました」
```
- postbackデータに `eventId`/選択値を載せ、状態をattendancesに即時書き込み。
- 「出欠を変更」はリッチメニュー or 同イベントの再タップで履歴付き更新。

### 4.3 リッチメニュー（常時導線）
`[出欠を回答]` `[次回の予定]` `[自分の出欠状況]` `[事務局に連絡]`
- MVPでは前3つを実装、`事務局に連絡`は専務へ取次（メール/管理画面通知）。

### 4.4 自動催促（受信側の見え方）
- 未回答者にのみ段階的に届く。静音時間中は翌朝にまとめて送信。
- 「【リマインド】○月例会の出欠が未回答です。3日前までにご回答を」＋ボタン。

---

## 5. スケジューラ＆催促ロジック

```
Cloud Scheduler（毎時 8:00-21:00 JST に tick）→ core-api /tasks/tick (OIDC認証)
  tick():
    for ev in events(status=open):
      for stage in policy(ev).stages:
        fire_at = ev.deadline + stage.offset
        if now >= fire_at and not already_fired(ev, stage):
          targets = resolve_audience(ev, stage.audience)   # all / unanswered
          enqueue_delivery(ev, stage, targets)             # Cloud Tasks
```
- **催促ポリシー初期値（例会, `drive-analysis.md`由来）**
  | stage | タイミング | 対象 | 文面 |
  |---|---|---|---|
  | 依頼 | 締切7日前 | 全対象 | 出欠依頼 |
  | リマインド | 締切3日前 | 未回答 | 督促(やわらかめ) |
  | 最終 | 締切前日19:00 | 未回答 | 最終督促 |
  | 当日朝(任意) | 当日8:00 | 出席者 | 開催リマインド |
- **理事会の資料提出/配信**（後続F6）も同じ枠組みで `materialDeadline` 基準に拡張可能（7/5/3日前）。

---

## 6. ガードレール実装（全自動配信の安全機構）

| 機構 | 実装 |
|---|---|
| 冪等 | Cloud Tasksの`task name = sha1(eventId+memberId+stage)`で重複排除。`deliveryJobs.idempotencyKey`でも二重防止 |
| 静音時間 | worker送信前にJST判定。21:00-8:00ならタスクを翌8:00へ`schedule_time`再設定 |
| レート制限 | 会員ごと `perMemberPerDay`（既定3）超過はスキップ＋ログ。全体 `globalPerMin` トークンバケット |
| サニティチェック | enqueue前に「対象人数 ≤ 名簿総数」「本文非空」「プレースホルダ未置換なし」を検証。逸脱で`status=halted`＋専務へ通知 |
| キルスイッチ | 全送信直前に `settings/global.killSwitch` を確認。ON中はskip＋ログ。管理画面トグル＋（任意）LINEからも停止可 |
| 監査ログ | 送信/設定変更/停止を `auditLogs` と Cloud Logging へ二重記録 |
| 失敗処理 | LINE 4xx/ブロックは`deliveryLogs.result=failed`、リトライ上限後に管理画面で可視化 |

---

## 7. 名簿インポート（初期データ投入）

```
xlsx(会員名簿カード型) → core-api /admin/import (管理者操作)
  1. Drive SAでxlsxをDL（または手動アップロード）
  2. カード型レイアウトをパースし1会員=1レコードへ正規化
  3. 組織図/役員名簿から committee/role/officerRole を付与
  4. memberType（regular/external_auditor/office/ob…）で分類
  5. プレビュー → 管理者確認 → Firestore upsert
  6. 会員ごとに招待コード発行（CSV/QR出力）
```
- 以後は**Firestoreが正**。xlsxは初期投入と年度更新時のみ参照。

---

## 8. 管理画面（最小）

- 認証: **IAP**（役員のGoogle Workspaceアカウントのみ許可）。ロール: 管理者/閲覧。
- 画面:
  1. **ダッシュボード**: 直近イベントの出欠状況・回答率・未回答者数
  2. **イベント**: 一覧/作成/編集（対象範囲・締切・催促ポリシー）
  3. **イベント詳細**: 出欠一覧（手動修正/委任登録）・**手動催促**・集計CSV出力
  4. **配信ログ**: 送信結果・失敗の再送・予定ジョブ
  5. **設定**: 静音時間・レート上限・催促ポリシー・**キルスイッチ**
  6. **名簿**: 会員CRUD・招待コード発行/再発行
- MVPはサーバサイドレンダリング（FastAPI + Jinja/HTMX）でシンプルに。

---

## 9. 技術スタック（推奨）

| 層 | 採用（推奨） | 備考 |
|---|---|---|
| 言語/FW | **Python 3.12 + FastAPI** | LINE SDK・Google API・Vertex AI SDKと相性良。Node/TS希望なら可 |
| LINE | `line-bot-sdk` | Webhook署名検証・Flex/QuickReply・Push |
| DB | Firestore (Native) | サーバレス・低運用 |
| キュー/定期 | Cloud Tasks / Cloud Scheduler | 冪等配信・締切tick |
| 認証(管理) | IAP | 役員のWorkspaceアカウント |
| LLM | Vertex AI Gemini | MVPでは出欠自然文解釈に任意利用 |
| IaC | Terraform（推奨）or gcloudスクリプト | 再現性 |
| CI/CD | Cloud Build → Cloud Run | mainマージでデプロイ |

> 言語は**Python/FastAPIを既定**とするが、確定前にユーザー意向を確認可（Node/TypeScriptも可能）。

### リポジトリ構成（案）
```
/app
  /webhook      # LINEイベント処理
  /api          # 管理API
  /worker       # 配信ジョブ実行
  /domain       # members/events/attendances ロジック
  /infra        # firestore/tasks/secret クライアント
  /templates    # 管理画面(Jinja) + LINEメッセージテンプレ
/scripts        # 名簿インポート等
/infra/terraform
/docs           # 本設計書群
```

---

## 10. シーケンス（出欠依頼〜催促〜集計）

```
管理者: イベント作成(締切設定) ─► core-api ─► events(open)
Scheduler(7日前) ─► tick ─► enqueue(依頼, 全対象) ─► Tasks
Tasks ─► worker ─►(ガードレール)─► LINE Push(Flex 出欠ボタン) ─► 会員
会員: [出席]タップ ─► webhook ─► attendances更新 ─► 確認返信
Scheduler(3日前) ─► tick ─► enqueue(リマインド, 未回答のみ) ─► …
管理者: 画面で出席率/未回答者を確認、CSV出力、必要なら手動催促
締切到来 ─► events.status=closed ─► 五役へ集計サマリ通知
```

---

## 11. 構築マイルストーン（MVP）

| # | 内容 | 主な完了条件 |
|---|---|---|
| M0 | 基盤整備 | runtime SA作成・Firestore初期化・Secret登録・Cloud Run雛形・**LINE Webhook疎通(オウム返し)** |
| M1 | 名簿＋本人確認 | xlsx正規化インポート・招待コード発行・友だち追加紐付け完了 |
| M2 | イベント＋出欠 | イベントCRUD・出欠Push/回答取得・リアルタイム集計 |
| M3 | 催促＋ガードレール | Scheduler/Tasks・逓増催促・静音/レート/サニティ/キル/冪等/監査 |
| M4 | 管理画面 | IAP保護・出欠状況/配信ログ/設定/名簿 |
| M5 | 仕上げ | 集計サマリ自動通知・簡易FAQ(任意)・E2Eテスト・本番投入 |

> まずは **M0（LINE Webhook疎通PoC）** から着手するのが安全。

---

## 12. 未決・確認事項（MVP着手前）
1. **バックエンド言語**: Python/FastAPI で進めてよいか（Node/TS希望の有無）。
2. **静音時間の確定値**: 既定 21:00–8:00 でよいか。
3. **管理画面の認証範囲**: IAPで許可する役員アカウント（Workspaceドメイン/個別）。
4. **招待コードの初期配布手段**: 既存連絡網/総会/紙のどれで配るか。
5. **LINE公式アカウントのプラン/Push上限**: フリープランの月間Push上限（21名×月数回なら問題ない見込みだが要確認）。
6. **チャネルアクセストークン**の発行（Push送信に必須・現状 `.env` 未設定）。
