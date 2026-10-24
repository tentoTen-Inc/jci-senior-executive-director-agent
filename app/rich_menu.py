"""リッチメニューの画像生成と領域定義（docs/liff-menu-design.md §2）。

| 種類 | 対象 | ボタン |
|---|---|---|
| member（2×2） | 会員 | 予定と出欠／日程調整／AIに聞く／事務局に連絡 |
| board（3×2） | 委員長・直前理事長 | 上の4つ＋資料を作る・直す＋委員会の議案 |
| officer（タブ＋3×2） | 五役・監事・事務局 | 「みんな」タブ: board と同じ6つ |
| officer_ops（タブ＋3＋2） | 同上 | 「運営」タブ: 予定を登録・変更／出欠の状況／日程調整を作る／ |
| | | 承認待ち／管理画面を開く |

五役のタブは rich menu alias と richmenuswitch で切り替える。ボタンは postback "menu|<label>" で
member_menu に合流する。LIFF の画面があるボタンは、LIFF ID を渡せば LIFF を直接開く（uri）。
LINE Messaging API への登録は scripts/setup_rich_menu.py、会員ごとの結びつけは rich_menu_link。
"""
from __future__ import annotations

from dataclasses import dataclass

from .member_menu import (
    ADMIN_URL,
    MENU_APPROVALS,
    MENU_ASK,
    MENU_ATT_STATUS,
    MENU_CALENDAR,
    MENU_CONTACT,
    MENU_DOCS,
    MENU_EVENTS,
    MENU_POLL_NEW,
    MENU_POLLS,
    MENU_PROPOSALS,
)

# LINE リッチメニュー大サイズ
WIDTH = 2500
HEIGHT = 1686
#: 五役のタブの帯の高さ
TAB_HEIGHT = 230

MEMBER, BOARD, OFFICER, OFFICER_OPS = "member", "board", "officer", "officer_ops"
KINDS = (MEMBER, BOARD, OFFICER, OFFICER_OPS)
#: 五役のタブ（rich menu alias の ID と表示名）
TABS = {OFFICER: ("jci-officer-main", "みんな"), OFFICER_OPS: ("jci-officer-ops", "運営")}

_COMMON = [MENU_EVENTS, MENU_POLLS, MENU_ASK, MENU_CONTACT]
_BOARD = [MENU_EVENTS, MENU_POLLS, MENU_ASK, MENU_CONTACT, MENU_DOCS, MENU_PROPOSALS]
#: 種類ごとのボタンの並び（行ごと）
LAYOUTS: dict[str, list[list[str]]] = {
    MEMBER: [_COMMON[:2], _COMMON[2:]],
    BOARD: [_BOARD[:3], _BOARD[3:]],
    OFFICER: [_BOARD[:3], _BOARD[3:]],
    OFFICER_OPS: [[MENU_CALENDAR, MENU_ATT_STATUS, MENU_POLL_NEW], [MENU_APPROVALS, "__admin__"]],
}
#: ボタンに出す文字（ラベルと違うもの）
TEXTS = {"__admin__": "管理画面を開く"}
#: LIFF の画面があるボタン（`?view=`）。LIFF ID を渡したときだけ LIFF を直接開く
LIFF_VIEWS: dict[str, str] = {MENU_EVENTS: "events", MENU_POLLS: "polls"}
#: 背景（作業を始めるボタンは緑、運営は落ち着いた紫）
_ACCENT = {MENU_DOCS: "#1e7a4f", MENU_PROPOSALS: "#1e7a4f"}
_TONES = {MEMBER: ["#1f3a5f", "#2e5a88"], BOARD: ["#1f3a5f", "#2e5a88"],
          OFFICER: ["#1f3a5f", "#2e5a88"], OFFICER_OPS: ["#3d2f5f", "#55447f"]}


@dataclass
class MenuArea:
    x: int
    y: int
    width: int
    height: int
    label: str  # postback の menu|<label>。タブは "tab:<kind>"、管理画面は "__admin__"


def _top(kind: str) -> int:
    return TAB_HEIGHT if kind in TABS else 0


def menu_areas(kind: str = MEMBER) -> list[MenuArea]:
    """メニューのタップ領域（五役はタブの帯＋ボタン）。"""
    areas = []
    if kind in TABS:
        tw = WIDTH // len(TABS)
        for i, tab in enumerate(TABS):
            areas.append(MenuArea(x=i * tw, y=0, width=tw, height=TAB_HEIGHT, label=f"tab:{tab}"))
    rows = LAYOUTS[kind]
    top = _top(kind)
    rh = (HEIGHT - top) // len(rows)
    for r, row in enumerate(rows):
        cw = WIDTH // len(row)
        for c, label in enumerate(row):
            areas.append(MenuArea(x=c * cw, y=top + r * rh, width=cw, height=rh, label=label))
    return areas


def _action(label: str, liff_id: str | None) -> dict:
    if label.startswith("tab:"):
        tab = label.removeprefix("tab:")
        return {"type": "richmenuswitch", "richMenuAliasId": TABS[tab][0], "data": label}
    if label == "__admin__":
        return {"type": "uri", "uri": ADMIN_URL}
    if liff_id and label in LIFF_VIEWS:
        return {"type": "uri", "uri": f"https://liff.line.me/{liff_id}?view={LIFF_VIEWS[label]}"}
    return {"type": "postback", "data": f"menu|{label}", "displayText": label}


def rich_menu_definition(name: str | None = None, kind: str = MEMBER,
                         liff_id: str | None = None) -> dict:
    """LINE Rich Menu 作成用の JSON 定義を返す。"""
    return {
        "size": {"width": WIDTH, "height": HEIGHT},
        "selected": True,
        "name": name or f"jci-{kind}",
        "chatBarText": "メニュー",
        "areas": [
            {"bounds": {"x": a.x, "y": a.y, "width": a.width, "height": a.height},
             "action": _action(a.label, liff_id)}
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


def _center(draw, area: MenuArea, text: str, font, fill: str) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((area.x + area.width // 2 - tw // 2, area.y + area.height // 2 - th // 2 - bbox[1]),
              text, fill=fill, font=font)


def generate_image(path: str, kind: str = MEMBER) -> str:
    """リッチメニュー画像を生成して保存し、パスを返す。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (WIDTH, HEIGHT), "#13253d")
    draw = ImageDraw.Draw(img)
    font = _load_font(110 if len(LAYOUTS[kind][0]) == 2 else 90)
    tab_font = _load_font(80)
    tones = _TONES[kind]
    m = 12  # セルの間の余白（タイル状に見せる）
    for idx, area in enumerate(menu_areas(kind)):
        box = [area.x + m, area.y + m, area.x + area.width - m, area.y + area.height - m]
        if area.label.startswith("tab:"):
            active = area.label == f"tab:{kind}"
            draw.rectangle(box, fill="#ffffff" if active else "#0b1729")
            _center(draw, area, TABS[area.label.removeprefix("tab:")][1], tab_font,
                    "#13253d" if active else "#9fb3c8")
            continue
        draw.rectangle(box, fill=_ACCENT.get(area.label, tones[idx % 2]))
        _center(draw, area, TEXTS.get(area.label, area.label), font, "#ffffff")
    img.save(path, "PNG")
    return path
