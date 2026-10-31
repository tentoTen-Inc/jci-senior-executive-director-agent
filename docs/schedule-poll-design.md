# 日程調整（LIFF）詳細設計 v1.0

最終更新: 2026-10-20
前提: 要件 `docs/requirements.md` §F3（F3-6 を追加）、カレンダー `docs/calendar-design.md`、データレイク `docs/datalake-design.md`

---

## 1. 目的・スコープ

五役会・理事会などの**開催日を候補日から決める**作業（従来は調整さん）を LINE の中で完結させる。
専務が候補日を作り、ボットがグループに案内し、各自が LINE 内の画面（LIFF）で ○△× を回答する。
集計・未回答者の催促・決定後のカレンダー登録までをシステムが担う。

| 要件 | 内容 | 優先 | フェーズ |
|---|---|---|---|
| F3-6a | 候補日・対象者・案内先グループ・締切を指定して日程調整を作る（管理API） | MUST | P8-1 |
| F3-6b | LIFF 画面で候補日ごとに ○△× とコメントを回答（全員の回答が見える） | MUST | P8-1 |
| F3-6c | グループへの案内・未回答者の催促・決定の連絡（ボットの送信） | MUST | P8-1 |
| F3-6d | 決定した候補日でイベントを作り、カレンダーへ登録（命名規則 §4.4 に従う） | MUST | P8-1 |
| F3-6e | 管理画面「日程調整」（作成・回答一覧・案内・催促・決定） | MUST | P8-2 |
| F3-6f | LINE で専務が「日程調整の状況」を聞ける | SHOULD | 後続 |

**採らなかった案**
- LINE 標準の「日程調整（LINEスケジュール）」: ボットから作成・結果取得ができず、システムと連携できない。
- グループ内のボタン（postback）で ○△×: 押すたびにグループへ投稿が流れ（12人×3日）、本人だけへの確認表示もできない。

---

## 2. 全体構成

```
管理画面 / 管理API ──作成──► SchedulePoll（Firestore schedulePolls）
      │ 案内
      ▼
LINE グループ ◄── Push（Flex: 候補日・締切・「日程を回答する」ボタン）
      │ タップ（https://liff.line.me/{LIFF_ID}?poll={poll_id}）
      ▼
LIFF 画面 /liff/poll（jci-sed-agent・公開）
  liff.init → liff.getIDToken()
      │ Authorization: Bearer <IDトークン>
      ▼
/liff/api/polls/{id}（GET 調整内容＋全員の回答 / PUT 自分の回答）
  ① IDトークンを LINE で検証（client_id = LINE ログインチャネルID）→ userId・表示名
  ② 閲覧権: 案内先グループのメンバー（LINE API で確認）か、対象会員として連携済み
  ③ 回答を PollResponse（Firestore pollResponses）に保存
      │
      ▼
管理API: 回答一覧（会員×候補日）・未回答者 → 催促 / 決定 → Event 作成 → カレンダー
```

- LIFF アプリは **Messaging API チャネルと同じプロバイダーの LINE ログインチャネル**に作る
  （同じプロバイダーなら userId が一致し、連携済み会員と突き合わせられる）。
- 画面は依存の少ない単体の HTML＋JS（`app/static/liff_poll.html`）。LIFF SDK は LINE の CDN から読む。
  管理画面（React, IAP 配下）とは別物で、公開サービスで配る。

---

## 3. データモデル

### 3.1 SchedulePoll（`schedulePolls/{poll_id}`）
| 項目 | 型 | 説明 |
|---|---|---|
| `poll_id` | str | `poll_` + 乱数 |
| `title` | str | 「第2回五役予定者会議」 |
| `event_type` | EventType | 決定時に作るイベントの種別 |
| `fiscal_year` | int \| None | 翌年度の会議なら 2027（カレンダーの「27」前置） |
| `location` | str \| None | 「JC会館 or オンライン」 |
| `candidates` | list[PollCandidate] | `candidate_id`・`start`・`end`（候補日時） |
| `target_scope` | TargetScope | 回答してほしい人（五役・理事など）。集計と未回答者の判定に使う |
| `group_id` | str \| None | 案内・催促・決定を送る LINE グループ。None なら連携済みの対象者に個別に送る（委員会あてでグループが無いとき・§9） |
| `deadline` | datetime \| None | 回答締切 |
| `status` | `open` \| `closed` \| `decided` | 決定・締切後は回答を受け付けない |
| `decided_candidate_id` / `event_id` | | 決定した候補と作ったイベント |
| `announced_at` / `reminded_at` | datetime \| None | 送信の記録 |

