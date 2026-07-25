"""アンケート取込・集計・要約（F7-1/F7-2 / P4-1）のテスト。

Forms API の応答形は実フォーム（2604_対内アンケート）の実測構造に合わせた擬似データ。
内容は合成（実会員のPIIは使わない）。
"""
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import forms, llm, main
from app.deps import set_repo
from app.repository import InMemoryRepository
from app.survey_agg import aggregate_survey, free_texts, overall_average
from app.survey_import import build_survey, name_question_id, parse_questions
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
IAP = {"X-Goog-Authenticated-User-Email": "sed@10to10.co.jp"}
FORM_ID = "form123"
NOW = datetime(2026, 7, 26, 10, 0)

FORM_PAYLOAD = {
    "form": {
        "info": {"title": "2026年度 4月例会アンケート", "description": "検証のためのアンケート"},
        "items": [
            {"title": "01.氏名", "questionItem": {"question": {
                "questionId": "q_name", "textQuestion": {},
            }}},
            {"title": "02.所属等", "questionItem": {"question": {
                "questionId": "q_belong",
                "choiceQuestion": {"type": "RADIO", "options": [
                    {"value": "五役"}, {"value": "総務委員会"}, {},
                ]},
            }}},
            {"title": "目的達成度について"},  # セクション見出し（questionItem なし）
            {"title": "Q1. 意義を再認識できましたか？", "questionItem": {"question": {
                "questionId": "q1", "required": True, "scaleQuestion": {"low": 1, "high": 5},
            }}},
            {"title": "Q2. 新たな気づきはありましたか？", "questionItem": {"question": {
                "questionId": "q2", "required": True, "scaleQuestion": {"low": 1, "high": 5},
            }}},
            {"title": "Q3. 印象に残った反応は？", "questionItem": {"question": {
                "questionId": "q3", "textQuestion": {"paragraph": True},
            }}},
            {"title": "画像だけの設問", "questionItem": {"question": {"questionId": "q_img"}}},
        ],
    },
    "responses": [
        {
            "responseId": "r2",
            "createTime": "2026-05-11T01:00:00.000Z",
            "lastSubmittedTime": "2026-05-11T02:00:00.000123Z",
            "respondentEmail": "b@example.jp",
            "answers": {
                "q_name": {"textAnswers": {"answers": [{"value": "山田花子"}]}},
                "q1": {"textAnswers": {"answers": [{"value": "5"}]}},
                "q2": {"textAnswers": {"answers": [{"value": "4"}]}},
                "q3": {"textAnswers": {"answers": [{"value": "子どもの反応が良かった"}]}},
            },
        },
        {
            "responseId": "r1",
            "createTime": "2026-05-10T09:51:02.144Z",
            "lastSubmittedTime": "2026-05-10T09:51:02.144924Z",
            "respondentEmail": "a@example.jp",
            "answers": {
                "q_name": {"textAnswers": {"answers": [{"value": "田中太郎"}]}},
                "q_belong": {"textAnswers": {"answers": [{"value": "総務委員会"}]}},
                "q1": {"textAnswers": {"answers": [{"value": "3"}]}},
                "q2": {"textAnswers": {"answers": [{"value": "4"}]}},
                "q3": {"textAnswers": {"answers": [{"value": "会場が分かりにくかった"}]}},
            },
        },
    ],
}

DIGEST_JSON = json.dumps({
    "summary": "全体に好意的。会場案内に改善余地。",
    "themes": [
        {"label": "子どもの反応", "count": 1, "examples": ["子どもの反応が良かった", "x", "y"]},
        {"label": "会場案内", "count": 1, "examples": ["会場が分かりにくかった"]},
        {"label": "", "count": 3},
    ],
    "sentiment": {"positive": 1, "neutral": 0, "negative": 1},
    "improvements": ["会場までの案内表示を増やす"],
}, ensure_ascii=False)


@pytest.fixture(autouse=True)
def repo():
    r = InMemoryRepository()
    set_repo(r)
    yield r
    set_repo(None)


