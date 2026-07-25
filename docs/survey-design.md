# 例会アンケート（F7）詳細設計 v1.0

最終更新: 2026-07-26
前提: 要件 `docs/requirements.md` §F7、実データ分析 `docs/drive-analysis.md` §9.2、管理画面 `docs/dashboard-design.md`

---

## 1. 目的・スコープ

例会後アンケートの **配信は現行の Google フォームを維持**し、エージェントが
**回答取得・集計・自由記述の要約・未提出者の催促・レポート通知**を担う（要件 §F7）。

| 要件 | 内容 | 優先 | フェーズ |
|---|---|---|---|
| F7-1 | 現行 Google フォーム運用を維持し、回答を Forms API で取得・集計 | MUST | P4-1 |
| F7-2 | Likert 集計＋自由記述の Gemini による要約／分類／感情傾向 | MUST | P4-1 |
| F7-3 | 未提出者を特定し段階的に催促 | MUST | P4-2 |
| F7-4 | 集計レポート（定量＋定性）を五役・担当委員会へ自動通知 | MUST | P4-3 |
| F7-5 | 過去回との比較・経時トレンド | SHOULD | P4-3 |

**確定**: 配信基盤は Google フォームのまま（対外＝非会員は LINE 不可のため必須。対内も同じフォームを使い運用を変えない）。
エージェントは「フォームを置き換える」のではなく「フォームに接続する」。

---

## 2. 認証・取込経路（実証済み）

```
[Cloud Run 実行SA app-runtime] ──impersonate──► [drive-reader@…] ──► Forms API
                                                     ▲
                                    対象フォームを drive-reader に共有（Drive取込と同じ運用）
```

- 既存 `app/drive.py` と同じ**鍵レス impersonation**。スコープは
  `forms.body.readonly` と `forms.responses.readonly`（読み取り専用）。
- `forms.googleapis.com` は有効化済み（`docs/drive-analysis.md` §9）。
- 本設計時に実フォーム（`2604_対内アンケート`）で構造・回答の取得を実機確認済み。

### 2.1 Forms API から得られる形（実測）
```
GET /v1/forms/{formId}
  info.title / info.description
  items[]: pageBreak・説明テキスト（questionItem なし＝セクション見出し）
           questionItem.question.questionId
             + scaleQuestion{low, high} | textQuestion{paragraph} | choiceQuestion{type, options[]}

GET /v1/forms/{formId}/responses
  responses[]: responseId / createTime / lastSubmittedTime / respondentEmail
               answers{ <questionId>: { textAnswers.answers[].value } }
```
> Likert の値も**文字列**（"3"）で返る。数値化は取込側で行う。
> `respondentEmail` は現行フォームで収集されている（未提出者特定に使える。§4）。

### 2.2 現行フォームの構造（`2604_対内アンケート` 実測）
- 識別: `01.氏名`(text) / `02.所属等`(RADIO) / `03.役職名`(RADIO)
- セクション: 目的達成度について / 事業運営について / メンバー自身の意識について
- 設問: `Q1..Q9` = **5段階スケール（low=1, high=5）と段落テキストの混在**

→ 取込は**この構造に依存しない**汎用パーサにする（設問数・順序・文言が年度ごとに変わる前提）。
識別項目は「タイトルに『氏名』を含む text 設問」を氏名として扱い、無ければ `respondentEmail` を使う。

---

## 3. データモデル

### 3.1 新規 `Survey`
```
surveys/{surveyId}
  survey_id, form_id, title
  kind: "internal" | "external"      # 対内 / 対外
  event_id: str | None               # 対象の例会イベント
  questions: [SurveyQuestion]
  responses: [SurveyResponse]
  digest: SurveyDigest | None        # ★Gemini生成物（原データと分離）
  synced_at: datetime | None
  reminder_count, reminded_at        # P4-2
```
```
SurveyQuestion: question_id, title, type("scale"|"text"|"choice"), section,
                scale_low, scale_high, options[]
SurveyResponse: response_id, submitted_at, respondent_email, respondent_name,
                answers: { question_id: str }
SurveyDigest:   summary, themes[{label, count, examples[]}], sentiment(positive/neutral/negative の件数),
                improvements[], model, generated_at
```

