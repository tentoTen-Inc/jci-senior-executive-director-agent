import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import {
  Badge,
  Button,
  EmptyState,
  ErrorNote,
  Loading,
  PageHeader,
  Panel,
  Segmented,
  fmtDateTime,
  useUi,
} from "../components/ui";

// 記録: LINE のやり取り（docs/admin-ui-design.md §3）
// 左にグループの一覧、右に選んだグループの会話（LINE の吹き出し）とファイル。

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
  user_id?: string | null;
  file_name: string | null;
  content_type: string | null;
  size: number | null;
  sent_at: string | null;
  text_excerpt: string | null;
};

type Member = { member_id: string; name: string; line_user_id: string | null };

const TYPE_LABELS: Record<string, string> = {
  text: "テキスト",
  file: "ファイル",
  image: "画像",
  video: "動画",
  audio: "音声",
  sticker: "スタンプ",
  location: "位置情報",
  template: "ボタン付きのメッセージ",
  flex: "カード型のメッセージ",
};

const FILE_STATUS: Record<string, { label: string; tone: "green" | "gray" | "red" | "amber" | "blue" }> = {
  stored: { label: "保存済み", tone: "green" },
  deleted_unsent: { label: "送信取消のため削除", tone: "gray" },
  too_large: { label: "大きすぎて保存していません", tone: "amber" },
  external: { label: "外部のリンク", tone: "blue" },
  unavailable: { label: "取得できませんでした", tone: "gray" },
  failed: { label: "取得に失敗しました", tone: "red" },
};
const fileStatus = (s: string) => FILE_STATUS[s] ?? { label: "確認中", tone: "gray" as const };

const WEEK = "日月火水木金土";
function dayLabel(iso: string): string {
  const d = new Date(iso);
  const y = d.getFullYear() !== new Date().getFullYear() ? `${d.getFullYear()}年` : "";
  return `${y}${d.getMonth() + 1}月${d.getDate()}日(${WEEK[d.getDay()]})`;
}
const dayKey = (iso: string) => new Date(iso).toDateString();
const hhmm = (iso: string) => new Date(iso).toTimeString().slice(0, 5);