@pytest.fixture(autouse=True)
def fake_forms(monkeypatch):
    monkeypatch.setattr(forms, "fetch_form", lambda form_id: FORM_PAYLOAD)


def import_form(**over):
    payload = {"form_id": FORM_ID, "kind": "internal"}
    payload.update(over)
    return client.post("/api/surveys/import-form", json=payload, headers=IAP)


# --------------------------------------------------------------------------- #
# パース
# --------------------------------------------------------------------------- #
def test_parse_questions_types_sections_and_skips():
    qs = parse_questions(FORM_PAYLOAD["form"])
    by_id = {q.question_id: q for q in qs}
    assert set(by_id) == {"q_name", "q_belong", "q1", "q2", "q3"}  # 画像設問はスキップ
    assert by_id["q_name"].type == "text"
    assert by_id["q_belong"].type == "choice"
    assert by_id["q_belong"].options == ["五役", "総務委員会"]  # value 無しは落とす
    assert (by_id["q1"].type, by_id["q1"].scale_low, by_id["q1"].scale_high) == ("scale", 1, 5)
    # セクション見出しは以降の設問に付く
    assert by_id["q_name"].section is None
    assert by_id["q1"].section == "目的達成度について"
    assert by_id["q3"].section == "目的達成度について"


def test_name_question_detection():
    qs = parse_questions(FORM_PAYLOAD["form"])
    assert name_question_id(qs) == "q_name"
    assert name_question_id([q for q in qs if q.question_id != "q_name"]) is None


def test_build_survey_normalizes_responses():
    survey = build_survey(
        FORM_PAYLOAD, form_id=FORM_ID, kind="internal", event_id="ev1", now=NOW
    )
    assert survey.title == "2026年度 4月例会アンケート"
    assert survey.synced_at == NOW
    # 送信時刻の昇順に並べる（APIの返却順に依存しない）
    assert [r.response_id for r in survey.responses] == ["r1", "r2"]
    r1 = survey.responses[0]
    assert r1.submitted_at == datetime(2026, 5, 10, 9, 51, 2, 144924)  # Z付きを解釈
    assert r1.respondent_email == "a@example.jp"
    assert r1.respondent_name == "田中太郎"
    assert r1.answers["q1"] == "3"


def test_reimport_keeps_survey_id_and_digest(repo):
    first = import_form().json()
    sid = first["survey_id"]
    # 要約を付けてから再取込
    repo_survey = repo.get_survey(sid)
    from app.models import SurveyDigest

    repo_survey.digest = SurveyDigest(summary="前回の要約")
    repo_survey.reminder_count = 2
    repo.upsert_survey(repo_survey)

    again = import_form().json()
    assert again["survey_id"] == sid  # form_id が同じなら更新
    assert again["digest"]["summary"] == "前回の要約"  # 生成物は消さない
    assert again["reminder_count"] == 2
    assert len(repo.list_surveys()) == 1


# --------------------------------------------------------------------------- #
# 集計
# --------------------------------------------------------------------------- #
def test_aggregate_scales_and_texts():
    survey = build_survey(FORM_PAYLOAD, form_id=FORM_ID, kind="internal", event_id=None, now=NOW)
    agg = aggregate_survey(survey)
    assert agg.responses == 2
    q1 = next(s for s in agg.scales if s.question_id == "q1")
    assert (q1.answered, q1.average) == (2, 4.0)  # "3","5" → 4.0（文字列を数値化）
    assert q1.distribution == {"3": 1, "5": 1}
    q2 = next(s for s in agg.scales if s.question_id == "q2")
    assert q2.average == 4.0 and q2.distribution == {"4": 2}
    # 氏名(text)も text 設問として集計対象に入る
    q3 = next(t for t in agg.texts if t.question_id == "q3")
    assert q3.answers == ["会場が分かりにくかった", "子どもの反応が良かった"]
    assert overall_average(agg) == 4.0
    assert "[Q3. 印象に残った反応は？] 会場が分かりにくかった" in free_texts(agg)


