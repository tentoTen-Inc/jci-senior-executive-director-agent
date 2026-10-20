import { Fragment, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import ContactTabs from "../components/ContactTabs";
import {
  Badge,
  Button,
  EmptyState,
  ErrorNote,
  Field,
  Loading,
  PageHeader,
  Panel,
  Progress,
  Segmented,
  fmtDateTime,
  useUi,
} from "../components/ui";

// 対外連絡（docs/admin-ui-design.md §3「連絡・アンケート」）
// 取り込み → AI で要約 → 会員へ LINE で届ける → やることの対応を確認する

type Digest = {
  summary: string;
  announcement: string;
  audience_hint: string | null;
  deadline: string | null;
  actions: string[];
  model: string | null;
  generated_at: string | null;
};
type Attachment = { name: string; mime: string | null; text_excerpt: string | null };
type Notice = {
  notice_id: string;
  source: string;
  received_at: string;
  from_addr: string | null;
  from_name: string | null;
  subject: string;
  body_text: string;
  attachments: Attachment[];
  digest: Digest | null;
  status: string;
  delivery?: { delivered_at: string; target_count: number } | null;
};
type DeliverResult = { targets: number; sent: number; blocked: number; deferred: number; failed: number };
type NoticeAction = {
  action_id: string;
  title: string;
  due: string | null;
  assignees: string[];
  done_by: string[];
  status: string;
  reminder_count: number;
  reminded_at: string | null;
};
type Member = {
  member_id: string;
  name: string;
  status: string;
  member_type: string;
  committee: string | null;
  committees?: { committee: string }[];
  officer_role: string | null;
  line_user_id: string | null;
};
type GmailResult = {
  total: number;
  created: number;
  updated: number;
  dry_run: boolean;
  items?: { subject: string; action: string }[];
};

type Tone = "green" | "gray" | "red" | "amber" | "blue";
type Filter = "new" | "reviewed" | "delivered" | "archived" | "all";

const STATUS: Record<string, { label: string; tone: Tone }> = {
  new: { label: "新着", tone: "amber" },
  reviewed: { label: "要約済", tone: "blue" },
  delivered: { label: "配信済", tone: "green" },
  archived: { label: "アーカイブ", tone: "gray" },
};
const FILTERS: { value: Filter; label: string }[] = [
  { value: "new", label: "新着" },
  { value: "reviewed", label: "要約済" },
  { value: "delivered", label: "配信済" },
  { value: "archived", label: "アーカイブ" },
  { value: "all", label: "すべて" },
];

// 配信の対象になる会員（app/models.py Member.is_deliverable と同じ条件）
const DELIVERABLE_TYPES = new Set(["regular", "external_auditor", "office", "youth_support"]);
const isDeliverable = (m: Member) => m.status === "active" && !!m.line_user_id && DELIVERABLE_TYPES.has(m.member_type);
const committeesOf = (m: Member) => new Set([...(m.committees ?? []).map((c) => c.committee), ...(m.committee ? [m.committee] : [])]);

type ScopeKind = "all" | "committee" | "officer" | "custom";
type Scope = { kind: ScopeKind; value: string[] };

function resolveScope(members: Member[], s: Scope): Member[] {
  const ms = members.filter(isDeliverable);
  if (s.kind === "committee") return ms.filter((m) => [...committeesOf(m)].some((c) => s.value.includes(c)));
  if (s.kind === "officer") return ms.filter((m) => !!m.officer_role && s.value.includes(m.officer_role));
  if (s.kind === "custom") return ms.filter((m) => s.value.includes(m.member_id));
  return ms;
}

function scopeLabel(s: Scope, names: Map<string, string>): string {
  if (s.kind === "all") return "LINE 連携済みの全員";
  if (s.kind === "custom") return s.value.map((id) => names.get(id) ?? "（名前不明）").join("、");
  return s.value.join("、");
}

function namesPreview(ms: { name: string }[], max = 12): string {
  const head = ms.slice(0, max).map((m) => m.name).join("、");
  return ms.length > max ? `${head} ほか${ms.length - max}人` : head;
}

type Rule = [RegExp, string];
const COMMON_RULES: Rule[] = [
  [/ガードレール/, "安全のため送信を止めました（送信の一時停止中・静音時間・送信の上限など）。設定と送信の記録を確認してください。"],
  [/not found/i, "対象が見つかりませんでした。画面を読み込み直してください。"],
];
/** サーバーのエラーのうち、開発用の言葉を含むものを画面向けの言葉にする（それ以外はそのまま出す） */
function friendly(e: unknown, rules: Rule[] = []): string {
  const m = e instanceof Error ? e.message : String(e);
  for (const [re, text] of [...rules, ...COMMON_RULES]) if (re.test(m)) return text;
  return m;
}

function resultLine(r: DeliverResult, head: string): string {
  const extra = [r.deferred && `時間をおいて送る ${r.deferred}人`, r.blocked && `届かない ${r.blocked}人`, r.failed && `失敗 ${r.failed}人`]
    .filter(Boolean)
    .join("・");
  return `${head}（送信 ${r.sent}人${extra ? `・${extra}` : ""}）`;
}

function DueBadge({ iso, settled }: { iso: string | null | undefined; settled?: boolean }) {
  if (!iso) return <span className="text-ink-faint">—</span>;
  const ms = new Date(iso).getTime() - Date.now();
  const tone: Tone = settled ? "gray" : ms < 0 ? "red" : ms <= 3 * 86400000 ? "amber" : "gray";
  return <Badge tone={tone}>〆{fmtDateTime(iso, false)}{!settled && ms < 0 ? " 過ぎ" : ""}</Badge>;
}

const sender = (n: Notice) => n.from_name || n.from_addr || "差出人不明";

// ---------------------------------------------------------------- 画面
export default function Notices() {
  const [params, setParams] = useSearchParams();
  const [items, setItems] = useState<Notice[] | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("new");
  const [touchedFilter, setTouchedFilter] = useState(false);

  const load = () =>
    api<Notice[]>("/notices")
      .then((ns) => {
        setItems(ns);
        setErr(null);
      })
      .catch((e) => setErr(friendly(e)));
  useEffect(() => {
    load();
    api<Member[]>("/members").then(setMembers).catch(() => {});
  }, []);

  // 新着が無ければ最初は「すべて」を見せる
  useEffect(() => {
    if (items && !touchedFilter && !items.some((n) => n.status === "new")) setFilter("all");
  }, [items, touchedFilter]);

  const selId = params.get("id");
  const sel = items?.find((n) => n.notice_id === selId) ?? null;
  const openDetail = (id: string | null) => {
    const next = new URLSearchParams(params);
    if (id) next.set("id", id);
    else next.delete("id");
    setParams(next);
    window.scrollTo({ top: 0 });
  };

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const n of items ?? []) c[n.status] = (c[n.status] ?? 0) + 1;
    return c;
  }, [items]);
  const shown = (items ?? [])
    .filter((n) => filter === "all" || n.status === filter)
    .sort((a, b) => b.received_at.localeCompare(a.received_at));
  const lastGmail = (items ?? [])
    .filter((n) => n.source === "gmail")
    .reduce<string | null>((max, n) => (!max || n.received_at > max ? n.received_at : max), null);

  if (err && !items) return (
    <div>
      <ContactTabs />
      <ErrorNote>{err}</ErrorNote>
    </div>
  );
  if (!items) return (
    <div>
      <ContactTabs />
      <Loading />
    </div>
  );

  if (sel) {
    return (
      <div>
        <ContactTabs />
        <NoticeDetail notice={sel} members={members} onBack={() => openDetail(null)} onChanged={load} />
      </div>
    );
  }

  return (
    <div>
      <ContactTabs />
      <PageHeader
        title="対外連絡"
        description="ブロック協議会などから届いた連絡を、AI で要約して会員へ LINE で届けます。"
      />
      {err && <ErrorNote>{err}</ErrorNote>}

      <GmailBar lastReceived={lastGmail} onImported={load} />

      <Panel
        title="届いた連絡"
        flush
      >
        <div className="px-5 pb-3">
          <Segmented
            value={filter}
            onChange={(v) => {
              setFilter(v);
              setTouchedFilter(true);
            }}
            options={FILTERS.map((f) => ({
              value: f.value,
              label: (
                <>
                  {f.label}
                  {f.value !== "all" && counts[f.value] ? <span className="num ml-1 opacity-70">{counts[f.value]}</span> : null}
                </>
              ),
            }))}
          />
        </div>
        {shown.length === 0 ? (
          <div className="px-5 pb-3">
            <EmptyState title={filter === "all" ? "まだ連絡はありません" : `「${STATUS[filter]?.label}」の連絡はありません`}>
              {filter === "all"
                ? "専用 Gmail に届いたメールは毎時自動で取り込まれます。急ぎのときは「今すぐ取り込む」を押してください。"
                : filter === "new"
                  ? "新しく届いた連絡はすべて確認済みです。"
                  : undefined}
            </EmptyState>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="tbl min-w-[640px]">
              <thead>
                <tr>
                  <th>受信日</th>
                  <th>件名</th>
                  <th>差出人</th>
                  <th>期限</th>
                  <th>状態</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((n) => {
                  const st = STATUS[n.status] ?? { label: "—", tone: "gray" as Tone };
                  return (
                    <tr key={n.notice_id} className="cursor-pointer" onClick={() => openDetail(n.notice_id)}>
                      <td className="num whitespace-nowrap text-ink-sub">{fmtDateTime(n.received_at, false)}</td>
                      <td>
                        <button type="button" className="text-left font-bold hover:underline" onClick={() => openDetail(n.notice_id)}>
                          {n.subject}
                        </button>
                        {n.digest?.summary && <div className="mt-0.5 line-clamp-1 text-xs text-ink-sub">{n.digest.summary}</div>}
                      </td>
                      <td className="text-ink-sub">{sender(n)}</td>
                      <td className="whitespace-nowrap">
                        <DueBadge iso={n.digest?.deadline} settled={n.status === "archived"} />
                      </td>
                      <td className="whitespace-nowrap">
                        <Badge tone={st.tone}>{st.label}</Badge>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <ManualImport
        onCreated={(n) => {
          load();
          openDetail(n.notice_id);
        }}
      />
    </div>
  );
}

// ---------------------------------------------------------------- Gmail の取り込み（1行）
function GmailBar({ lastReceived, onImported }: { lastReceived: string | null; onImported: () => void }) {
  const { toast } = useUi();
  const [busy, setBusy] = useState<"check" | "import" | null>(null);
  const [preview, setPreview] = useState<GmailResult | null>(null);

  const errMap: Rule[] = [
    [/OAuth|未登録/, "Gmail との連携がまだ設定されていません。システム担当者に「Gmail の連携設定」を依頼してください。"],
    [/Gmail取込に失敗/, "Gmail を読めませんでした。少し時間をおいてもう一度お試しください。"],
  ];

  async function run(dryRun: boolean) {
    setBusy(dryRun ? "check" : "import");
    try {
      const r = await api<GmailResult>("/notices/import-gmail", { method: "POST", body: JSON.stringify({ dry_run: dryRun }) });
      if (dryRun) {
        setPreview(r);
      } else {
        setPreview(null);
        toast(r.total ? `取り込みました（新しく ${r.created}件・更新 ${r.updated}件）` : "新しいメールはありませんでした");
        onImported();
      }
    } catch (e) {
      toast(friendly(e, errMap), "error");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="mb-4 rounded-card bg-white px-5 py-3">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <div className="flex min-w-0 flex-1 items-start gap-2 text-sm">
          <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-line" aria-hidden />
          <span>
            専用 Gmail の「対外連絡」ラベルのメールを<b>毎時自動で</b>取り込み中
            <span className="ml-2 whitespace-nowrap text-xs text-ink-sub">
              {lastReceived ? `最後に届いたメール ${fmtDateTime(lastReceived)}` : "まだ取り込んだメールはありません"}
            </span>
          </span>
        </div>
        <div className="flex shrink-0 gap-1.5">
          <Button size="sm" variant="ghost" disabled={!!busy} onClick={() => run(true)}>
            {busy === "check" ? "確認中…" : "取り込む前に確認"}
          </Button>
          <Button size="sm" disabled={!!busy} onClick={() => run(false)}>
            {busy === "import" ? "取り込み中…" : "今すぐ取り込む"}
          </Button>
        </div>
      </div>
      {preview && (
        <div className="mt-3 rounded-2xl bg-gray-50 px-4 py-3 text-sm">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-bold">
              {preview.total === 0
                ? "新しく取り込むメールはありません"
                : `${preview.total}件のメールが見つかりました（新しく ${preview.created}件・更新 ${preview.updated}件）`}
            </span>
            <button type="button" className="text-xs font-bold text-ink-sub" onClick={() => setPreview(null)}>
              閉じる
            </button>
          </div>
          {preview.items && preview.items.length > 0 && (
            <ul className="mt-2 space-y-1">
              {preview.items.map((it, i) => (
                <li key={i} className="flex items-center gap-2">
                  <Badge tone={it.action === "created" ? "green" : "gray"}>{it.action === "created" ? "新規" : "更新"}</Badge>
                  <span className="truncate">{it.subject}</span>
                </li>
              ))}
            </ul>
          )}
          {preview.total > 0 && <div className="mt-2 text-xs text-ink-sub">まだ取り込んでいません。よければ「今すぐ取り込む」を押してください。</div>}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 手動で貼り付け
function ManualImport({ onCreated }: { onCreated: (n: Notice) => void }) {
  const { toast } = useUi();
  const [form, setForm] = useState({ subject: "", from_name: "", body_text: "" });
  const [saving, setSaving] = useState(false);

  async function submit() {
    if (!form.subject.trim() || !form.body_text.trim()) {
      toast("件名と本文を入力してください", "error");
      return;
    }
    setSaving(true);
    try {
      const created = await api<Notice>("/notices", { method: "POST", body: JSON.stringify(form) });
      setForm({ subject: "", from_name: "", body_text: "" });
      toast(`「${created.subject}」を取り込みました`);
      onCreated(created);
    } catch (e) {
      toast(friendly(e), "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <details className="group mb-4 rounded-card bg-white px-5 py-4">
      <summary className="cursor-pointer list-none font-extrabold">
        <span className="mr-1 inline-block transition group-open:rotate-90">›</span>
        メールを貼り付けて取り込む
        <span className="ml-2 text-xs font-normal text-ink-sub">Gmail 以外で届いた連絡や、自動で取り込まれなかったとき</span>
      </summary>
      <div className="mt-4 space-y-3">
        <div className="grid gap-3 md:grid-cols-3">
          <Field label="件名" className="md:col-span-2">
            <input className="w-full" value={form.subject} onChange={(e) => setForm({ ...form, subject: e.target.value })} />
          </Field>
          <Field label="差出人（任意）">
            <input className="w-full" placeholder="例: 福島ブロック協議会" value={form.from_name} onChange={(e) => setForm({ ...form, from_name: e.target.value })} />
          </Field>
        </div>
        <Field label="本文" hint="メールの本文をそのまま貼り付けてください。原文として保存されます。同じ内容を2回貼り付けても重複しません。">
          <textarea className="h-36 w-full" value={form.body_text} onChange={(e) => setForm({ ...form, body_text: e.target.value })} />
        </Field>
        <div className="flex justify-end">
          <Button variant="primary" disabled={saving} onClick={submit}>
            {saving ? "取り込み中…" : "取り込む"}
          </Button>
        </div>
      </div>
    </details>
  );
}

// ---------------------------------------------------------------- 送り先を選ぶ
function ScopePicker({ members, scope, onChange }: { members: Member[]; scope: Scope; onChange: (s: Scope) => void }) {
  const [q, setQ] = useState("");
  const deliverable = members.filter(isDeliverable);
  const committees = useMemo(
    () => [...new Set(members.flatMap((m) => [...committeesOf(m)]))].sort(),
    [members]
  );
  const officers = useMemo(
    () => [...new Set(members.map((m) => m.officer_role).filter((r): r is string => !!r))],
    [members]
  );
  const toggle = (v: string) =>
    onChange({ ...scope, value: scope.value.includes(v) ? scope.value.filter((x) => x !== v) : [...scope.value, v] });
  const chip = (v: string, label: string) => (
    <button
      key={v}
      type="button"
      onClick={() => toggle(v)}
      aria-pressed={scope.value.includes(v)}
      className={`rounded-full px-3 py-1 text-xs font-bold transition ${
        scope.value.includes(v) ? "bg-line text-white" : "bg-gray-100 text-ink-sub hover:bg-gray-200"
      }`}
    >
      {label}
    </button>
  );

  return (
    <div className="space-y-2">
      <Segmented<ScopeKind>
        value={scope.kind}
        onChange={(k) => onChange({ kind: k, value: [] })}
        options={[
          { value: "all", label: "全員" },
          { value: "committee", label: "委員会" },
          { value: "officer", label: "役職" },
          { value: "custom", label: "人を選ぶ" },
        ]}
      />
      {scope.kind === "committee" && (
        <div className="flex flex-wrap gap-1.5">{committees.length ? committees.map((c) => chip(c, c)) : <span className="text-xs text-ink-faint">委員会の登録がありません</span>}</div>
      )}
      {scope.kind === "officer" && (
        <div className="flex flex-wrap gap-1.5">{officers.length ? officers.map((r) => chip(r, r)) : <span className="text-xs text-ink-faint">役職の登録がありません</span>}</div>
      )}
      {scope.kind === "custom" && (
        <div className="rounded-xl border border-gray-200 p-2">
          <input className="mb-2 w-full !py-1.5" placeholder="名前で探す" value={q} onChange={(e) => setQ(e.target.value)} />
          <div className="grid max-h-48 grid-cols-2 gap-x-3 gap-y-1 overflow-y-auto sm:grid-cols-3">
            {deliverable
              .filter((m) => !q || m.name.includes(q))
              .map((m) => (
                <label key={m.member_id} className="flex items-center gap-1.5">
                  <input type="checkbox" checked={scope.value.includes(m.member_id)} onChange={() => toggle(m.member_id)} />
                  <span className="truncate">{m.name}</span>
                </label>
              ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 詳細
function NoticeDetail({
  notice,
  members,
  onBack,
  onChanged,
}: {
  notice: Notice;
  members: Member[];
  onBack: () => void;
  onChanged: () => void;
}) {
  const { confirm, toast } = useUi();
  const [busy, setBusy] = useState<string | null>(null);
  const [draft, setDraft] = useState(notice.digest?.announcement ?? "");
  const [scope, setScope] = useState<Scope>({ kind: "all", value: [] });
  const [actions, setActions] = useState<NoticeAction[]>([]);
  const [openAction, setOpenAction] = useState<string | null>(null);

  const names = useMemo(() => new Map(members.map((m) => [m.member_id, m.name])), [members]);
  const recipients = resolveScope(members, scope);
  const scopeReady = scope.kind === "all" || scope.value.length > 0;
  const st = STATUS[notice.status] ?? { label: "—", tone: "gray" as Tone };

  useEffect(() => setDraft(notice.digest?.announcement ?? ""), [notice.notice_id, notice.digest?.announcement]);

  const loadActions = () =>
    api<NoticeAction[]>(`/notices/${notice.notice_id}/actions`)
      .then(setActions)
      .catch(() => setActions([]));
  useEffect(() => {
    loadActions();
  }, [notice.notice_id]);

  async function post<T>(label: string, path: string, body?: object, errMap?: Rule[]): Promise<T | null> {
    setBusy(label);
    try {
      return await api<T>(`/notices/${notice.notice_id}/${path}`, {
        method: "POST",
        body: body ? JSON.stringify(body) : undefined,
      });
    } catch (e) {
      toast(friendly(e, errMap), "error");
      return null;
    } finally {
      setBusy(null);
    }
  }

  async function makeDigest() {
    const r = await post<Notice>("digest", "digest", undefined, [
      [/生成できませんでした|LLM/, "AI の要約を作れませんでした。少し時間をおいてもう一度お試しください。"],
    ]);
    if (r) {
      toast(notice.digest ? "要約を作り直しました" : "要約と告知文を作りました");
      onChanged();
    }
  }

  async function archive() {
    const ok = await confirm({
      title: "アーカイブしますか？",
      body: <>「{notice.subject}」を対応不要としてアーカイブに移します。会員への連絡は送られません。</>,
      confirmLabel: "アーカイブする",
    });
    if (!ok) return;
    if (await post("archive", "archive")) {
      toast("アーカイブしました");
      onChanged();
      onBack();
    }
  }

  async function deliver() {
    const again = notice.status === "delivered";
    if (!scopeReady) {
      toast("送り先を選んでください", "error");
      return;
    }
    if (recipients.length === 0) {
      toast("送り先に LINE 連携済みの人がいません", "error");
      return;
    }
    const ok = await confirm({
      title: again ? `もう一度 ${recipients.length}人に送りますか？` : `${recipients.length}人に LINE で送りますか？`,
      body: (
        <>
          <div>
            送り先: <b className="text-ink">{scopeLabel(scope, names)}</b>（{recipients.length}人）
          </div>
          <div className="mt-1 text-xs">{namesPreview(recipients)}</div>
          {again && <div className="mt-2 text-danger">この連絡は配信済みです。同じ人にもう一度届きます。</div>}
          <div className="mt-2 text-xs">送ったメッセージは取り消せません。</div>
        </>
      ),
      confirmLabel: again ? "もう一度送る" : "送る",
    });
    if (!ok) return;
    const r = await post<DeliverResult & { notice: Notice }>(
      "deliver",
      "deliver",
      { target_scope: scope, body_text: draft, force: again },
      [[/force=true/, "この連絡はすでに配信済みです。画面を読み込み直してください。"]]
    );
    if (r) {
      toast(resultLine(r, `${r.targets}人に送りました`));
      onChanged();
    }
  }

  async function createActions() {
    if (!scopeReady || recipients.length === 0) {
      toast("対象の人を選んでください", "error");
      return;
    }
    const ok = await confirm({
      title: "やることを登録しますか？",
      body: (
        <>
          AI が見つけたやること（{notice.digest?.actions.length ?? 0}件）を、
          <b className="text-ink">{scopeLabel(scope, names)}</b>（{recipients.length}人）の対応状況として記録します。
          <div className="mt-1 text-xs">この時点では LINE は送られません。登録済みのものは重ねて登録されません。</div>
        </>
      ),
      confirmLabel: "登録する",
    });
    if (!ok) return;
    const list = await post<NoticeAction[]>("actions", "actions", { target_scope: scope }, [
      [/起票するアクション|titles/, "登録するやることがありません。先に AI で要約してください。"],
    ]);
    if (list) {
      setActions(list);
      toast(`やることを ${list.length}件 にしました`);
    }
  }

  async function remind(a: NoticeAction) {
    const pendingIds = a.assignees.filter((m) => !a.done_by.includes(m));
    const pendingNames = pendingIds.map((id) => ({ name: names.get(id) ?? "（名前不明）" }));
    const ok = await confirm({
      title: `まだの ${pendingIds.length}人に催促を送りますか？`,
      body: (
        <>
          <div>「{a.title}」がまだの人だけに LINE で送ります。</div>
          <div className="mt-1 text-xs">{namesPreview(pendingNames)}</div>
          <div className="mt-2 text-xs">メッセージには「対応しました」ボタンが付きます。送ったメッセージは取り消せません。</div>
        </>
      ),
      confirmLabel: "催促を送る",
    });
    if (!ok) return;
    const r = await post<DeliverResult & { pending: number }>("remind:" + a.action_id, `actions/${a.action_id}/remind`);
    if (r) {
      toast(resultLine({ ...r, targets: r.pending }, `${r.pending}人に催促しました`));
      loadActions();
    }
  }

  async function markDone(a: NoticeAction, memberId: string) {
    const name = names.get(memberId) ?? "この人";
    const ok = await confirm({
      title: `${name} さんを対応済みにしますか？`,
      body: <>「{a.title}」を {name} さんの代わりに対応済みとして記録します（本人に連絡は送られません）。</>,
      confirmLabel: "対応済みにする",
    });
    if (!ok) return;
    if (await post("done:" + memberId, `actions/${a.action_id}/done`, { member_id: memberId })) {
      toast(`${name} さんを対応済みにしました`);
      loadActions();
    }
  }

  return (
    <div>
      <button type="button" onClick={onBack} className="mb-3 text-sm font-bold text-line-ink hover:underline">
        ‹ 一覧に戻る
      </button>
      <PageHeader
        title={notice.subject}
        description={
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <Badge tone={st.tone}>{st.label}</Badge>
            <span>{sender(notice)}</span>
            <span className="num">受信 {fmtDateTime(notice.received_at)}</span>
            <span>{notice.source === "gmail" ? "Gmail から自動で取り込み" : "貼り付けて取り込み"}</span>
          </span>
        }
        actions={
          notice.status !== "archived" && (
            <Button variant="ghost" disabled={!!busy} onClick={archive}>
              アーカイブする
            </Button>
          )
        }
      />

      <div className="grid gap-4 lg:grid-cols-2">
        {/* 1. 内容を確認 */}
        <Panel
          title="AI の要約"
          action={
            <Button size="sm" disabled={!!busy} onClick={makeDigest}>
              {busy === "digest" ? "AI が読んでいます…" : notice.digest ? "作り直す" : "AI で要約する"}
            </Button>
          }
        >
          {notice.digest ? (
            <div className="space-y-3 text-sm">
              <p className="leading-relaxed">{notice.digest.summary}</p>
              <dl className="grid grid-cols-[5.5rem_1fr] gap-y-1">
                <dt className="text-ink-sub">対象（推定）</dt>
                <dd>{notice.digest.audience_hint || "わかりません"}</dd>
                <dt className="text-ink-sub">期限</dt>
                <dd>
                  <DueBadge iso={notice.digest.deadline} settled={notice.status === "archived"} />
                </dd>
              </dl>
              {notice.digest.actions.length > 0 && (
                <div>
                  <div className="field-label">会員がやること</div>
                  <ul className="list-disc space-y-0.5 pl-5">
                    {notice.digest.actions.map((a, i) => (
                      <li key={i}>{a}</li>
                    ))}
                  </ul>
                </div>
              )}
              <p className="text-xs text-ink-faint">※ AI の要約です。原文と見比べて確認してください。</p>
            </div>
          ) : (
            <EmptyState title="まだ要約していません">「AI で要約する」を押すと、要約・会員向けの告知文・期限・やることを作ります。</EmptyState>
          )}
        </Panel>

        <Panel title="原文">
          <div className="max-h-[420px] overflow-y-auto whitespace-pre-wrap rounded-xl bg-gray-50 p-3 text-sm leading-relaxed">
            {notice.body_text}
          </div>
          {notice.attachments.length > 0 && (
            <div className="mt-3">
              <div className="field-label">添付ファイル</div>
              <ul className="space-y-0.5 text-sm">
                {notice.attachments.map((a, i) => (
                  <li key={i}>・{a.name}</li>
                ))}
              </ul>
            </div>
          )}
        </Panel>
      </div>

      {/* 2. 会員へ届ける */}
      <Panel title="会員へ LINE で届ける">
        {!notice.digest ? (
          <EmptyState title="先に AI で要約してください">要約すると、会員向けの告知文の下書きができます。</EmptyState>
        ) : (
          <div className="grid gap-5 lg:grid-cols-2">
            <Field label="告知文（送る前に直せます）">
              <textarea className="h-48 w-full" value={draft} onChange={(e) => setDraft(e.target.value)} />
            </Field>
            <div>
              <span className="field-label">送り先</span>
              <ScopePicker members={members} scope={scope} onChange={setScope} />
              <div className="mt-3 rounded-xl bg-gray-50 px-3 py-2 text-sm">
                {scopeReady ? (
                  <>
                    <b className="num">{recipients.length}人</b> に届きます
                    <span className="text-xs text-ink-sub">（LINE 連携済みの人だけ）</span>
                    {recipients.length > 0 && scope.kind !== "all" && <div className="mt-0.5 text-xs text-ink-sub">{namesPreview(recipients)}</div>}
                  </>
                ) : (
                  <span className="text-ink-sub">送り先を選んでください</span>
                )}
              </div>
              {notice.status === "delivered" && notice.delivery && (
                <div className="mt-2 text-xs font-bold text-line-ink">
                  {fmtDateTime(notice.delivery.delivered_at)} に {notice.delivery.target_count}人へ送信済み
                </div>
              )}
              <div className="mt-3 flex justify-end">
                <Button
                  variant="primary"
                  disabled={!!busy || !draft.trim() || !scopeReady || recipients.length === 0}
                  onClick={deliver}
                >
                  {busy === "deliver" ? "送信中…" : notice.status === "delivered" ? "もう一度送る" : "LINE で送る"}
                </Button>
              </div>
            </div>
          </div>
        )}
      </Panel>

      {/* 3. 対応の確認 */}
      <Panel
        title="やることの対応状況"
        action={
          notice.digest && notice.digest.actions.length > 0 && (
            <Button size="sm" disabled={!!busy} onClick={createActions}>
              {actions.length ? "AI のやることを追加で登録" : "やることを登録する"}
            </Button>
          )
        }
        flush={actions.length > 0}
      >
        {actions.length === 0 ? (
          <EmptyState title="まだ登録していません">
            {notice.digest?.actions.length
              ? "「やることを登録する」を押すと、上で選んだ送り先の一人ひとりが対応したかを記録できます。会員は LINE の「対応しました」ボタンで完了にできます。"
              : "AI の要約に「会員がやること」があると、ここで対応状況を記録できます。"}
          </EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="tbl min-w-[600px]">
              <thead>
                <tr>
                  <th>やること</th>
                  <th>期限</th>
                  <th>対応</th>
                  <th>催促</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {actions.map((a) => {
                  const done = a.done_by.length;
                  const total = a.assignees.length;
                  const pendingIds = a.assignees.filter((m) => !a.done_by.includes(m));
                  const expanded = openAction === a.action_id;
                  return (
                    <Fragment key={a.action_id}>
                      <tr>
                        <td className="font-bold">{a.title}</td>
                        <td className="whitespace-nowrap">
                          <DueBadge iso={a.due} settled={done === total} />
                        </td>
                        <td className="w-40">
                          <div className="num mb-1 text-xs">
                            {done === total ? <span className="font-bold text-line-ink">全員済み {done}/{total}人</span> : <>{done}/{total}人</>}
                          </div>
                          <Progress value={done} max={total} />
                        </td>
                        <td className="num whitespace-nowrap text-ink-sub">
                          {a.reminder_count > 0 ? `${a.reminder_count}回${a.reminded_at ? `（${fmtDateTime(a.reminded_at, false)}）` : ""}` : "—"}
                        </td>
                        <td className="whitespace-nowrap text-right">
                          {pendingIds.length > 0 && (
                            <>
                              <Button size="sm" variant="ghost" onClick={() => setOpenAction(expanded ? null : a.action_id)}>
                                {expanded ? "閉じる" : "まだの人"}
                              </Button>
                              <Button size="sm" disabled={!!busy} onClick={() => remind(a)}>
                                {busy === "remind:" + a.action_id ? "送信中…" : "催促する"}
                              </Button>
                            </>
                          )}
                        </td>
                      </tr>
                      {expanded && (
                        <tr>
                          <td colSpan={5} className="bg-gray-50/70">
                            <div className="mb-1 text-xs text-ink-sub">まだの人（電話などで確認できたら、代わりに対応済みにできます）</div>
                            <div className="flex flex-wrap gap-1.5">
                              {pendingIds.map((id) => (
                                <span key={id} className="inline-flex items-center gap-1 rounded-full bg-white py-0.5 pl-3 pr-1 text-sm">
                                  {names.get(id) ?? "（名前不明）"}
                                  <Button size="sm" variant="ghost" disabled={!!busy} onClick={() => markDone(a, id)}>
                                    対応済みにする
                                  </Button>
                                </span>
                              ))}
                            </div>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