function size(bytes: number | null): string {
  if (bytes == null) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function groupName(g: Group | undefined): string {
  if (!g) return "";
  return g.group_name ?? (g.source_type === "room" ? "複数人トーク" : "（名前を取得できていません）");
}

const MEDIA = new Set(["file", "image", "video", "audio"]);

/** 会話のファイル・画像と、ファイル一覧の記録を突き合わせる（名前か送った人と、送信時刻が近いもの）。 */
function findFile(c: Conversation, files: LineFile[]): LineFile | undefined {
  if (c.direction !== "in" || (!c.file_name && !MEDIA.has(c.message_type ?? ""))) return undefined;
  const t = new Date(c.occurred_at).getTime();
  const near = (f: LineFile, ms: number) => !!f.sent_at && Math.abs(new Date(f.sent_at).getTime() - t) < ms;
  if (c.file_name) return files.find((f) => f.file_name === c.file_name && (!f.sent_at || near(f, 5 * 60 * 1000)));
  return files.find((f) => (!f.user_id || !c.user_id || f.user_id === c.user_id) && near(f, 60 * 1000));
}

export default function LineGroups() {
  const { toast } = useUi();
  const [groups, setGroups] = useState<Group[]>([]);
  const [members, setMembers] = useState<Member[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [talk, setTalk] = useState<Conversation[]>([]);
  const [files, setFiles] = useState<LineFile[]>([]);
  const [tab, setTab] = useState<"talk" | "files">("talk");
  const [err, setErr] = useState<string | null>(null);
  const [notReady, setNotReady] = useState(false);
  const [loading, setLoading] = useState(true);
  const [opening, setOpening] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const detail = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api<Member[]>("/members").then(setMembers).catch(() => {});
    api<Group[]>("/line/groups")
      .then((gs) => {
        setGroups(gs);
        // PC では最初のグループを開いておく（スマホでは一覧から選ぶ）
        if (gs.length > 0 && window.matchMedia("(min-width: 1024px)").matches) open(gs[0].group_id, false);
      })
      .catch((e) => {
        if (String(e.message).includes("503")) setNotReady(true);
        else setErr(e.message);
      })
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 会話を開いたら最新（いちばん下）を見せる
  useEffect(() => {
    if (tab === "talk" && scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight;
  }, [talk, tab]);

  const nameByLineId = useMemo(() => {
    const m = new Map<string, string>();
    for (const x of members) if (x.line_user_id) m.set(x.line_user_id, x.name);
    return m;
  }, [members]);
  const sender = (userId: string | null | undefined) => (userId && nameByLineId.get(userId)) || "メンバー";

  async function open(groupId: string, scroll = true) {
    setSel(groupId);
    setTab("talk");
    setTalk([]);
    setFiles([]);
    setOpening(true);
    if (scroll && !window.matchMedia("(min-width: 1024px)").matches) {
      window.setTimeout(() => detail.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
    }
    try {
      const q = `group_id=${encodeURIComponent(groupId)}`;
      const [t, f] = await Promise.all([
        api<Conversation[]>(`/line/messages?${q}&limit=200`),
        api<LineFile[]>(`/line/files?${q}&limit=100`),
      ]);
      setTalk(t);
      setFiles(f);
    } catch (e) {
      toast(`会話を読み込めませんでした（${(e as Error).message}）`, "error");
    } finally {
      setOpening(false);
    }
  }

  async function download(f: LineFile) {
    const res = await fetch(`/api/line/files/${encodeURIComponent(f.message_id)}/download`, {
      credentials: "include",
    });
    if (!res.ok) {
      toast(`ファイルを保存できませんでした（${res.status}）`, "error");
      return;
    }
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = f.file_name ?? "file";
    a.click();
    URL.revokeObjectURL(url);
  }

  const current = groups.find((g) => g.group_id === sel);
  // API は新しい順。会話は LINE と同じく古い順（下が最新）に並べる
  const ordered = useMemo(() => [...talk].reverse(), [talk]);

  return (
    <div>
      <PageHeader
        title="LINE のやり取り"
        description="公式LINE「猪苗代専務AI」が参加しているグループの会話とファイルです。送信取消されたものは表示しません。"
      />

      <div className="mb-4 flex items-center gap-2 rounded-2xl bg-info-soft px-4 py-2.5 text-sm text-info">
        <span aria-hidden>🔒</span>
        <span>閲覧は専務・管理者のみです。会員には見えません。</span>
      </div>

      {err && <ErrorNote>{err}</ErrorNote>}

      {loading ? (
        <Loading />
      ) : notReady ? (
        <Panel>
          <EmptyState title="まだ LINE の記録を表示できません">
            LINE のやり取りを保存する準備が終わると、ここに表示されます。
          </EmptyState>
        </Panel>
      ) : groups.length === 0 ? (
        <Panel>
          <EmptyState title="まだ記録がありません">
            公式LINE「猪苗代専務AI」をグループに招待すると、そのグループのやり取りが記録されます。
          </EmptyState>
        </Panel>
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
          {/* ------------------------------------------------ グループの一覧 */}
          <Panel title="グループ" flush className="lg:mb-0 lg:self-start">
            <ul className="px-2 pb-1">
              {groups.map((g) => {
                const active = g.group_id === sel;
                return (
                  <li key={g.group_id}>
                    <button
                      type="button"
                      onClick={() => open(g.group_id)}
                      aria-current={active}
                      className={`flex w-full items-center gap-3 rounded-2xl px-3 py-2.5 text-left transition ${
                        active ? "bg-line-soft" : "hover:bg-gray-50"
                      }`}
                    >
                      <span
                        className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-full text-sm font-extrabold ${
                          g.left_at ? "bg-gray-100 text-ink-faint" : "bg-line text-white"
                        }`}
                        aria-hidden
                      >
                        <svg viewBox="0 0 24 24" className="h-5 w-5" fill="currentColor">
                          <path d="M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Zm7 0a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM2.5 19c0-3.2 2.9-5.5 6.5-5.5s6.5 2.3 6.5 5.5c0 .6-.4 1-1 1h-11c-.6 0-1-.4-1-1Zm14.6 1c.2-.3.4-.6.4-1 0-1.9-.8-3.6-2.1-4.8.4-.1.9-.2 1.4-.2 2.9 0 5.2 1.9 5.2 4.5 0 .8-.6 1.5-1.4 1.5h-3.5Z" />
                        </svg>
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-2">
                          <span className={`truncate font-bold ${g.group_name ? "" : "text-ink-sub"}`}>{groupName(g)}</span>
                        </span>
                        <span className="num mt-0.5 flex flex-wrap items-center gap-x-2 text-xs text-ink-sub">
                          <span>{g.messages.toLocaleString()}件</span>
                          <span>最終 {fmtDateTime(g.last_event_at)}</span>
                        </span>
                      </span>
                      {g.left_at ? <Badge tone="gray">退出</Badge> : <Badge tone="green">参加中</Badge>}
                    </button>
                  </li>
                );
              })}
            </ul>
          </Panel>

          {/* ------------------------------------------------ 会話・ファイル */}
          <div ref={detail} className="min-w-0 scroll-mt-4">
            {!sel ? (
              <Panel>
                <EmptyState title="グループを選んでください">一覧のグループを押すと、会話とファイルが表示されます。</EmptyState>
              </Panel>
            ) : (
              <section className="rounded-card bg-white">
                <div className="flex flex-wrap items-center justify-between gap-3 border-b border-gray-100 px-5 py-4">
                  <div className="min-w-0">
                    <h2 className="truncate text-base font-extrabold">{groupName(current)}</h2>
                    <div className="mt-0.5 text-xs text-ink-sub">
                      {current?.left_at
                        ? `${fmtDateTime(current.left_at, false)} に退出しました`
                        : current?.joined_at
                          ? `${fmtDateTime(current.joined_at, false)} から参加`
                          : "参加中"}
                    </div>
                  </div>
                  <Segmented
                    value={tab}
                    onChange={setTab}
                    options={[
                      { value: "talk", label: "会話" },
                      { value: "files", label: `ファイル ${files.length}` },
                    ]}
                  />
                </div>

                {opening ? (
                  <Loading />
                ) : tab === "talk" ? (
                  ordered.length === 0 ? (
                    <div className="p-5">
                      <EmptyState title="やり取りはありません" />
                    </div>
                  ) : (
                    <>
                      <div ref={scroller} className="max-h-[calc(100vh-300px)] min-h-[360px] overflow-y-auto px-3 py-4 sm:px-5">
                        {ordered.map((c, i) => {
                          const prev = ordered[i - 1];
                          const newDay = !prev || dayKey(prev.occurred_at) !== dayKey(c.occurred_at);
                          const sameSender =
                            !newDay && prev && prev.direction === c.direction && (c.direction === "out" || prev.user_id === c.user_id);
                          return (
                            <Fragment key={`${c.event_id}-${c.occurred_at}-${i}`}>
                              {newDay && (
                                <div className="my-3 flex justify-center">
                                  <span className="num rounded-full bg-gray-100 px-3 py-0.5 text-xs font-bold text-ink-sub">
                                    {dayLabel(c.occurred_at)}
                                  </span>
                                </div>
                              )}
                              <Bubble
                                c={c}
                                name={c.direction === "out" ? "専務AI" : sender(c.user_id)}
                                showName={!sameSender}
                                file={findFile(c, files)}
                                onDownload={download}
                              />
                            </Fragment>
                          );
                        })}
                      </div>
                      <div className="border-t border-gray-100 px-5 py-2 text-xs text-ink-faint">
                        直近 {talk.length} 件を表示しています（いちばん下が最新）。
                      </div>
                    </>
                  )
                ) : files.length === 0 ? (
                  <div className="p-5">
                    <EmptyState title="ファイルはありません" />
                  </div>
                ) : (
                  <div className="overflow-x-auto pb-2">
                    <table className="tbl">
                      <thead>
                        <tr>
                          <th>ファイル</th>
                          <th>送った人・日時</th>
                          <th>大きさ</th>
                          <th>状態</th>
                          <th />
                        </tr>
                      </thead>
                      <tbody>
                        {files.map((f) => {
                          const st = fileStatus(f.status);
                          return (
                            <tr key={f.message_id}>
                              <td className="min-w-[160px]">
                                <div className="break-all font-bold">📎 {f.file_name ?? "（名前のないファイル）"}</div>
                                {f.text_excerpt && <div className="mt-0.5 line-clamp-2 text-xs text-ink-sub">{f.text_excerpt}</div>}
                              </td>
                              <td className="whitespace-nowrap">
                                <div>{sender(f.user_id)}</div>
                                <div className="num text-xs text-ink-sub">{fmtDateTime(f.sent_at)}</div>
                              </td>
                              <td className="num whitespace-nowrap text-ink-sub">{size(f.size)}</td>
                              <td><Badge tone={st.tone}>{st.label}</Badge></td>
                              <td className="text-right">
                                {f.status === "stored" && (
                                  <Button size="sm" onClick={() => download(f)}>ダウンロード</Button>
                                )}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 吹き出し
function Bubble({
  c,
  name,
  showName,
  file,
  onDownload,
}: {
  c: Conversation;
  name: string;
  showName: boolean;
  file: LineFile | undefined;
  onDownload: (f: LineFile) => void;
}) {
  const out = c.direction === "out";
  const time = <span className="num shrink-0 self-end pb-0.5 text-[11px] text-ink-faint">{hhmm(c.occurred_at)}</span>;

  let content;
  if (c.file_name || file) {
    const st = file ? fileStatus(file.status) : null;
    const label = c.file_name ?? TYPE_LABELS[c.message_type ?? ""] ?? "ファイル";
    content = (
      <div className="flex flex-wrap items-center gap-2">
        <span className="break-all font-bold">📎 {label}</span>
        {st && <Badge tone={st.tone}>{st.label}</Badge>}
        {file?.status === "stored" && (
          <button type="button" onClick={() => onDownload(file)} className="text-xs font-bold text-line-ink underline">
            ダウンロード
          </button>
        )}
      </div>
    );
  } else if (c.text) {
    content = <div className="whitespace-pre-wrap break-words">{c.text}</div>;
  } else {
    content = <div className="text-ink-sub">（{TYPE_LABELS[c.message_type ?? ""] ?? "メッセージ"}）</div>;
  }

  return (
    <div className={`flex gap-2 ${out ? "justify-end" : "justify-start"} ${showName ? "mt-3" : "mt-1"}`}>
      {!out && (
        <span
          className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-gray-200 text-xs font-bold text-ink-sub ${
            showName ? "" : "invisible"
          }`}
          aria-hidden
        >
          {name.slice(0, 1)}
        </span>
      )}
      <div className={`flex min-w-0 max-w-[85%] flex-col sm:max-w-[70%] ${out ? "items-end" : "items-start"}`}>
        {showName && (
          <div className="mb-0.5 flex items-center gap-1.5 px-1 text-xs text-ink-sub">
            <span>{name}</span>
          </div>
        )}
        <div className={`flex min-w-0 items-end gap-1.5 ${out ? "flex-row-reverse" : ""}`}>
          <div
            className={`min-w-0 rounded-2xl px-3.5 py-2 text-sm leading-relaxed ${
              out ? "rounded-tr-md bg-line-soft text-ink" : "rounded-tl-md bg-gray-100 text-ink"
            }`}
          >
            {c.mentions_bot && <div className="mb-0.5 text-[11px] font-bold text-info">専務AI への呼びかけ</div>}
            {content}
          </div>
          {time}
        </div>
      </div>
    </div>
  );
}
