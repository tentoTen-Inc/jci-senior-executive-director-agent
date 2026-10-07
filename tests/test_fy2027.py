"""2027年度への切り替え（組織図の取込・兼務・青年賛助会員・運用開始日）のテスト。"""
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import calendar_import, gcal, main, rag
from app.deps import set_repo
from app.events import resolve_scope
from app.kpi import attendance_trends
from app.models import (
    Attendance,
    AttendanceStatus,
    CommitteeSeat,
    Event,
    EventStatus,
    EventType,
    Member,
    MemberStatus,
    MemberType,
    TargetScope,
    TargetScopeKind,
)
from app.org_import import RosterEntry, name_key, plan
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

admin = TestClient(main.app, headers=ADMIN_AUTH)


def _entries(*items: dict) -> list[RosterEntry]:
    return [RosterEntry.model_validate(x) for x in items]


# --------------------------------------------------------------------------- #
# 組織図の取込
# --------------------------------------------------------------------------- #
def test_existing_member_matched_by_name_keeps_id_and_line():
    existing = [Member(member_id="m2", name="遠藤孝行", line_user_id="U_sed")]
    [change] = plan(existing, _entries({"name": "遠藤 孝行", "officer_role": "専務理事"}))
    assert change.action == "update"
    assert change.member.member_id == "m2"
    assert change.member.line_user_id == "U_sed"  # LINE 連携を引き継ぐ
    assert change.member.name == "遠藤 孝行"
    assert change.member.officer_role == "専務理事"
    assert existing[0].officer_role is None  # 元のオブジェクトは変えない


def test_new_members_get_next_ids_and_primary_committee():
    existing = [Member(member_id="m2", name="遠藤孝行")]
    changes = plan(existing, _entries(
        {"name": "遠藤 孝行"},
        {"name": "渡部 源大", "committees": [
            {"committee": "組織向上委員会", "role": "副委員長"},
            {"committee": "地域未来創造委員会", "role": "委員"}]},
        {"name": "嶋崎 真敬", "member_type": "youth_support",
         "committees": [{"committee": "地域未来創造委員会", "role": "委員"}]},
    ))
    created = [c.member for c in changes if c.action == "create"]
    assert [m.member_id for m in created] == ["m3", "m4"]
    genta = created[0]
    # 先頭が主たる所属
    assert (genta.committee, genta.committee_role) == ("組織向上委員会", "副委員長")
    assert genta.committee_names == {"組織向上委員会", "地域未来創造委員会"}


def test_rerun_is_unchanged():
    existing = [Member(member_id="m2", name="遠藤 孝行", officer_role="専務理事")]
    [change] = plan(existing, _entries({"name": "遠藤孝行", "officer_role": "専務理事"}))
    # 氏名の表記（空白）だけが違う
    assert change.action == "update" and change.fields == ["name"]
    [again] = plan([change.member], _entries({"name": "遠藤孝行", "officer_role": "専務理事"}))
    assert again.action == "unchanged"


def test_missing_members_are_deactivated_not_deleted():
    existing = [Member(member_id="m1", name="前年度 太郎"), Member(member_id="m2", name="遠藤孝行")]
    changes = plan(existing, _entries({"name": "遠藤 孝行"}), deactivate_missing=True)
    deact = [c for c in changes if c.action == "deactivate"]
    assert [c.member.member_id for c in deact] == ["m1"]
    assert deact[0].member.status == MemberStatus.inactive
    # 既定（deactivate_missing=False）は名簿に無い会員に触らない
    assert plan(existing, _entries({"name": "遠藤 孝行"}))[-1].action != "deactivate"


def test_duplicate_names_rejected():
    with pytest.raises(ValueError):
        plan([], _entries({"name": "村尾 碧"}, {"name": "村尾　碧"}))


def test_name_key_ignores_spaces():
    assert name_key("渡部　源大") == name_key("渡部 源大") == name_key("渡部源大")


# --------------------------------------------------------------------------- #
# 兼務・青年賛助会員
# --------------------------------------------------------------------------- #
def _member(mid, name, *, seats=(), mtype=MemberType.regular, line=True, **kw):
    return Member(member_id=mid, name=name, member_type=mtype,
                  line_user_id=f"U_{mid}" if line else None,
                  committees=[CommitteeSeat(committee=c, role=r) for c, r in seats], **kw)


