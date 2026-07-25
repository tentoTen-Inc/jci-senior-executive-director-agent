# 管理ダッシュボード 詳細設計書（ドラフト v0.1）

最終更新: 2026-06-21
対象LOM: 一般社団法人 猪苗代青年会議所 / GCP `jci-sed-agent`
前提: 要件 `docs/requirements.md`、MVP `docs/mvp-design.md`、運用 `docs/runbook.md`

---

## 1. 目的・スコープ

専務理事・事務局が「会の運営状況を一望し、専務エージェントを管理・監督する」ための司令塔。
現状の最小ダッシュボード（`/dashboard` の単一HTML）を、React SPA としてリッチ化する。

### 確定事項（壁打ちの結論）
- **利用者**: 専務理事・事務局のみ（単一ロール＝フル管理）。
- **認証**: Cloud IAP（Google Workspace）。許可リストで専務・事務局のみ。操作者メールを監査記録。
- **技術**: React + TypeScript の SPA。Cloud Run で配信。
- **対象4領域**: ①Google認証＋会員管理 ②ホーム＋出欠可視化 ③議案ライフサイクル/進捗 ④エージェントKPI。
- **フェーズ**: Phase1（認証＋会員＋ホーム＋出欠）→ Phase2（議案＋KPI）。

### Out of Scope（当面）
- 委員長・一般会員向けの閲覧画面（ロール拡張は将来）。
- アンケート（F7）の専用画面（Phase3以降、後続機能と同時）。
  ※ 対外連絡（F5）の画面は Phase3 で追加済み（`/app/notices`・`docs/external-notice-design.md`）。

---

## 2. 全体アーキテクチャ

```
[専務・事務局] ──(Google login)──► [Cloud IAP] ──► [Cloud Run: jci-sed-agent]
                                                      ├─ /api/*    管理API(JSON)  ← IAPヘッダで認証
                                                      ├─ /line/webhook (LINE用・IAP対象外/公開)
                                                      ├─ /tasks/*  (Scheduler OIDC)
                                                      └─ /app/*    React静的配信(SPA)
                                                            │
                                                   [Firestore] / [Vertex AI Gemini]
```

### 認証の考え方（重要）
- LINE Webhook（`/line/webhook`）と Scheduler（`/tasks/tick`）は**外部/機械**から来るため IAP をかけられない。
- 一方ダッシュボード（SPA＋管理API）は**人間（専務・事務局）専用**で IAP 向き。
- → **2サービス分離**を推奨:
  - `jci-sed-agent`（既存・公開）: `/line/webhook`, `/tasks/*`, `/health`
  - `jci-sed-admin`（新規・IAP保護）: SPA配信＋`/api/*`（管理API）
  - 両者は同じFirestore/同じコードベースを共有（起動時の環境変数 `ROLE=public|admin` でルータを出し分け）。
- これにより「公開部分は最小・管理部分はIAPで全面保護」を両立。現行の `X-Admin-Token` は管理サービス側では不要化（後方互換で当面残置可）。

> 代替案: 単一サービスのまま `/api/*` だけアプリ層で IAP ヘッダ必須にする方法もあるが、IAPはサービス単位で掛けるのが素直なため分離を推奨。最終判断は §10 の確認事項。

---

## 3. 画面設計（IA）

SPA のルーティング（`/app` 配下）:

| ルート | 画面 | 主な内容 |
|---|---|---|
| `/app` | ホーム | 専務サマリ・要対応アラート・今週の予定・エージェント稼働状態 |
| `/app/members` | 会員管理 | 会員一覧/編集・LINE連携状況・招待コード発行/配布状況・出欠率履歴 |
| `/app/events` | 出欠管理 | イベント一覧→詳細（出欠表・手動修正・催促）・出席率トレンド |
| `/app/proposals` | 議案ライフサイクル | カンバン・委員会別提出マトリクス・形式/LLMレビュー |
| `/app/notices` | 対外連絡（F5・Phase3） | 取込一覧・AI要約/告知文と原文の並置（`docs/external-notice-design.md`） |
| `/app/agent` | エージェントKPI | 業務/AI品質/運用KPI・配信ログ・コスト |
| `/app/settings` | 設定・監査 | 静音/レート/催促ポリシー・キルスイッチ・監査ログ |

