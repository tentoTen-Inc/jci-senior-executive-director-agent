"""意見箱（docs/feedback-box-design.md）。

匿名では会員ID が残らないこと、AI の分類と専務理事への通知（静音時間）、管理画面の API。
"""
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import feedback_box, line_push, llm, main
from app import schedule_poll as sp
from app.deps import set_repo
from app.models import Feedback, FeedbackKind, FeedbackStatus, Member
from app.repository import InMemoryRepository
from app.rich_menu import BOARD, MEMBER, OFFICER, rich_menu_definition

client = TestClient(main.app)
MORNING = datetime(2026, 10, 26, 9, 0)
NIGHT = datetime(2026, 10, 26, 22, 0)
USERS = {"tok-reg": "U_reg", "tok-stranger": "U_nobody"}
ADMIN = {"X-Goog-Authenticated-User-Email": "sed@example.com"}  # IAP 配下の管理サービス


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def pushed(monkeypatch):
    sent = []

    def push(to, messages):
        sent.append((to, [m.text for m in messages]))
        return True
    monkeypatch.setattr(line_push, "push_messages", push)
    return sent


@pytest.fixture
def repo(monkeypatch, pushed):
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="m_reg", name="一般 会員", line_user_id="U_reg"))
    r.upsert_member(Member(member_id="m_sed", name="専務 三郎", line_user_id="U_sed",
                           officer_role="専務理事"))
    r.upsert_member(Member(member_id="m_pres", name="理事長 四郎", line_user_id="U_pres",
                           officer_role="理事長"))
    set_repo(r)
    monkeypatch.setenv("LIFF_ID", "2000000000-abcdEFGH")
    monkeypatch.setenv("LINE_LOGIN_CHANNEL_ID", "2000000000")
    monkeypatch.setenv("ROLE", "admin")
    sp._token_cache.clear()

    def verify(token, client_id):
        if token not in USERS:
            raise sp.PollError(401, "LINE ログインの確認に失敗しました。")
        return {"sub": USERS[token], "name": "LINE名"}
    monkeypatch.setattr(sp, "_verify_id_token_raw", verify)
    yield r
    set_repo(None)


def _triage(monkeypatch, **data):
    calls = []

    def fake(prompt, **kw):
        calls.append(prompt)
        return llm.Generation(text=json.dumps(data, ensure_ascii=False))
    monkeypatch.setattr(llm, "generate_feedback_triage", fake)
    return calls


# --------------------------------------------------------------------------- #
# LIFF「意見箱」
# --------------------------------------------------------------------------- #
def test_form_lists_selectable_kinds(repo):
    form = client.get("/liff/api/feedback", headers=_auth("tok-reg")).json()
    assert [k["label"] for k in form["kinds"]] == ["不具合", "ほしい機能", "使いにくい点", "その他"]


def test_send_named(repo):
    res = client.post("/liff/api/feedback", headers=_auth("tok-reg"),
                      json={"kind": "feature", "text": "  出欠をカレンダーに入れたい ",
                            "anonymous": False})
    assert res.status_code == 200
    [item] = repo.list_feedback()
    assert item.kind == FeedbackKind.feature and item.text == "出欠をカレンダーに入れたい"
    assert item.member_id == "m_reg" and item.status == FeedbackStatus.new
    assert item.source == "liff"


def test_send_anonymous_keeps_no_member_id(repo):
    res = client.post("/liff/api/feedback", headers=_auth("tok-reg"),
                      json={"kind": "bug", "text": "ボタンが反応しない", "anonymous": True})
    assert res.status_code == 200
    [item] = repo.list_feedback()
    assert item.anonymous is True and item.member_id is None
    stored = json.dumps(item.model_dump(mode="json"), ensure_ascii=False)
    assert "m_reg" not in stored and "U_reg" not in stored and "一般 会員" not in stored
    # 管理画面でも名前は出ない
    row = client.get("/api/feedback", headers=ADMIN).json()["items"][0]
    assert row["member_name"] is None and row["member_id"] is None


@pytest.mark.parametrize("body", [
    {"kind": "unanswered", "text": "質問"},  # LIFF では選べない
    {"kind": "でたらめ", "text": "x"},
    {"kind": "bug", "text": "   "},
    {"kind": "bug", "text": "あ" * (feedback_box.TEXT_MAX + 1)},
])
def test_send_validation(repo, body):
    res = client.post("/liff/api/feedback", headers=_auth("tok-reg"), json=body)
    assert res.status_code == 400 and repo.list_feedback() == []


@pytest.mark.parametrize("token, status", [("tok-stranger", 403), ("bad", 401)])
def test_send_requires_linked_member(repo, token, status):
    res = client.post("/liff/api/feedback", headers=_auth(token),
                      json={"kind": "bug", "text": "x", "anonymous": True})
    assert res.status_code == status and repo.list_feedback() == []


def test_menu_feedback_button_opens_liff():
    for kind in (MEMBER, BOARD, OFFICER):
        uris = [a["action"].get("uri") for a in
                rich_menu_definition(kind=kind, liff_id="123-abc")["areas"]]
        assert "https://liff.line.me/123-abc?view=feedback" in uris
        labels = [a["action"].get("data") for a in rich_menu_definition(kind=kind)["areas"]]
        assert "menu|意見箱" in labels and "menu|事務局に連絡" not in labels


# --------------------------------------------------------------------------- #
# tick: AI の分類と専務理事への通知
# --------------------------------------------------------------------------- #
def _add(repo, kind=FeedbackKind.other, text="使い方が分かりにくい", at=MORNING, **kw):
    item = Feedback(feedback_id=f"fb_{len(repo.list_feedback())}", kind=kind, text=text,
                    created_at=at, **kw)
    repo.save_feedback(item)
    return item


