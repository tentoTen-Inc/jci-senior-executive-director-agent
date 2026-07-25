# 対外連絡（F5）詳細設計書 v1.0

最終更新: 2026-07-25
対象LOM: 一般社団法人 猪苗代青年会議所 / GCP `jci-sed-agent`
前提: 要件 `docs/requirements.md` §F5、管理画面 `docs/dashboard-design.md`、運用 `docs/runbook.md`

---

## 1. 目的・スコープ

ブロック協議会・地区協議会・全国等からの**対外連絡を漏れなく適切な対象へ伝達する**（要件 §F5、Phase3 の中核）。
専務が「受信メールを読んで、対象を判断して、LINEで流し直す」手作業を置き換える。

| 要件 | 内容 | 優先 | 実装フェーズ |
|---|---|---|---|
| F5-1 | 専用Gmailアドレスへの転送を自動取込（本文/添付PDF解析）。管理画面投入は補助 | MUST | P3-2（補助はP3-1） |
| F5-2 | Gemini で要約・整形し、メンバー向け告知文を生成 | MUST | P3-1 |
| F5-3 | 対象者・期限・必要アクションを抽出しタスク化 | SHOULD | P3-1(抽出)／P3-4(タスク) |
| F5-4 | 適切な対象（全員/委員会/出向者）へ自動配信 | MUST | P3-3 |
| F5-5 | 対応状況を追跡し未対応者を催促 | SHOULD | P3-4 |
| F5-6 | 原文（出典）への参照を残し、要約と原文を切り分けて提示 | MUST | P3-1 |

### 設計の芯
1. **原文は不変**（`body_text` / 添付）。生成物（要約・告知文）は別フィールドに置き、画面でも分けて表示する（F5-6・誤要約対策）。
2. **配信はガードレール既存実装を流用**（静音時間・レート上限・キルスイッチ・`sanity_check`）。対外連絡専用の配信経路は作らない。
3. **取込は冪等**。Gmail message id / 手動投入のハッシュを `source_ref` にして重複作成しない。
4. **LLMは助言**。配信前に専務が対象と文面を確認して発火する（議案の承認フローと同じ思想）。

---

## 2. 取込経路（F5-1）

```
[ブロック協議会等] ──メール──► [LOMの受信箱] ──転送ルール──► [専用Gmailアドレス]
                                                                    │
                                            (A) Gmail API polling ◄─┘
                                                    │
[専務] ──貼り付け/PDF投入──► (B) POST /api/notices ──► ExternalNotice(status=new)
```

- **(A) 主経路 = Gmail API ポーリング**。既存 `POST /tasks/tick`（Cloud Scheduler・毎時8–21時JST）に相乗りし、
  `users.messages.list?q=label:<ラベル> newer_than:2d` で差分取得 → 既知 `source_ref` はスキップ。
  Pub-Sub push（`users.watch`）は将来の低遅延化オプション。**まずポーリングで十分**（対外連絡は日次〜週次）。
- **(B) 補助経路 = 管理画面から本文貼り付け／テキスト投入**。Gmail 設定が未了でも運用を開始できる。

### 認証方式（要確認事項）
専用アドレスの種類で方式が変わる。**どちらにするかは運用判断（§7）**。

| 専用アドレス | 方式 | 備考 |
|---|---|---|
| Workspace（例 `notice@10to10.co.jp`） | SA のドメイン全体委任（DWD）＋ `signJwt` で鍵レス impersonation | Drive取込（`app/drive.py`）と同じ思想。管理者操作が必要 |
| 個人Gmail（例 `inawashiro.jc@gmail.com`） | OAuth 2.0 の refresh token を Secret Manager に保管（`gmail.readonly`） | DWD 不可。初回のみ本人の同意フローが必要 |

いずれも**読み取り専用スコープ**。送信・削除はしない。

---

## 3. データモデル

### 3.1 新規: `ExternalNotice`
```
externalNotices/{noticeId}
  notice_id: str
  lom_id: "inawashiro"
  source: "gmail" | "manual"
  source_ref: str | None          # Gmail message id / 手動投入の本文ハッシュ（冪等キー）
  received_at: datetime
  from_addr: str | None
  from_name: str | None
  subject: str
  body_text: str                  # 原文（不変・プレーンテキスト）
  attachments: [NoticeAttachment] # name / mime / storage_uri / text_excerpt
  digest: NoticeDigest | None     # ★生成物（原文と切り分け）
  status: "new"|"reviewed"|"delivered"|"archived"
  delivery: NoticeDelivery | None # job_id / delivered_at / target_count（P3-3）
  history: [{at, action, by}]
```

