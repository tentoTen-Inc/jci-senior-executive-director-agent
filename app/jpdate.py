"""日本語の日付表記（曜日を OS の言語設定に依存させない）。

`strftime("%a")` はサーバーのロケールで英語（Tue 等）になるため、曜日は自前で付ける。
"""
from __future__ import annotations

from datetime import datetime

WEEKDAYS = "月火水木金土日"


def weekday(dt: datetime) -> str:
    """「火」のような1文字の曜日。"""
    return WEEKDAYS[dt.weekday()]


def month_day_time(dt: datetime) -> str:
    """「10月20日(火) 19:00」。"""
    return f"{dt.month}月{dt.day}日({weekday(dt)}) {dt:%H:%M}"
