"""LINE の資料・やり取りのナレッジ索引と検索（docs/lake-ai-design.md §3〜§4）。

- 索引: PDF 本文（800字ごと・100字重ね）と20字以上のテキストメッセージを、
  BigQuery ML のリモートモデル（gemini-embedding-001）でベクトル化して
  `knowledge_chunks` に入れる（tick から毎時）。
- 検索: 質問をベクトル化し、**質問者が見られる範囲だけ**から距離の近い順に取る。
  見られる範囲 = 自分の個別トーク ＋ 自分が発言したことのあるグループ・複数人トーク。
- ベクトル化も検索も BigQuery の中で完結する（アプリは SQL を投げるだけ）。

`LAKE_EMBEDDING_MODEL`（line_lake 内のモデル名）が未設定なら何もしない。
"""
from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from . import config, file_links, lake_query
from .repository import Repository

logger = logging.getLogger("jci-agent.lake_knowledge")

CHUNK_CHARS = 800
CHUNK_STEP = 700  # 100字重ねる
MIN_MESSAGE_CHARS = 20  # 挨拶・スタンプ程度の短い発言は検索対象にしない
INDEX_BATCH = 500
DIMENSIONS = 768
TOP_K = 5
#: コサイン距離のしきい値（小さいほど近い）。これより遠いものは根拠に使わない
MAX_DISTANCE = 0.45
JST = ZoneInfo("Asia/Tokyo")


def model_id() -> str | None:
    return os.environ.get("LAKE_EMBEDDING_MODEL", "").strip() or None


def is_enabled() -> bool:
    return model_id() is not None


def _table(name: str) -> str:
    return f"`{config.PROJECT_ID}.{lake_query.dataset()}.{name}`"


def _embed_options(task: str) -> str:
    return (f"STRUCT(TRUE AS flatten_json_output, '{task}' AS task_type, "
            f"{DIMENSIONS} AS output_dimensionality)")


# --------------------------------------------------------------------------- #
# 索引
# --------------------------------------------------------------------------- #
def candidate_chunks_sql() -> str:
    """まだ索引していない断片（ベクトル化の入力。列 content が本文）。"""
    return f"""
WITH file_chunks AS (
  SELECT
    'file' AS source_kind,
    f.message_id AS source_id,
    n AS chunk_no,
    f.source_type AS scope_type,
    IF(f.source_type = 'user', f.user_id, f.group_id) AS scope_id,
    f.user_id,
    f.sent_at,
    f.file_name AS title,
    CONCAT(
      '資料「', COALESCE(f.file_name, ''), '」(',
      FORMAT_TIMESTAMP('%Y-%m-%d', f.sent_at, 'Asia/Tokyo'), ')\\n',
      SUBSTR(f.text, n * {CHUNK_STEP} + 1, {CHUNK_CHARS})
    ) AS content
  FROM {_table('v_files')} AS f,
    UNNEST(GENERATE_ARRAY(0, DIV(CHAR_LENGTH(f.text) - 1, {CHUNK_STEP}))) AS n
  WHERE f.status = 'stored' AND CHAR_LENGTH(COALESCE(f.text, '')) > 0
),
message_chunks AS (
  SELECT
    'message' AS source_kind,
    m.message_id AS source_id,
    0 AS chunk_no,
    m.source_type AS scope_type,
    CASE m.source_type WHEN 'user' THEN m.user_id WHEN 'group' THEN m.group_id
      ELSE m.room_id END AS scope_id,
    m.user_id,
    m.sent_at,
    CAST(NULL AS STRING) AS title,
    CONCAT(
      'LINEの発言(', FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', m.sent_at, 'Asia/Tokyo'), ')\\n',
      LEFT(m.text, 2000)
    ) AS content
  FROM {_table('v_messages_for_ai')} AS m
  WHERE m.message_type = 'text' AND CHAR_LENGTH(m.text) >= {MIN_MESSAGE_CHARS}
),
candidates AS (
  SELECT CONCAT(source_kind, ':', source_id, ':', CAST(chunk_no AS STRING)) AS chunk_id, *
  FROM (SELECT * FROM file_chunks UNION ALL SELECT * FROM message_chunks)
)
SELECT c.*
FROM candidates AS c
LEFT JOIN {_table('knowledge_chunks')} AS k USING (chunk_id)
WHERE k.chunk_id IS NULL AND c.scope_id IS NOT NULL
LIMIT {INDEX_BATCH}
""".strip()


def index_sql() -> str:
    return f"""
INSERT INTO {_table('knowledge_chunks')}
  (chunk_id, source_kind, source_id, chunk_no, scope_type, scope_id, user_id, sent_at,
   title, content, embedding, indexed_at)
SELECT
  chunk_id, source_kind, source_id, chunk_no, scope_type, scope_id, user_id, sent_at,
  title, content, ml_generate_embedding_result, CURRENT_TIMESTAMP()
FROM ML.GENERATE_EMBEDDING(
  MODEL {_table(model_id() or '')},
  ({candidate_chunks_sql()}),
  {_embed_options('RETRIEVAL_DOCUMENT')}
)
WHERE ARRAY_LENGTH(ml_generate_embedding_result) > 0
""".strip()


def cleanup_sql() -> str:
    """送信取消されたメッセージ・ファイルの断片を消す。"""
    return f"""
DELETE FROM {_table('knowledge_chunks')}
WHERE source_id IN (SELECT message_id FROM {_table('v_messages')} WHERE unsent)
   OR source_id IN (SELECT message_id FROM {_table('v_files')} WHERE status = 'deleted_unsent')
""".strip()


