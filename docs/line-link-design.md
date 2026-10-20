# LINE 連携（LIFF 申請＋専務承認）・予約投稿 詳細設計 v1.0

最終更新: 2026-10-21
前提: 要件 `docs/requirements.md` §F2（F2-7 を追加）、日程調整 `docs/schedule-poll-design.md`（同じ LIFF アプリを使う）

## 1. 目的
会員の LINE 連携を、招待コードを1人ずつ配らずに進める。新入会員の追加時も同じ手順で済むようにする。
招待コード方式（`app/invite.py`）はそのまま残し、併用できる。

## 2. 流れ
```
グループに「LINE 連携のお願い」（Flex・予約投稿）
  └ ボタン → https://liff.line.me/{LIFF_ID}?link=1（/liff/poll の連携モード）
       LINE ログイン → 未連携の会員一覧から自分の名前を選んで申請（友だち未追加なら追加を促す）
         └ 専務に LINE で通知（「承認する」「却下」ボタン）／管理画面「会員管理」でも承認できる
              承認 → Member.line_user_id に紐づけ → 本人に「連携が完了しました」
```
- 日程調整で名前を選んで回答した人（自己申告）も「連携候補」として管理画面に並び、承認だけで連携できる。

## 3. データ
- `lineLinkRequests/{line_user_id}`: `member_id`・`display_name`（LINE 名）・`status`（pending/approved/rejected）・申請/判断の日時と判断者。
- `scheduledPosts/{post_id}`: `group_id`・`kind`（`link_invite` / `poll_announce`）・`send_at`・`status`（pending/sent/failed/cancelled）。

## 4. ルール
- 申請画面を使えるのは、ボットが入っているグループのメンバー（LINE API で確認）か連携済みの会員だけ。
- 選べるのは未連携の有効な会員だけ。同じ会員への申請が承認待ちのとき、別の LINE からは申請できない。
- 承認できるのは専務理事（LINE のボタン）と管理画面の利用者。1つの LINE を2人に、1人に2つの LINE を紐づけない。
- 予約投稿は tick（毎時 0 分、8〜21時）が送る。緊急停止中は送らずに保留。失敗は再送しない（`failed` と理由を残す）。

## 5. API
| 区分 | メソッド・パス | 内容 |
|---|---|---|
| LIFF | GET/POST `/liff/api/link` | 状態と選べる会員 / 申請 |
| 管理 | GET `/api/line-link` | 承認待ちの申請と、日程調整からの連携候補 |
| 管理 | POST `/api/line-link/{line_user_id}/approve`・`/reject` | 承認（`member_id` 指定可）・却下 |
| 管理 | GET/POST `/api/scheduled-posts`、POST `/api/scheduled-posts/{id}/cancel` | 予約投稿の一覧・作成・取消 |