def test_aggregate_without_responses():
    payload = {"form": FORM_PAYLOAD["form"], "responses": []}
    survey = build_survey(payload, form_id=FORM_ID, kind="internal", event_id=None, now=NOW)
    agg = aggregate_survey(survey)
    q1 = next(s for s in agg.scales if s.question_id == "q1")
    assert (q1.answered, q1.average, q1.distribution) == (0, None, {})  # 平均は0ではなくNone
    assert overall_average(agg) is None
    assert free_texts(agg) == []


def test_aggregate_ignores_non_numeric_scale_answers():
    payload = {
        "form": FORM_PAYLOAD["form"],
        "responses": [{
            "responseId": "r9", "createTime": "2026-05-12T00:00:00Z",
            "answers": {"q1": {"textAnswers": {"answers": [{"value": "未回答"}]}}},
        }],
    }
    survey = build_survey(payload, form_id=FORM_ID, kind="internal", event_id=None, now=NOW)
    q1 = next(s for s in aggregate_survey(survey).scales if s.question_id == "q1")
    assert (q1.answered, q1.average) == (0, None)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def test_import_and_detail(repo):
    res = import_form(event_id="ev1")
    assert res.status_code == 200
    sid = res.json()["survey_id"]
    assert any(a.action == "survey.import" for a in repo.list_audit())

    detail = client.get(f"/api/surveys/{sid}").json()
    assert detail["survey"]["event_id"] == "ev1"
    assert detail["aggregate"]["responses"] == 2
    assert len(detail["aggregate"]["scales"]) == 2


def test_import_dry_run_does_not_save(repo):
    res = import_form(dry_run=True)
    assert res.json() == {
        "dry_run": True,
        "title": "2026年度 4月例会アンケート",
        "questions": 5,
        "responses": 2,
    }
    assert repo.list_surveys() == []


def test_import_rejects_unknown_kind():
    assert import_form(kind="unknown").status_code == 400


def test_import_forms_failure_is_502(monkeypatch):
    def boom(form_id):
        raise RuntimeError("permission denied")

    monkeypatch.setattr(forms, "fetch_form", boom)
    res = import_form()
    assert res.status_code == 502
    assert "drive-reader" in res.json()["detail"]


def test_list_and_kind_filter():
    import_form()
    import_form(form_id="form999", kind="external")
    assert len(client.get("/api/surveys").json()) == 2
    external = client.get("/api/surveys?kind=external").json()
    assert [s["form_id"] for s in external] == ["form999"]


def test_sync_refetches(repo):
    sid = import_form().json()["survey_id"]
    repo_survey = repo.get_survey(sid)
    repo_survey.responses = []
    repo.upsert_survey(repo_survey)

    res = client.post(f"/api/surveys/{sid}/sync", headers=IAP)
    assert res.status_code == 200
    assert len(repo.get_survey(sid).responses) == 2


def test_digest_summarizes_free_texts(monkeypatch, repo):
    captured = {}

    def fake(texts, **kw):
        captured["texts"] = texts
        return llm.Generation(text=DIGEST_JSON, input_tokens=800, output_tokens=200)

    monkeypatch.setattr(llm, "generate_survey_digest", fake)
    sid = import_form().json()["survey_id"]

    res = client.post(f"/api/surveys/{sid}/digest", headers=IAP)
    assert res.status_code == 200
    digest = res.json()["digest"]
    assert digest["summary"] == "全体に好意的。会場案内に改善余地。"
    # 空ラベルのテーマは落とし、examples は2件までに制限
    assert [t["label"] for t in digest["themes"]] == ["子どもの反応", "会場案内"]
    assert len(digest["themes"][0]["examples"]) == 2
    assert digest["sentiment"] == {"positive": 1, "neutral": 0, "negative": 1}
    assert digest["improvements"] == ["会場までの案内表示を増やす"]
    assert digest["generated_at"] is not None
    # 設問文を添えて渡す（文脈が分かるように）
    assert any("Q3." in t for t in captured["texts"])

    logs = repo.list_inference_logs()
    assert len(logs) == 1
    assert (logs[0].kind, logs[0].target, logs[0].ok) == ("survey_digest", sid, True)
    assert any(a.action == "survey.digest" for a in repo.list_audit())