### 3.2 PollResponse（`pollResponses/{poll_id}_{line_user_id}`）
| 項目 | 型 | 説明 |
|---|---|---|
| `poll_id` / `line_user_id` | str | 1人1件（上書き） |
| `display_name` | str | LINE の表示名（IDトークンの name） |
| `member_id` | str \| None | 回答者が誰か（下記） |
| `answers` | dict[candidate_id, `ok`\|`maybe`\|`ng`] | ○△× |
| `comment` | str \| None | 「19:30 から参加可」など |

**回答者と会員の突き合わせ**
1. 会員の `line_user_id` が一致 → その会員（招待コードで連携済み）。
2. 一致しなければ、初回の回答時に画面で「あなたは」を対象者一覧から選んでもらう（自己申告）。
   - 同じ会員を別の LINE ユーザーが選ぶことはできない（先着）。管理画面には LINE の表示名も並べて出す。
   - 会員の `line_user_id` は更新しない（本人連携は招待コードの手順のまま。なりすまし対策）。

---

## 4. 処理仕様

### 4.1 公開API（`/liff/api/...`、X-Admin-Token 不要・IDトークン必須）
- IDトークンの検証: `POST https://api.line.me/oauth2/v2.1/verify`（`id_token`, `client_id`）。
  失敗・期限切れは 401（画面は `liff.login()` でやり直す）。検証結果は短時間キャッシュ。
- 閲覧権: `GET /v2/bot/group/{group_id}/member/{userId}` が 200（グループのメンバー）か、
  連携済みの対象会員。どちらでもなければ 403。グループのメンバー確認は数分キャッシュ。
- `GET /liff/api/polls/{id}`: 調整内容・候補日・対象者の名前・全員の回答（名前・○△×・コメント）・自分の回答。
- `PUT /liff/api/polls/{id}/response`: `{member_id?, answers, comment}`。`status != open` なら 409。

### 4.2 管理API（`/api/polls`、既存の管理認証）
| メソッド | パス | 内容 |
|---|---|---|
| POST | `/polls` | 作成（status=open。まだ送信しない） |
| GET | `/polls` / `/polls/{id}` | 一覧 / 詳細（回答の表・候補日ごとの ○△× 数・未回答者） |
| GET | `/polls/committees` | 委員会ごとの対象者数と送り先（グループ or 個別。§9） |
| POST | `/polls/{id}/announce` | グループへ案内（Flex）。グループが無ければ対象者に個別に |
| POST | `/polls/{id}/remind` | 未回答者の名前を挙げてグループへ催促（テキスト） |
| POST | `/polls/{id}/decide` | `{candidate_id}` で決定 → Event 作成（受付中・カレンダー登録）→ 回答を出欠に写す → グループへ決定の連絡 |
| POST | `/polls/{id}/close` | 締め切る（決定はしない） |

- すべて監査ログに残す。送信は `line_push`（データレイクにも記録）。
- 決定で作る Event: `type=event_type`、`title`、`fiscal_year`、`location`、`target_scope`、候補の日時。
  カレンダーのタイトルは命名規則どおり（例 `27【五役会】第2回五役予定者会議`）。

### 4.3 ボットの送信文
- 案内（Flex）: タイトル、対象（「五役の皆さま」等）、候補日の一覧、締切、ボタン「日程を回答する」。
- 催促: 「第2回五役予定者会議の日程調整、未回答の方: 佐川さん・國分さん（締切 10/26）」＋回答リンク。
- 決定: 「第2回五役予定者会議は 11/6(金) 19:00〜 に決まりました（JC会館 or オンライン）」。
- グループでは普段メンションされたときしか話さない。これらは**専務が管理画面で押したときだけ**送る（自動送信はしない）。

