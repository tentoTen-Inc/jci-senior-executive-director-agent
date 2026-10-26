"""専務理事への短い知らせ（意見箱・議案の提出・提出締切）。

送信は既存の配信（ガードレール・配信記録）を通す。静音時間は送らず False を返すので、
呼び側は「まだ送っていない」として次の tick で送り直す。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from linebot.v3.messaging import TextMessage

from . import line_push
from .delivery import execute_delivery
from .guardrails import is_quiet_hours
from .models import DeliveryJob, DeliveryStatus, MemberStatus
from .repository import Repository

NOTIFY_ROLES = ("専務理事",)


def notify_sed(repo: Repository, text: str, now: datetime, kind: str) -> bool:
    """専務理事に送る。送った（送る相手がいない場合も含む）なら True、静音時間・停止中は False。"""
    if is_quiet_hours(now, repo.get_settings()):
        return False
    targets = [m.member_id for m in repo.list_members()
               if m.officer_role in NOTIFY_ROLES and m.line_user_id
               and m.status == MemberStatus.active]
    if not targets:
        return True
    job = DeliveryJob(job_id=f"{kind}_{now:%Y%m%d%H%M%S}_{uuid.uuid4().hex[:4]}",
                      type=kind, targets=targets, scheduled_at=now,
                      status=DeliveryStatus.queued)
    repo.save_delivery_job(job)
    report = execute_delivery(repo, job, text, now=now,
                              sender=line_push.member_messages_sender(
                                  repo, [TextMessage(text=text)]))
    return not (report.halted or report.deferred)
