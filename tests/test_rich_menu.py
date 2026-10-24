"""立場ごとのリッチメニューの生成・領域定義・会員への結びつけ（docs/doc-session-design.md §3）。"""
import os
from datetime import datetime

import pytest

from app import config, rich_menu_link
from app.member_menu import MENU_CALENDAR, MENU_DOCS, MENU_HELP, MENU_ITEMS
from app.models import CommitteeSeat, Member, MemberType
from app.repository import InMemoryRepository
from app.rich_menu import (
    BOARD,
    HEIGHT,
    KINDS,
    MEMBER,
    OFFICER,
    WIDTH,
    generate_image,
    menu_areas,
    rich_menu_definition,
)


def test_member_menu_is_2x2():
    areas = menu_areas()
    assert len(areas) == 4
    assert areas[0].x == 0 and areas[0].y == 0
    assert areas[1].x == WIDTH // 2
    assert areas[2].y == HEIGHT // 2
    assert {a.label for a in areas} == set(MENU_ITEMS)


@pytest.mark.parametrize("kind, extra", [(BOARD, {MENU_DOCS, MENU_HELP}),
                                         (OFFICER, {MENU_DOCS, MENU_CALENDAR})])
def test_board_and_officer_menus_are_3x2(kind, extra):
    areas = menu_areas(kind)
    assert len(areas) == 6
    assert {a.label for a in areas} == set(MENU_ITEMS) | extra
    assert areas[1].x == WIDTH // 3 and areas[3].y == HEIGHT // 2


@pytest.mark.parametrize("kind", KINDS)
def test_definition_has_one_area_per_button(kind):
    d = rich_menu_definition(kind=kind)
    assert d["size"] == {"width": WIDTH, "height": HEIGHT}
    assert d["name"] == f"jci-{kind}"
    datas = [a["action"]["data"] for a in d["areas"]]
    assert len(datas) == len(menu_areas(kind)) == len(set(datas))
    assert all(x.startswith("menu|") for x in datas)


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
    (Member(member_id="d", name="副理事長", officer_role="副理事長"), OFFICER),
    (Member(member_id="e", name="専務", officer_role="専務理事"), OFFICER),
    (Member(member_id="f", name="監事", officer_role="監事"), OFFICER),
    (Member(member_id="g", name="事務局", member_type=MemberType.office), OFFICER),
])
def test_kind_for(member, kind):
    assert rich_menu_link.kind_for(member) == kind


@pytest.fixture
def linked(monkeypatch):
    repo = InMemoryRepository()
    settings = repo.get_settings()
    settings.rich_menus = {MEMBER: "rm_member", BOARD: "rm_board", OFFICER: "rm_officer"}
    repo.save_settings(settings)
    posts = []
    monkeypatch.setattr(config, "line_channel_access_token", lambda: "tok")
    monkeypatch.setattr(rich_menu_link, "_post", lambda url, token: posts.append(url))
    return repo, posts


def test_link_posts_menu_for_role(linked):
    repo, posts = linked
    sed = Member(member_id="m2", name="専務", officer_role="専務理事", line_user_id="U_sed")
    assert rich_menu_link.link(repo, sed) == OFFICER
    assert posts == ["https://api.line.me/v2/bot/user/U_sed/richmenu/rm_officer"]


def test_link_skips_unlinked_or_unregistered(linked, monkeypatch):
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