def test_process_triages_and_notifies_sed_with_bug_prefix(repo, pushed, monkeypatch):
    calls = _triage(monkeypatch, kind="bug", feature="出欠", urgency="高",
                    summary="出欠ボタンが反映されない")
    _add(repo, kind=FeedbackKind.other, text="出欠のボタンを押しても変わりません",
         member_id="m_reg")
    result = feedback_box.process(repo, MORNING)
    assert result == {"triaged": 1, "notified": 1}
    assert "その他" in calls[0]  # 送った人の選んだ種類を渡して補正させる
    [item] = repo.list_feedback()
    assert item.kind == FeedbackKind.other  # 送った人の選択は残す
    assert (item.triage.kind, item.triage.feature, item.triage.urgency) == ("bug", "出欠", "高")
    [(to, [text])] = pushed
    assert to == "U_sed"  # 専務理事だけ
    assert text.startswith("【不具合】") and "出欠ボタンが反映されない（一般 会員）" in text
    # 2回目は知らせない
    assert feedback_box.process(repo, MORNING + timedelta(minutes=10)) == {
        "triaged": 0, "notified": 0}
    assert len(pushed) == 1


def test_process_waits_for_quiet_hours(repo, pushed, monkeypatch):
    _triage(monkeypatch, kind="feature", feature="日程調整", urgency="低", summary="要望")
    _add(repo, kind=FeedbackKind.feature, anonymous=True, at=NIGHT)
    assert feedback_box.process(repo, NIGHT)["notified"] == 0
    assert pushed == []
    assert feedback_box.process(repo, NIGHT + timedelta(hours=11))["notified"] == 1
    [(_, [text])] = pushed
    assert not text.startswith("【不具合】") and "（匿名）" in text


def test_process_batches_several(repo, pushed, monkeypatch):
    _triage(monkeypatch, kind="feature", feature="その他", urgency="低", summary="要望")
    for _ in range(3):
        _add(repo, kind=FeedbackKind.feature)
    feedback_box.process(repo, MORNING)
    [(_, [text])] = pushed
    assert "3 件届きました" in text


def test_triage_failure_retries_later_but_still_notifies(repo, pushed, monkeypatch):
    monkeypatch.setattr(llm, "generate_feedback_triage",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    _add(repo, kind=FeedbackKind.bug, text="落ちる")
    assert feedback_box.process(repo, MORNING) == {"triaged": 1, "notified": 1}
    assert pushed[0][1][0].startswith("【不具合】")  # 分類できなくても選んだ種類で知らせる
    assert feedback_box.process(repo, MORNING + timedelta(minutes=30))["triaged"] == 0
    _triage(monkeypatch, kind="bug", feature="その他", urgency="中", summary="落ちる")
    assert feedback_box.process(repo, MORNING + timedelta(hours=1))["triaged"] == 1
    assert repo.list_feedback()[0].triage.summary == "落ちる"


# --------------------------------------------------------------------------- #
# 管理画面
# --------------------------------------------------------------------------- #
def test_admin_list_and_status(repo):
    item = _add(repo, member_id="m_reg")
    data = client.get("/api/feedback", headers=ADMIN).json()
    row = data["items"][0]
    assert row["member_name"] == "一般 会員" and row["status_label"] == "新規"
    assert data["statuses"] == {"new": "新規", "in_progress": "対応中", "done": "完了",
                                "declined": "見送り"}
    res = client.post(f"/api/feedback/{item.feedback_id}/status", json={"status": "done"},
                      headers=ADMIN)
    assert res.status_code == 200 and res.json()["items"][0]["status"] == "done"
    saved = repo.get_feedback(item.feedback_id)
    assert saved.updated_by == "sed@example.com"
    assert client.post(f"/api/feedback/{item.feedback_id}/status",
                       json={"status": "x"}, headers=ADMIN).status_code == 400
    assert client.post("/api/feedback/nope/status", json={"status": "done"},
                       headers=ADMIN).status_code == 404


def test_admin_digest_counts_from_ids(repo, monkeypatch):
    a, b = _add(repo, text="出欠が押せない"), _add(repo, text="出欠の反映が遅い")
    _add(repo, text="見送った意見", status=FeedbackStatus.declined)
    seen = []

    def fake(prompt, **kw):
        seen.append(prompt)
        return llm.Generation(text=json.dumps({
            "summary": "出欠の不具合が目立つ",
            "themes": [{"label": "出欠", "ids": [a.feedback_id, b.feedback_id, "fb_unknown"]}],
        }, ensure_ascii=False))
    monkeypatch.setattr(llm, "generate_feedback_digest", fake)
    data = client.post("/api/feedback/digest", headers=ADMIN).json()
    assert "見送った意見" not in seen[0]
    assert data["digest"]["count"] == 2
    assert data["digest"]["themes"] == [
        {"label": "出欠", "count": 2, "feedback_ids": [a.feedback_id, b.feedback_id]}]


def test_admin_digest_failure(repo, monkeypatch):
    _add(repo)
    monkeypatch.setattr(llm, "generate_feedback_digest",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert client.post("/api/feedback/digest", headers=ADMIN).status_code == 503


def test_admin_api_requires_auth(repo, monkeypatch):
    monkeypatch.delenv("ROLE")  # 公開サービスでは IAP のヘッダを信用しない
    assert client.get("/api/feedback", headers=ADMIN).status_code == 401
