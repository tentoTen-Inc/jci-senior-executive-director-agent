"""ドメインモデルと列挙型（docs/mvp-design.md §3 準拠）。

Firestore のドキュメントは pydantic モデルの ``model_dump(mode="json")`` で
シリアライズし、``model_validate`` で復元する。
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# 列挙型
# --------------------------------------------------------------------------- #
class AttendanceStatus(StrEnum):
    出席 = "出席"
    WEB出席 = "Web出席"
    欠席 = "欠席"
    委任 = "委任"
    未回答 = "未回答"


class LateLeave(StrEnum):
    全日程参加 = "全日程参加"
    遅刻 = "遅刻"
    早退 = "早退"
    遅刻早退 = "遅刻早退"


class AbsenceReason(StrEnum):
    体調不良 = "体調不良"
    家庭の事情 = "家庭の事情"
    仕事 = "仕事"
    冠婚葬祭 = "冠婚葬祭"
    その他 = "その他"


class MemberType(StrEnum):
    regular = "regular"  # 正会員
    external_auditor = "external_auditor"  # 外部監事
    office = "office"  # 事務局
    ob = "ob"  # OB
    support = "support"  # 賛助会員


class MemberStatus(StrEnum):
    active = "active"
    inactive = "inactive"


# 配信対象に含める会員区分（OB・賛助は除外）
_DELIVERABLE_TYPES = (MemberType.regular, MemberType.external_auditor, MemberType.office)


class EventType(StrEnum):
    例会 = "例会"
    理事会 = "理事会"
    五役会 = "五役会"
    委員会 = "委員会"
    総会 = "総会"
    イベント = "イベント"


class EventStatus(StrEnum):
    draft = "draft"
    open = "open"
    closed = "closed"


class TargetScopeKind(StrEnum):
    all = "all"
    committee = "committee"
    officer = "officer"
    custom = "custom"


class DeliveryStatus(StrEnum):
    queued = "queued"
    sent = "sent"
    halted = "halted"


class DeliveryResult(StrEnum):
    ok = "ok"
    blocked = "blocked"
    failed = "failed"


# --------------------------------------------------------------------------- #
# サブモデル
# --------------------------------------------------------------------------- #
class Contact(BaseModel):
    mobile: str | None = None
    email: str | None = None
    home_tel: str | None = None
    home_address: str | None = None
    postal_code: str | None = None
    work: str | None = None
    work_title: str | None = None  # 勤務先での役職（JCの役職ではない）
    work_tel: str | None = None
    fax: str | None = None


class Secondment(BaseModel):
    org: str
    role: str


class TargetScope(BaseModel):
    kind: TargetScopeKind = TargetScopeKind.all
    value: list[str] = Field(default_factory=list)


class AttendanceHistory(BaseModel):
    at: datetime
    status: AttendanceStatus
    by: str = "self"


# --------------------------------------------------------------------------- #
# 主要エンティティ
# --------------------------------------------------------------------------- #
class Member(BaseModel):
    member_id: str
    lom_id: str = "inawashiro"
    name: str
    kana: str | None = None
    birthday: str | None = None  # 生年月日（和暦表記をそのまま保持: 例 "H3.11.15"）
    member_type: MemberType = MemberType.regular
    status: MemberStatus = MemberStatus.active
    committee: str | None = None
    committee_role: str | None = None  # 委員長/副委員長/委員
    officer_role: str | None = None  # 理事長/専務理事/...
    secondments: list[Secondment] = Field(default_factory=list)
    contact: Contact = Field(default_factory=Contact)
    line_user_id: str | None = None
    linked_at: datetime | None = None

    @property
    def is_deliverable(self) -> bool:
        """配信対象（有効・LINE紐付け済み・退会/OB等でない）か。"""
        return (
            self.status == MemberStatus.active
            and self.line_user_id is not None
            and self.member_type in _DELIVERABLE_TYPES
        )


class InviteCode(BaseModel):
    code: str
    member_id: str
    expires_at: datetime
    used_at: datetime | None = None


class LinkState(BaseModel):
    """LINEユーザー単位の本人確認試行状態（総当たり対策）。"""

    line_user_id: str
    failed_attempts: int = 0
    locked: bool = False


class Event(BaseModel):
    event_id: str
    type: EventType
    title: str
    datetime_start: datetime
    datetime_end: datetime | None = None
    location: str | None = None
    target_scope: TargetScope = Field(default_factory=TargetScope)
    attendance_deadline: datetime | None = None
    material_deadline: datetime | None = None
    delivery_at: datetime | None = None
    reminder_policy_id: str | None = None
    quorum: int | None = None  # 定足数（人数）。理事会等で判定に使う（F4-5）
    status: EventStatus = EventStatus.draft


class Attendance(BaseModel):
    event_id: str
    member_id: str
    status: AttendanceStatus = AttendanceStatus.未回答
    proxy_member_id: str | None = None  # 委任先の会員（委任状・代理出席, F4-5）
    late_leave: LateLeave | None = None
    absence_reasons: list[AbsenceReason] = Field(default_factory=list)
    free_text: str | None = None
    responded_at: datetime | None = None
    history: list[AttendanceHistory] = Field(default_factory=list)

    @property
    def doc_id(self) -> str:
        return f"{self.event_id}_{self.member_id}"


class ReminderStage(BaseModel):
    name: str  # 依頼/リマインド/最終 等
    # 締切からのオフセット（分）。負の値=締切前。
    offset_minutes: int
    audience: str = "unanswered"  # all | unanswered | attendees
    template: str = "remind"


class ReminderPolicy(BaseModel):
    policy_id: str
    event_type: EventType
    stages: list[ReminderStage] = Field(default_factory=list)


class QuietHours(BaseModel):
    start: str = "21:00"
    end: str = "08:00"
    tz: str = "Asia/Tokyo"


class RateLimit(BaseModel):
    per_member_per_day: int = 3
    global_per_min: int = 30


class Settings(BaseModel):
    kill_switch: bool = False
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    rate_limit: RateLimit = Field(default_factory=RateLimit)


class DeliveryJob(BaseModel):
    job_id: str
    type: str  # attendance_request | reminder | summary
    event_id: str | None = None
    stage: str | None = None
    targets: list[str] = Field(default_factory=list)
    template_id: str | None = None
    scheduled_at: datetime | None = None
    status: DeliveryStatus = DeliveryStatus.queued
    idempotency_key: str | None = None


class DeliveryLog(BaseModel):
    log_id: str
    job_id: str
    member_id: str
    content: str
    result: DeliveryResult
    reason: str | None = None
    sent_at: datetime


class Escalation(BaseModel):
    """メンバーからの事務局への取次依頼（docs/mvp-design.md §4.3 F8-3）。"""

    escalation_id: str
    member_id: str
    kind: str = "contact"  # contact | question | other
    text: str | None = None
    created_at: datetime
    status: str = "open"  # open | handled


class AuditLog(BaseModel):
    """操作・自動配信・設定変更の監査証跡（docs/dashboard-design.md §4.2）。"""

    audit_id: str
    at: datetime
    actor: str  # email or "system"
    action: str
    target: str | None = None
    detail: str | None = None


# --------------------------------------------------------------------------- #
# 対外連絡（docs/external-notice-design.md §3, F5）
# --------------------------------------------------------------------------- #
class NoticeAttachment(BaseModel):
    name: str
    mime: str | None = None
    storage_uri: str | None = None
    text_excerpt: str | None = None  # PDF等から抽出した本文抜粋


class NoticeDigest(BaseModel):
    """Gemini による生成物。原文（body_text）とは切り分けて保持する（F5-6）。"""

    summary: str
    announcement: str  # メンバー向け告知文（LINE配信用）
    audience_hint: str | None = None  # 「全員」「総務委員会」「出向者」等の推定対象
    deadline: datetime | None = None
    actions: list[str] = Field(default_factory=list)
    model: str | None = None
    generated_at: datetime | None = None


class NoticeDelivery(BaseModel):
    """配信結果（P3-3 で記録）。"""

    job_id: str
    delivered_at: datetime
    target_count: int = 0


class NoticeHistory(BaseModel):
    at: datetime
    action: str
    by: str = "system"


class ExternalNotice(BaseModel):
    """ブロック協議会等からの対外連絡（docs/external-notice-design.md §3.1）。"""

    notice_id: str
    lom_id: str = "inawashiro"
    source: str = "manual"  # gmail | manual
    source_ref: str | None = None  # Gmail message id / 本文ハッシュ（冪等キー）
    received_at: datetime
    from_addr: str | None = None
    from_name: str | None = None
    subject: str
    body_text: str  # 原文（不変）
    attachments: list[NoticeAttachment] = Field(default_factory=list)
    digest: NoticeDigest | None = None
    status: str = "new"  # new | reviewed | delivered | archived
    delivery: NoticeDelivery | None = None
    history: list[NoticeHistory] = Field(default_factory=list)


class ConversationTurn(BaseModel):
    at: datetime
    role: str  # user | assistant
    text: str


class Conversation(BaseModel):
    """会員との直近のやり取り（docs/nl-assistant-design.md §4, F8-5）。

    直近数往復のみ保持する（長期記憶は持たない）。
    """

    member_id: str
    turns: list[ConversationTurn] = Field(default_factory=list)
    updated_at: datetime | None = None


class NoticeAction(BaseModel):
    """対外連絡から起票した「対象者がやること」（F5-3/F5-5）。

    対応状況は会員単位で追跡する（`done_by`）。未対応者だけに催促を送る。
    """

    action_id: str
    notice_id: str
    title: str
    due: datetime | None = None
    assignees: list[str] = Field(default_factory=list)  # member_id
    done_by: list[str] = Field(default_factory=list)  # 対応済みの member_id
    created_at: datetime
    status: str = "open"  # open | closed
    reminder_count: int = 0
    reminded_at: datetime | None = None

    @property
    def pending(self) -> list[str]:
        return [m for m in self.assignees if m not in self.done_by]


class InferenceUsage(BaseModel):
    """LLM 1回の呼び出しで消費したトークン（プロバイダ応答から取得）。"""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class InferenceLog(BaseModel):
    """LLM推論ログ。月間コストKPIの元データ（docs/dashboard-design.md §6）。"""

    log_id: str
    at: datetime
    kind: str  # proposal_review | ...（用途）
    model: str
    target: str | None = None  # proposal_id 等
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    ok: bool = True
    error: str | None = None


# --------------------------------------------------------------------------- #
# 議案ライフサイクル（docs/dashboard-design.md §4.1, F6）
# --------------------------------------------------------------------------- #
class ProposalStage(StrEnum):
    entry = "entry"  # エントリー
    submitted = "submitted"  # 資料提出
    sed_review = "sed_review"  # 専務レビュー
    goyaku = "goyaku"  # 五役会
    board = "board"  # 理事会上程
    decided = "decided"  # 審議結果
    executing = "executing"  # 事業実施
    reported = "reported"  # 報告
    verified = "verified"  # 検証


# カンバンの表示順
PROPOSAL_STAGE_ORDER = [
    ProposalStage.entry,
    ProposalStage.submitted,
    ProposalStage.sed_review,
    ProposalStage.goyaku,
    ProposalStage.board,
    ProposalStage.decided,
    ProposalStage.executing,
    ProposalStage.reported,
    ProposalStage.verified,
]


class ProposalDeadlines(BaseModel):
    entry: datetime | None = None
    submit: datetime | None = None
    deliver: datetime | None = None


class FormatCheckResult(BaseModel):
    passed: bool
    issues: list[str] = Field(default_factory=list)
    checked_at: datetime | None = None


class LlmReview(BaseModel):
    summary: str
    points: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    reviewed_at: datetime | None = None
    model: str | None = None


class SedApproval(BaseModel):
    status: str = "pending"  # pending | approved | returned
    by: str | None = None
    at: datetime | None = None
    comment: str | None = None


class ProposalHistory(BaseModel):
    at: datetime
    stage: ProposalStage
    by: str = "system"


class Proposal(BaseModel):
    proposal_id: str
    lom_id: str = "inawashiro"
    title: str
    number: str | None = None  # 第N号議案
    committee: str | None = None
    owner_member_id: str | None = None
    event_id: str | None = None  # 上程先の会議体
    stage: ProposalStage = ProposalStage.entry
    doc_type: str = "事業計画書"
    content: str | None = None  # 議案本文（Drive取込/手入力）
    storage_uri: str | None = None
    deadlines: ProposalDeadlines = Field(default_factory=ProposalDeadlines)
    format_check: FormatCheckResult | None = None
    llm_review: LlmReview | None = None
    sed_approval: SedApproval = Field(default_factory=SedApproval)
    history: list[ProposalHistory] = Field(default_factory=list)
    status: str = "open"  # open | closed
