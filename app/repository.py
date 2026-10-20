"""リポジトリ抽象とインメモリ実装。

本番は FirestoreRepository（app/firestore_repo.py）を使用し、
テストは InMemoryRepository を使う。両者は Repository プロトコルを満たす。
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .models import (
    Attendance,
    AuditLog,
    CalendarSyncState,
    Conversation,
    DeliveryJob,
    DeliveryLog,
    Escalation,
    Event,
    EventStatus,
    ExternalNotice,
    InferenceLog,
    InviteCode,
    LineGroup,
    LinkRequest,
    LinkState,
    Member,
    MemberStatus,
    NoticeAction,
    PendingCalendarOp,
    PollResponse,
    Proposal,
    ReminderPolicy,
    ScheduledPost,
    SchedulePoll,
    Settings,
    Survey,
)


class Repository(Protocol):
    # --- members ---
    def upsert_member(self, member: Member) -> None: ...
    def get_member(self, member_id: str) -> Member | None: ...
    def get_member_by_line_user_id(self, line_user_id: str) -> Member | None: ...
    def list_members(self, *, active_only: bool = False) -> list[Member]: ...

    # --- invite codes ---
    def save_invite_code(self, code: InviteCode) -> None: ...
    def get_invite_code(self, code: str) -> InviteCode | None: ...
    def list_invite_codes(self) -> list[InviteCode]: ...

    # --- link state ---
    def get_link_state(self, line_user_id: str) -> LinkState | None: ...
    def save_link_state(self, state: LinkState) -> None: ...

    # --- events ---
    def upsert_event(self, event: Event) -> None: ...
    def get_event(self, event_id: str) -> Event | None: ...
    def list_events(self, *, status: EventStatus | None = None) -> list[Event]: ...

    # --- attendances ---
    def upsert_attendance(self, attendance: Attendance) -> None: ...
    def get_attendance(self, event_id: str, member_id: str) -> Attendance | None: ...
    def list_attendances(self, event_id: str) -> list[Attendance]: ...

    # --- policies / settings ---
    def upsert_policy(self, policy: ReminderPolicy) -> None: ...
    def get_policy(self, policy_id: str) -> ReminderPolicy | None: ...
    def get_settings(self) -> Settings: ...
    def save_settings(self, settings: Settings) -> None: ...
    def get_gcal_sync_state(self) -> CalendarSyncState: ...
    def save_gcal_sync_state(self, state: CalendarSyncState) -> None: ...
    def save_calendar_op(self, op: PendingCalendarOp) -> None: ...
    def get_calendar_op(self, op_id: str) -> PendingCalendarOp | None: ...
    def save_line_group(self, group: LineGroup) -> None: ...
    def get_line_group(self, group_id: str) -> LineGroup | None: ...
    def list_line_groups(self) -> list[LineGroup]: ...
    def claim_line_event(self, event_id: str) -> bool:
        """LINE イベントの処理権を取る。初回だけ True（再送・重複配信は False）。"""
        ...

    # --- delivery ---
    def save_delivery_job(self, job: DeliveryJob) -> None: ...
    def get_delivery_job(self, job_id: str) -> DeliveryJob | None: ...
    def save_delivery_log(self, log: DeliveryLog) -> None: ...
    def list_delivery_logs(self, *, job_id: str | None = None) -> list[DeliveryLog]: ...

    # --- escalations ---
    def save_escalation(self, escalation: Escalation) -> None: ...
    def get_escalation(self, escalation_id: str) -> Escalation | None: ...
    def list_escalations(self, *, status: str | None = None) -> list[Escalation]: ...

    # --- audit ---
    def save_audit(self, entry: AuditLog) -> None: ...
    def list_audit(self, *, limit: int = 100) -> list[AuditLog]: ...

    # --- proposals ---
    def upsert_proposal(self, proposal: Proposal) -> None: ...
    def get_proposal(self, proposal_id: str) -> Proposal | None: ...
    def list_proposals(self, *, status: str | None = None) -> list[Proposal]: ...

    # --- inference (LLMコスト) ---
    def save_inference_log(self, log: InferenceLog) -> None: ...
    def list_inference_logs(self, *, since: datetime | None = None) -> list[InferenceLog]: ...

    # --- 対外連絡 ---
    def upsert_notice(self, notice: ExternalNotice) -> None: ...
    def get_notice(self, notice_id: str) -> ExternalNotice | None: ...
    def get_notice_by_source_ref(self, source_ref: str) -> ExternalNotice | None: ...
    def list_notices(self, *, status: str | None = None) -> list[ExternalNotice]: ...
    def upsert_notice_action(self, action: NoticeAction) -> None: ...
    def get_notice_action(self, action_id: str) -> NoticeAction | None: ...
    def list_notice_actions(
        self, *, notice_id: str | None = None, status: str | None = None
    ) -> list[NoticeAction]: ...

    # --- 会話履歴 ---
    def get_conversation(self, member_id: str) -> Conversation | None: ...
    def save_conversation(self, conversation: Conversation) -> None: ...

    # --- アンケート ---
    def upsert_survey(self, survey: Survey) -> None: ...
    def get_survey(self, survey_id: str) -> Survey | None: ...
    def get_survey_by_form_id(self, form_id: str) -> Survey | None: ...
    def list_surveys(self, *, kind: str | None = None) -> list[Survey]: ...

    # --- 日程調整 ---
    def save_poll(self, poll: SchedulePoll) -> None: ...
    def get_poll(self, poll_id: str) -> SchedulePoll | None: ...
    def list_polls(self) -> list[SchedulePoll]: ...
    def save_poll_response(self, response: PollResponse) -> None: ...
    def list_poll_responses(self, poll_id: str) -> list[PollResponse]: ...

    # --- LINE 連携の申請・予約投稿 ---
    def save_link_request(self, request: LinkRequest) -> None: ...
    def get_link_request(self, line_user_id: str) -> LinkRequest | None: ...
    def list_link_requests(self) -> list[LinkRequest]: ...
    def save_scheduled_post(self, post: ScheduledPost) -> None: ...
    def list_scheduled_posts(self) -> list[ScheduledPost]: ...


class InMemoryRepository:
    """テスト・ローカル用のインメモリ実装。"""

    def __init__(self) -> None:
        self._members: dict[str, Member] = {}
        self._invite_codes: dict[str, InviteCode] = {}
        self._link_states: dict[str, LinkState] = {}
        self._events: dict[str, Event] = {}
        self._attendances: dict[str, Attendance] = {}
        self._policies: dict[str, ReminderPolicy] = {}
        self._settings: Settings = Settings()
        self._gcal_sync: CalendarSyncState = CalendarSyncState()
        self._calendar_ops: dict[str, PendingCalendarOp] = {}
        self._line_groups: dict[str, LineGroup] = {}
        self._jobs: dict[str, DeliveryJob] = {}
        self._logs: list[DeliveryLog] = []
        self._escalations: dict[str, Escalation] = {}
        self._audit: list[AuditLog] = []
        self._proposals: dict[str, Proposal] = {}
        self._inference_logs: list[InferenceLog] = []
        self._notices: dict[str, ExternalNotice] = {}
        self._notice_actions: dict[str, NoticeAction] = {}
        self._conversations: dict[str, Conversation] = {}
        self._surveys: dict[str, Survey] = {}
        self._claimed_line_events: set[str] = set()
        self._polls: dict[str, SchedulePoll] = {}
        self._poll_responses: dict[str, PollResponse] = {}
        self._link_requests: dict[str, LinkRequest] = {}
        self._scheduled_posts: dict[str, ScheduledPost] = {}

    # --- members ---
    def upsert_member(self, member: Member) -> None:
        self._members[member.member_id] = member.model_copy(deep=True)

    def get_member(self, member_id: str) -> Member | None:
        m = self._members.get(member_id)
        return m.model_copy(deep=True) if m else None

    def get_member_by_line_user_id(self, line_user_id: str) -> Member | None:
        for m in self._members.values():
            if m.line_user_id == line_user_id:
                return m.model_copy(deep=True)
        return None

    def list_members(self, *, active_only: bool = False) -> list[Member]:
        members = list(self._members.values())
        if active_only:
            members = [m for m in members if m.status == MemberStatus.active]
        return [m.model_copy(deep=True) for m in members]

    # --- invite codes ---
    def save_invite_code(self, code: InviteCode) -> None:
        self._invite_codes[code.code] = code.model_copy(deep=True)

    def get_invite_code(self, code: str) -> InviteCode | None:
        c = self._invite_codes.get(code)
        return c.model_copy(deep=True) if c else None

    def list_invite_codes(self) -> list[InviteCode]:
        return [c.model_copy(deep=True) for c in self._invite_codes.values()]

    # --- link state ---
    def get_link_state(self, line_user_id: str) -> LinkState | None:
        s = self._link_states.get(line_user_id)
        return s.model_copy(deep=True) if s else None

    def save_link_state(self, state: LinkState) -> None:
        self._link_states[state.line_user_id] = state.model_copy(deep=True)

    # --- events ---
    def upsert_event(self, event: Event) -> None:
        self._events[event.event_id] = event.model_copy(deep=True)

    def get_event(self, event_id: str) -> Event | None:
        e = self._events.get(event_id)
        return e.model_copy(deep=True) if e else None

    def list_events(self, *, status: EventStatus | None = None) -> list[Event]:
        events = list(self._events.values())
        if status is not None:
            events = [e for e in events if e.status == status]
        return [e.model_copy(deep=True) for e in events]

    # --- attendances ---
    def upsert_attendance(self, attendance: Attendance) -> None:
        self._attendances[attendance.doc_id] = attendance.model_copy(deep=True)

    def get_attendance(self, event_id: str, member_id: str) -> Attendance | None:
        a = self._attendances.get(f"{event_id}_{member_id}")
        return a.model_copy(deep=True) if a else None

    def list_attendances(self, event_id: str) -> list[Attendance]:
        return [
            a.model_copy(deep=True)
            for a in self._attendances.values()
            if a.event_id == event_id
        ]

    # --- policies / settings ---
    def upsert_policy(self, policy: ReminderPolicy) -> None:
        self._policies[policy.policy_id] = policy.model_copy(deep=True)

    def get_policy(self, policy_id: str) -> ReminderPolicy | None:
        p = self._policies.get(policy_id)
        return p.model_copy(deep=True) if p else None

    def get_settings(self) -> Settings:
        return self._settings.model_copy(deep=True)

    def save_settings(self, settings: Settings) -> None:
        self._settings = settings.model_copy(deep=True)

    def get_gcal_sync_state(self) -> CalendarSyncState:
        return self._gcal_sync.model_copy(deep=True)

    def save_gcal_sync_state(self, state: CalendarSyncState) -> None:
        self._gcal_sync = state.model_copy(deep=True)

    def save_calendar_op(self, op: PendingCalendarOp) -> None:
        self._calendar_ops[op.op_id] = op.model_copy(deep=True)

    def get_calendar_op(self, op_id: str) -> PendingCalendarOp | None:
        op = self._calendar_ops.get(op_id)
        return op.model_copy(deep=True) if op else None

    def save_line_group(self, group: LineGroup) -> None:
        self._line_groups[group.group_id] = group.model_copy(deep=True)

    def get_line_group(self, group_id: str) -> LineGroup | None:
        group = self._line_groups.get(group_id)
        return group.model_copy(deep=True) if group else None

    def claim_line_event(self, event_id: str) -> bool:
        if event_id in self._claimed_line_events:
            return False
        self._claimed_line_events.add(event_id)
        return True

    def list_line_groups(self) -> list[LineGroup]:
        return [g.model_copy(deep=True) for g in self._line_groups.values()]

    # --- delivery ---
    def save_delivery_job(self, job: DeliveryJob) -> None:
        self._jobs[job.job_id] = job.model_copy(deep=True)

    def get_delivery_job(self, job_id: str) -> DeliveryJob | None:
        j = self._jobs.get(job_id)
        return j.model_copy(deep=True) if j else None

    def save_delivery_log(self, log: DeliveryLog) -> None:
        self._logs.append(log.model_copy(deep=True))

    def list_delivery_logs(self, *, job_id: str | None = None) -> list[DeliveryLog]:
        logs = self._logs
        if job_id is not None:
            logs = [log for log in logs if log.job_id == job_id]
        return [log.model_copy(deep=True) for log in logs]

    # --- escalations ---
    def save_escalation(self, escalation: Escalation) -> None:
        self._escalations[escalation.escalation_id] = escalation.model_copy(deep=True)

    def get_escalation(self, escalation_id: str) -> Escalation | None:
        e = self._escalations.get(escalation_id)
        return e.model_copy(deep=True) if e else None

    def list_escalations(self, *, status: str | None = None) -> list[Escalation]:
        items = list(self._escalations.values())
        if status is not None:
            items = [e for e in items if e.status == status]
        items.sort(key=lambda e: e.created_at, reverse=True)
        return [e.model_copy(deep=True) for e in items]

    # --- audit ---
    def save_audit(self, entry: AuditLog) -> None:
        self._audit.append(entry.model_copy(deep=True))

    def list_audit(self, *, limit: int = 100) -> list[AuditLog]:
        items = sorted(self._audit, key=lambda a: a.at, reverse=True)[:limit]
        return [a.model_copy(deep=True) for a in items]

    # --- proposals ---
    def upsert_proposal(self, proposal: Proposal) -> None:
        self._proposals[proposal.proposal_id] = proposal.model_copy(deep=True)

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        p = self._proposals.get(proposal_id)
        return p.model_copy(deep=True) if p else None

    def list_proposals(self, *, status: str | None = None) -> list[Proposal]:
        items = list(self._proposals.values())
        if status is not None:
            items = [p for p in items if p.status == status]
        return [p.model_copy(deep=True) for p in items]

    # --- inference (LLMコスト) ---
    def save_inference_log(self, log: InferenceLog) -> None:
        self._inference_logs.append(log.model_copy(deep=True))

    def list_inference_logs(self, *, since: datetime | None = None) -> list[InferenceLog]:
        items = self._inference_logs
        if since is not None:
            items = [x for x in items if x.at >= since]
        return [x.model_copy(deep=True) for x in items]

    # --- 対外連絡 ---
    def upsert_notice(self, notice: ExternalNotice) -> None:
        self._notices[notice.notice_id] = notice.model_copy(deep=True)

    def get_notice(self, notice_id: str) -> ExternalNotice | None:
        n = self._notices.get(notice_id)
        return n.model_copy(deep=True) if n else None

    def get_notice_by_source_ref(self, source_ref: str) -> ExternalNotice | None:
        for n in self._notices.values():
            if n.source_ref == source_ref:
                return n.model_copy(deep=True)
        return None

    def list_notices(self, *, status: str | None = None) -> list[ExternalNotice]:
        items = list(self._notices.values())
        if status is not None:
            items = [n for n in items if n.status == status]
        items.sort(key=lambda n: n.received_at, reverse=True)
        return [n.model_copy(deep=True) for n in items]

    def upsert_notice_action(self, action: NoticeAction) -> None:
        self._notice_actions[action.action_id] = action.model_copy(deep=True)

    def get_notice_action(self, action_id: str) -> NoticeAction | None:
        a = self._notice_actions.get(action_id)
        return a.model_copy(deep=True) if a else None

    def list_notice_actions(
        self, *, notice_id: str | None = None, status: str | None = None
    ) -> list[NoticeAction]:
        items = list(self._notice_actions.values())
        if notice_id is not None:
            items = [a for a in items if a.notice_id == notice_id]
        if status is not None:
            items = [a for a in items if a.status == status]
        items.sort(key=lambda a: a.created_at)
        return [a.model_copy(deep=True) for a in items]

    # --- 会話履歴 ---
    def get_conversation(self, member_id: str) -> Conversation | None:
        c = self._conversations.get(member_id)
        return c.model_copy(deep=True) if c else None

    def save_conversation(self, conversation: Conversation) -> None:
        self._conversations[conversation.member_id] = conversation.model_copy(deep=True)

    # --- アンケート ---
    def upsert_survey(self, survey: Survey) -> None:
        self._surveys[survey.survey_id] = survey.model_copy(deep=True)

    def get_survey(self, survey_id: str) -> Survey | None:
        s = self._surveys.get(survey_id)
        return s.model_copy(deep=True) if s else None

    def get_survey_by_form_id(self, form_id: str) -> Survey | None:
        for s in self._surveys.values():
            if s.form_id == form_id:
                return s.model_copy(deep=True)
        return None

    def list_surveys(self, *, kind: str | None = None) -> list[Survey]:
        items = list(self._surveys.values())
        if kind is not None:
            items = [s for s in items if s.kind == kind]
        items.sort(key=lambda s: (s.synced_at or datetime.min), reverse=True)
        return [s.model_copy(deep=True) for s in items]

    # --- 日程調整 ---
    def save_poll(self, poll: SchedulePoll) -> None:
        self._polls[poll.poll_id] = poll.model_copy(deep=True)

    def get_poll(self, poll_id: str) -> SchedulePoll | None:
        poll = self._polls.get(poll_id)
        return poll.model_copy(deep=True) if poll else None

    def list_polls(self) -> list[SchedulePoll]:
        items = sorted(self._polls.values(), key=lambda p: p.created_at or datetime.min,
                       reverse=True)
        return [p.model_copy(deep=True) for p in items]

    def save_poll_response(self, response: PollResponse) -> None:
        self._poll_responses[response.doc_id] = response.model_copy(deep=True)

    def list_poll_responses(self, poll_id: str) -> list[PollResponse]:
        return [r.model_copy(deep=True) for r in self._poll_responses.values()
                if r.poll_id == poll_id]

    # --- LINE 連携の申請・予約投稿 ---
    def save_link_request(self, request: LinkRequest) -> None:
        self._link_requests[request.line_user_id] = request.model_copy(deep=True)

    def get_link_request(self, line_user_id: str) -> LinkRequest | None:
        request = self._link_requests.get(line_user_id)
        return request.model_copy(deep=True) if request else None

    def list_link_requests(self) -> list[LinkRequest]:
        return [r.model_copy(deep=True) for r in self._link_requests.values()]

    def save_scheduled_post(self, post: ScheduledPost) -> None:
        self._scheduled_posts[post.post_id] = post.model_copy(deep=True)

    def list_scheduled_posts(self) -> list[ScheduledPost]:
        return sorted((p.model_copy(deep=True) for p in self._scheduled_posts.values()),
                      key=lambda p: p.send_at)


def utcnow() -> datetime:
    """テストで monkeypatch しやすいよう集約した現在時刻取得。"""
    return datetime.now()
