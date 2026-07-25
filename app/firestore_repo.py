"""Firestore 実装の Repository。

Repository プロトコルを満たす。ライブ接続を伴うため CI ではテストせず、
インターフェースの整合とシリアライズ往復のみを担保する設計。
"""
from __future__ import annotations

from datetime import datetime

from .models import (
    Attendance,
    AuditLog,
    Conversation,
    DeliveryJob,
    DeliveryLog,
    Escalation,
    Event,
    EventStatus,
    ExternalNotice,
    InferenceLog,
    InviteCode,
    LinkState,
    Member,
    MemberStatus,
    NoticeAction,
    Proposal,
    ReminderPolicy,
    Settings,
)

# コレクション名
COL_MEMBERS = "members"
COL_INVITE_CODES = "inviteCodes"
COL_LINK_STATES = "linkStates"
COL_EVENTS = "events"
COL_ATTENDANCES = "attendances"
COL_POLICIES = "reminderPolicies"
COL_SETTINGS = "settings"
COL_JOBS = "deliveryJobs"
COL_LOGS = "deliveryLogs"
COL_ESCALATIONS = "escalations"
COL_AUDIT = "auditLogs"
COL_PROPOSALS = "proposals"
COL_INFERENCE_LOGS = "inferenceLogs"
COL_NOTICES = "externalNotices"
COL_NOTICE_ACTIONS = "noticeActions"
COL_CONVERSATIONS = "conversations"
SETTINGS_DOC = "global"


