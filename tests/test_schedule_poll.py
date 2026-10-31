"""日程調整（docs/schedule-poll-design.md）のテスト。"""
import pytest
from fastapi.testclient import TestClient

from app import main
from app import schedule_poll as sp
from app.deps import set_repo
from app.models import EventType, Member, PollStatus
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

admin = TestClient(main.app, headers=ADMIN_AUTH)
public = TestClient(main.app)
GROUP = "Cgroup"
USERS = {"tok-sed": ("U_sed", "たかゆき"), "tok-a": ("U_a", "さがわ"), "tok-b": ("U_b", "こひ"),
         "tok-x": ("U_x", "部外者")}
GROUP_MEMBERS = {"U_sed", "U_a", "U_b"}


@pytest.fixture
def repo(monkeypatch):
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="m2", name="遠藤 孝行", officer_role="専務理事",
                           line_user_id="U_sed"))
    r.upsert_member(Member(member_id="m3", name="渡部 邦祐", officer_role="理事長"))
    r.upsert_member(Member(member_id="m5", name="佐川 竜二", officer_role="副理事長"))
    r.upsert_member(Member(member_id="m10", name="宮澤 正史"))
    set_repo(r)
    monkeypatch.setenv("LIFF_ID", "2000000000-abcdEFGH")
    monkeypatch.setenv("LINE_LOGIN_CHANNEL_ID", "2000000000")
    sp._token_cache.clear()
    sp._member_cache.clear()

    def verify(token, client_id):
        assert client_id == "2000000000"
        if token not in USERS:
            raise sp.PollError(401, "LINE ログインの確認に失敗しました。")
        sub, name = USERS[token]
        return {"sub": sub, "name": name}

    monkeypatch.setattr(sp, "_verify_id_token_raw", verify)
    monkeypatch.setattr(sp, "_is_group_member_raw", lambda g, u: g == GROUP and u in GROUP_MEMBERS)
    yield r
    set_repo(None)


@pytest.fixture
def pushed(monkeypatch):
    sent = []
    monkeypatch.setattr(sp.line_push, "push_messages",
                        lambda to, msgs: sent.append((to, msgs)) or True)
    return sent


