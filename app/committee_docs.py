"""委員会資料（Google ドキュメント）の項目の読み書き（docs/committee-docs-edit-design.md）。

- 誰がどの委員会の資料を更新できるか（§2）
- 委員会フォルダと資料の探索（§3）
- ドキュメントの表から「項目名｜内容」を取り出し、内容だけを書き換える（§3・§5）

ドキュメントは doc-editor を権限借用して読み書きする（04-委員会 だけを編集者で共有）。
`_get_json` / `_post_json` がテストのモック境界。
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .models import Member

logger = logging.getLogger("jci-agent.committee_docs")

DOC_EDITOR_SA = os.environ.get(
    "DOC_EDITOR_SA", "doc-editor@jci-sed-agent.iam.gserviceaccount.com"
)
SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.readonly",
]
FOLDER = "application/vnd.google-apps.folder"
GOOGLE_DOC = "application/vnd.google-apps.document"
DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"
DOCS_API = "https://docs.googleapis.com/v1/documents"

#: すべての委員会の資料を更新できる役職
ALL_COMMITTEES_ROLES = ("理事長", "専務理事")
#: 理事メンバーの役職（ほかに委員長の席を持つ会員）
BOARD_ROLES = ("理事長", "副理事長", "専務理事", "直前理事長")
CHAIR = "委員長"
#: この見出しで始まる行は参考情報なので書き換えない（LOM日程など）
READONLY_PREFIX = "（参考）"
#: フォルダ名の先頭の並び順（「03-」「03ｰ」など）
_ORDER_PREFIX = re.compile(r"^\s*\d+\s*[-ｰ－_]\s*")


def folder_id() -> str:
    """委員会フォルダの親（2027年度は 04-委員会）。"""
    return os.environ.get("COMMITTEE_DOCS_FOLDER", "").strip()


def is_enabled() -> bool:
    return bool(folder_id())


# --------------------------------------------------------------------------- #
# 権限（§2）
# --------------------------------------------------------------------------- #
def is_board_member(member: Member) -> bool:
    """理事メンバー（理事長・副理事長・専務理事・直前理事長・委員長）か。"""
    if member.officer_role in BOARD_ROLES:
        return True
    return member.committee_role == CHAIR or any(s.role == CHAIR for s in member.committees)


def own_committees(member: Member) -> set[str]:
    """本人が所属・担当する委員会（兼務と副理事長の担当を含む）。"""
    return member.committee_names | set(member.supervised_committees)


def can_edit(member: Member, committee: str) -> bool:
    """その委員会の資料を更新できるか。"""
    if not is_board_member(member):
        return False
    if member.officer_role in ALL_COMMITTEES_ROLES:
        return True
    return committee in own_committees(member)


def editable_committees(member: Member, committees: list[str]) -> list[str]:
    """候補の委員会のうち、本人が更新できるもの（並びは候補の順）。"""
    return [c for c in committees if can_edit(member, c)]


# --------------------------------------------------------------------------- #
# ドライブ・ドキュメント（§3）
# --------------------------------------------------------------------------- #
def _token() -> str:
    """doc-editor を権限借用したアクセストークン。"""
    from google.auth import default, impersonated_credentials
    from google.auth.transport.requests import Request

    source, _ = default()
    target = impersonated_credentials.Credentials(
        source_credentials=source, target_principal=DOC_EDITOR_SA, target_scopes=SCOPES,
    )
    target.refresh(Request())
    return target.token


def _get_json(url: str, token: str) -> dict:
    """GET して JSON を返す。テストでモックする境界。"""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _post_json(url: str, body: dict, token: str) -> dict:
    """POST して JSON を返す。テストでモックする境界。"""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _children(parent: str, token: str) -> list[dict]:
    out, page = [], None
    while True:
        params = {
            "q": f"'{parent}' in parents and trashed=false",
            "pageSize": 1000,
            "fields": "nextPageToken,files(id,name,mimeType,modifiedTime)",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if page:
            params["pageToken"] = page
        data = _get_json(f"{DRIVE_FILES}?{urllib.parse.urlencode(params)}", token)
        out += data.get("files", [])
        page = data.get("nextPageToken")
        if not page:
            return out


def committee_name(folder_name: str) -> str:
    """フォルダ名から並び順を外した委員会名（「03-組織未来推進委員会」→「組織未来推進委員会」）。"""
    return _ORDER_PREFIX.sub("", folder_name).strip()


def committee_folders(token: str) -> dict[str, str]:
    """委員会名 → フォルダID（04-委員会 の直下のフォルダ）。"""
    return {
        committee_name(f["name"]): f["id"]
        for f in _children(folder_id(), token) if f["mimeType"] == FOLDER
    }


def find_docs(folder: str, token: str) -> list[dict]:
    """委員会フォルダ配下（サブフォルダを含む）の Google ドキュメント（id・name・path）。"""
    docs: list[dict] = []
    stack = [(folder, "")]
    while stack:
        current, path = stack.pop()
        for f in _children(current, token):
            if f["mimeType"] == FOLDER:
                stack.append((f["id"], f"{path}/{f['name']}" if path else f["name"]))
            elif f["mimeType"] == GOOGLE_DOC:
                docs.append({"id": f["id"], "name": f["name"], "path": path,
                             "modified_at": f.get("modifiedTime")})
    return docs


def read_doc(doc_id: str, token: str) -> dict:
    return _get_json(f"{DOCS_API}/{doc_id}?includeTabsContent=true", token)


def doc_url(doc_id: str) -> str:
    return f"https://docs.google.com/document/d/{doc_id}/edit"


# --------------------------------------------------------------------------- #
# 項目の読み取り（§3）
# --------------------------------------------------------------------------- #
@dataclass
class DocField:
    """表の「項目名｜内容」の内容セル。start〜end はセルの中身（最後の改行を除く）。"""

    name: str
    text: str
    editable: bool
    start: int
    end: int
    style: dict = field(default_factory=dict)


def _body(doc: dict) -> list[dict]:
    tabs = doc.get("tabs") or []
    if tabs:
        return tabs[0].get("documentTab", {}).get("body", {}).get("content", [])
    return doc.get("body", {}).get("content", [])


def _cell_text(cell: dict) -> str:
    lines = []
    for item in cell.get("content", []):
        para = item.get("paragraph")
        if para is None:
            continue
        text = "".join(e.get("textRun", {}).get("content", "") for e in para.get("elements", []))
        lines.append(text.replace("\xa0", " ").rstrip("\n").strip())
    return "\n".join(lines).strip()


def _cell_style(cell: dict) -> dict:
    """セルの最初の文字の書式（新しい文に付け直す）。"""
    for item in cell.get("content", []):
        for e in item.get("paragraph", {}).get("elements", []):
            style = e.get("textRun", {}).get("textStyle", {})
            return {k: v for k, v in style.items() if k not in ("baselineOffset", "link")}
    return {}


#: 空欄のセルは文字の大きさ・フォントを持たないことがあるので、同じ行の見出しから借りる項目
_BORROWED_STYLE = ("fontSize", "weightedFontFamily")


def _field(name: str, cell: dict, editable: bool, label: dict) -> DocField:
    content = cell.get("content", [])
    start = content[0]["startIndex"] if content else cell["startIndex"] + 1
    end = content[-1]["endIndex"] - 1 if content else start
    style = _cell_style(cell)
    for key in _BORROWED_STYLE:
        if key not in style and key in (borrowed := _cell_style(label)):
            style[key] = borrowed[key]
    return DocField(name=name, text=_cell_text(cell), editable=editable,
                    start=start, end=max(start, end), style=style)


#: 4列の項目表（「◯ファイル名｜値｜◯委員会名｜値」）の項目名の印
_KEY_MARKS = ("◯", "○")
#: 審議の記録の表の前にある見出し（「【五役会議①】開催日：…」）
_TABLE_HEADING = re.compile(r"^【(.+?)】")
#: 小見出しとみなす1行の長さの上限（「■問題の原因」「対内対象者」など）
_SUBHEADING_MAX = 20


def _paragraph_text(element: dict) -> str:
    para = element.get("paragraph") or {}
    return "".join(e.get("textRun", {}).get("content", "")
                   for e in para.get("elements", [])).strip()


def _label(cell: dict) -> str:
    return _cell_text(cell).replace("\n", "")


def _is_subheading(text: str) -> bool:
    return bool(text) and "\n" not in text and len(text) <= _SUBHEADING_MAX


def _two_column(rows: list[list[dict]]) -> list[DocField]:
    """2列の表。左のセルが空の行は直前の見出しの続き（結合セル）として扱い、
    右のセルが「■問題」のような1行だけで次の行が続きなら、それを小見出しにして次の行を
    「見出し・問題」の項目にする（事業計画書の事業要項）。"""
    fields: list[DocField] = []
    label, label_cell, i = "", None, 0
    while i < len(rows):
        cells = rows[i]
        if _label(cells[0]):
            label, label_cell = _label(cells[0]), cells[0]
        if not label:
            i += 1
            continue
        editable = not label.startswith(READONLY_PREFIX)
        text = _cell_text(cells[1])
        follows = i + 1 < len(rows) and not _label(rows[i + 1][0])
        if follows and _is_subheading(text):
            sub = text.lstrip("■").strip()
            fields.append(_field(f"{label}・{sub}", rows[i + 1][1], editable, label_cell))
            i += 2
            continue
        fields.append(_field(label, cells[1], editable, label_cell))
        i += 1
    return fields


def _key_value(rows: list[list[dict]]) -> list[DocField]:
    """4列などの項目表（「項目｜値｜項目｜値」）。項目名の先頭の「◯」は外す。"""
    fields: list[DocField] = []
    for cells in rows:
        for key, value in zip(cells[0::2], cells[1::2], strict=False):
            name = _label(key).lstrip("".join(_KEY_MARKS)).strip()
            if name:
                fields.append(_field(name, value, not name.startswith(READONLY_PREFIX), key))
    return fields


def _grid(rows: list[list[dict]], prefix: str) -> list[DocField]:
    """見出し行のある表（「行の見出し・列の見出し」。前に【会議名】の見出しがあれば頭に付ける）。"""
    fields: list[DocField] = []
    headers = [_label(c) for c in rows[0]]
    for cells in rows[1:]:
        label = _label(cells[0])
        if not label:
            continue
        for header, cell in zip(headers[1:], cells[1:], strict=False):
            if header:
                name = f"{prefix}{label}・{header}"
                fields.append(_field(name, cell, not label.startswith(READONLY_PREFIX), cells[0]))
    return fields


def _is_key_value(rows: list[list[dict]], columns: int) -> bool:
    if columns % 2 or columns < 4 or not rows:
        return False
    first = _label(rows[0][0])
    return first.startswith(_KEY_MARKS) or len(rows) == 1


def extract_fields(doc: dict) -> list[DocField]:
    """ドキュメントの表から項目を取り出す（docs/proposal-ledger-design.md §3）。

    - 2列の表: 「項目名｜内容」。結合された見出しの続きの行・■小見出しにも対応
    - 項目表: 「◯項目｜値｜◯項目｜値」
    - 見出し行のある表: 「行の見出し・列の見出し」（年間スケジュール・審議の記録）
    """
    fields: list[DocField] = []
    heading = ""
    for element in _body(doc):
        if "paragraph" in element:
            text = _paragraph_text(element)
            if text:
                heading = text
            continue
        table = element.get("table")
        if table is None:
            continue
        rows = [r.get("tableCells", []) for r in table.get("tableRows", [])]
        columns = table.get("columns", 0)
        if columns == 2:
            fields += _two_column(rows)
        elif _is_key_value(rows, columns):
            fields += _key_value(rows)
        elif columns >= 3 and len(rows) >= 2:
            match = _TABLE_HEADING.match(heading)
            fields += _grid(rows, f"{match.group(1)}・" if match else "")
    # 同じ名前の項目が複数あると書き換え先が決まらないので、2つ目以降に番号を付ける
    seen: dict[str, int] = {}
    for f in fields:
        seen[f.name] = seen.get(f.name, 0) + 1
        if seen[f.name] > 1:
            f.name = f"{f.name}({seen[f.name]})"
    return fields


# --------------------------------------------------------------------------- #
# 書き込み（§5）
# --------------------------------------------------------------------------- #
def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def build_requests(fields: list[DocField], edits: dict[str, str]) -> list[dict]:
    """内容セルの中身を新しい文に置き換える batchUpdate のリクエスト。

    後ろの位置から順に書き換える（前の書き換えで後ろの位置がずれないように）。
    読み取り専用の項目・名前が一致しない項目・空の文は無視する。
    """
    targets = [(f, edits[f.name].strip()) for f in fields
               if f.editable and edits.get(f.name, "").strip()]
    requests: list[dict] = []
    for f, text in sorted(targets, key=lambda t: t[0].start, reverse=True):
        if f.end > f.start:
            requests.append({"deleteContentRange": {
                "range": {"startIndex": f.start, "endIndex": f.end}}})
        requests.append({"insertText": {"location": {"index": f.start}, "text": text}})
        if f.style:
            requests.append({"updateTextStyle": {
                "range": {"startIndex": f.start, "endIndex": f.start + _utf16_len(text)},
                "textStyle": f.style,
                "fields": ",".join(sorted(f.style)),
            }})
    return requests


def apply(doc_id: str, revision_id: str, requests: list[dict], token: str) -> dict:
    """読んだときの版から変わっていなければ書き込む（変わっていたら Google が 400 を返す）。"""
    return _post_json(f"{DOCS_API}/{doc_id}:batchUpdate", {
        "requests": requests, "writeControl": {"requiredRevisionId": revision_id},
    }, token)
