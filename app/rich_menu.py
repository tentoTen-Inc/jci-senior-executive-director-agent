"""リッチメニューの画像生成と領域定義（docs/mvp-design.md §4.3・docs/doc-session-design.md §3）。

立場ごとに3種類のメニューを持つ。各領域タップで postback "menu|<label>" を送り、
member_menu のルーティングに合流する。

| 種類 | 対象 | ボタン |
|---|---|---|
| member（2×2） | 下記以外 | 出欠を回答／次回の予定／自分の出欠状況／事務局に連絡 |
| board（3×2） | 委員長など理事メンバー | 上の4つ＋資料を作る・直す＋使い方 |
| officer（3×2） | 五役・監事・事務局 | 上の4つ＋資料を作る・直す＋予定を登録・変更 |

LINE Messaging API への登録は scripts/setup_rich_menu.py、
会員ごとの結びつけは rich_menu_link が行う。
"""
from __future__ import annotations

from dataclasses import dataclass

from .member_menu import (
    MENU_ANSWER,
    MENU_CALENDAR,
    MENU_CONTACT,
    MENU_DOCS,
    MENU_HELP,
    MENU_MY_ATT,
    MENU_SCHEDULE,
)

# LINE リッチメニュー大サイズ
WIDTH = 2500
HEIGHT = 1686

MEMBER, BOARD, OFFICER = "member", "board", "officer"
KINDS = (MEMBER, BOARD, OFFICER)

_BASE = [
    (MENU_ANSWER, "出欠を回答"),
    (MENU_SCHEDULE, "次回の予定"),
    (MENU_MY_ATT, "自分の出欠状況"),
    (MENU_CONTACT, "事務局に連絡"),
]
#: 種類ごとのセル: (label, 表示テキスト)
MENUS: dict[str, list[tuple[str, str]]] = {
    MEMBER: _BASE,
    BOARD: _BASE + [(MENU_DOCS, "資料を作る・直す"), (MENU_HELP, "使い方")],
    OFFICER: _BASE + [(MENU_DOCS, "資料を作る・直す"), (MENU_CALENDAR, "予定を登録・変更")],
}
#: 会員用（従来の 2×2）。後方互換のため残す
CELLS = MENUS[MEMBER]

# 背景色（落ち着いた紺～青のトーン。作業を始めるボタンは緑で区別する）
_COLORS = {MENU_DOCS: "#1e7a4f", MENU_CALENDAR: "#1e7a4f"}
_TONES = ["#1f3a5f", "#2e5a88"]


@dataclass
class MenuArea:
    x: int
    y: int
    width: int
    height: int
    label: str  # postback の menu|<label>


def _grid(kind: str) -> tuple[int, int]:
    return (2, 2) if len(MENUS[kind]) == 4 else (3, 2)


def menu_areas(kind: str = MEMBER) -> list[MenuArea]:
    """メニューのタップ領域を返す（4つなら 2×2、6つなら 3×2）。"""
    cols, rows = _grid(kind)
    cw, ch = WIDTH // cols, HEIGHT // rows
    areas = []
    for idx, (label, _text) in enumerate(MENUS[kind]):
        col, row = idx % cols, idx // cols
        areas.append(MenuArea(x=col * cw, y=row * ch, width=cw, height=ch, label=label))
    return areas


def rich_menu_definition(name: str | None = None, kind: str = MEMBER) -> dict:
    """LINE Rich Menu 作成用の JSON 定義を返す。"""
    return {
        "size": {"width": WIDTH, "height": HEIGHT},
        "selected": True,
        "name": name or f"jci-{kind}",
        "chatBarText": "メニュー",
        "areas": [
            {
                "bounds": {"x": a.x, "y": a.y, "width": a.width, "height": a.height},
                "action": {"type": "postback", "data": f"menu|{a.label}", "displayText": a.label},
            }
            for a in menu_areas(kind)
        ],
    }


def _load_font(size: int):
    from PIL import ImageFont

    # 日本語フォント候補（環境により存在するものを使う）
    candidates = [
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def generate_image(path: str, kind: str = MEMBER) -> str:
    """リッチメニュー画像を生成して保存し、パスを返す。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (WIDTH, HEIGHT), "#13253d")
    draw = ImageDraw.Draw(img)
    font = _load_font(110 if _grid(kind)[0] == 2 else 90)
    areas = menu_areas(kind)

    for idx, (area, (label, text)) in enumerate(zip(areas, MENUS[kind], strict=True)):
        color = _COLORS.get(label, _TONES[(idx + idx // _grid(kind)[0]) % 2])
        # セル背景（境界を少し空けてタイル状に）
        m = 12
        draw.rectangle(
            [area.x + m, area.y + m, area.x + area.width - m, area.y + area.height - m],
            fill=color,
        )
        # 中央にテキスト
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        cx = area.x + area.width // 2 - tw // 2
        cy = area.y + area.height // 2 - th // 2
        draw.text((cx, cy), text, fill="#ffffff", font=font)

    img.save(path, "PNG")
    return path
