"""ドライブの資料の取込（docs/drive-knowledge-design.md）。"""
import pytest

from app import drive, drive_knowledge, lake_query
from app.models import Member

ROOT = "root2027"
TREE = {
    ROOT: [
        {"id": "f_iinkai", "name": "04-委員会", "mimeType": drive_knowledge.FOLDER},
        {"id": "f_gyakubu", "name": "01-執行部", "mimeType": drive_knowledge.FOLDER},
        {"id": "f_jimu", "name": "00-事務局", "mimeType": drive_knowledge.FOLDER},
        {"id": "d_meibo", "name": "2027会員名簿", "mimeType": drive_knowledge.GOOGLE_SHEET,
         "modifiedTime": "2026-10-20T01:00:00.000Z"},
    ],
    "f_iinkai": [
        {"id": "d_summary", "name": "2027_summary_総務委員会",
         "mimeType": drive_knowledge.GOOGLE_DOC, "modifiedTime": "2026-10-23T04:10:45.668Z"},
    ],
    "f_gyakubu": [
        {"id": "d_budget", "name": "予算案", "mimeType": drive_knowledge.GOOGLE_SHEET,
         "modifiedTime": "2026-10-22T00:00:00Z"},
    ],
    "f_jimu": [
        {"id": "d_jimu", "name": "議事録", "mimeType": drive_knowledge.GOOGLE_DOC,
         "modifiedTime": "2026-10-22T00:00:00Z"},
    ],
}


@pytest.fixture
def drive_tree(monkeypatch):
    monkeypatch.setenv("DRIVE_KNOWLEDGE_FOLDERS", ROOT)
    monkeypatch.setattr(drive_knowledge, "_folder_name", lambda fid, token: "2027年度")
    monkeypatch.setattr(drive_knowledge, "_list_children", lambda fid, token: TREE.get(fid, []))
    monkeypatch.setattr(drive, "_token", lambda: "tok")


def test_disabled_without_folders(monkeypatch):
    monkeypatch.delenv("DRIVE_KNOWLEDGE_FOLDERS", raising=False)
    assert drive_knowledge.sync() is None


def test_crawl_keeps_path(drive_tree):
    files = {f["id"]: f["path"] for f in drive_knowledge.crawl("tok")}
    assert files["d_summary"] == "2027年度/04-委員会"
    assert files["d_budget"] == "2027年度/01-執行部"
    assert "f_iinkai" not in files  # フォルダ自体は返さない


def test_excludes_personal_info():
    assert drive_knowledge.excluded("2027年度", "2027会員名簿")
    assert drive_knowledge.excluded("2027年度/00-事務局", "議事録")
    assert not drive_knowledge.excluded("2027年度/04-委員会", "2027_summary_総務委員会")


def test_access_and_scopes():
    assert drive_knowledge.access_for("2027年度/01-執行部") == "officers"
    assert drive_knowledge.access_for("2026年度/五役会/第1回") == "officers"
    assert drive_knowledge.access_for("2027年度/04-委員会") == "members"
    officer = Member(member_id="m1", name="遠藤孝行", officer_role="専務理事")
    member = Member(member_id="m2", name="加藤律樹")
    assert drive_knowledge.scopes_for(officer) == ["members", "officers"]
    assert drive_knowledge.scopes_for(member) == ["members"]
    assert drive_knowledge.scopes_for(None) == []


def test_sync_updates_changed_and_removes_gone(drive_tree, monkeypatch):
    executed = []
    monkeypatch.setattr(drive_knowledge, "_known", lambda: {
        "d_summary": ("2026-10-23T04:10:45.668Z", "2027年度/04-委員会"),  # 変わっていない
        "d_old": ("2026-01-01T00:00:00.000Z", "2027年度"),  # ドライブから消えた
    })
    monkeypatch.setattr(drive_knowledge, "_extract", lambda f, token: f"本文:{f['name']}")
    monkeypatch.setattr(lake_query, "execute",
                        lambda sql, params=None: executed.append((sql, params)) or 1)

    result = drive_knowledge.sync()

    assert result == {"files": 2, "updated": 1, "moved": 0, "pending": 0, "removed": 1}
    upserts = [p for s, p in executed if "MERGE" in s]
    assert len(upserts) == 1  # まとめて1回で書く
    assert upserts[0]["file_ids"] == ["d_budget"]
    assert upserts[0]["accesses"] == ["officers"]
    assert upserts[0]["texts"] == ["本文:予算案"]
    deletes = [p for s, p in executed if s.startswith("DELETE")]
    assert deletes == [{"ids": ["d_old"]}]