class FirestoreRepository:
    def __init__(self, client=None, project: str | None = None) -> None:
        if client is None:
            from google.cloud import firestore  # 遅延 import（テスト時の依存回避）

            client = firestore.Client(project=project)
        self._db = client

    # --- members ---
    def upsert_member(self, member: Member) -> None:
        self._db.collection(COL_MEMBERS).document(member.member_id).set(
            member.model_dump(mode="json")
        )

    def get_member(self, member_id: str) -> Member | None:
        snap = self._db.collection(COL_MEMBERS).document(member_id).get()
        return Member.model_validate(snap.to_dict()) if snap.exists else None

    def get_member_by_line_user_id(self, line_user_id: str) -> Member | None:
        query = self._db.collection(COL_MEMBERS).where("line_user_id", "==", line_user_id).limit(1)
        for snap in query.stream():
            return Member.model_validate(snap.to_dict())
        return None

    def list_members(self, *, active_only: bool = False) -> list[Member]:
        col = self._db.collection(COL_MEMBERS)
        if active_only:
            col = col.where("status", "==", MemberStatus.active.value)
        return [Member.model_validate(s.to_dict()) for s in col.stream()]

    # --- invite codes ---
    def save_invite_code(self, code: InviteCode) -> None:
        self._db.collection(COL_INVITE_CODES).document(code.code).set(code.model_dump(mode="json"))

    def get_invite_code(self, code: str) -> InviteCode | None:
        snap = self._db.collection(COL_INVITE_CODES).document(code).get()
        return InviteCode.model_validate(snap.to_dict()) if snap.exists else None

    def list_invite_codes(self) -> list[InviteCode]:
        return [
            InviteCode.model_validate(s.to_dict())
            for s in self._db.collection(COL_INVITE_CODES).stream()
        ]

    # --- link state ---
    def get_link_state(self, line_user_id: str) -> LinkState | None:
        snap = self._db.collection(COL_LINK_STATES).document(line_user_id).get()
        return LinkState.model_validate(snap.to_dict()) if snap.exists else None

    def save_link_state(self, state: LinkState) -> None:
        self._db.collection(COL_LINK_STATES).document(state.line_user_id).set(
            state.model_dump(mode="json")
        )

    # --- events ---
    def upsert_event(self, event: Event) -> None:
        self._db.collection(COL_EVENTS).document(event.event_id).set(event.model_dump(mode="json"))

    def get_event(self, event_id: str) -> Event | None:
        snap = self._db.collection(COL_EVENTS).document(event_id).get()
        return Event.model_validate(snap.to_dict()) if snap.exists else None

    def list_events(self, *, status: EventStatus | None = None) -> list[Event]:
        col = self._db.collection(COL_EVENTS)
        if status is not None:
            col = col.where("status", "==", status.value)
        return [Event.model_validate(s.to_dict()) for s in col.stream()]

    # --- attendances ---
    def upsert_attendance(self, attendance: Attendance) -> None:
        self._db.collection(COL_ATTENDANCES).document(attendance.doc_id).set(
            attendance.model_dump(mode="json")
        )

    def get_attendance(self, event_id: str, member_id: str) -> Attendance | None:
        snap = self._db.collection(COL_ATTENDANCES).document(f"{event_id}_{member_id}").get()
        return Attendance.model_validate(snap.to_dict()) if snap.exists else None

    def list_attendances(self, event_id: str) -> list[Attendance]:
        query = self._db.collection(COL_ATTENDANCES).where("event_id", "==", event_id)
        return [Attendance.model_validate(s.to_dict()) for s in query.stream()]

    # --- policies / settings ---
    def upsert_policy(self, policy: ReminderPolicy) -> None:
        self._db.collection(COL_POLICIES).document(policy.policy_id).set(
            policy.model_dump(mode="json")
        )

    def get_policy(self, policy_id: str) -> ReminderPolicy | None:
        snap = self._db.collection(COL_POLICIES).document(policy_id).get()
        return ReminderPolicy.model_validate(snap.to_dict()) if snap.exists else None

    def get_settings(self) -> Settings:
        snap = self._db.collection(COL_SETTINGS).document(SETTINGS_DOC).get()
        return Settings.model_validate(snap.to_dict()) if snap.exists else Settings()

    def save_settings(self, settings: Settings) -> None:
        self._db.collection(COL_SETTINGS).document(SETTINGS_DOC).set(settings.model_dump(mode="json"))

    # --- delivery ---
    def save_delivery_job(self, job: DeliveryJob) -> None:
        self._db.collection(COL_JOBS).document(job.job_id).set(job.model_dump(mode="json"))

    def get_delivery_job(self, job_id: str) -> DeliveryJob | None:
        snap = self._db.collection(COL_JOBS).document(job_id).get()
        return DeliveryJob.model_validate(snap.to_dict()) if snap.exists else None

    def save_delivery_log(self, log: DeliveryLog) -> None:
        self._db.collection(COL_LOGS).document(log.log_id).set(log.model_dump(mode="json"))

    def list_delivery_logs(self, *, job_id: str | None = None) -> list[DeliveryLog]:
        col = self._db.collection(COL_LOGS)
        if job_id is not None:
            col = col.where("job_id", "==", job_id)
        return [DeliveryLog.model_validate(s.to_dict()) for s in col.stream()]

    # --- escalations ---
    def save_escalation(self, escalation: Escalation) -> None:
        self._db.collection(COL_ESCALATIONS).document(escalation.escalation_id).set(
            escalation.model_dump(mode="json")
        )

    def get_escalation(self, escalation_id: str) -> Escalation | None:
        snap = self._db.collection(COL_ESCALATIONS).document(escalation_id).get()
        return Escalation.model_validate(snap.to_dict()) if snap.exists else None

    def list_escalations(self, *, status: str | None = None) -> list[Escalation]:
        col = self._db.collection(COL_ESCALATIONS)
        if status is not None:
            col = col.where("status", "==", status)
        return sorted(
            (Escalation.model_validate(s.to_dict()) for s in col.stream()),
            key=lambda e: e.created_at,
            reverse=True,
        )

    # --- audit ---
    def save_audit(self, entry: AuditLog) -> None:
        self._db.collection(COL_AUDIT).document(entry.audit_id).set(entry.model_dump(mode="json"))

    def list_audit(self, *, limit: int = 100) -> list[AuditLog]:
        from google.cloud.firestore import Query

        col = (
            self._db.collection(COL_AUDIT)
            .order_by("at", direction=Query.DESCENDING)
            .limit(limit)
        )
        return [AuditLog.model_validate(s.to_dict()) for s in col.stream()]

    # --- proposals ---
    def upsert_proposal(self, proposal: Proposal) -> None:
        self._db.collection(COL_PROPOSALS).document(proposal.proposal_id).set(
            proposal.model_dump(mode="json")
        )

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        snap = self._db.collection(COL_PROPOSALS).document(proposal_id).get()
        return Proposal.model_validate(snap.to_dict()) if snap.exists else None

    def list_proposals(self, *, status: str | None = None) -> list[Proposal]:
        col = self._db.collection(COL_PROPOSALS)
        if status is not None:
            col = col.where("status", "==", status)
        return [Proposal.model_validate(s.to_dict()) for s in col.stream()]

    # --- inference (LLMコスト) ---
    def save_inference_log(self, log: InferenceLog) -> None:
        self._db.collection(COL_INFERENCE_LOGS).document(log.log_id).set(
            log.model_dump(mode="json")
        )

    def list_inference_logs(self, *, since: datetime | None = None) -> list[InferenceLog]:
        col = self._db.collection(COL_INFERENCE_LOGS)
        if since is not None:
            col = col.where("at", ">=", since.isoformat())
        return [InferenceLog.model_validate(s.to_dict()) for s in col.stream()]

    # --- 対外連絡 ---
    def upsert_notice(self, notice: ExternalNotice) -> None:
        self._db.collection(COL_NOTICES).document(notice.notice_id).set(
            notice.model_dump(mode="json")
        )

    def get_notice(self, notice_id: str) -> ExternalNotice | None:
        snap = self._db.collection(COL_NOTICES).document(notice_id).get()
        return ExternalNotice.model_validate(snap.to_dict()) if snap.exists else None

    def get_notice_by_source_ref(self, source_ref: str) -> ExternalNotice | None:
        query = self._db.collection(COL_NOTICES).where("source_ref", "==", source_ref).limit(1)
        for snap in query.stream():
            return ExternalNotice.model_validate(snap.to_dict())
        return None

    def list_notices(self, *, status: str | None = None) -> list[ExternalNotice]:
        from google.cloud.firestore import Query

        col = self._db.collection(COL_NOTICES)
        if status is not None:
            col = col.where("status", "==", status)
        col = col.order_by("received_at", direction=Query.DESCENDING)
        return [ExternalNotice.model_validate(s.to_dict()) for s in col.stream()]

    def upsert_notice_action(self, action: NoticeAction) -> None:
        self._db.collection(COL_NOTICE_ACTIONS).document(action.action_id).set(
            action.model_dump(mode="json")
        )

    def get_notice_action(self, action_id: str) -> NoticeAction | None:
        snap = self._db.collection(COL_NOTICE_ACTIONS).document(action_id).get()
        return NoticeAction.model_validate(snap.to_dict()) if snap.exists else None

    def list_notice_actions(
        self, *, notice_id: str | None = None, status: str | None = None
    ) -> list[NoticeAction]:
        col = self._db.collection(COL_NOTICE_ACTIONS)
        if notice_id is not None:
            col = col.where("notice_id", "==", notice_id)
        if status is not None:
            col = col.where("status", "==", status)
        return sorted(
            (NoticeAction.model_validate(s.to_dict()) for s in col.stream()),
            key=lambda a: a.created_at,
        )

    # --- 会話履歴 ---
    def get_conversation(self, member_id: str) -> Conversation | None:
        snap = self._db.collection(COL_CONVERSATIONS).document(member_id).get()
        return Conversation.model_validate(snap.to_dict()) if snap.exists else None

    def save_conversation(self, conversation: Conversation) -> None:
        self._db.collection(COL_CONVERSATIONS).document(conversation.member_id).set(
            conversation.model_dump(mode="json")
        )