### 3.1 ホーム（ワイヤー）
```
┌─ 専務ダッシュボード ───────────────────────────────┐
│ [要対応 5]  未提出議案 2 / レビュー待ち 1 / 未対応連絡 1 / 連携未 3 │
├───────────────┬───────────────────────────────┤
│ 直近イベント    │ 今週の予定                       │
│ 7月例会 7/15    │ ・7/10 総務委員会                 │
│ 回答 12/21 57%  │ ・7/12 五役会(資料締切)          │
│ [未回答に催促]  │ ・7/15 7月例会                    │
├───────────────┴───────────────────────────────┤
│ エージェント稼働: 配信成功 98% / キルスイッチ OFF [切替]      │
└─────────────────────────────────────────────────┘
```

### 3.2 議案ライフサイクル（カンバン・目玉）
ステージ列（Drive分析 §2/§11 の実運用に準拠）:
```
[エントリー] [資料提出] [専務レビュー] [五役会] [理事会上程] [審議結果] [事業実施] [報告] [検証]
   ●第1号        ●第3号       ●第2号                                              ●第N号
   総務委         人材育成      コト創り
   締切7/8        ⚠遅延        要確認2件
```
- カードに: 議案名・委員会・担当・期限・遅延フラグ・レビュー状態
- カード詳細: 形式チェック結果（必須項目欠落/全角英数）＋Gemini内容レビュー（要約・論点・要確認点）＋専務の承認/差戻し操作
- 委員会別マトリクス: 行=委員会 / 列=締切区分（エントリー/提出/配信）/ セル=状態色

### 3.3 エージェントKPI
- 上段カード: 出欠回答率（今期 vs 前期）/ 平均回答時間 / 配信成功率 / 月間コスト
- グラフ: 回答率トレンド（例会ごと）、催促回数の推移、LLMレビュー採用率
- 表: 配信ログ（検索・フィルタ）、失敗/ブロック内訳

---

## 4. データモデル（新規・拡張）

### 4.1 新規: `Proposal`（議案）★Phase2の中核
```
proposals/{proposalId}
  proposal_id: str
  lom_id: "inawashiro"
  title: str                      # 議案名
  number: str | None              # 第N号議案
  committee: str                  # 提出委員会
  owner_member_id: str            # 担当者/委員長
  event_id: str | None            # 上程先の会議体イベント
  stage: ProposalStage            # entry|submitted|sed_review|goyaku|board|decided|executing|reported|verified
  doc_type: "事業計画書"|"事業報告書"|...   # テンプレ種別
  storage_uri: str | None         # Drive/GCSの資料参照
  deadlines: { entry, submit, deliver }    # 7/5/3日前ルール
  format_check: { passed: bool, issues: [str] }     # 必須項目欠落・全角英数 等
  llm_review: { summary, points: [str], concerns: [str], reviewed_at } | None
  sed_approval: { status: "pending"|"approved"|"returned", by, at, comment } | None
  history: [{ at, stage, by }]
  status: "open"|"closed"
```
`ProposalStage`（StrEnum）= entry / submitted / sed_review / goyaku / board / decided / executing / reported / verified

### 4.2 新規: `AuditLog`（監査ログ）
```
auditLogs/{id}
  at, actor(email), action, target, detail
```
※ 現状 Cloud Logging には出しているが、UI表示用にFirestoreへも記録。

### 4.3 新規: `InferenceLog`（LLM推論ログ）
```
inferenceLogs/{id}
  log_id, at, kind("proposal_review"|...), model, target(proposal_id 等)
  input_tokens, output_tokens, cost_usd, ok, error
```
※ Gemini 呼び出しごとに1件記録し、月間コストKPI（§6）の元データにする。単価は
`app/inference.py` の `MODEL_PRICES`（環境変数 `LLM_PRICE_*_USD_PER_1M` で上書き可）。

