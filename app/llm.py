"""Vertex AI Gemini による議案の内容レビュー（F6-5, docs/dashboard-design.md §6）。

- 本番は Vertex AI の Gemini を呼ぶ。
- 認証/ライブラリ未設定や呼び出し失敗時は graceful degrade（reviewed=False を返す）。
- テストでは `generate_review` をモックする。
- 消費トークンは応答の usage_metadata から取得し、コスト記録に使う（§6 月間コスト）。

レビューは「助言」であり最終判断は人間（専務理事）が行う（F6-8）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from pydantic import BaseModel, Field

from . import config, jpdate
from .models import InferenceUsage, LlmReview, NoticeDigest, SurveyDigest, SurveyTheme

logger = logging.getLogger("jci-agent.llm")

DEFAULT_MODEL = "gemini-2.5-pro"

_PROMPT = """あなたは青年会議所(JC)の専務理事を補佐するアシスタントです。
次の議案(事業計画書)の内容をレビューし、JSONで出力してください。
出力フィールド:
- summary: 議案の3行以内の要約
- points: 審議のポイント(論点)の配列(最大5件)
- concerns: 曖昧・矛盾・記載不足など要確認点の配列(最大5件)
助言が目的で最終判断は人間が行います。事実を断定しすぎないでください。

# 議案本文
{content}

# 出力(JSONのみ)
"""


_NOTICE_PROMPT = """あなたは青年会議所(JC)の専務理事を補佐するアシスタントです。
ブロック協議会等から届いた対外連絡を、LOM会員向けに整理してJSONで出力してください。
出力フィールド:
- summary: 連絡内容の3行以内の要約
- announcement: 会員へLINEで流す告知文(敬体・200字以内・重要な日付と場所は必ず含める)
- audience_hint: 想定する伝達対象("全員" "総務委員会" "出向者" 等。不明なら null)
- deadline: 期限や締切があれば "YYYY-MM-DD" 形式(なければ null)
- actions: 必要なアクション(参加登録/資料提出/出向 等)の配列(最大5件)
原文にない事実を追加しないでください。判断に迷う点は actions に「専務確認」として残してください。

# 対外連絡（原文）
件名: {subject}
差出人: {sender}
本文:
{body}

# 出力(JSONのみ)
"""


_INTENT_PROMPT = """会員のメッセージから、どのイベントの出欠をどう回答したいのかを推定してください。

厳守事項:
- event_id は「候補イベント」のIDから選ぶ。どれか判断できなければ null。
- status は 出席 / Web出席 / 欠席 / 委任 のいずれか。判断できなければ null。
- 推測が曖昧な場合や、出欠回答の意図が読み取れない場合は confident=false にする。

出力JSON:
- event_id: 対象イベントのID(または null)
- status: 出欠(または null)
- confident: 明確に読み取れたか(true/false)

# 候補イベント
{events}

# 会員のメッセージ
{text}

# 出力(JSONのみ)
"""

_CALENDAR_PROMPT = """あなたは青年会議所(JC)の予定管理アシスタントです。
役員のメッセージから、予定(例会・理事会など)を登録・変更・中止したいのかを読み取り、JSONで出力してください。

今日: {today}

厳守事項:
- action は create(新規登録) / update(既存の変更) / cancel(既存の中止) のいずれか。
  予定の操作ではない(出欠の回答・質問・雑談など)なら null。
- update / cancel の event_id は「既存の予定」のIDから選ぶ。
  特定できなければ null にして confident=false。
- 日時は "YYYY-MM-DDTHH:MM"(24時間)。「来週火曜」等は今日を基準に解決する。
  年が無ければ今日以降で最も近い日。
- type は 例会 / 理事会 / 五役会 / 委員会 / 総会 / 行事 / 対外 / 個別 のいずれか。
  読み取れなければ null。
  行事=事業・懇親会など、対外=福島ブロック・他LOMなど外部の会議、個別=個人の打合せ・用事。
- fiscal_year は「次年度」「来年度」「2027年度」のように予定の属する年度が明示されたときだけ西暦4桁
  (JCの年度は暦年。次年度・来年度は今日の年+1)。明示が無ければ null。
