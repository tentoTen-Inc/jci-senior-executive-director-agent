"""催促スケジューラ・配信実行のユニットテスト。"""
from datetime import datetime, timedelta

from app.attendance import record_attendance
from app.delivery import execute_delivery
from app.models import (
    AttendanceStatus,
    Event,
    EventStatus,
    EventType,
    Member,
    QuietHours,
    Settings,
    TargetScope,
    TargetScopeKind,
)
from app.reminders import (
    EXAMPLE_MEETING_POLICY,
    due_stages,
    fire_time,
    plan_reminders,
    resolve_audience,
)
from app.repository import InMemoryRepository

DEADLINE = datetime(2026, 6, 25, 23, 59)


def setup_repo() -> InMemoryRepository:
    repo = InMemoryRepository()
    for mid in ("m1", "m2", "m3"):
        repo.upsert_member(Member(member_id=mid, name=mid, line_user_id=f"U_{mid}"))
    repo.upsert_policy(EXAMPLE_MEETING_POLICY)
    repo.upsert_event(
        Event(
            event_id="e1",
            type=EventType.例会,
            title="6月例会",
            datetime_start=datetime(2026, 6, 25, 19, 0),
            attendance_deadline=DEADLINE,
            target_scope=TargetScope(kind=TargetScopeKind.all),
            reminder_policy_id="rp_例会_default",
            status=EventStatus.open,
        )
    )
    return repo


def test_fire_time_and_due_stages():
    repo = setup_repo()
    event = repo.get_event("e1")
    # 依頼は7日前
    req_stage = EXAMPLE_MEETING_POLICY.stages[0]
    assert fire_time(event, req_stage) == DEADLINE - timedelta(days=7)
    # 6日前時点 → 依頼のみ due
    now = DEADLINE - timedelta(days=6)
    due = due_stages(EXAMPLE_MEETING_POLICY, event, now)
    assert [s.name for s in due] == ["依頼"]


def test_resolve_audience_unanswered():
    repo = setup_repo()
    record_attendance(repo, "e1", "m1", AttendanceStatus.出席, now=DEADLINE - timedelta(days=6))
    ids = resolve_audience(repo, repo.get_event("e1"), "unanswered")
    assert set(ids) == {"m2", "m3"}


def test_plan_reminders_sends_only_latest_due_stage():
    """過ぎた段階をまとめて送らない。依頼・リマインドが両方 due ならリマインドだけ。"""
    repo = setup_repo()
    now = DEADLINE - timedelta(days=2)  # 依頼/リマインドが due
    jobs = plan_reminders(repo, now)
    assert {j.stage for j in jobs} == {"リマインド"}


def test_no_reminders_for_events_already_started():
    """受付中のまま残った過去の予定に、後から連携した会員へ出欠確認を送らない（2026-10-21）。"""
    repo = setup_repo()
    after_start = datetime(2026, 6, 25, 19, 0)
    assert plan_reminders(repo, after_start) == []
    assert plan_reminders(repo, datetime(2026, 10, 21, 8, 0)) == []


def test_late_linked_member_gets_only_latest_stage():
    """依頼を送ったあとに連携した人には、過ぎた依頼ではなく今の段階だけを送る。"""
    repo = setup_repo()
    t_req = datetime(2026, 6, 19, 12, 0)
    job = plan_reminders(repo, t_req)[0]
    execute_delivery(repo, job, "出欠依頼", now=t_req, sender=lambda mid: True)
    repo.upsert_member(Member(member_id="m4", name="m4", line_user_id="U_m4"))  # 後から連携
    jobs = plan_reminders(repo, DEADLINE - timedelta(hours=12))  # 依頼・リマインド・最終が due
    assert [(j.stage, j.targets) for j in jobs] == [("最終", ["m1", "m2", "m3", "m4"])]


def test_member_level_idempotent_after_ok_send():
    """ok送信済みメンバーは再planで対象から外れる（二重送信しない）。"""
    repo = setup_repo()
    now = datetime(2026, 6, 19, 12, 0)  # 依頼ステージのみ due・昼間
    job = plan_reminders(repo, now)[0]
    execute_delivery(repo, job, "出欠依頼", now=now, sender=lambda mid: True)
    # 全員ok済み → 再planでは依頼ステージの対象者ゼロ
    jobs2 = plan_reminders(repo, now)
    assert all(j.stage != "依頼" for j in jobs2)


def test_blocked_member_is_resent_next_tick():
    """静音時間でブロックされた未送信者は次tickで再送対象になる。"""
    from app.models import QuietHours, Settings

    repo = setup_repo()
    repo.save_settings(Settings(quiet_hours=QuietHours(start="21:00", end="08:00")))
    # 1) 静音時間(23時)に発火 → 全員deferred(未ok)
    t_night = datetime(2026, 6, 19, 23, 0)
    job = plan_reminders(repo, t_night)[0]
    rep1 = execute_delivery(repo, job, "出欠依頼", now=t_night, sender=lambda mid: True)
    assert rep1.deferred == 3 and rep1.sent == 0
    # 2) 翌朝(昼間)に再tick → 同じ3人が対象として残り、今度は送信される
    t_day = datetime(2026, 6, 20, 12, 0)
    jobs2 = plan_reminders(repo, t_day)
    req = next(j for j in jobs2 if j.stage == "依頼")
    assert set(req.targets) == {"m1", "m2", "m3"}
    rep2 = execute_delivery(repo, req, "出欠依頼", now=t_day, sender=lambda mid: True)
    assert rep2.sent == 3


def test_execute_delivery_sends_to_targets():
    repo = setup_repo()
    now = datetime(2026, 6, 19, 12, 0)  # 昼間（静音時間外）・依頼ステージのみ due
    jobs = plan_reminders(repo, now)
    job = jobs[0]
    sent_to: list[str] = []
    report = execute_delivery(repo, job, "出欠のご回答をお願いします", now=now,
                              sender=lambda mid: sent_to.append(mid) or True)
    assert report.sent == 3
    assert set(sent_to) == {"m1", "m2", "m3"}
    assert len(repo.list_delivery_logs(job_id=job.job_id)) == 3


def test_execute_delivery_halts_on_sanity_problem():
    repo = setup_repo()
    now = DEADLINE - timedelta(days=6)
    job = plan_reminders(repo, now)[0]
    report = execute_delivery(repo, job, "   ", now=now, sender=lambda mid: True)
    assert report.halted is True
    assert "empty_body" in report.problems


def test_execute_delivery_blocked_by_kill_switch():
    repo = setup_repo()
    repo.save_settings(Settings(kill_switch=True))
    now = DEADLINE - timedelta(days=6)
    job = plan_reminders(repo, now)[0]
    report = execute_delivery(repo, job, "本文", now=now, sender=lambda mid: True)
    assert report.sent == 0
    assert report.blocked == 3


def test_execute_delivery_defers_in_quiet_hours():
    repo = setup_repo()
    repo.save_settings(Settings(quiet_hours=QuietHours(start="21:00", end="08:00")))
    now = DEADLINE.replace(hour=23, minute=0) - timedelta(days=6)
    job = plan_reminders(repo, now)[0]
    report = execute_delivery(repo, job, "本文", now=now, sender=lambda mid: True)
    assert report.deferred == 3
    assert report.sent == 0