### 4.4 拡張
- `Member`: 既存で概ね充足。UI表示用に派生指標（出欠率）はAPI集計で算出（モデルには持たせない）。
- `Settings`: 既存（kill_switch / quiet_hours / rate_limit）流用。催促ポリシー編集は `ReminderPolicy` のCRUDを追加。

---

## 5. API設計（`/api/*`、IAP保護）

既存 `/admin/*` を `/api/*` に整理・拡張（IAP配下では認証はIAPが担保、actorはIAPヘッダから取得）。

### 既存（流用・改名）
| メソッド/パス | 用途 |
|---|---|
| GET `/api/members` / POST `/api/members` | 会員一覧/upsert |
| POST `/api/members/new`, GET/PUT `/api/members/{id}` | 会員の追加・取得・編集（F10-5） |
| PUT `/api/events/{id}` | イベントの編集（F10-5） |
| POST `/api/members/{id}/invite` | 招待コード発行 |
| GET/POST `/api/events`, GET `/api/events/{id}` | イベント |
| GET `/api/events/{id}/attendances`, PUT …/{member_id} | 出欠取得/修正 |
| POST `/api/events/{id}/remind` / close、GET …/summary、…/attendances.csv | 催促/集計 |
| GET/PUT `/api/settings`、GET/POST `/api/policies`(+seed) | 設定/ポリシー |

### 新規
| メソッド/パス | 用途 |
|---|---|
| GET `/api/home` | ホーム集約（要対応件数・直近イベント・今週予定・稼働状態） |
| GET `/api/members/{id}/attendance-history` | 個人別出欠率履歴 |
| GET `/api/members/invite-status` | 連携/招待コード配布状況の一覧 |
| GET/POST `/api/proposals`, GET/PUT `/api/proposals/{id}` | 議案CRUD |
| POST `/api/proposals/{id}/format-check` | 形式チェック実行 |
| POST `/api/proposals/{id}/llm-review` | Geminiレビュー実行 |
| POST `/api/proposals/{id}/approve` / `/return` | 専務の承認/差戻し |
| GET `/api/kpi/overview` | KPIカード（回答率・成功率・コスト等） |
| GET `/api/kpi/trends` | 時系列（回答率・催促回数・採用率） |
| GET `/api/delivery-logs` | 配信ログ検索 |
| GET `/api/audit-logs` | 監査ログ |
| GET `/api/policies/{id}` PUT | 催促ポリシー編集 |

---

## 6. KPI 定義（算出式）

| KPI | 定義 | データ源 |
|---|---|---|
| 出欠回答率 | 回答者 / 対象者（イベント単位・期間平均） | attendances/events |
| 回答率改善 | 当期回答率 − 前期回答率 | 同上 |
| 平均回答時間 | (回答時刻 − 出欠依頼配信時刻) の中央値 | attendances/deliveryLogs |
| 催促回数 | reminder種別の配信数（イベント/期間） | deliveryLogs(type=reminder) |
| 配信成功率 | ok / (ok+failed) | deliveryLogs |
| ブロック率 | blocked / 全配信 | deliveryLogs |
| 資料提出遅延率 | 締切超過の議案 / 全議案 | proposals.deadlines |
| LLMレビュー採用率 | approved(無修正) / レビュー実施数 | proposals.sed_approval |
| 月間コスト | Geminiトークン×単価＋Cloud Run（概算） | 集計（トークンは推論時に記録） |
| エスカレーション数 | open件数 | escalations |

> コスト精緻化のため、Gemini呼び出し時に入出力トークンを推論ログ（§4.3 `InferenceLog`）へ記録する。
> 月間コストは当月1日以降のログを集計し、`GET /api/kpi/overview` の `cost` で返す（実装済み）。
> インフラ費は環境変数 `INFRA_MONTHLY_USD` の固定概算を加算する。

---

## 7. フロントエンド構成（React SPA）