def test_digest_without_free_text_is_400(monkeypatch, repo):
    monkeypatch.setattr(
        forms, "fetch_form", lambda form_id: {"form": FORM_PAYLOAD["form"], "responses": []}
    )
    sid = import_form().json()["survey_id"]
    assert client.post(f"/api/surveys/{sid}/digest").status_code == 400
    assert repo.list_inference_logs() == []  # LLMを呼んでいない


def test_digest_failure_is_503_and_recorded(monkeypatch, repo):
    def boom(*a, **kw):
        raise RuntimeError("vertex unavailable")

    monkeypatch.setattr(llm, "generate_survey_digest", boom)
    sid = import_form().json()["survey_id"]
    assert client.post(f"/api/surveys/{sid}/digest").status_code == 503
    logs = repo.list_inference_logs()
    assert len(logs) == 1 and logs[0].ok is False
    assert repo.get_survey(sid).digest is None


def test_not_found_and_auth():
    assert client.get("/api/surveys/nope").status_code == 404
    assert client.post("/api/surveys/nope/sync").status_code == 404
    assert client.post("/api/surveys/nope/digest").status_code == 404
    assert TestClient(main.app).get("/api/surveys").status_code == 401


# --------------------------------------------------------------------------- #
# 会員照合・未提出者・催促（P4-2 / F7-3）
# --------------------------------------------------------------------------- #
def seed_members(repo):
    from app.models import Contact, Member

    # a@example.jp はメール一致、山田花子 は氏名一致（フォーム側は空白なし）
    repo.upsert_member(Member(
        member_id="m1", name="田中 太郎", line_user_id="U1",
        contact=Contact(email="a@example.jp"),
    ))
    repo.upsert_member(Member(member_id="m2", name="山田花子", line_user_id="U2"))
    repo.upsert_member(Member(member_id="m3", name="鈴木一郎", line_user_id="U3"))
    repo.upsert_member(Member(member_id="m4", name="連携なし"))  # LINE未連携＝配信対象外


def test_match_by_email_then_name(repo):
    seed_members(repo)
    from app.survey_match import match_responses

    sid = import_form().json()["survey_id"]
    survey = repo.get_survey(sid)
    result = match_responses(repo, survey)
    # r1 はメール一致（氏名は "田中太郎" vs "田中 太郎" で空白違い）、r2 は氏名一致
    assert result.matched == {"r1": "m1", "r2": "m2"}
    assert result.unmatched_response_ids == []
    # 照合結果は取込時に回答へ書き戻される
    assert {r.response_id: r.member_id for r in survey.responses} == {"r1": "m1", "r2": "m2"}


def test_unmatched_response_is_counted_not_guessed(repo, monkeypatch):
    seed_members(repo)
    payload = {
        "form": FORM_PAYLOAD["form"],
        "responses": [{
            "responseId": "rx", "createTime": "2026-05-13T00:00:00Z",
            "respondentEmail": "guest@example.com",
            "answers": {"q_name": {"textAnswers": {"answers": [{"value": "外部の方"}]}}},
        }],
    }
    monkeypatch.setattr(forms, "fetch_form", lambda form_id: payload)
    sid = import_form().json()["survey_id"]

    res = client.get(f"/api/surveys/{sid}/pending").json()
    assert res["unmatched"] == 1
    assert res["answered"] == 0  # 照合できない回答は回答者数に数えない
    assert set(res["pending_member_ids"]) == {"m1", "m2", "m3"}  # m4 は未連携で対象外