### 3.2 集計（`app/survey_agg.py`・保存せず都度算出）
- scale 設問ごとに: 回答数 / 平均 / 分布（1〜5の件数）
- text 設問ごとに: 自由記述の一覧（Gemini 要約の入力）
- 全体: 回答者数、対内なら**会員照合済み人数**

---

## 4. 未提出者の特定（P4-2, F7-3）

現行フォームは**回答者メールを収集**し、氏名も設問にある。よって次の優先順で会員に突き合わせる。

1. `respondentEmail` が `Member.email` と一致
2. `01.氏名` の値が `Member.name` と一致（**空白を除去して比較**。フォーム説明文にも「スペースを入れずご記入ください」とある）
3. どちらも一致しない回答は「照合不能」として集計には含めるが催促判定には使わない

未提出者 = 対象範囲（`TargetScope`。既定は例会イベントの対象者）− 照合済み回答者。
催促は**既存の配信基盤とガードレール**（静音・レート・キルスイッチ・sanity_check）をそのまま通す。

> 対外アンケート（非会員）は照合対象を持たないため、**催促は対内のみ**。

---

## 5. API（`/api/*`）

| メソッド/パス | 用途 | フェーズ |
|---|---|---|
| POST `/api/surveys/import-form` | フォームIDから取込（`dry_run` 可）。同一 form_id は更新 | P4-1 |
| GET `/api/surveys` | 一覧 | P4-1 |
| GET `/api/surveys/{id}` | 詳細（設問・集計・要約） | P4-1 |
| POST `/api/surveys/{id}/sync` | 回答を再取得 | P4-1 |
| POST `/api/surveys/{id}/digest` | 自由記述の Gemini 要約・分類・感情傾向 | P4-1 |
| GET `/api/surveys/{id}/pending` | 未提出者一覧（対内のみ） | P4-2 |
| POST `/api/surveys/{id}/remind` | 未提出者へ催促 | P4-2 |
| POST `/api/surveys/{id}/report` | レポート生成＋五役へ通知 | P4-3 |
| GET `/api/surveys/trends` | 過去回比較（平均スコアの推移） | P4-3 |

監査: `survey.import` / `survey.digest` / `survey.remind` / `survey.report`。
Gemini のトークンは `InferenceLog(kind="survey_digest")` に記録しコストKPIへ合算。

---

## 6. 画面（`/app/surveys`）

```
┌─ アンケート ─────────────────────────────────┐
│ [フォームIDを入力] [取込] ※drive-readerに共有が必要      │
├──────────────────────────────────────────────┤
│ 2026年度 4月例会アンケート（対内）  回答 7 / 対象 21     │
│   Q1 まちづくりの意義  平均 4.3 ▇▇▇▇▁                  │
│   Q4 人づくり・地域づくり 平均 3.9 ▇▇▇▁▁              │
│   [AI要約] 好意的7割。改善点は集客と会場動線。          │
│   [未提出者 14名に催促] [レポートを五役へ通知]          │
└──────────────────────────────────────────────┘
```
- 定量（平均・分布）と **AI要約は「参考」表記**で分離（議案・対外連絡と同じ方針）。

---

## 7. 実装フェーズ

| フェーズ | 内容 |
|---|---|
| **P4-1**（実装済み） | `Survey` モデル＋Forms取込（汎用パーサ）＋Likert集計＋自由記述のGemini要約＋API/画面 |
| **P4-2**（実装済み） | 会員照合＋未提出者一覧＋催促（既存ガードレール流用） |
| **P4-3** | レポート生成＋五役への自動通知＋過去回比較トレンド |

---

## 8. 運用上の注意

- **対象フォームを `drive-reader@jci-sed-agent.iam.gserviceaccount.com` に共有**する（各回のフォームごと）。
- フォームの設問文・順序は年度ごとに変わる前提。**設問IDで紐づけ**、文言変更に耐える。
- 氏名の突き合わせは表記ゆれ（空白）に弱いため、照合不能分は画面に件数を出して人が確認できるようにする。
- 回答は個人が特定できるデータ。**管理サービス（IAP保護）内でのみ表示**し、LINE側には出さない。

---

> v1.0。P4-1 から実装に入る。