def _create(**overrides) -> dict:
    body = {
        "title": "第2回五役予定者会議", "event_type": "五役会", "fiscal_year": 2027,
        "location": "JC会館 or オンライン", "audience": "五役の皆さま",
        "candidates": [{"start": "2026-11-06T19:00:00"}, {"start": "2026-11-05T19:00:00"}],
        "target_scope": {"kind": "officer", "value": ["理事長", "副理事長", "専務理事"]},
        "group_id": GROUP, "deadline": "2026-10-26T23:59:00",
    } | overrides
    res = admin.post("/api/polls", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _answer(poll_id, token, answers, **extra):
    return public.put(f"/liff/api/polls/{poll_id}/response", headers=_auth(token),
                      json={"answers": answers} | extra)


def test_create_sorts_candidates_and_lists_unlinked_targets(repo):
    data = _create()
    labels = [c["label"] for c in data["candidates"]]
    assert labels == ["11/5(木) 19:00〜", "11/6(金) 19:00〜"]
    # LINE 未連携の会員も対象者・未回答者に入る
    assert data["pending"] == ["遠藤 孝行", "渡部 邦祐", "佐川 竜二"]


def test_liff_requires_login_and_group_membership(repo):
    poll_id = _create()["poll"]["poll_id"]
    assert public.get(f"/liff/api/polls/{poll_id}").status_code == 401
    assert public.get(f"/liff/api/polls/{poll_id}", headers=_auth("bad")).status_code == 401
    assert public.get(f"/liff/api/polls/{poll_id}", headers=_auth("tok-x")).status_code == 403
    res = public.get(f"/liff/api/polls/{poll_id}", headers=_auth("tok-a"))
    assert res.status_code == 200
    view = res.json()
    assert view["me"]["linked"] is False
    assert [m["name"] for m in view["members"]] == ["遠藤 孝行", "渡部 邦祐", "佐川 竜二"]
    assert "line_user_id" not in str(view["responses"])  # 他人の userId は出さない


def test_linked_member_answers_without_choosing(repo):
    poll_id = _create()["poll"]["poll_id"]
    res = _answer(poll_id, "tok-sed", {"c1": "ok", "c2": "ng"}, comment="19:30から")
    assert res.status_code == 200
    view = res.json()
    assert view["me"]["member_name"] == "遠藤 孝行"
    assert view["responses"][0]["name"] == "遠藤 孝行"
    assert view["counts"]["c1"]["ok"] == 1
    assert admin.get(f"/api/polls/{poll_id}").json()["pending"] == ["渡部 邦祐", "佐川 竜二"]


def test_unlinked_member_self_selects_once(repo):
    poll_id = _create()["poll"]["poll_id"]
    assert _answer(poll_id, "tok-a", {"c1": "maybe", "c2": "ok"}, member_id="m5").status_code == 200
    # 同じ人を別の LINE で選ぶことはできない
    res = _answer(poll_id, "tok-b", {"c1": "ok", "c2": "ok"}, member_id="m5")
    assert res.status_code == 409
    # 対象外の会員は選べない
    assert _answer(poll_id, "tok-b", {"c1": "ok", "c2": "ok"},
                   member_id="m10").status_code == 400
    # 選ばなければ LINE名で回答
    view = _answer(poll_id, "tok-b", {"c1": "ok", "c2": "ok"}).json()
    names = [r["name"] for r in view["responses"]]
    assert names == ["佐川 竜二", "こひ（LINE名）"]
    summary = admin.get(f"/api/polls/{poll_id}").json()
    assert summary["pending"] == ["遠藤 孝行", "渡部 邦祐"]
    assert summary["others"][0]["name"] == "こひ（LINE名）"


def test_answer_validation(repo):
    poll_id = _create()["poll"]["poll_id"]
    assert _answer(poll_id, "tok-a", {"c1": "ok"}).status_code == 400  # 全候補に回答が必要
    assert _answer(poll_id, "tok-a", {"c1": "ok", "c2": "yes"}).status_code == 400


def test_closed_poll_rejects_answers(repo):
    poll_id = _create()["poll"]["poll_id"]
    assert admin.post(f"/api/polls/{poll_id}/close").status_code == 200
    assert _answer(poll_id, "tok-a", {"c1": "ok", "c2": "ok"}).status_code == 409


def test_announce_pushes_flex_to_group(repo, pushed):
    poll_id = _create()["poll"]["poll_id"]
    res = admin.post(f"/api/polls/{poll_id}/announce")
    assert res.status_code == 200
    (to, msgs), = pushed
    assert to == GROUP
    flex = msgs[0].to_dict()
    assert flex["type"] == "flex"
    uri = flex["contents"]["footer"]["contents"][0]["action"]["uri"]
    assert uri == f"https://liff.line.me/2000000000-abcdEFGH?poll={poll_id}"
    assert repo.get_poll(poll_id).announced_at is not None


def test_announce_requires_liff_settings(repo, pushed, monkeypatch):
    poll_id = _create()["poll"]["poll_id"]
    monkeypatch.delenv("LIFF_ID")
    assert admin.post(f"/api/polls/{poll_id}/announce").status_code == 400
    assert pushed == []


def test_remind_names_pending_members(repo, pushed):
    poll_id = _create()["poll"]["poll_id"]
    _answer(poll_id, "tok-sed", {"c1": "ok", "c2": "ok"})
    res = admin.post(f"/api/polls/{poll_id}/remind")
    assert res.json()["reminded"] == ["渡部 邦祐", "佐川 竜二"]
    text = pushed[0][1][0].text
    assert "渡部 邦祐さん・佐川 竜二さん" in text and "liff.line.me" in text


def test_decide_creates_event_and_notifies(repo, pushed):
    poll_id = _create()["poll"]["poll_id"]
    res = admin.post(f"/api/polls/{poll_id}/decide", json={"candidate_id": "c2"})
    assert res.status_code == 200
    event = repo.get_event(res.json()["event"]["event_id"])
    assert event.type == EventType.五役会
    assert event.title == "第2回五役予定者会議"
    assert event.fiscal_year == 2027
    assert event.datetime_start.isoformat() == "2026-11-06T19:00:00"
    assert event.status == "open"  # 下書きにしない（予定と出欠にすぐ出る）
    poll = repo.get_poll(poll_id)
    assert poll.status == PollStatus.decided and poll.event_id == event.event_id
    assert "11月6日(金) 19:00〜" in pushed[0][1][0].text
    # 二重に決定しない
    assert admin.post(f"/api/polls/{poll_id}/decide",
                      json={"candidate_id": "c1"}).status_code == 409


def test_decide_carries_answers_to_attendance(repo, pushed):
    """決定した候補日への回答を出欠に写す。○=出席・×=欠席・△=未回答（コメントはメモに）。"""
    poll_id = _create()["poll"]["poll_id"]
    _answer(poll_id, "tok-sed", {"c1": "ng", "c2": "ok"}, comment="19:30から")
    _answer(poll_id, "tok-a", {"c1": "ok", "c2": "maybe"}, member_id="m5")
    _answer(poll_id, "tok-b", {"c1": "ok", "c2": "ng"}, member_id="m3")
    event_id = admin.post(f"/api/polls/{poll_id}/decide",
                          json={"candidate_id": "c2"}).json()["event"]["event_id"]
    sed, vp, pres = (repo.get_attendance(event_id, m) for m in ("m2", "m5", "m3"))
    assert sed.status == "出席" and sed.free_text == "日程調整のコメント: 19:30から"
    assert sed.history[-1].by == "日程調整" and sed.responded_at is not None
    assert vp.status == "未回答" and vp.responded_at is None  # △は本人に答えてもらう
    assert pres.status == "欠席"


def test_liff_page_embeds_liff_id(repo, monkeypatch):
    res = public.get("/liff/poll")
    assert res.status_code == 200
    assert 'const LIFF_ID = "2000000000-abcdEFGH"' in res.text
    monkeypatch.delenv("LINE_LOGIN_CHANNEL_ID")
    assert public.get("/liff/poll").status_code == 503


def test_admin_poll_api_is_protected(repo):
    assert public.get("/api/polls").status_code == 401
    assert not "/liff/api/polls/x".startswith(main.PROTECTED_PREFIXES)