```
web/
  src/
    main.tsx, App.tsx (router)
    api/client.ts          # fetch ラッパ(IAP配下はCookie認証、credentials:'include')
    pages/ Home, Members, Events, EventDetail, Proposals, Agent, Settings
    components/ KpiCard, TrendChart, Kanban, AttendanceTable, AlertList
    lib/ format, types
  index.html, vite.config.ts, package.json, tsconfig.json
```
- ビルドツール: **Vite**。UI: 軽量に **Tailwind**（インライン依存なし、ビルド時に同梱）。グラフ: **Recharts** か軽量SVG。すべて**バンドルに同梱**（CSP/外部CDN不使用）。
- 配信: Cloud Run の管理サービスが `web/dist` を静的配信（FastAPI StaticFiles）。`/api/*` は同一オリジン。
- ビルドは Cloud Build / Dockerfile のマルチステージ（node build → python runtime に dist をコピー）。

---

## 8. 認証（IAP）移行手順（概要）

1. （必要なら）外部HTTPS LB または Cloud Run のIAP統合を有効化。
2. OAuth同意画面・IAPを構成し、`jci-sed-admin` サービスに適用。
3. IAP の「IAP-secured Web App User」ロールを専務・事務局のGoogleアカウントに付与（許可リスト）。
4. アプリは `X-Goog-Authenticated-User-Email` を actor として監査記録（既に部分対応）。
5. 公開サービス（webhook/tick）は現状維持。管理APIから `X-Admin-Token` 依存を外す（後方互換で残置可）。

> 組織ポリシーは当プロジェクトで `allowAll` 上書き済み（`docs/runbook.md` §7）。IAP適用と整合を確認。

---

## 9. 実装フェーズと issue 分割（叩き台）

### Phase 1（基盤＋早期価値）
1. SPA基盤: Vite+React+Tailwind雛形、Cloud Run静的配信、Dockerfileマルチステージ
2. 管理サービス分離（ROLE出し分け）＋IAP適用＋`/api/*`整理、監査ログ(Firestore)
3. ホーム画面＋`GET /api/home`集約
4. 会員管理: 一覧/編集/LINE連携・招待コード状況/個人別出欠率
5. 出欠管理: イベント詳細・出欠表・手動修正/催促・出席率トレンド

### Phase 2（中核＋管制）
6. `Proposal`モデル＋議案CRUD API
7. 議案カンバンUI＋委員会別マトリクス
8. 形式チェック（必須項目/全角英数）＋Gemini内容レビュー結線（F6）
9. 専務の承認/差戻しフロー
10. エージェントKPI: 集計API＋カード/グラフ、配信ログ検索、コスト記録

---

## 10. 確定事項・残課題

### 確定（v1.0）
1. **サービス分離**: 管理用を別 Cloud Run（`jci-sed-admin`）に分離し IAP 適用。公開部（webhook/tick）は既存 `jci-sed-agent` に残す。同一コードを `ROLE=admin|public` で出し分け。
2. **議案データ入力**: **Drive 取込中心**。委員会が Drive に上げた資料をエージェントが取り込み議案カード化（手入力は補助）。
3. **UIライブラリ**: Vite + React + TypeScript + Tailwind + Recharts、すべてバンドル同梱（外部CDN不使用）。
4. **コスト記録**: Gemini 呼び出しのトークン記録は Phase2 で追加（`InferenceLog`＝§4.3 として実装済み）。
5. **既存 `/dashboard`（最小HTML）**: 新SPA移行後に廃止（廃止済み。`/app/` へ 308 リダイレクトのみ残置）。
   キルスイッチ等の管理操作は SPA `/app/settings` に移設済み。
6. **進め方**: Phase1 から issue 化して自律実装。

### 残課題（ユーザー提供・手動が必要）
- **IAP 許可リスト**: 管理サービスにアクセスできる専務・事務局の Google アカウント一覧（後日提供）。
- **IAP/OAuth 構成**: 組織管理者操作が要る場合あり。コード側は `X-Goog-Authenticated-User-Email` 対応まで実装し、IAP の実適用は手動手順（`docs/runbook.md` に追記）として残す。

---

> v1.0 確定。Phase1 から issue 化して実装に入る。IAP の実適用のみ手動作業として残置。