def index_knowledge() -> dict | None:
    """未索引の断片をベクトル化して追加し、送信取消分を消す（tick から呼ぶ）。"""
    if not is_enabled():
        return None
    try:
        added = lake_query.execute(index_sql())
        removed = lake_query.execute(cleanup_sql())
    except Exception as exc:  # noqa: BLE001 - tick 本体を止めない
        logger.warning("ナレッジ索引に失敗: %s", exc)
        return {"error": "index_failed"}
    return {"indexed": added, "removed": removed}


# --------------------------------------------------------------------------- #
# 検索
# --------------------------------------------------------------------------- #
def search_sql() -> str:
    """見られる範囲に絞ってから、質問との距離で上位を返す。

    VECTOR_SEARCH は入力に「絞り込み付きのサブクエリ」を受け付けないため、
    範囲の制御を確実にするよう ML.DISTANCE の総当たりで順位付けする（現規模では十分速い）。
    件数が数十万を超えたら、範囲の判定を別クエリにして VECTOR_SEARCH＋ベクトル索引に切り替える。
    """
    return f"""
WITH query_embedding AS (
  SELECT ml_generate_embedding_result AS embedding
  FROM ML.GENERATE_EMBEDDING(
    MODEL {_table(model_id() or '')},
    (SELECT @question AS content),
    {_embed_options('RETRIEVAL_QUERY')}
  )
),
my_scopes AS (
  SELECT DISTINCT COALESCE(group_id, room_id) AS scope_id
  FROM {_table('v_messages')}
  WHERE user_id = @user_id AND COALESCE(group_id, room_id) IS NOT NULL
),
ranked AS (
  SELECT
    k.chunk_id, k.source_kind, k.source_id, k.scope_type, k.scope_id,
    k.title, k.content, k.sent_at,
    ML.DISTANCE(k.embedding, q.embedding, 'COSINE') AS distance
  FROM {_table('knowledge_chunks')} AS k
  CROSS JOIN query_embedding AS q
  WHERE (k.scope_type = 'user' AND k.scope_id = @user_id)
     OR (k.scope_type IN ('group', 'room') AND k.scope_id IN (SELECT scope_id FROM my_scopes))
)
SELECT * FROM ranked
WHERE distance <= @max_distance
ORDER BY distance
LIMIT {TOP_K}
""".strip()


def search(user_id: str | None, question: str) -> list[dict]:
    """質問者が見られる範囲から、質問に近い断片を返す。無効・失敗なら空。"""
    if not is_enabled() or not user_id or not (question or "").strip():
        return []
    try:
        return lake_query.query(search_sql(), {
            "user_id": user_id, "question": question[:2000], "max_distance": MAX_DISTANCE,
        })
    except Exception as exc:  # noqa: BLE001 - 検索できなくても従来の材料で答える
        logger.warning("ナレッジ検索に失敗: %s", exc)
        return []


def _where(repo: Repository, chunk: dict) -> str:
    if chunk.get("scope_type") == "user":
        return "あなたとの個別トーク"
    group = repo.get_line_group(chunk.get("scope_id") or "")
    if group and group.group_name:
        return f"グループ「{group.group_name}」"
    return "グループ"


def _date(value) -> str:
    if not value:
        return "日付不明"
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(JST).strftime("%Y-%m-%d")


def context_section(repo: Repository, chunks: list[dict]) -> str:
    """回答材料に足す「LINEで共有された資料・やり取り」（出典番号付き）。"""
    if not chunks:
        return ""
    lines = ["## LINEで共有された資料・やり取り（関連度の高い順。使ったら出典番号を書く）"]
    for i, chunk in enumerate(chunks, start=1):
        kind = f"資料「{chunk['title']}」" if chunk.get("title") else "発言"
        body = (chunk.get("content") or "").split("\n", 1)[-1].strip()
        lines.append(f"[{i}] {kind}／{_where(repo, chunk)}／{_date(chunk.get('sent_at'))}")
        lines.append(body)
    return "\n".join(lines)


def _short_date(value) -> str:
    text = _date(value)
    if text == "日付不明":
        return text
    _, month, day = text.split("-")
    return f"{int(month)}/{int(day)}"


def citation_footer(repo: Repository, chunks: list[dict], answer: str,
                    user_id: str | None) -> str:
    """回答で使われた出典番号（[1] 等）の一覧。ファイルにはタップで開けるリンクを付ける。

    同じファイルの複数の断片は1行にまとめる（[2][3] 年内スケジュール.pdf）。
    """
    cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", answer)
                    if 1 <= int(n) <= len(chunks)})
    if not cited:
        return ""
    groups: dict[tuple, dict] = {}
    for n in cited:
        chunk = chunks[n - 1]
        key = (chunk.get("source_kind"), chunk.get("source_id"))
        groups.setdefault(key, {"nums": [], "chunk": chunk})["nums"].append(n)

    lines = ["📎 出典"]
    for group in groups.values():
        chunk = group["chunk"]
        label = "".join(f"[{n}]" for n in group["nums"])
        where = _where(repo, chunk)
        when = _short_date(chunk.get("sent_at"))
        if chunk.get("source_kind") == "file":
            lines.append(f"{label} {chunk.get('title') or 'ファイル'}（{when}・{where}）")
            url = file_links.file_url(chunk["source_id"], user_id) if user_id else None
            if url:
                lines.append(url)
        else:
            body = (chunk.get("content") or "").split("\n", 1)[-1].strip().replace("\n", " ")
            excerpt = body if len(body) <= 30 else body[:30] + "…"
            lines.append(f"{label} {where}での発言（{when}）「{excerpt}」")
    return "\n".join(lines)
