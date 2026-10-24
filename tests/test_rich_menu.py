"""立場ごとのリッチメニューの生成・領域定義・会員への結びつけ（docs/liff-menu-design.md §2）。"""
import os
from datetime import datetime

import pytest

from app import config, rich_menu, rich_menu_link
from app.member_menu import (
    MENU_APPROVALS,
    MENU_ASK,
    MENU_ATT_STATUS,
    MENU_CALENDAR,
    MENU_DOCS,
    MENU_EVENTS,
    MENU_FEEDBACK,
    MENU_POLL_NEW,
    MENU_POLLS,
    MENU_PROPOSALS,
)
from app.models import CommitteeSeat, Member, MemberType
from app.repository import InMemoryRepository
from app.rich_menu import (
    BOARD,
    HEIGHT,
    KINDS,
    MEMBER,
    OFFICER,
    OFFICER_OPS,
    TAB_HEIGHT,
    WIDTH,
    generate_image,
    menu_areas,
    rich_menu_definition,
)

COMMON = [MENU_EVENTS, MENU_POLLS, MENU_ASK, MENU_FEEDBACK]


def _labels(kind):
    return [a.label for a in menu_areas(kind)]


def test_member_menu_is_2x2():
    areas = menu_areas()
    assert _labels(MEMBER) == COMMON
    assert (areas[1].x, areas[2].y) == (WIDTH // 2, HEIGHT // 2)


def test_board_menu_is_3x2():
    assert _labels(BOARD) == COMMON + [MENU_DOCS, MENU_PROPOSALS]
    areas = menu_areas(BOARD)
    assert areas[1].x == WIDTH // 3 and areas[3].y == HEIGHT // 2


def test_officer_menus_have_tabs():
    assert _labels(OFFICER) == ["tab:officer", "tab:officer_ops", *COMMON, MENU_DOCS,
                                MENU_PROPOSALS]
    assert _labels(OFFICER_OPS) == ["tab:officer", "tab:officer_ops", MENU_CALENDAR,
                                    MENU_ATT_STATUS, MENU_POLL_NEW, MENU_APPROVALS, "__admin__"]
    first_button = menu_areas(OFFICER_OPS)[2]
    assert first_button.y == TAB_HEIGHT


def test_tab_and_admin_actions():
    actions = {a["action"].get("data") or a["action"].get("uri"): a["action"]
               for a in rich_menu_definition(kind=OFFICER_OPS)["areas"]}
    assert actions["tab:officer"] == {"type": "richmenuswitch",
                                      "richMenuAliasId": "jci-officer-main", "data": "tab:officer"}
    assert actions["https://admin.647jc.org"]["type"] == "uri"
    assert actions[f"menu|{MENU_CALENDAR}"]["type"] == "postback"


def test_liff_buttons_open_liff_only_when_view_exists(monkeypatch):
    monkeypatch.setattr(rich_menu, "LIFF_VIEWS", {MENU_EVENTS: "events"})
    areas = {a["action"].get("displayText") or a["action"].get("uri"): a["action"]
             for a in rich_menu_definition(kind=MEMBER, liff_id="123-abc")["areas"]}
    assert "https://liff.line.me/123-abc?view=events" in areas
    assert areas[MENU_POLLS]["type"] == "postback"  # 画面がまだ無いボタンは postback
    # LIFF ID が無ければすべて postback
    datas = [a["action"]["type"] for a in rich_menu_definition(kind=MEMBER)["areas"]]
    assert set(datas) == {"postback"}


@pytest.mark.parametrize("kind", KINDS)
def test_definition_has_one_area_per_button(kind):
    d = rich_menu_definition(kind=kind)
    assert d["size"] == {"width": WIDTH, "height": HEIGHT}
    assert d["name"] == f"jci-{kind}"
    assert len(d["areas"]) == len(menu_areas(kind))
    for a in d["areas"]:
        b = a["bounds"]
        assert b["x"] + b["width"] <= WIDTH and b["y"] + b["height"] <= HEIGHT


@pytest.mark.parametrize("kind", KINDS)
def test_generate_image(tmp_path, kind):
    out = str(tmp_path / f"menu_{kind}.png")
    generate_image(out, kind)
    with open(out, "rb") as f:
        assert f.read(8) == b"\x89PNG\r\n\x1a\n"
    assert os.path.getsize(out) > 0


# --------------------------------------------------------------------------- #
# 会員への結びつけ
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("member, kind", [
    (Member(member_id="a", name="会員"), MEMBER),
    (Member(member_id="b", name="副委員長",
            committees=[CommitteeSeat(committee="総務委員会", role="副委員長")]), MEMBER),
    (Member(member_id="c", name="委員長",
            committees=[CommitteeSeat(committee="総務委員会", role="委員長")]), BOARD),
    (Member(member_id="h", name="直前理事長", officer_role="直前理事長"), BOARD),
    (Member(member_id="d", name="副理事長", officer_role="副理事長"), OFFICER),
    (Member(member_id="e", name="専務", officer_role="専務理事"), OFFICER),
    (Member(member_id="i", name="理事長", officer_role="理事長"), OFFICER),
    (Member(member_id="f", name="監事", officer_role="監事"), OFFICER),
    (Member(member_id="g", name="事務局", member_type=MemberType.office), OFFICER),
])
def test_kind_for(member, kind):
    assert rich_menu_link.kind_for(member) == kind


@pytest.fixture
def linked(monkeypatch):
    repo = InMemoryRepository()
    settings = repo.get_settings()
    settings.rich_menus = {MEMBER: "rm_member", BOARD: "rm_board", OFFICER: "rm_officer",
                           OFFICER_OPS: "rm_ops"}
    repo.save_settings(settings)
    posts = []
    monkeypatch.setattr(config, "line_channel_access_token", lambda: "tok")
    monkeypatch.setattr(rich_menu_link, "_post", lambda url, token: posts.append(url))
    return repo, posts


def test_link_posts_menu_for_role(linked):
    repo, posts = linked
    sed = Member(member_id="m2", name="専務", officer_role="専務理事", line_user_id="U_sed")
    assert rich_menu_link.link(repo, sed) == OFFICER  # 五役は「みんな」タブを結ぶ
    assert posts == ["https://api.line.me/v2/bot/user/U_sed/richmenu/rm_officer"]


def test_link_skips_unlinked_or_unregistered(linked):
    repo, posts = linked
    assert rich_menu_link.link(repo, Member(member_id="x", name="未連携")) is None
    repo.save_settings(repo.get_settings().model_copy(update={"rich_menus": {}}))
    assert rich_menu_link.link(repo, Member(member_id="y", name="会員",
                                            line_user_id="U_y")) is None
    assert posts == []


def test_link_failure_does_not_raise(linked, monkeypatch):
    repo, _ = linked

    def boom(url, token):
        raise OSError("LINE down")
    monkeypatch.setattr(rich_menu_link, "_post", boom)
    assert rich_menu_link.link(repo, Member(member_id="z", name="会員",
                                            line_user_id="U_z")) is None


def test_approve_link_attaches_menu(linked, monkeypatch):
    from app import line_link, line_push
    repo, posts = linked
    monkeypatch.setattr(line_push, "push_text", lambda *a, **k: True)
    repo.upsert_member(Member(member_id="m13", name="委員長", committees=[
        CommitteeSeat(committee="総務委員会", role="委員長")]))
    line_link.approve(repo, "U_chair", "admin", datetime(2026, 10, 25), member_id="m13")
    assert posts == ["https://api.line.me/v2/bot/user/U_chair/richmenu/rm_board"]