def test_sync_updates_path_of_moved_files(drive_tree, monkeypatch):
    executed = []
    monkeypatch.setattr(drive_knowledge, "_known", lambda: {
        # 理事会から執行部へ移された（更新日時は同じ）
        "d_budget": ("2026-10-22T00:00:00.000Z", "2027年度/03-理事会"),
        "d_summary": ("2026-10-23T04:10:45.668Z", "2027年度/04-委員会"),
    })
    monkeypatch.setattr(drive_knowledge, "_extract",
                        lambda f, token: pytest.fail("移動だけなら本文は取り直さない"))
    monkeypatch.setattr(lake_query, "execute",
                        lambda sql, params=None: executed.append((sql, params)) or 1)

    result = drive_knowledge.sync()

    assert result["moved"] == 1 and result["updated"] == 0
    moves = [p for s, p in executed if s.lstrip().startswith("UPDATE")]
    assert moves == [{"file_ids": ["d_budget"], "paths": ["2027年度/01-執行部"],
                      "accesses": ["officers"]}]


def test_sync_records_unreadable_files(drive_tree, monkeypatch):
    executed = []
    monkeypatch.setattr(drive_knowledge, "_known", lambda: {})
    monkeypatch.setattr(drive_knowledge, "_extract", lambda f, token: None)
    monkeypatch.setattr(lake_query, "execute",
                        lambda sql, params=None: executed.append(params) or 1)
    drive_knowledge.sync()
    assert len(executed) == 1
    assert executed[0]["texts"] == ["", ""]  # 名簿と事務局は取り込まない


def test_sync_batches(drive_tree, monkeypatch):
    many = [{"id": f"d{i}", "name": f"資料{i}", "mimeType": drive_knowledge.GOOGLE_DOC,
             "modifiedTime": "2026-10-22T00:00:00Z"} for i in range(drive_knowledge.SYNC_BATCH + 5)]
    monkeypatch.setitem(TREE, "f_iinkai", many)
    monkeypatch.setattr(drive_knowledge, "_known", lambda: {})
    monkeypatch.setattr(drive_knowledge, "_extract", lambda f, token: "x")
    monkeypatch.setattr(lake_query, "execute", lambda sql, params=None: 1)
    result = drive_knowledge.sync()
    assert result["updated"] == drive_knowledge.SYNC_BATCH
    assert result["pending"] == 6  # 予算案＋あふれた5件


def test_sync_stops_at_time_budget(drive_tree, monkeypatch):
    clock = iter([0, 10, drive_knowledge.SYNC_SECONDS + 1])
    monkeypatch.setattr(drive_knowledge.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(drive_knowledge, "_known", lambda: {})
    monkeypatch.setattr(drive_knowledge, "_extract", lambda f, token: "x")
    monkeypatch.setattr(lake_query, "execute", lambda sql, params=None: 1)
    result = drive_knowledge.sync()
    assert result["updated"] == 1  # 時間切れで残りは次回
    assert result["pending"] == 1


def test_sync_failure_does_not_raise(drive_tree, monkeypatch):
    def boom():
        raise RuntimeError("bq down")
    monkeypatch.setattr(drive_knowledge, "_known", boom)
    assert drive_knowledge.sync() == {"error": "drive_sync_failed"}


def test_norm_matches_bigquery_format():
    assert drive_knowledge._norm("2026-10-22T00:00:00Z") == "2026-10-22T00:00:00.000Z"
    assert drive_knowledge._norm("2026-10-23T04:10:45.668Z") == "2026-10-23T04:10:45.668Z"