### 3.2 `NoticeDigest`（Gemini生成物・F5-2/F5-3）
```
  summary: str                # 3行以内の要約
  announcement: str           # メンバー向け告知文（LINE配信用）
  audience_hint: str | None   # 「全員」「総務委員会」「出向者」等の推定対象
  deadline: datetime | None   # 抽出した期限
  actions: [str]              # 必要アクション（参加登録/提出/出向 等）
  model: str
  generated_at: datetime
```
> `digest` は**助言**。配信前に専務が対象・文面を確認する（画面に明記）。

### 3.3 コスト記録
Gemini 呼び出しは既存 `InferenceLog`（`docs/dashboard-design.md` §4.3）に
`kind="external_notice_digest"` で記録し、月間コストKPIに合算する。

---

## 4. API（`/api/*`、IAP保護）

| メソッド/パス | 用途 | フェーズ |
|---|---|---|
| GET `/api/notices?status=` | 一覧（受信降順） | P3-1 |
| POST `/api/notices` | 手動投入（subject/body_text/from_addr/received_at） | P3-1 |
| GET `/api/notices/{id}` | 詳細（原文＋生成物） | P3-1 |
| POST `/api/notices/{id}/digest` | Gemini要約・告知文生成（再実行可） | P3-1 |
| POST `/api/notices/{id}/archive` | 対応不要としてアーカイブ | P3-1 |
| POST `/api/notices/import-gmail` | Gmail からの取込（dry-run 可） | P3-2 |
| POST `/api/notices/{id}/deliver` | 対象を指定して配信（ガードレール適用・`force`で再配信） | P3-3 |
| GET `/api/notices/{id}/actions` / POST …/{action_id}/done | 対応状況の追跡 | P3-4 |

作成・生成・配信・アーカイブは監査ログ（`notice.create` / `notice.digest` / `notice.deliver` / `notice.archive`）に記録する。

---

## 5. 画面（`/app/notices`「対外連絡」）

```
┌─ 対外連絡 ────────────────────────────────────┐
│ [新着 2] [要約済 1] [配信済 5] [アーカイブ]              │
├──────────────────────────────────────────────┤
│ ● 2026ブロック会員大会の参加者登録について   ブロック協議会 │
│   受信 7/24  期限 8/5  対象(推定) 全員  [要約生成][配信]  │
├──────────────────────────────────────────────┤
│ 詳細                                                    │
│  ┌ 要約（AI・助言） ─────┐ ┌ 原文（出典）─────────┐ │
│  │ 要約3行 / 論点 / 期限   │ │ 差出人・件名・本文全文  │ │
│  │ 告知文（LINE配信用）    │ │ 添付: PDF名（抜粋）     │ │
│  └────────────────────────┘ └────────────────────────┘ │
└──────────────────────────────────────────────┘
```
- 左右2カラムで**生成物と原文を並置**（F5-6）。要約側には「AIの助言。最終判断は専務」を明記。
- 手動投入は「メール本文を貼り付け → 取込」の1操作。

---

## 6. 実装フェーズ（issue分割）

| フェーズ | 内容 | 依存 |
|---|---|---|
| **P3-1**（実装済み） | `ExternalNotice` モデル＋手動投入/一覧/詳細API＋Gemini要約・告知文生成（コスト記録込み）＋SPA「対外連絡」画面 | なし（Gmail設定不要で運用開始できる） |
| **P3-2** | Gmail取込（認証・ラベル差分ポーリング・添付PDFのテキスト抽出・冪等）＋tick結線 | 専用アドレスと認証方式の確定（§7） |
| **P3-3**（実装済み） | 対象解決（全員/委員会/役職/任意）＋配信実行（既存ガードレール流用）＋配信履歴 | P3-1 |
| **P3-4** | アクションのタスク化・対応状況追跡・未対応者への段階催促 | P3-3 |

---

## 7. 残課題（ユーザー提供・手動が必要）

1. **専用Gmailアドレスの確定**（Workspace か 個人Gmail か）。§2 の認証方式が変わる。
2. **転送ルールの設定**（LOM受信箱 → 専用アドレス）。Gmail のフィルタ設定は手動作業。
3. **取込対象ラベル名**（例 `対外連絡`）。転送時に自動ラベル付けするフィルタを併せて作成。
4. 個人Gmail採用時は**初回OAuth同意**（refresh token を Secret Manager `gmail-oauth-refresh-token` に登録）。

---

> v1.0。P3-1 から実装に入る（Gmail 設定は P3-2 の前提として並行で確認）。
