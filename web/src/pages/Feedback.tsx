import { useEffect, useMemo, useState } from "react";
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
  Stat,
  fmtDateTime,
  useUi,
} from "../components/ui";

// 意見箱（docs/feedback-box-design.md §5）。会員が LINE の［意見箱］から送った意見と、AI の分類・傾向のまとめ

type Status = "new" | "in_progress" | "done" | "declined";
type Triage = {
  kind: string | null;
  feature: string | null;
  urgency: "高" | "中" | "低" | null;
  summary: string | null;
};
type Item = {
  feedback_id: string;
  kind: string;
  kind_label: string;
  effective_kind_label: string;
  text: string;
  anonymous: boolean;
  member_name: string | null;
  source: "liff" | "chat";
  status: Status;
  status_label: string;
  triage: Triage | null;
  created_at: string;
  updated_at: string | null;
};
type Theme = { label: string; count: number; feedback_ids: string[] };
type Digest = { summary: string; themes: Theme[]; count: number; generated_at: string | null };
type Data = { items: Item[]; digest: Digest | null; kinds: Record<string, string>; statuses: Record<Status, string> };

const STATUS_TONE: Record<Status, "green" | "blue" | "gray" | "amber"> = {
  new: "green",
  in_progress: "blue",
  done: "gray",
  declined: "gray",
};
const URGENCY_TONE = { 高: "red", 中: "amber", 低: "gray" } as const;
const STATUSES: Status[] = ["new", "in_progress", "done", "declined"];

type Filter = "open" | Status | "all";

export default function FeedbackPage() {
  const { toast } = useUi();
  const [data, setData] = useState<Data | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("open");
  const [theme, setTheme] = useState<Theme | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<Data>("/feedback").then(setData).catch((e) => setErr(e.message));
  }, []);

  const rows = useMemo(() => {
    if (!data) return [];
    let items = data.items;
    if (theme) items = items.filter((i) => theme.feedback_ids.includes(i.feedback_id));
    if (filter === "open") return items.filter((i) => i.status === "new" || i.status === "in_progress");
    if (filter === "all") return items;
    return items.filter((i) => i.status === filter);
  }, [data, filter, theme]);

  async function setStatus(item: Item, status: Status) {
    try {
      const next = await api<Data>(`/feedback/${item.feedback_id}/status`, {
        method: "POST",
        body: JSON.stringify({ status }),
      });
      setData(next);
      toast(`「${next.statuses[status]}」にしました`);
    } catch (e) {
      toast((e as Error).message, "error");
    }
  }

  async function digest() {
    setBusy(true);
    try {
      setData(await api<Data>("/feedback/digest", { method: "POST" }));
      setTheme(null);
      toast("傾向をまとめました");
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  }

  if (err) return <ErrorNote>{err}</ErrorNote>;
  if (!data) return <Loading />;

  const count = (s: Status) => data.items.filter((i) => i.status === s).length;
  const bugs = data.items.filter(
    (i) => (i.status === "new" || i.status === "in_progress") && i.effective_kind_label === "不具合"
  ).length;
  const filters: { value: Filter; label: string }[] = [
    { value: "open", label: `未完了 ${count("new") + count("in_progress")}` },
    ...STATUSES.map((s) => ({ value: s as Filter, label: `${data.statuses[s]} ${count(s)}` })),
    { value: "all", label: `すべて ${data.items.length}` },
  ];

  return (
    <div>
      <PageHeader
        title="意見箱"
        description="会員が LINE の［意見箱］から送った不具合・要望などです。AI が種類・対象の機能・緊急度を付けています。"
      />

      <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="新規" value={count("new")} unit="件" highlight={count("new") > 0} />
        <Stat label="対応中" value={count("in_progress")} unit="件" />
        <Stat label="未完了の不具合" value={bugs} unit="件" />
        <Stat label="これまで" value={data.items.length} unit="件" />
      </div>

      <Panel
        title="傾向のまとめ"
        action={
          <Button variant="primary" size="sm" disabled={busy || data.items.length === 0} onClick={digest}>
            {busy ? "AI が読んでいます…" : data.digest ? "まとめ直す" : "AI でまとめる"}
          </Button>
        }
      >
        {data.digest ? (
          <div className="space-y-3 text-sm">
            <p className="leading-relaxed">{data.digest.summary}</p>
            <div className="text-xs text-ink-sub">
              {data.digest.count}件をもとに {fmtDateTime(data.digest.generated_at)} にまとめました（見送りは除く）。論点を押すと、その意見だけを表示します。
            </div>
            <ul className="flex flex-wrap gap-2">
              {data.digest.themes.map((t) => (
                <li key={t.label}>
                  <button
                    type="button"
                    onClick={() => setTheme(theme?.label === t.label ? null : t)}
                    className={`rounded-full px-3.5 py-1.5 text-sm font-bold transition ${
                      theme?.label === t.label ? "bg-ink text-white" : "bg-gray-100 text-ink hover:bg-gray-200"
                    }`}
                  >
                    {t.label} <span className="num">{t.count}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <div className="text-sm text-ink-sub">似た意見をまとめた論点と件数を、AI が出します。</div>
        )}
      </Panel>

      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <Segmented value={filter} options={filters} onChange={setFilter} />
        {theme && (
          <Button size="sm" onClick={() => setTheme(null)}>
            論点「{theme.label}」の絞り込みを解除
          </Button>
        )}
      </div>

      {rows.length === 0 ? (
        <Panel>
          <EmptyState title="該当する意見はありません">
            {data.items.length === 0 ? "会員が LINE の［意見箱］から送ると、ここに並びます。" : "ほかの切り替えも見てください。"}
          </EmptyState>
        </Panel>
      ) : (
        <ul className="space-y-3">
          {rows.map((i) => (
            <li key={i.feedback_id}>
              <FeedbackCard item={i} statuses={data.statuses} onStatus={(s) => setStatus(i, s)} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function FeedbackCard({
  item,
  statuses,
  onStatus,
}: {
  item: Item;
  statuses: Record<Status, string>;
  onStatus: (s: Status) => void;
}) {
  const t = item.triage;
  const corrected = t?.kind && item.effective_kind_label !== item.kind_label;
  return (
    <article className="rounded-card bg-white px-5 py-4">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Badge tone={item.effective_kind_label === "不具合" ? "red" : "blue"}>{item.effective_kind_label}</Badge>
        {corrected && <span className="text-xs text-ink-faint">（送った人の選択: {item.kind_label}）</span>}
        {t?.urgency && <Badge tone={URGENCY_TONE[t.urgency]}>緊急度 {t.urgency}</Badge>}
        {t?.feature && <Badge tone="gray">{t.feature}</Badge>}
        <Badge tone={STATUS_TONE[item.status]}>{item.status_label}</Badge>
      </div>
      {t?.summary && <div className="mb-1 font-bold">{t.summary}</div>}
      <div className="whitespace-pre-wrap break-words rounded-2xl bg-gray-50 px-3.5 py-2 text-sm leading-relaxed">
        {item.text}
      </div>
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
        <div className="text-xs text-ink-sub">
          <span className="num">{fmtDateTime(item.created_at)}</span>
          {item.anonymous ? "匿名" : item.member_name ?? "会員"}
          {item.source === "chat" && "（AI に質問して答えられなかったもの）"}
        </div>
        <label className="flex items-center gap-2 text-xs font-bold text-ink-sub">
          状態
          <select value={item.status} onChange={(e) => onStatus(e.target.value as Status)}>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {statuses[s]}
              </option>
            ))}
          </select>
        </label>
      </div>
    </article>
  );
}
