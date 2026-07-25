"""LINE Push API による送信（docs/mvp-design.md §6）。

reply（応答）と異なり、催促・サマリは Push で能動的に送る。
チャネルアクセストークン未設定時は送信せず False を返す（落とさない）。
"""
from __future__ import annotations

import logging

from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    Message,
    MessagingApi,
    PushMessageRequest,
    TextMessage,
)

from . import config

logger = logging.getLogger("jci-agent.push")


def _token_ready(token: str | None) -> bool:
    return bool(token and token != "PLACEHOLDER_SET_ME")


def push_messages(user_id: str, messages: list[Message]) -> bool:
    token = config.line_channel_access_token()
    if not _token_ready(token):
        logger.warning("access token 未設定のため push スキップ: to=%s", user_id)
        return False
    configuration = Configuration(access_token=token)
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=user_id, messages=messages)
        )
    return True


def push_text(user_id: str, text: str) -> bool:
    return push_messages(user_id, [TextMessage(text=text)])


def member_messages_sender(repo, messages: list[Message]):
    """member_id を LINE userId に解決してメッセージ列を Push する sender を作る。

    ``delivery.execute_delivery`` の sender として渡す（LINE未連携は False＝失敗扱い）。
    """

    def sender(member_id: str) -> bool:
        member = repo.get_member(member_id)
        if member is None or not member.line_user_id:
            return False
        return push_messages(member.line_user_id, messages)

    return sender


def member_text_sender(repo, text: str):
    """テキスト1通を Push する sender（``member_messages_sender`` の簡易版）。"""
    return member_messages_sender(repo, [TextMessage(text=text)])