def test_pending_uses_event_scope(repo):
    from app.models import Event, EventStatus, EventType, TargetScope, TargetScopeKind

    seed_members(repo)
    repo.upsert_member(repo.get_member("m3").model_copy(update={"committee": "総務委員会"}))
    repo.upsert_event(Event(
        event_id="ev1", type=EventType.例会, title="4月例会",
        datetime_start=datetime(2026, 4, 20, 19, 0),
        target_scope=TargetScope(kind=TargetScopeKind.committee, value=["総務委員会"]),
        status=EventStatus.open,
    ))
    sid = import_form(event_id="ev1").json()["survey_id"]
    res = client.get(f"/api/surveys/{sid}/pending").json()
    assert res["total_targets"] == 1  # 総務委員会の m3 のみ
    assert res["pending_member_ids"] == ["m3"]


def test_pending_rejects_external(repo):
    import_form(form_id="fx", kind="external")
    sid = client.get("/api/surveys?kind=external").json()[0]["survey_id"]
    assert client.get(f"/api/surveys/{sid}/pending").status_code == 400
    assert client.post(f"/api/surveys/{sid}/remind").status_code == 400


def test_remind_pending_only(repo, monkeypatch):
    from app import line_push
    from app.models import QuietHours, Settings

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        line_push, "push_messages",
        lambda user_id, messages: bool(sent.append((user_id, messages[0].text))) or True,
    )
    seed_members(repo)
    repo.save_settings(Settings(quiet_hours=QuietHours(start="00:00", end="00:00")))
    sid = import_form().json()["survey_id"]

    res = client.post(f"/api/surveys/{sid}/remind", headers=IAP)
    assert res.status_code == 200
    body = res.json()
    assert (body["pending"], body["sent"], body["reminder_count"]) == (1, 1, 1)
    assert [u for u, _ in sent] == ["U3"]  # 回答済みの m1/m2 には送らない
    assert "アンケートのお願い" in sent[0][1]
    assert repo.get_survey(sid).reminder_count == 1
    assert any(a.action == "survey.remind" for a in repo.list_audit())


def test_remind_body_override_and_no_pending(repo, monkeypatch):
    from app import line_push
    from app.models import QuietHours, Settings

    sent: list[str] = []
    monkeypatch.setattr(
        line_push, "push_messages",
        lambda user_id, messages: bool(sent.append(messages[0].text)) or True,
    )
    seed_members(repo)
    repo.save_settings(Settings(quiet_hours=QuietHours(start="00:00", end="00:00")))
    sid = import_form().json()["survey_id"]

    client.post(f"/api/surveys/{sid}/remind", json={"body_text": "アンケートご協力ください"})
    assert sent == ["アンケートご協力ください"]

    # 全員回答済みにすると 409
    survey = repo.get_survey(sid)
    survey.responses[0].member_id = "m1"
    survey.responses.append(survey.responses[0].model_copy(update={
        "response_id": "r3", "member_id": "m3", "respondent_email": None,
        "respondent_name": "鈴木一郎",
    }))
    repo.upsert_survey(survey)
    assert client.post(f"/api/surveys/{sid}/remind").status_code == 409


def test_pending_and_remind_404():
    assert client.get("/api/surveys/nope/pending").status_code == 404
    assert client.post("/api/surveys/nope/remind").status_code == 404


# --------------------------------------------------------------------------- #
# レポート・通知・トレンド（P4-3 / F7-4/F7-5）
# --------------------------------------------------------------------------- #
def seed_officer(repo):
    from app.models import Member

    repo.upsert_member(Member(
        member_id="of1", name="理事長", line_user_id="UOF", officer_role="理事長",
    ))


def digest_it(monkeypatch, sid):
    monkeypatch.setattr(
        llm, "generate_survey_digest",
        lambda texts, **kw: llm.Generation(text=DIGEST_JSON),
    )
    client.post(f"/api/surveys/{sid}/digest")


