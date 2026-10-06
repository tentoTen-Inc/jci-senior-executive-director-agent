import { useEffect, useState } from "react";
import { api } from "../api/client";
import { Card } from "../components/Card";

type Group = {
  group_id: string;
  source_type: string;
  group_name: string | null;
  joined_at: string | null;
  left_at: string | null;
  last_event_at: string | null;
  messages: number;
};

type Conversation = {
  direction: "in" | "out";
  occurred_at: string;
  channel: string | null;
  user_id: string | null;
  message_type: string | null;
  text: string | null;
  file_name: string | null;
  mentions_bot: boolean | null;
  event_id: string;
};

type LineFile = {
  message_id: string;
  status: string;
  file_name: string | null;
  content_type: string | null;
  size: number | null;
  sent_at: string | null;
  text_excerpt: string | null;
};

const TYPE_LABELS: Record<string, string> = {
  text: "テキスト",
  file: "ファイル",
  image: "画像",
  video: "動画",
  audio: "音声",
  sticker: "スタンプ",
  location: "位置情報",
  template: "ボタン",
};

const FILE_STATUS: Record<string, string> = {
  stored: "保存済み",
  deleted_unsent: "送信取消で削除",
  too_large: "大きすぎて未保存",
  external: "外部リンク",
  unavailable: "取得できず",
  failed: "取得失敗",
};

/** ISO 文字列を「10/6 21:05」に（日本時間）。 */
function when(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("ja-JP", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Asia/Tokyo",
  });
}

function size(bytes: number | null): string {
  if (bytes == null) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function body(c: Conversation): string {
  if (c.text) return c.text;
  if (c.file_name) return `📎 ${c.file_name}`;
  return `（${TYPE_LABELS[c.message_type ?? ""] ?? c.message_type ?? "不明"}）`;
}

export default function LineGroups() {
  const [groups, setGroups] = useState<Group[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [talk, setTalk] = useState<Conversation[]>([]);
  const [files, setFiles] = useState<LineFile[]>([]);
  const [msg, setMsg] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api<Group[]>("/line/groups")
      .then(setGroups)
      .catch((e) =>
        setMsg(
          String(e.message).includes("503")
            ? "データレイクがまだ有効になっていません（セットアップ後に表示されます）。"
            : e.message
        )
      )
      .finally(() => setLoading(false));
  }, []);

  async function open(groupId: string) {
    setSel(groupId);
    setTalk([]);
    setFiles([]);
    try {
      const q = `group_id=${encodeURIComponent(groupId)}`;
      const [t, f] = await Promise.all([
        api<Conversation[]>(`/line/messages?${q}&limit=200`),
        api<LineFile[]>(`/line/files?${q}&limit=100`),
      ]);
      setTalk(t);
      setFiles(f);
    } catch (e) {
      setMsg((e as Error).message);
    }
  }

  async function download(f: LineFile) {
    const res = await fetch(`/api/line/files/${encodeURIComponent(f.message_id)}/download`, {
      credentials: "include",
    });
    if (!res.ok) {
      setMsg(`ダウンロードできませんでした (${res.status})`);
      return;
    }
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = f.file_name ?? f.message_id;
    a.click();
    URL.revokeObjectURL(url);
  }

  const current = groups.find((g) => g.group_id === sel);

  return (
    <div>
      <h1 className="text-lg font-semibold text-navy mb-3">LINEグループ</h1>
      {msg && <div className="mb-2 text-sm text-brand">{msg}</div>}
      <p className="text-xs text-slate-500 mb-3">
        公式LINE「猪苗代専務AI」が参加しているグループのやり取りとファイルです。送信取消されたものは表示しません。
        閲覧は専務・管理者のみです。
      </p>

      <Card title="参加中のグループ">
        {loading ? (
          <p className="text-slate-500 text-sm">読み込み中…</p>
        ) : groups.length === 0 ? (
          <p className="text-slate-500 text-sm">まだ記録がありません。</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-slate-500">
                <th className="py-1">グループ</th>
                <th>メッセージ</th>
                <th>最終発言</th>
                <th>状態</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {groups.map((g) => (
                <tr
                  key={g.group_id}
                  className={`border-t ${sel === g.group_id ? "bg-slate-50" : ""}`}
                >
                  <td className="py-1">
                    {g.group_name ?? (
                      <span className="text-slate-400">
                        {g.source_type === "room" ? "複数人トーク" : "（名前未取得）"}
                      </span>
                    )}
                  </td>
                  <td>{g.messages.toLocaleString()} 件</td>
                  <td className="text-slate-500">{when(g.last_event_at)}</td>
                  <td>{g.left_at ? <span className="text-slate-400">退出済み</span> : "参加中"}</td>
                  <td>
                    <button
                      className="text-xs bg-slate-200 text-navy rounded px-2 py-1"
                      onClick={() => open(g.group_id)}
                    >
                      開く
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {sel && (
        <>
          <Card title={`ファイル: ${current?.group_name ?? sel}`}>
            {files.length === 0 ? (
              <p className="text-slate-500 text-sm">ファイルはありません。</p>
            ) : (
              <table className="w-full text-sm">
                <tbody>
                  {files.map((f) => (
                    <tr key={f.message_id} className="border-t align-top">
                      <td className="py-1">
                        <div>{f.file_name ?? f.message_id}</div>
                        {f.text_excerpt && (
                          <div className="text-xs text-slate-500 line-clamp-2">{f.text_excerpt}</div>
                        )}
                      </td>
                      <td className="text-slate-500 whitespace-nowrap">{when(f.sent_at)}</td>
                      <td className="text-slate-500 whitespace-nowrap">{size(f.size)}</td>
                      <td className="whitespace-nowrap">{FILE_STATUS[f.status] ?? f.status}</td>
                      <td>
                        {f.status === "stored" && (
                          <button
                            className="text-xs bg-slate-200 text-navy rounded px-2 py-1"
                            onClick={() => download(f)}
                          >
                            ダウンロード
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>

          <Card title="最近のやり取り（新しい順・最大200件）">
            {talk.length === 0 ? (
              <p className="text-slate-500 text-sm">やり取りはありません。</p>
            ) : (
              <ul className="space-y-1 text-sm">
                {talk.map((c) => (
                  <li
                    key={`${c.event_id}-${c.occurred_at}`}
                    className={`rounded px-2 py-1 ${
                      c.direction === "out" ? "bg-blue-50 ml-8" : "bg-slate-50 mr-8"
                    }`}
                  >
                    <div className="text-xs text-slate-500">
                      {when(c.occurred_at)}{" "}
                      {c.direction === "out" ? "🤖 ボット" : `👤 ${c.user_id?.slice(-6) ?? "不明"}`}
                      {c.mentions_bot && <span className="ml-1 text-brand">@メンション</span>}
                    </div>
                    <div className="whitespace-pre-wrap break-words">{body(c)}</div>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