### 4.4 設定（環境変数・公開情報）
- `LIFF_ID`: 案内のリンク（`https://liff.line.me/{LIFF_ID}?poll=…`）と画面の `liff.init` に使う。
- `LINE_LOGIN_CHANNEL_ID`: IDトークン検証の `client_id`。
- 未設定なら公開APIは 503、案内は 400（「LIFF が未設定」）。

---

## 5. セキュリティ・プライバシー
- 回答の閲覧・送信は LINE ログイン（IDトークン）必須。リンクを知っているだけでは見られない。
- 閲覧できるのは案内先グループのメンバーと対象会員だけ（調整さんと同じく、参加者同士は回答が見える）。
- 保存する個人情報は LINE の userId・表示名・回答のみ。画像（プロフィール写真）は保存しない。

---

## 6. 実装フェーズ
| フェーズ | 内容 |
|---|---|
| P8-1 | モデル・リポジトリ・公開API・LIFF 画面・管理API・送信文（本設計 §3〜§4） |
| P8-2 | 管理画面「日程調整」（作成フォーム・回答の表・案内/催促/決定ボタン） |
| 後続 | LINE で状況確認（F3-6f）、締切前日の自動催促（設定で ON） |

## 7. 手動の準備（専務）
LINE Developers で、Messaging API と同じプロバイダーに LINE ログインチャネルを作り、LIFF アプリを追加する。
- エンドポイントURL `https://line.647jc.org/liff/poll`、サイズ Tall、Scope `openid` `profile`
- チャネルを「公開」にする（開発中は管理者しか使えない）
- チャネルID と LIFF ID を環境変数に設定する

## 9. 委員会あての日程調整（2026-10-25 追加）
専務の依頼:「日程調整が五役・理事会・全員しかないが、各委員会ごとに委員会を開催する。その時にも使えるように委員会を追加して。」

- 送り先（管理画面と LIFF「日程調整を作る」の両方）に「委員会」を追加し、委員会を選ぶ（組織図＝会員の所属・副理事長の担当から）。
  `target_scope = {kind: committee, value: [委員会名]}`。
- 回答できる人（対象者）: その委員会に所属する会員（兼務を含む）＋担当の副理事長（`supervised_committees`）。
  ※ 日程が決まって作る予定（Event）は従来どおり委員会あてで、出欠の対象に担当副理事長は入らない（`Member.supervised_committees` の決めごと）。
- 案内先: 専務AIが入っている（抜けていない）LINE グループのうち、名前にその委員会名を含むもの。
  無ければ `group_id = None` とし、LINE 連携済みの対象者に個別トークで送る（未連携の人には届かない）。
- 送る前の確認画面に、グループか個別か・送り先名・人数（対象 / 連携済み）を出す（「送り先名を見せてから送る」と同じ扱い）。
- 個別のとき: 案内（Flex）と決定の連絡は対象者それぞれに、催促は未回答の人だけに本人あての文（他の人の名前は並べない）。
  閲覧権は対象会員のみ（グループのメンバー確認はしない）。
- 集計・締切・決定の流れは既存と同じ。
- テスト: `tests/test_poll_committee.py`

## 決定した予定は受付中、回答は出欠に写す（2026-10-31 専務の指摘）
- これまでは決定で作る予定を下書きにしていたため、会員の「予定と出欠」に出ず、出欠もすべて未回答だった。
  日程調整で出欠はすでに分かっているので、決定と同時に**受付中**で作り、回答を出欠に写す（`schedule_poll.carry_answers`）。
- 写し方: 決定した候補日への回答が ○ → 出席、× → 欠席、△ → 未回答（決めつけず本人に答えてもらう）。
  コメントは出欠のメモ（`free_text`）に「日程調整のコメント: …」で残す。履歴の `by` は「日程調整」。
- LINE 未連携の回答（会員が分からない）は写さない。すでに出欠がある会員は上書きしない。
- 受付中にしても、出欠確認の配信ポリシーが無いので LINE の出欠確認は自動では送られない。