def test_report_contains_quantitative_and_qualitative(repo, monkeypatch):
    from app.survey_report import build_report

    seed_members(repo)
    sid = import_form().json()["survey_id"]
    digest_it(monkeypatch, sid)

    text = build_report(repo, repo.get_survey(sid), now=NOW)
    assert "# 2026年度 4月例会アンケート アンケート集計" in text
    assert "区分: 対内" in text
    assert "回答数: 2件" in text
    assert "総合平均: 4.0（5段階）" in text
    assert "回答率: 67%（2/3名）" in text  # m1,m2 が回答、m3 が未提出
    assert "| Q1. 意義を再認識できましたか？ | 4.0 | 2 | 0 / 0 / 1 / 0 / 1 |" in text
    assert "全体に好意的。会場案内に改善余地。" in text
    assert "感情傾向: 肯定 1 / 中立 0 / 否定 1" in text
    assert "会場までの案内表示を増やす" in text
    assert "要約・分類はAIの助言です" in text


def test_report_without_digest_says_so(repo):
    from app.survey_report import build_report

    sid = import_form().json()["survey_id"]
    text = build_report(repo, repo.get_survey(sid), now=NOW)
    assert "未生成です" in text


def test_report_notifies_officers(repo, monkeypatch):
    from app import line_push
    from app.models import QuietHours, Settings

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        line_push, "push_messages",
        lambda user_id, messages: bool(sent.append((user_id, messages[0].text))) or True,
    )
    seed_members(repo)
    seed_officer(repo)
    repo.save_settings(Settings(quiet_hours=QuietHours(start="00:00", end="00:00")))
    sid = import_form().json()["survey_id"]
    digest_it(monkeypatch, sid)

    res = client.post(f"/api/surveys/{sid}/report", json={}, headers=IAP)
    assert res.status_code == 200
    body = res.json()
    assert body["notified"] is True and body["sent"] == 1
    assert "アンケート集計" in body["report"]
    assert [u for u, _ in sent] == ["UOF"]  # 五役のみ
    text = sent[0][1]
    assert "回答 2/4名" in text  # 五役を含めた対象4名に対する回答2名
    assert "総合平均 4.0（5段階）" in text
    assert "要約(AI): 全体に好意的" in text
    assert any(a.action == "survey.report" for a in repo.list_audit())


def test_report_without_notify_skips_delivery(repo, monkeypatch):
    from app import line_push

    sent: list[str] = []
    monkeypatch.setattr(
        line_push, "push_messages", lambda u, m: bool(sent.append(u)) or True
    )
    seed_officer(repo)
    sid = import_form().json()["survey_id"]
    res = client.post(f"/api/surveys/{sid}/report", json={"notify": False})
    assert res.json()["notified"] is False
    assert sent == []


def test_report_requires_officers(repo):
    sid = import_form().json()["survey_id"]
    res = client.post(f"/api/surveys/{sid}/report", json={})
    assert res.status_code == 400
    assert "五役" in res.json()["detail"]


def test_report_404():
    assert client.post("/api/surveys/nope/report", json={}).status_code == 404


def test_trends_are_ordered_oldest_first(repo, monkeypatch):
    import_form()  # 総合平均 4.0
    low = {
        "form": FORM_PAYLOAD["form"],
        "responses": [{
            "responseId": "rz", "createTime": "2026-06-01T00:00:00Z",
            "answers": {
                "q1": {"textAnswers": {"answers": [{"value": "2"}]}},
                "q2": {"textAnswers": {"answers": [{"value": "2"}]}},
            },
        }],
    }
    monkeypatch.setattr(forms, "fetch_form", lambda form_id: low)
    import_form(form_id="form2")

    points = client.get("/api/surveys/trends").json()
    assert [p["overall_average"] for p in points] == [4.0, 2.0]  # 取込順（古い順）
    assert points[0]["responses"] == 2 and points[1]["responses"] == 1


def test_trends_kind_filter_and_route_not_shadowed(repo):
    import_form()
    import_form(form_id="fx", kind="external")
    assert len(client.get("/api/surveys/trends").json()) == 2
    assert len(client.get("/api/surveys/trends?kind=external").json()) == 1
    # /surveys/trends が /surveys/{survey_id} に飲まれていない
    assert client.get("/api/surveys/nope").status_code == 404