- update では変更したい項目だけを埋め、他は null。
- 必須項目(create は開始日時と 種別かタイトル)が欠ける、または解釈が曖昧なら confident=false にし、
  question に聞き返す一文(敬体)を入れる。
- メッセージに無い事実を作らない。

出力JSON:
- action: create | update | cancel | null
- event_id: 対象の既存予定ID(または null)
- type: 種別(または null)
- fiscal_year: 年度(西暦4桁の整数 または null)
- title: 予定名(または null。種別そのものなら null でよい)
- start: 開始日時(または null)
- end: 終了日時(または null)
- location: 場所(または null)
- confident: 明確に読み取れたか(true/false)
- question: 聞き返す文(不要なら null)

# 既存の予定(これから開催)
{events}

# 役員のメッセージ
{text}

# 出力(JSONのみ)
"""

_DOC_EDIT_PROMPT = """あなたは青年会議所(JC)の専務理事を補佐するアシスタントです。
理事メンバーの依頼にもとづいて、委員会の資料(Google ドキュメント)の表の項目に書く文章の案を作り、
JSONで出力してください。

厳守事項:
- 書き換える項目名は「項目の一覧」から一字一句そのまま選ぶ。
  一覧に無い項目・[読み取り専用] の項目は返さない。
- 依頼が一部の項目(例: 「スローガンを変えて」)だけなら、その項目だけを返す。
  「サマリーを更新して」「〇〇な委員会にしたい」のように全体の依頼なら、関係する項目をまとめて返す。
- 今の内容がある項目は、それを生かして書き直す。依頼と関係ない内容を消さない。
- JC の事業計画書の文体(「〜します」「〜を目指します」等の敬体、簡潔)で書く。
  スローガンは1行の短い言葉にする。
- 「〇〇・N行目・列名」は資料の中の表の1マス。依頼に関係する表だけを、
  上の行から必要な行数だけ埋める。1マスには短い語句を書く(例: 時間「19:00」、担当者「委員長」)。
- 「〇〇・N月」のような年間スケジュールのセルは、依頼に月・例会・事業の指定があるときだけ書く。
  指定が無い月は返さない(予定を推測で埋めない)。書くときは6文字以内(例: 例会、総会、〇〇事業)。
- 参考資料(昨年度の資料・理事長所信など)の方針や言葉づかいは参考にしてよいが、
  根拠のない数値・日付・固有名詞・実績を作らない。
- 依頼の内容が読み取れず案を作れないときは edits を空にし、question に聞き返す一文(敬体)を入れる。

出力JSON:
- edits: [{{field: 項目名, text: 新しい内容(改行可)}}]
- summary: 何をどう変えたかの一文(敬体)
- question: 聞き返す文(不要なら null)

# 委員会
{committee}

# 資料
{title}

# 項目の一覧(今の内容)
{fields}

# 参考資料(検索結果の抜粋)
{knowledge}

# 依頼
{request}

# 出力(JSONのみ)
"""

_SURVEY_PROMPT = """あなたは青年会議所(JC)の専務理事を補佐するアシスタントです。
例会アンケートの自由記述を分析し、JSONで出力してください。

出力フィールド:
- summary: 全体傾向の要約(3行以内)
- themes: 論点のまとまり(最大5件)。各要素は
  {{label: 分類名, count: 該当件数, examples: 代表的な記述(最大2件・原文のまま)}}
- sentiment: {{positive: 件数, neutral: 件数, negative: 件数}}(合計は記述件数に一致させる)
- improvements: 次回に向けた改善提案(最大5件・記述に根拠があるものだけ)

記述に無い事実を作らないでください。件数は必ず実際の記述数の範囲に収めてください。

# 自由記述（{count}件）
{texts}

# 出力(JSONのみ)
"""

_QA_PROMPT = """あなたは青年会議所(JC)猪苗代の事務局アシスタントです。
会員からの質問に、下の「提供情報」だけを根拠にして答えてください。