def test_committee_scope_includes_concurrent_members():
    repo = InMemoryRepository()
    repo.upsert_member(_member("a", "遠藤 大介", seats=[("組織向上委員会", "委員長")]))
    repo.upsert_member(_member("b", "村尾 碧", seats=[("総務委員会", "副委員長"),
                                                      ("組織向上委員会", "委員")]))
    repo.upsert_member(_member("c", "櫻井 駿太", seats=[("総務委員会", "委員長")]))
    scope = TargetScope(kind=TargetScopeKind.committee, value=["組織向上委員会"])
    assert {m.member_id for m in resolve_scope(repo, scope)} == {"a", "b"}  # 兼務の村尾さんも届く


def test_legacy_single_committee_still_matches():
    repo = InMemoryRepository()
    repo.upsert_member(Member(member_id="x", name="旧データ", committee="総務委員会",
                              line_user_id="U_x"))
    scope = TargetScope(kind=TargetScopeKind.committee, value=["総務委員会"])
    assert [m.member_id for m in resolve_scope(repo, scope)] == ["x"]


def test_youth_support_member_is_deliverable_but_support_is_not():
    assert _member("y", "嶋崎 真敬", mtype=MemberType.youth_support).is_deliverable
    assert not _member("s", "賛助 会社", mtype=MemberType.support).is_deliverable


def test_rag_lists_all_committees():
    m = _member("b", "村尾 碧", seats=[("総務委員会", "副委員長"), ("組織向上委員会", "委員")])
    text = "\n".join(rag._self_lines(m))
    assert "所属委員会: 総務委員会（副委員長）、組織向上委員会（委員）" in text


def test_editing_primary_committee_keeps_concurrent_seats():
    repo = InMemoryRepository()
    repo.upsert_member(_member("b", "村尾 碧", seats=[("総務委員会", "副委員長"),
                                                      ("組織向上委員会", "委員")],
                               committee="総務委員会", committee_role="副委員長"))
    set_repo(repo)
    try:
        res = admin.put("/api/members/b", json={"committee_role": "委員長"})
    finally:
        set_repo(None)
    seats = [(s["committee"], s["role"]) for s in res.json()["committees"]]
    assert seats == [("総務委員会", "委員長"), ("組織向上委員会", "委員")]


# --------------------------------------------------------------------------- #
# 運用開始日（2026年度のデータは消さずに対象外にする）
# --------------------------------------------------------------------------- #
@pytest.fixture
def fy_repo():
    repo = InMemoryRepository()
    settings = repo.get_settings()
    settings.operation_start = date(2027, 1, 1)
    repo.save_settings(settings)
    for eid, start in (("old", datetime(2026, 7, 21, 19, 0)),
                       ("new", datetime(2027, 1, 20, 19, 0))):
        repo.upsert_event(Event(event_id=eid, type=EventType.例会, title=eid,
                                datetime_start=start, status=EventStatus.open))
    set_repo(repo)
    yield repo
    set_repo(None)


def test_admin_list_hides_events_before_operation_start(fy_repo):
    assert [e["event_id"] for e in admin.get("/api/events").json()] == ["new"]
    everything = admin.get("/api/events", params={"include_previous": "true"}).json()
    assert sorted(e["event_id"] for e in everything) == ["new", "old"]
    assert fy_repo.get_event("old") is not None  # データは消さない


def test_kpi_trends_ignore_previous_year(fy_repo):
    fy_repo.upsert_member(_member("a", "会員"))
    for eid in ("old", "new"):
        fy_repo.upsert_attendance(Attendance(event_id=eid, member_id="a",
                                             status=AttendanceStatus.出席))
    assert [p.event_id for p in attendance_trends(fy_repo)] == ["new"]


def test_calendar_import_skips_before_operation_start(fy_repo, monkeypatch):
    monkeypatch.setenv("GCAL_CALENDAR_ID", "inawashiro.jc@gmail.com")
    now = datetime(2026, 10, 7, 12, 0)

    def item(gid, start):
        return {"id": gid, "etag": '"1"', "status": "confirmed", "summary": "【例会】" + gid,
                "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S+09:00")},
                "end": {"dateTime": (start + timedelta(hours=2))
                        .strftime("%Y-%m-%dT%H:%M:%S+09:00")},
                "updated": "2026-10-07T03:00:00.000Z"}

    monkeypatch.setattr(gcal, "list_events", lambda **kw: {"items": [
        item("g2026", datetime(2026, 11, 18, 19, 0)), item("g2027", datetime(2027, 1, 20, 19, 0)),
    ], "nextSyncToken": "t"})
    result = calendar_import.pull_changes(fy_repo, now)
    assert result["created"] == 1
    imported = [e for e in fy_repo.list_events() if e.gcal_event_id]
    assert [e.gcal_event_id for e in imported] == ["g2027"]