厳守事項:
- 提供情報に無いことは推測で答えない。分からない場合は grounded=false にする。
- 他の会員の個人情報(連絡先・出欠・個別の対応状況)は答えず、事務局へ案内する。
- 事務局や専務の判断・手続きが必要な依頼は needs_human=true にする。
- 「LINEで共有された資料・やり取り」を根拠にしたら、その文の末尾に出典番号（例: [1]）を付ける。
  資料は案や会議資料のことがあるため、確定か分からない内容は「資料によると」と書く。
- answer は敬体・300字以内。日時や場所は提供情報のとおり正確に書く。

出力JSON:
- answer: 会員への返信文
- grounded: 提供情報だけで答えられたか(true/false)
- needs_human: 人(専務・事務局)の対応が必要か(true/false)

# 提供情報
{context}

# 直近のやり取り
{history}

# 会員の質問
{question}

# 出力(JSONのみ)
"""


class Generation(BaseModel):
    """LLM応答の生テキストと消費トークン。"""

    text: str
    input_tokens: int = 0
    output_tokens: int = 0


class ReviewOutcome(BaseModel):
    """レビュー結果とコスト記録用の使用量。"""

    review: LlmReview
    usage: InferenceUsage


FLASH = "gemini-2.5-flash"
PRO = "gemini-2.5-pro"

#: AI に任せるタスクごとのモデル選定（コスト・速さ・品質のバランスで選ぶ）。
#: - 会員とのやり取り（相手が返事を待っている）: Flash。速く安い。要点を答える用途で十分な品質。
#:   Pro は回答前に長く考えるため 1 回 20〜30 秒かかっていた（2026-10-12 本番計測）。
#: - 腰を据えて読む分析（待たせない非同期処理・件数も少ない）: Pro。品質を優先。
#: タスクを追加したらここに理由とともに登録する。個別に変えるときは環境変数
#: GEMINI_MODEL_<タスク名の大文字>（例: GEMINI_MODEL_MEMBER_QA=gemini-2.5-pro）で上書きする。
TASK_MODELS: dict[str, str] = {
    "member_qa": FLASH,  # 会員の自由文の質問への回答（LINE で返事を待つ）
    "attendance_intent": FLASH,  # 自然文からの出欠の読み取り（LINE で返事を待つ・単純な抽出）
    "calendar_intent": FLASH,  # 自然文からの予定操作の読み取り（LINE で返事を待つ）
    "proposal_review": PRO,  # 議案の内容レビュー（論点・矛盾の指摘。品質重視・非同期）
    "external_notice_digest": PRO,  # 対外連絡の要約と告知文（誤要約を避けたい・非同期）
    "survey_digest": PRO,  # アンケート自由記述の分析（傾向の読み取り・非同期・件数少）
    # 委員会資料の案（LINE で返事を待つので Flash。文章の質も要るため少しだけ考えさせる・件数少）
    "doc_edit": FLASH,
}


#: タスクごとの「考える時間」の上限（トークン）。None はモデルの既定（Pro は常に考える）。
#: Flash は既定だと答える前に長く考え、会員の質問 1 件で 7,800 トークン・37 秒かかった
#: （上限0で 1.8 秒・回答の質は同等。2026-10-12 計測）。会員とのやり取りは速さ優先で抑える。
TASK_THINKING_BUDGET: dict[str, int | None] = {
    "member_qa": 0,  # 材料に書いてあることを答える。考えなくても質は同等だった
    "attendance_intent": 0,  # 出欠の読み取りは単純な抽出
    "calendar_intent": 512,  # 「来週火曜」等の日付の計算があるので少しだけ考えさせる
    "doc_edit": 1024,  # 依頼と今の内容・参考資料を突き合わせて文章を組み立てる
}


def thinking_budget_for(task: str) -> int | None:
    """タスクの「考える時間」の上限。環境変数 GEMINI_THINKING_<TASK> で上書きできる。"""
    import os

    override = os.environ.get(f"GEMINI_THINKING_{task.upper()}", "").strip()
    if override:
        return int(override)
    return TASK_THINKING_BUDGET.get(task)


def model_for(task: str) -> str:
    """タスクに使うモデル。環境変数 GEMINI_MODEL_<TASK> > 上の表 > GEMINI_MODEL > 既定。"""
    import os

    override = os.environ.get(f"GEMINI_MODEL_{task.upper()}", "").strip()
    if override:
        return override
    if task in TASK_MODELS:
        return TASK_MODELS[task]
    return model_name()


def model_name() -> str:
    """表に無いタスクの既定モデル（後方互換）。"""
    import os

    return os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)


def _call_gemini(prompt: str, *, model: str, project: str, location: str,
                 thinking_budget: int | None = None) -> Generation:
    """Vertex AI Gemini の REST API に JSON 応答を求め、応答と使用トークンを返す。

    SDK（vertexai.generative_models）は使わない。本番の版では「考える時間」の上限を指定できず、
    Flash が既定で長く考えて 1 回 20〜37 秒かかっていた（2026-10-12 計測。上限0なら約2秒）。
    「考える」トークンも出力として課金されるため、コスト計上の出力トークンに含める。
    """
    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    if thinking_budget is not None:
        body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": thinking_budget}
    url = (f"https://{location}-aiplatform.googleapis.com/v1/projects/{project}"
           f"/locations/{location}/publishers/google/models/{model}:generateContent")
    res = _post_gemini(url, body)
    parts = ((res.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text:
        raise RuntimeError(f"Gemini から本文が返りませんでした: {json.dumps(res)[:300]}")
    usage = res.get("usageMetadata") or {}
    return Generation(
        text=text,
        input_tokens=int(usage.get("promptTokenCount", 0) or 0),
        output_tokens=int(usage.get("candidatesTokenCount", 0) or 0)
        + int(usage.get("thoughtsTokenCount", 0) or 0),
    )


def _post_gemini(url: str, body: dict) -> dict:
    """generateContent を呼ぶ（テストでモックする境界）。"""
    import urllib.error
    import urllib.request

    from .lake import _token

    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {_token()}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = " ".join(exc.read().decode("utf-8", "replace").split())[:300]
        raise RuntimeError(f"Gemini API {exc.code}: {detail}") from exc


def _target(task: str) -> tuple[str, str, str]:
    """タスクに応じた (model, project, location) を返す。"""
    import os

    return (
        model_for(task),
        config.PROJECT_ID,
        os.environ.get("VERTEX_AI_LOCATION", "asia-northeast1"),
    )


def generate_review(content: str, *, model: str, project: str, location: str) -> Generation:
    """議案レビューの生成（テストでモックする境界）。"""
    return _call_gemini(
        _PROMPT.format(content=content), model=model, project=project, location=location,
        thinking_budget=thinking_budget_for("proposal_review"),
    )


def review_proposal(content: str) -> ReviewOutcome | None:
    """議案本文をレビューして結果と使用トークンを返す。失敗時は None。"""
    if not content or not content.strip():
        return None
    model, project, location = _target("proposal_review")
    try:
        gen = generate_review(content, model=model, project=project, location=location)
        data = json.loads(gen.text)
        review = LlmReview(
            summary=str(data.get("summary", "")).strip(),
            points=[str(x) for x in data.get("points", [])][:5],
            concerns=[str(x) for x in data.get("concerns", [])][:5],
            model=model,
        )
    except Exception:  # noqa: BLE001 - LLM未設定/失敗でも本処理は止めない
        logger.exception("LLMレビューに失敗しました")
        return None
    return ReviewOutcome(
        review=review,
        usage=InferenceUsage(
            model=model,
            input_tokens=gen.input_tokens,
            output_tokens=gen.output_tokens,
        ),
    )


# --------------------------------------------------------------------------- #
# アンケート自由記述の要約・分類（F7-2, docs/survey-design.md §5）
# --------------------------------------------------------------------------- #
class SurveyDigestOutcome(BaseModel):
    digest: SurveyDigest
    usage: InferenceUsage


def generate_survey_digest(
    texts: list[str], *, model: str, project: str, location: str
) -> Generation:
    """自由記述の要約生成（テストでモックする境界）。"""
    joined = "\n".join(f"- {t}" for t in texts)
    prompt = _SURVEY_PROMPT.format(count=len(texts), texts=joined)
    return _call_gemini(
        prompt, model=model, project=project, location=location,
        thinking_budget=thinking_budget_for("survey_digest"),
    )


def digest_survey(texts: list[str]) -> SurveyDigestOutcome | None:
    """自由記述を要約・分類・感情傾向にまとめる。失敗時・記述0件は None。"""
    if not texts:
        return None
    model, project, location = _target("survey_digest")
    try:
        gen = generate_survey_digest(texts, model=model, project=project, location=location)
        data = json.loads(gen.text)
        themes = [
            SurveyTheme(
                label=str(t.get("label", "")).strip(),
                count=int(t.get("count", 0) or 0),
                examples=[str(x) for x in (t.get("examples") or [])][:2],
            )
            for t in (data.get("themes") or [])[:5]
            if str(t.get("label", "")).strip()
        ]
        sentiment = {
            key: int((data.get("sentiment") or {}).get(key, 0) or 0)
            for key in ("positive", "neutral", "negative")
        }
        digest = SurveyDigest(
            summary=str(data.get("summary", "")).strip(),
            themes=themes,
            sentiment=sentiment,
            improvements=[str(x) for x in (data.get("improvements") or [])][:5],
            model=model,
        )
    except Exception:  # noqa: BLE001 - LLM未設定/失敗でも本処理は止めない
        logger.exception("アンケート要約の生成に失敗しました")
        return None
    return SurveyDigestOutcome(
        digest=digest,
        usage=InferenceUsage(
            model=model,
            input_tokens=gen.input_tokens,
            output_tokens=gen.output_tokens,
        ),
    )


# --------------------------------------------------------------------------- #
# 自然文からの出欠意図解釈（F4-7）
# --------------------------------------------------------------------------- #
class AttendanceIntent(BaseModel):
    event_id: str | None = None
    status: str | None = None  # 出席 | Web出席 | 欠席 | 委任
    confident: bool = False


class IntentOutcome(BaseModel):
    intent: AttendanceIntent
    usage: InferenceUsage


def generate_attendance_intent(
    text: str, events: str, *, model: str, project: str, location: str
) -> Generation:
    """出欠意図の解釈（テストでモックする境界）。"""
    prompt = _INTENT_PROMPT.format(text=text, events=events)
    return _call_gemini(
        prompt, model=model, project=project, location=location,
        thinking_budget=thinking_budget_for("attendance_intent"),
    )


def parse_attendance_intent(text: str, events: str) -> IntentOutcome | None:
    """自然文から「どのイベントを・どの出欠で」回答したいかを推定する。失敗時は None。"""
    if not text or not text.strip() or not events.strip():
        return None
    model, project, location = _target("attendance_intent")
    try:
        gen = generate_attendance_intent(
            text, events, model=model, project=project, location=location
        )
        data = json.loads(gen.text)
        intent = AttendanceIntent(
            event_id=(str(data["event_id"]) if data.get("event_id") else None),
            status=(str(data["status"]) if data.get("status") else None),
            confident=bool(data.get("confident", False)),
        )
    except Exception:  # noqa: BLE001 - LLM未設定/失敗でも本処理は止めない
        logger.exception("出欠意図の解釈に失敗しました")
        return None
    return IntentOutcome(
        intent=intent,
        usage=InferenceUsage(
            model=model,
            input_tokens=gen.input_tokens,
            output_tokens=gen.output_tokens,
        ),
    )


# --------------------------------------------------------------------------- #
# 自然文からの予定操作の解釈（F3-2 / docs/calendar-design.md §5.2）
# --------------------------------------------------------------------------- #
class CalendarIntent(BaseModel):
    action: str | None = None  # create | update | cancel
    event_id: str | None = None
    type: str | None = None
    fiscal_year: int | None = None  # 「次年度」「2027年度」等と明示されたときだけ
    title: str | None = None
    start: str | None = None  # "YYYY-MM-DDTHH:MM"
    end: str | None = None
    location: str | None = None
    confident: bool = False
    question: str | None = None


class CalendarIntentOutcome(BaseModel):
    intent: CalendarIntent
    usage: InferenceUsage


def generate_calendar_intent(
    text: str, events: str, today: str, *, model: str, project: str, location: str
) -> Generation:
    """予定操作の解釈（テストでモックする境界）。"""
    prompt = _CALENDAR_PROMPT.format(text=text, events=events, today=today)
    return _call_gemini(
        prompt, model=model, project=project, location=location,
        thinking_budget=thinking_budget_for("calendar_intent"),
    )


def _opt_str(value) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _opt_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_calendar_intent(text: str, events: str, now: datetime) -> CalendarIntentOutcome | None:
    """自然文から予定の登録・変更・中止の意図を推定する。失敗時は None。"""
    if not text or not text.strip():
        return None
    model, project, location = _target("calendar_intent")
    today = now.strftime(f"%Y-%m-%d({jpdate.weekday(now)}) %H:%M")
    try:
        gen = generate_calendar_intent(
            text, events or "(なし)", today, model=model, project=project, location=location
        )
        data = json.loads(gen.text)
        intent = CalendarIntent(
            action=_opt_str(data.get("action")),
            event_id=_opt_str(data.get("event_id")),
            type=_opt_str(data.get("type")),
            fiscal_year=_opt_int(data.get("fiscal_year")),
            title=_opt_str(data.get("title")),
            start=_opt_str(data.get("start")),
            end=_opt_str(data.get("end")),
            location=_opt_str(data.get("location")),
            confident=bool(data.get("confident", False)),
            question=_opt_str(data.get("question")),
        )
    except Exception:  # noqa: BLE001 - LLM未設定/失敗でも本処理は止めない
        logger.exception("予定操作の解釈に失敗しました")
        return None
    return CalendarIntentOutcome(
        intent=intent,
        usage=InferenceUsage(
            model=model,
            input_tokens=gen.input_tokens,
            output_tokens=gen.output_tokens,
        ),
    )


# --------------------------------------------------------------------------- #
# 会員の問い合わせ応答（F8, docs/nl-assistant-design.md §5）
# --------------------------------------------------------------------------- #
class QaAnswer(BaseModel):
    answer: str
    grounded: bool = False
    needs_human: bool = False


class QaOutcome(BaseModel):
    result: QaAnswer
    usage: InferenceUsage


def generate_answer(
    question: str, context: str, history: str, *, model: str, project: str, location: str
) -> Generation:
    """問い合わせ応答の生成（テストでモックする境界）。"""
    prompt = _QA_PROMPT.format(
        context=context, history=history or "（なし）", question=question
    )
    return _call_gemini(
        prompt, model=model, project=project, location=location,
        thinking_budget=thinking_budget_for("member_qa"),
    )


def answer_question(question: str, context: str, history: str = "") -> QaOutcome | None:
    """会員の質問に自LOM情報だけを根拠に答える。失敗時は None（呼び側でフォールバック）。"""
    if not question or not question.strip():
        return None
    model, project, location = _target("member_qa")
    try:
        gen = generate_answer(
            question, context, history, model=model, project=project, location=location
        )
        data = json.loads(gen.text)
        answer = str(data.get("answer", "")).strip()
        if not answer:
            raise ValueError("answer が空です")
        result = QaAnswer(
            answer=answer,
            grounded=bool(data.get("grounded", False)),
            needs_human=bool(data.get("needs_human", False)),
        )
    except Exception:  # noqa: BLE001 - LLM未設定/失敗でも本処理は止めない
        logger.exception("問い合わせ応答の生成に失敗しました")
        return None
    return QaOutcome(
        result=result,
        usage=InferenceUsage(
            model=model,
            input_tokens=gen.input_tokens,
            output_tokens=gen.output_tokens,
        ),
    )


# --------------------------------------------------------------------------- #
# 対外連絡の要約・告知文生成（F5-2/F5-3, docs/external-notice-design.md §3.2）
# --------------------------------------------------------------------------- #
class DigestOutcome(BaseModel):
    digest: NoticeDigest
    usage: InferenceUsage


def generate_notice_digest(
    subject: str, sender: str, body: str, *, model: str, project: str, location: str
) -> Generation:
    """対外連絡の要約生成（テストでモックする境界）。"""
    prompt = _NOTICE_PROMPT.format(subject=subject, sender=sender or "不明", body=body)
    return _call_gemini(
        prompt, model=model, project=project, location=location,
        thinking_budget=thinking_budget_for("external_notice_digest"),
    )


def _parse_date(value) -> datetime | None:
    """"YYYY-MM-DD" を datetime にする。解釈できなければ None（期限を捏造しない）。"""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        logger.info("期限の解釈に失敗しました: %r", value)
        return None


def digest_notice(subject: str, sender: str, body: str) -> DigestOutcome | None:
    """対外連絡の要約・告知文・対象・期限・アクションを生成する。失敗時は None。"""
    if not body or not body.strip():
        return None
    model, project, location = _target("external_notice_digest")
    try:
        gen = generate_notice_digest(
            subject, sender, body, model=model, project=project, location=location
        )
        data = json.loads(gen.text)
        digest = NoticeDigest(
            summary=str(data.get("summary", "")).strip(),
            announcement=str(data.get("announcement", "")).strip(),
            audience_hint=(str(data["audience_hint"]).strip() or None)
            if data.get("audience_hint")
            else None,
            deadline=_parse_date(data.get("deadline")),
            actions=[str(x) for x in data.get("actions", [])][:5],
            model=model,
        )
    except Exception:  # noqa: BLE001 - LLM未設定/失敗でも本処理は止めない
        logger.exception("対外連絡の要約生成に失敗しました")
        return None
    return DigestOutcome(
        digest=digest,
        usage=InferenceUsage(
            model=model,
            input_tokens=gen.input_tokens,
            output_tokens=gen.output_tokens,
        ),
    )


# --------------------------------------------------------------------------- #
# 委員会資料の案（docs/committee-docs-edit-design.md §6）
# --------------------------------------------------------------------------- #
class DocEditDraft(BaseModel):
    edits: dict[str, str] = Field(default_factory=dict)  # 項目名 → 新しい内容
    summary: str | None = None
    question: str | None = None


class DocEditOutcome(BaseModel):
    draft: DocEditDraft
    usage: InferenceUsage


def generate_doc_edit(prompt: str, *, model: str, project: str, location: str) -> Generation:
    """委員会資料の案の生成（テストでモックする境界）。"""
    return _call_gemini(
        prompt, model=model, project=project, location=location,
        thinking_budget=thinking_budget_for("doc_edit"),
    )


def draft_doc_edit(request: str, committee: str, title: str, fields: str,
                   knowledge: str) -> DocEditOutcome | None:
    """依頼から、資料の表の項目に書く案を作る。失敗時は None。"""
    model, project, location = _target("doc_edit")
    prompt = _DOC_EDIT_PROMPT.format(
        request=request, committee=committee, title=title, fields=fields,
        knowledge=knowledge or "(なし)",
    )
    try:
        gen = generate_doc_edit(prompt, model=model, project=project, location=location)
        data = json.loads(gen.text)
        edits: dict[str, str] = {}
        for item in data.get("edits") or []:
            name, text = _opt_str(item.get("field")), _opt_str(item.get("text"))
            if name and text:
                edits[name] = text
        draft = DocEditDraft(edits=edits, summary=_opt_str(data.get("summary")),
                             question=_opt_str(data.get("question")))
    except Exception:  # noqa: BLE001 - LLM未設定/失敗でも本処理は止めない
        logger.exception("委員会資料の案の作成に失敗しました")
        return None
    return DocEditOutcome(draft=draft, usage=InferenceUsage(
        model=model, input_tokens=gen.input_tokens, output_tokens=gen.output_tokens,
    ))
