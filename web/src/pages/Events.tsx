import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../api/client";
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

// 予定・出欠（docs/admin-ui-design.md §3）
// 一覧は月ごと・日付順。詳細（出欠・編集・中止/再開・催促）は右から開く引き出しで扱う。
// URL: ?new=1 で登録フォーム、?id=<予定> でその予定の詳細を開く（ホームからのリンク）。

// ---------------------------------------------------------------- 型
type EventRow = {
  event_id: string;
  type: string;
  title: string;
  datetime_start: string;
  location: string | null;
  fiscal_year?: number | null;
  attendance_deadline: string | null;
  reminder_policy_id?: string | null;
  target_scope: { kind: string; value: string[] };
  quorum: number | null;
  status: string;
  origin?: string;
  gcal_event_id?: string | null;
  gcal_sync_state?: string;
  gcal_error?: string | null;
};

type GcalStatus = {
  enabled: boolean;
  calendar_id: string | null;
  last_pulled_at: string | null;
  pull_error: string | null;
  unsynced: number;
  errors: { event_id: string; title: string; error: string | null }[];
};

type Summary = {
  total_targets: number;
  answered: number;
  unanswered: number;
  attendance_rate: number;
  counts: Record<string, number>;
  unanswered_member_ids?: string[];
  present: number;
  proxies: number;
  present_with_proxies: number;
  quorum: number | null;
  quorum_met: boolean | null;
  quorum_met_without_proxies: boolean | null;
};
type Attendance = { member_id: string; status: string; proxy_member_id: string | null };
type Member = { member_id: string; name: string };
type Trend = { title: string; date: string; attendance_rate: number; answer_rate: number };
type Policy = { policy_id: string; event_type: string; stages: { name: string }[] };

// ---------------------------------------------------------------- 表示用の言葉
const EVENT_TYPES = ["例会", "理事会", "五役会", "委員会", "総会", "行事", "対外", "個別"];
const FISCAL_YEAR_HELP = "翌年度の予定のときだけ入力（カレンダーのタイトルに「27」などが付きます）";

type Tone = "green" | "gray" | "red" | "amber" | "blue";
const STATUS_INFO: Record<string, { label: string; tone: Tone }> = {
  draft: { label: "下書き", tone: "gray" },
  open: { label: "出欠受付中", tone: "green" },
  closed: { label: "締切", tone: "blue" },
  cancelled: { label: "中止", tone: "gray" },
};
const statusInfo = (s: string) => STATUS_INFO[s] ?? { label: "—", tone: "gray" as Tone };

type StatusFilter = "all" | "draft" | "open" | "closed" | "cancelled";
const STATUS_FILTERS: { value: StatusFilter; label: string }[] = [
  { value: "all", label: "すべて" },
  { value: "draft", label: "下書き" },
  { value: "open", label: "出欠受付中" },
  { value: "closed", label: "締切" },
  { value: "cancelled", label: "中止" },
];

const SCOPE_KINDS = [
  { key: "all", label: "全員" },
  { key: "committee", label: "委員会で指定" },
  { key: "officer", label: "役職で指定" },
  { key: "custom", label: "会員を選ぶ" },
];

const ATT_STATUSES = ["出席", "Web出席", "欠席", "委任", "未回答"];
const ATT_TONE: Record<string, Tone> = {
  出席: "green",
  Web出席: "green",
  欠席: "gray",
  委任: "blue",
  未回答: "amber",
};

// ---------------------------------------------------------------- 小さな関数
/** カレンダーと同じ表記（app/calendar_sync.py calendar_title と同じ規則）。 */
function calTitle(e: Pick<EventRow, "type" | "title" | "fiscal_year" | "datetime_start">): string {
  const year = Number((e.datetime_start || "").slice(0, 4));
  const fy = e.fiscal_year;
  const prefix = fy && year && fy > year ? String(fy % 100).padStart(2, "0") : "";
  if (e.title.startsWith(`【${e.type}】`)) return prefix + e.title;
  if (e.title.trim() === e.type) return prefix ? `${prefix}【${e.type}】` : e.title;
  return `${prefix}【${e.type}】${e.title}`;
}

/** datetime-local 用（"YYYY-MM-DDTHH:MM"）に整える。 */
function dtLocal(iso: string | null | undefined): string {
  return iso ? iso.slice(0, 16) : "";
}
const dayKey = (iso: string) => iso.slice(0, 10);
const timeOf = (iso: string) => iso.slice(11, 16);
function todayKey(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
function monthLabel(key: string): string {
  const [y, m] = key.split("-");
  return `${y}年${Number(m)}月`;
}
function groupByMonth(rows: EventRow[]): [string, EventRow[]][] {
  const map = new Map<string, EventRow[]>();
  for (const r of rows) {
    const k = r.datetime_start.slice(0, 7);
    map.set(k, [...(map.get(k) ?? []), r]);
  }
  return [...map.entries()];
}

/** カレンダーへの反映で気にすべき状態（反映済み・連携なし・中止は null）。 */
function gcalIssue(e: EventRow, enabled: boolean): "pending" | "error" | null {
  if (!enabled || e.status === "cancelled") return null;
  const s = e.gcal_sync_state ?? "pending";
  if (s === "synced") return null;
  return s === "error" ? "error" : "pending";
}

function GcalIssueBadge({ issue, error }: { issue: "pending" | "error" | null; error?: string | null }) {
  if (!issue) return null;
  return (
    <span title={issue === "error" ? error ?? undefined : "Googleカレンダーにまだ反映されていません"}>
      <Badge tone={issue === "error" ? "red" : "amber"}>
        {issue === "error" ? "カレンダー反映エラー" : "カレンダー未反映"}
      </Badge>
    </span>
  );
}

function policyLabel(p: Policy): string {
  const stages = p.stages.map((s) => s.name).join("→");
  return `${p.event_type}用の標準${stages ? `（${stages}）` : ""}`;
}

function scopeText(scope: EventRow["target_scope"], names: Record<string, string>): string {
  if (scope.kind === "all") return "全員";
  const list =
    scope.kind === "custom" ? scope.value.map((id) => names[id] ?? "（名簿にない会員）") : scope.value;
  const head = scope.kind === "committee" ? "委員会" : scope.kind === "officer" ? "役職" : "会員";
  return list.length ? `${head}: ${list.join("、")}` : `${head}（未指定）`;
}

function friendlyError(e: unknown): string {
  const m = (e as Error)?.message ?? "";
  if (m.includes("(404)")) return "この予定は見つかりませんでした。削除された可能性があります。";
  if (m.includes("(503)")) return "Googleカレンダーとの連携が準備されていません。設定画面をご確認ください。";
  if (m.includes("(400)")) return "入力内容を確認してください（年度は西暦4桁など）。";
  return m || "うまくいきませんでした。時間をおいてもう一度お試しください。";
}

async function downloadFile(url: string, filename: string) {
  const res = await fetch(url, { credentials: "include" });
  if (!res.ok) throw new Error("ファイルを作れませんでした。時間をおいてもう一度お試しください。");
  const blob = await res.blob();
  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(href);
}
const safeName = (s: string) => s.replace(/[\\/:*?"<>|\s]+/g, "_");

/** 確認ダイアログが開いているときは Escape で引き出しを閉じない */
function otherDialogOpen(self: HTMLElement | null): boolean {
  return [...document.querySelectorAll('[role="dialog"]')].some((d) => d !== self);
}

// ---------------------------------------------------------------- 予定ごとの操作（一覧・詳細で共通）
function useEventActions(after: () => void, gcalEnabled: boolean) {
  const { confirm, toast } = useUi();
  const run = async (fn: () => Promise<void>) => {
    try {
      await fn();
    } catch (e) {
      toast(friendlyError(e), "error");
    }
  };
  return {
    startAccepting: (e: EventRow) =>
      run(async () => {
        const ok = await confirm({
          title: `「${calTitle(e)}」の出欠受付を始めますか？`,
          body: "会員が LINE から出欠を回答できるようになります。",
          confirmLabel: "受付を始める",
        });
        if (!ok) return;
        await api(`/events/${e.event_id}`, { method: "PUT", body: JSON.stringify({ status: "open" }) });
        toast("出欠の受付を始めました");
        after();
      }),
    remind: (e: EventRow, count: number, who?: string[]) =>
      run(async () => {
        const ok = await confirm({
          title: `未回答の ${count} 名に催促しますか？`,
          body: (
            <>
              「{calTitle(e)}」の出欠について、LINE で催促のメッセージを送ります。
              {who && who.length > 0 && (
                <span className="mt-2 block text-xs">
                  送り先: {who.slice(0, 12).join("、")}
                  {who.length > 12 ? ` ほか ${who.length - 12} 名` : ""}
                </span>
              )}
            </>
          ),
          confirmLabel: "催促する",
        });
        if (!ok) return;
        const r = await api<{ targets: string[] }>(`/events/${e.event_id}/remind`, { method: "POST" });
        toast(`${r.targets.length} 名への催促を送信の予定に入れました`);
        after();
      }),
    cancel: (e: EventRow) =>
      run(async () => {
        const ok = await confirm({
          title: `「${calTitle(e)}」を中止にしますか？`,
          body: (
            <>
              出欠の回答はそのまま残り、あとで再開できます。
              {gcalEnabled && "Googleカレンダーの予定は削除されます。"}
            </>
          ),
          confirmLabel: "中止にする",
          danger: true,
        });
        if (!ok) return;
        await api(`/events/${e.event_id}/cancel`, { method: "POST" });
        toast(`「${calTitle(e)}」を中止にしました`);
        after();
      }),
    restore: (e: EventRow) =>
      run(async () => {
        const ok = await confirm({
          title: `「${calTitle(e)}」を再開しますか？`,
          body: (
            <>
              出欠受付中に戻ります。
              {gcalEnabled && "Googleカレンダーにも予定を作り直します。"}
            </>
          ),
          confirmLabel: "再開する",
        });
        if (!ok) return;
        await api(`/events/${e.event_id}/restore`, { method: "POST", body: JSON.stringify({ status: "open" }) });
        toast(`「${calTitle(e)}」を再開しました（出欠受付中）`);
        after();
      }),
    resync: (e: EventRow) =>
      run(async () => {
        const r = await api<EventRow>(`/events/${e.event_id}/gcal-sync`, { method: "POST" });
        if (r.gcal_sync_state === "synced") toast(`「${calTitle(e)}」をカレンダーに反映しました`);
        else toast("カレンダーに反映できませんでした。時間をおいてもう一度お試しください。", "error");
        after();
      }),
  };
}

// ---------------------------------------------------------------- 入力フォーム（登録・編集で共通）
type Form = {
  type: string;
  title: string;
  datetime_start: string;
  location: string;
  attendance_deadline: string;
  reminder_policy_id: string;
  quorum: string;
  fiscal_year: string;
  scope_kind: string;
  scope_value: string[];
  status: string;
};
const EMPTY_FORM: Form = {
  type: "例会",
  title: "",
  datetime_start: "",
  location: "",
  attendance_deadline: "",
  reminder_policy_id: "",
  quorum: "",
  fiscal_year: "",
  scope_kind: "all",
  scope_value: [],
  status: "open",
};

function toForm(e: EventRow): Form {
  return {
    type: e.type,
    title: e.title,
    datetime_start: dtLocal(e.datetime_start),
    location: e.location ?? "",
    attendance_deadline: dtLocal(e.attendance_deadline),
    reminder_policy_id: e.reminder_policy_id ?? "",
    quorum: e.quorum != null ? String(e.quorum) : "",
    fiscal_year: e.fiscal_year != null ? String(e.fiscal_year) : "",
    scope_kind: e.target_scope?.kind ?? "all",
    scope_value: e.target_scope?.value ?? [],
    status: e.status,
  };
}

function validate(f: Form): string | null {
  if (!f.title.trim()) return "予定の名前を入力してください。";
  if (!f.datetime_start) return "開催日時を入力してください。";
  if (f.fiscal_year && !/^20\d\d$/.test(f.fiscal_year)) return "年度は西暦4桁（例: 2027）で入力してください。";
  if (f.quorum && !(Number(f.quorum) >= 0)) return "定足数は人数（数字）で入力してください。";
  return null;
}

function toPayload(f: Form) {
  return {
    type: f.type,
    title: f.title.trim(),
    datetime_start: f.datetime_start,
    location: f.location.trim() || null,
    attendance_deadline: f.attendance_deadline || null,
    reminder_policy_id: f.reminder_policy_id || null,
    quorum: f.quorum ? Number(f.quorum) : null,
    fiscal_year: f.fiscal_year ? Number(f.fiscal_year) : null,
    target_scope: { kind: f.scope_kind, value: f.scope_kind === "all" ? [] : f.scope_value },
  };
}

function EventForm({
  value: f,
  onChange,
  members,
  policies,
  mode,
}: {
  value: Form;
  onChange: (f: Form) => void;
  members: Member[];
  policies: Policy[];
  mode: "create" | "edit";
}) {
  const set = (patch: Partial<Form>) => onChange({ ...f, ...patch });
  const preview = f.title.trim()
    ? calTitle({
        type: f.type,
        title: f.title.trim(),
        fiscal_year: f.fiscal_year ? Number(f.fiscal_year) : null,
        datetime_start: f.datetime_start,
      })
    : null;
  const knownPolicy = !f.reminder_policy_id || policies.some((p) => p.policy_id === f.reminder_policy_id);
  const cancelled = f.status === "cancelled";

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-[10rem_1fr]">
        <Field label="種別">
          <select className="w-full" value={f.type} onChange={(e) => set({ type: e.target.value })}>
            {EVENT_TYPES.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
        </Field>
        <Field label="予定の名前（必須）" hint={preview ? <>カレンダーでの表示: <b className="text-ink-sub">{preview}</b></> : undefined}>
          <input
            className="w-full"
            placeholder="例: 10月例会"
            value={f.title}
            onChange={(e) => set({ title: e.target.value })}
          />
        </Field>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="開催日時（必須）">
          <input
            type="datetime-local"
            className="w-full"
            value={f.datetime_start}
            onChange={(e) => set({ datetime_start: e.target.value })}
          />
        </Field>
        <Field label="場所">
          <input
            className="w-full"
            placeholder="例: 猪苗代町体験交流館"
            value={f.location}
            onChange={(e) => set({ location: e.target.value })}
          />
        </Field>
      </div>
      {!cancelled && (
        <Field label="状態" className="max-w-xs">
          <select className="w-full" value={f.status} onChange={(e) => set({ status: e.target.value })}>
            <option value="open">出欠受付中（会員が回答できます）</option>
            <option value="draft">下書き（まだ会員に見せない）</option>
            {mode === "edit" && <option value="closed">締切（回答を締め切る）</option>}
          </select>
        </Field>
      )}

      <details className="group rounded-2xl bg-gray-50 px-4 py-3" open={mode === "edit"}>
        <summary className="cursor-pointer select-none text-sm font-bold">
          詳しい設定
          <span className="ml-2 text-xs font-medium text-ink-sub">出欠の締切・自動の催促・定足数・年度・対象</span>
        </summary>
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          <Field label="出欠の締切" hint="空欄なら開催日時が締切の目安になります">
            <input
              type="datetime-local"
              className="w-full"
              value={f.attendance_deadline}
              onChange={(e) => set({ attendance_deadline: e.target.value })}
            />
          </Field>
          <Field
            label="自動の催促"
            hint={
              policies.length === 0
                ? "催促のタイミングは設定画面で準備できます"
                : "締切（空欄なら開催日時）を基準に、決まった時刻に LINE でお知らせします"
            }
          >
            <select
              className="w-full"
              value={f.reminder_policy_id}
              onChange={(e) => set({ reminder_policy_id: e.target.value })}
            >
              <option value="">自動では催促しない</option>
              {policies.map((p) => (
                <option key={p.policy_id} value={p.policy_id}>{policyLabel(p)}</option>
              ))}
              {!knownPolicy && <option value={f.reminder_policy_id}>いまの設定のまま</option>}
            </select>
          </Field>
          <Field label="定足数（人数）" hint="理事会・総会などで成立の判定に使います">
            <input
              type="number"
              min={0}
              className="w-full"
              placeholder="例: 12"
              value={f.quorum}
              onChange={(e) => set({ quorum: e.target.value })}
            />
          </Field>
          <Field label="年度" hint={FISCAL_YEAR_HELP}>
            <input
              type="number"
              className="w-full"
              placeholder="例: 2027"
              value={f.fiscal_year}
              onChange={(e) => set({ fiscal_year: e.target.value })}
            />
          </Field>
          <Field label="出欠の対象">
            <select
              className="w-full"
              value={f.scope_kind}
              onChange={(e) => set({ scope_kind: e.target.value, scope_value: [] })}
            >
              {SCOPE_KINDS.map((k) => (
                <option key={k.key} value={k.key}>{k.label}</option>
              ))}
            </select>
          </Field>
          {(f.scope_kind === "committee" || f.scope_kind === "officer") && (
            <Field label={f.scope_kind === "committee" ? "委員会の名前（「、」か「,」で区切る）" : "役職（「、」か「,」で区切る）"}>
              <input
                className="w-full"
                placeholder={f.scope_kind === "committee" ? "例: 総務委員会、組織未来推進委員会" : "例: 理事長、副理事長、専務理事"}
                value={f.scope_value.join("、")}
                onChange={(e) =>
                  set({
                    scope_value: e.target.value
                      .split(/[,、，]/)
                      .map((x) => x.trim())
                      .filter(Boolean),
                  })
                }
              />
            </Field>
          )}
          {f.scope_kind === "custom" && (
            <div className="sm:col-span-2">
              <span className="field-label">対象の会員（{f.scope_value.length}名を選択中）</span>
              <div className="grid max-h-48 grid-cols-2 gap-x-3 gap-y-1 overflow-y-auto rounded-xl bg-white p-3 sm:grid-cols-3">
                {members.map((m) => {
                  const on = f.scope_value.includes(m.member_id);
                  return (
                    <label key={m.member_id} className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={on}
                        onChange={() =>
                          set({
                            scope_value: on
                              ? f.scope_value.filter((x) => x !== m.member_id)
                              : [...f.scope_value, m.member_id],
                          })
                        }
                      />
                      {m.name}
                    </label>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      </details>
    </div>
  );
}

// ---------------------------------------------------------------- 登録（モーダル）
function CreateDialog({
  members,
  policies,
  onClose,
  onCreated,
}: {
  members: Member[];
  policies: Policy[];
  onClose: () => void;
  onCreated: (title: string) => void;
}) {
  const { toast } = useUi();
  const [form, setForm] = useState<Form>(EMPTY_FORM);
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !otherDialogOpen(ref.current) && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function submit() {
    const v = validate(form);
    if (v) {
      setErr(v);
      return;
    }
    setSaving(true);
    try {
      await api("/events", { method: "POST", body: JSON.stringify({ ...toPayload(form), status: form.status }) });
      onCreated(calTitle({ ...form, fiscal_year: form.fiscal_year ? Number(form.fiscal_year) : null }));
    } catch (e) {
      toast(friendlyError(e), "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-black/30 p-4 sm:items-center">
      <div ref={ref} role="dialog" aria-modal="true" aria-label="予定を登録" className="w-full max-w-2xl rounded-card bg-white p-6">
        <div className="mb-4 flex items-start justify-between gap-3">
          <div>
            <div className="text-lg font-extrabold">予定を登録</div>
            <p className="mt-1 text-xs text-ink-sub">
              登録すると Googleカレンダーにも反映されます（連携中のとき）。
            </p>
          </div>
          <Button variant="ghost" size="sm" onClick={onClose}>閉じる</Button>
        </div>
        {err && <ErrorNote>{err}</ErrorNote>}
        <EventForm value={form} onChange={(f) => { setForm(f); setErr(null); }} members={members} policies={policies} mode="create" />
        <div className="mt-6 flex justify-end gap-2">
          <Button onClick={onClose}>やめる</Button>
          <Button variant="primary" onClick={submit} disabled={saving}>
            {saving ? "登録中…" : "登録する"}
          </Button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 詳細（右の引き出し）
function DetailDrawer({
  eventId,
  members,
  names,
  policies,
  gcalEnabled,
  onClose,
  onChanged,
}: {
  eventId: string;
  members: Member[];
  names: Record<string, string>;
  policies: Policy[];
  gcalEnabled: boolean;
  onClose: () => void;
  onChanged: () => void;
}) {
  const { toast } = useUi();
  const [event, setEvent] = useState<EventRow | null>(null);
  const [att, setAtt] = useState<Attendance[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [formErr, setFormErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [filter, setFilter] = useState<"all" | "unanswered" | "answered">("all");
  const [targetIds, setTargetIds] = useState<Set<string>>(new Set());
  const ref = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    try {
      const [ev, at] = await Promise.all([
        api<{ event: EventRow; summary: Summary }>(`/events/${eventId}`),
        api<{ attendances: Attendance[]; summary: Summary; target_member_ids: string[] }>(
          `/events/${eventId}/attendances`
        ),
      ]);
      setEvent(ev.event);
      setAtt(at.attendances);
      setTargetIds(new Set(at.target_member_ids));
      setSummary(at.summary);
      setErr(null);
    } catch (e) {
      setErr(friendlyError(e));
    }
  }, [eventId]);

  useEffect(() => {
    setEvent(null);
    setForm(null);
    setFilter("all");
    load();
  }, [load]);

  const editing = form !== null;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !editing && !otherDialogOpen(ref.current) && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, editing]);

  const refresh = () => {
    load();
    onChanged();
  };
  const actions = useEventActions(refresh, gcalEnabled);

  async function setStatus(memberId: string, status: string) {
    try {
      await api(`/events/${eventId}/attendances/${memberId}`, {
        method: "PUT",
        body: JSON.stringify({ status }),
      });
      toast(`${names[memberId] ?? "会員"} さんを「${status}」にしました`);
      refresh();
    } catch (e) {
      toast(friendlyError(e), "error");
    }
  }

  async function save() {
    if (!event || !form) return;
    const v = validate(form);
    if (v) {
      setFormErr(v);
      return;
    }
    setSaving(true);
    try {
      const body: Record<string, unknown> = toPayload(form);
      if (event.status !== "cancelled") body.status = form.status;
      await api(`/events/${event.event_id}`, { method: "PUT", body: JSON.stringify(body) });
      toast("予定を更新しました");
      setForm(null);
      refresh();
    } catch (e) {
      toast(friendlyError(e), "error");
    } finally {
      setSaving(false);
    }
  }

  async function download(kind: "csv" | "package") {
    if (!event) return;
    const base = safeName(`${dayKey(event.datetime_start)}_${event.title}`);
    try {
      if (kind === "csv") await downloadFile(`/api/events/${eventId}/attendances.csv`, `出欠_${base}.csv`);
      else await downloadFile(`/api/events/${eventId}/package?download=true`, `事前共有_${base}.md`);
    } catch (e) {
      toast(friendlyError(e), "error");
    }
  }

  const attByMember = Object.fromEntries(att.map((a) => [a.member_id, a]));
  const unansweredIds = new Set(summary?.unanswered_member_ids ?? []);
  // 対象者だけを並べる（五役・理事会などは対象が決まっているので全員は出さない）
  const rows = members.filter((m) => targetIds.has(m.member_id)).filter((m) => {
    const st = attByMember[m.member_id]?.status ?? "未回答";
    if (filter === "unanswered") return unansweredIds.has(m.member_id);
    if (filter === "answered") return st !== "未回答";
    return true;
  });
  const issue = event ? gcalIssue(event, gcalEnabled) : null;
  const policy = event?.reminder_policy_id ? policies.find((p) => p.policy_id === event.reminder_policy_id) : null;
  const isUpcoming = event ? dayKey(event.datetime_start) >= todayKey() : false;
  const unansweredNames = [...unansweredIds].map((id) => names[id] ?? "会員");

  return (
    <div
      className="fixed inset-0 z-40 flex justify-end bg-black/30"
      onClick={() => !editing && onClose()}
    >
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-label="予定の詳細"
        className="h-full w-full max-w-[760px] overflow-y-auto bg-canvas p-4 sm:p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between gap-2">
          <span className="text-sm font-bold text-ink-sub">予定の詳細</span>
          <Button variant="ghost" size="sm" onClick={onClose}>閉じる</Button>
        </div>

        {err && <ErrorNote>{err}</ErrorNote>}
        {!err && !event && <Loading />}

        {event && (
          <>
            {/* 見出し */}
            <section className="mb-4 rounded-card bg-white p-5">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={statusInfo(event.status).tone}>{statusInfo(event.status).label}</Badge>
                <GcalIssueBadge issue={issue} error={event.gcal_error} />
                {event.origin === "gcal" && <span className="text-xs text-ink-sub">Googleカレンダーから取り込んだ予定</span>}
                {event.origin === "line" && <span className="text-xs text-ink-sub">LINE から登録された予定</span>}
              </div>
              <h2 className={`mt-2 text-xl font-extrabold ${event.status === "cancelled" ? "text-ink-faint line-through" : ""}`}>
                {calTitle(event)}
              </h2>
              <div className="num mt-1 text-sm text-ink-sub">
                {fmtDateTime(event.datetime_start)}
                {event.location && `・${event.location}`}
              </div>
              <dl className="mt-3 grid gap-x-4 gap-y-1 text-sm sm:grid-cols-2">
                <div><dt className="inline text-ink-sub">出欠の締切: </dt><dd className="num inline">{event.attendance_deadline ? fmtDateTime(event.attendance_deadline) : "未設定"}</dd></div>
                <div><dt className="inline text-ink-sub">対象: </dt><dd className="inline">{scopeText(event.target_scope, names)}</dd></div>
                <div><dt className="inline text-ink-sub">自動の催促: </dt><dd className="inline">{policy ? policyLabel(policy) : event.reminder_policy_id ? "設定あり" : "なし"}</dd></div>
                <div><dt className="inline text-ink-sub">定足数: </dt><dd className="num inline">{event.quorum != null ? `${event.quorum}名` : "なし"}</dd></div>
              </dl>
              {issue === "error" && event.gcal_error && (
                <details className="mt-3 rounded-xl bg-danger-soft px-3 py-2 text-xs text-danger">
                  <summary className="cursor-pointer font-bold">カレンダーに反映できませんでした（詳しい内容）</summary>
                  <div className="mt-1 break-all">{event.gcal_error}</div>
                </details>
              )}
              {!editing && (
                <div className="mt-4 flex flex-wrap gap-2">
                  <Button size="sm" onClick={() => { setForm(toForm(event)); setFormErr(null); }}>編集</Button>
                  {issue && <Button size="sm" onClick={() => actions.resync(event)}>カレンダーに反映</Button>}
                  {event.status === "cancelled" ? (
                    <Button size="sm" onClick={() => actions.restore(event)}>再開する</Button>
                  ) : (
                    <Button size="sm" variant="danger" onClick={() => actions.cancel(event)}>中止にする</Button>
                  )}
                </div>
              )}
            </section>

            {editing && form && (
              <Panel title="予定を編集">
                {formErr && <ErrorNote>{formErr}</ErrorNote>}
                <EventForm value={form} onChange={(f) => { setForm(f); setFormErr(null); }} members={members} policies={policies} mode="edit" />
                <div className="mt-5 flex justify-end gap-2">
                  <Button onClick={() => setForm(null)}>やめる</Button>
                  <Button variant="primary" onClick={save} disabled={saving}>{saving ? "保存中…" : "保存する"}</Button>
                </div>
              </Panel>
            )}

            {!editing && summary && (
              <>
                <Panel
                  title="出欠"
                  action={
                    <div className="flex flex-wrap justify-end gap-1">
                      <Button size="sm" variant="ghost" onClick={() => download("csv")}>出欠表（CSV）</Button>
                      <Button size="sm" variant="ghost" onClick={() => download("package")}>事前共有の資料</Button>
                    </div>
                  }
                >
                  <div className="grid gap-3 sm:grid-cols-3">
                    <div className="rounded-2xl bg-gray-50 px-4 py-3 sm:col-span-2">
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="text-xs text-ink-sub">回答</span>
                        <span className="num text-xl font-extrabold">
                          {summary.answered}/{summary.total_targets}
                          <span className="ml-0.5 text-xs font-bold">人</span>
                        </span>
                      </div>
                      <div className="mt-2"><Progress value={summary.answered} max={summary.total_targets} /></div>
                      <div className="num mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-ink-sub">
                        {ATT_STATUSES.map((s) => (
                          <span key={s}>{s} {summary.counts[s] ?? 0}</span>
                        ))}
                      </div>
                    </div>
                    <div className="rounded-2xl bg-gray-50 px-4 py-3">
                      <div className="text-xs text-ink-sub">出席率</div>
                      <div className="num text-xl font-extrabold">
                        {Math.round(summary.attendance_rate * 100)}
                        <span className="ml-0.5 text-xs font-bold">%</span>
                      </div>
                      <div className="num text-xs text-ink-sub">出席 {summary.present}名（委任 {summary.proxies}名）</div>
                    </div>
                  </div>

                  {summary.quorum !== null && (
                    <div className="mt-3 rounded-2xl bg-gray-50 px-4 py-3 text-sm">
                      <div className="mb-1 text-xs text-ink-sub">定足数 {summary.quorum}名</div>
                      <div className="num flex flex-wrap items-center gap-2">
                        <span>委任を含む {summary.present_with_proxies}名</span>
                        <Badge tone={summary.quorum_met ? "green" : "red"}>{summary.quorum_met ? "足りています" : "足りません"}</Badge>
                        <span className="ml-2">委任を除く {summary.present}名</span>
                        <Badge tone={summary.quorum_met_without_proxies ? "green" : "red"}>
                          {summary.quorum_met_without_proxies ? "足りています" : "足りません"}
                        </Badge>
                      </div>
                      <p className="mt-1 text-xs text-ink-faint">委任を定足数に数えるかは規約によります。判断の参考に両方を表示しています。</p>
                    </div>
                  )}

                  {event.status !== "cancelled" && isUpcoming && summary.unanswered > 0 && (
                    <div className="mt-4 flex flex-wrap items-center justify-between gap-2 rounded-2xl bg-line-soft px-4 py-3">
                      <span className="text-sm font-bold text-line-ink">まだ回答していない方が {summary.unanswered} 名います</span>
                      <Button variant="primary" size="sm" onClick={() => actions.remind(event, summary.unanswered, unansweredNames)}>
                        未回答の方に催促
                      </Button>
                    </div>
                  )}
                  {event.status === "draft" && (
                    <div className="mt-4 flex flex-wrap items-center justify-between gap-2 rounded-2xl bg-gray-50 px-4 py-3">
                      <span className="text-sm text-ink-sub">下書きのため、会員はまだ回答できません。</span>
                      <Button size="sm" onClick={() => actions.startAccepting(event)}>出欠の受付を始める</Button>
                    </div>
                  )}
                </Panel>

                <Panel
                  title="会員ごとの回答"
                  action={
                    <Segmented
                      value={filter}
                      onChange={setFilter}
                      options={[
                        { value: "all", label: `対象者 ${summary.total_targets}` },
                        { value: "unanswered", label: `未回答 ${summary.unanswered}` },
                        { value: "answered", label: "回答済み" },
                      ]}
                    />
                  }
                  flush
                >
                  {rows.length === 0 ? (
                    <div className="px-5 pb-3">
                      <EmptyState title={filter === "unanswered" ? "未回答の方はいません" : "表示する会員がいません"} />
                    </div>
                  ) : (
                    <div className="overflow-x-auto">
                      <table className="tbl min-w-[520px]">
                        <thead>
                          <tr><th>会員</th><th>回答</th><th>委任先</th><th className="text-right">手で変更</th></tr>
                        </thead>
                        <tbody>
                          {rows.map((m) => {
                            const a = attByMember[m.member_id];
                            const st = a?.status ?? "未回答";
                            return (
                              <tr key={m.member_id}>
                                <td className="font-bold">{m.name}</td>
                                <td><Badge tone={ATT_TONE[st] ?? "gray"}>{st}</Badge></td>
                                <td className="text-ink-sub">
                                  {a?.proxy_member_id ? names[a.proxy_member_id] ?? "（名簿にない会員）" : "—"}
                                </td>
                                <td className="text-right">
                                  <select
                                    className="!py-1 text-xs"
                                    aria-label={`${m.name} さんの回答を変更`}
                                    value={st}
                                    onChange={(e) => setStatus(m.member_id, e.target.value)}
                                  >
                                    {ATT_STATUSES.map((s) => (
                                      <option key={s} value={s}>{s}</option>
                                    ))}
                                  </select>
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  )}
                </Panel>
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- カレンダー連携の一行
function GcalLine({
  status,
  busy,
  onPull,
  onBackfill,
}: {
  status: GcalStatus | null;
  busy: boolean;
  onPull: () => void;
  onBackfill: () => void;
}) {
  if (!status) return null;
  if (!status.enabled) {
    return <span>Googleカレンダーとは連携していません（設定画面で準備できます）。</span>;
  }
  return (
    <span className="inline-flex flex-wrap items-center gap-x-1.5 gap-y-1">
      <span className="inline-block h-2 w-2 rounded-full bg-line" aria-hidden />
      <span>Googleカレンダーと連携中</span>
      <span>・最終取込 <span className="num">{status.last_pulled_at ? fmtDateTime(status.last_pulled_at) : "まだありません"}</span></span>
      {status.pull_error && (
        <span title={status.pull_error}>
          ・<span className="font-bold text-danger">取り込みでエラーがありました</span>
        </span>
      )}
      {status.unsynced > 0 && (
        <>
          <span>・<span className="font-bold text-hold">未反映 {status.unsynced}件</span></span>
          <Button size="sm" variant="ghost" onClick={onBackfill} disabled={busy}>まとめて反映</Button>
        </>
      )}
      <Button size="sm" variant="ghost" onClick={onPull} disabled={busy}>今すぐ取り込む</Button>
    </span>
  );
}

// ---------------------------------------------------------------- 画面本体
export default function Events() {
  const { toast } = useUi();
  const [params, setParams] = useSearchParams();
  const selId = params.get("id");
  const creating = params.get("new") === "1";

  const [events, setEvents] = useState<EventRow[] | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [trends, setTrends] = useState<Trend[]>([]);
  const [gcalStatus, setGcalStatus] = useState<GcalStatus | null>(null);
  const [gcalBusy, setGcalBusy] = useState(false);

  const [showPrevious, setShowPrevious] = useState(false);
  const [showPast, setShowPast] = useState(false);
  const [typeFilter, setTypeFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [q, setQ] = useState("");

  // 一覧の行に出す出欠（予定ごとに取得してためておく）
  const [briefs, setBriefs] = useState<Record<string, Summary>>({});
  const briefsRef = useRef<Record<string, Summary>>({});
  const [briefsVersion, setBriefsVersion] = useState(0);

  const reload = useCallback((previous: boolean) => {
    api<EventRow[]>(`/events${previous ? "?include_previous=true" : ""}`)
      .then((rows) => {
        setEvents(rows);
        setLoadErr(null);
      })
      .catch((e) => setLoadErr(friendlyError(e)));
    api<GcalStatus>("/gcal/status").then(setGcalStatus).catch(() => {});
    briefsRef.current = {};
    setBriefs({});
    setBriefsVersion((v) => v + 1);
  }, []);
  const reloadNow = useCallback(() => reload(showPrevious), [reload, showPrevious]);

  useEffect(() => {
    reload(false);
    api<Member[]>("/members").then(setMembers).catch(() => {});
    api<Policy[]>("/policies").then(setPolicies).catch(() => {});
    api<Trend[]>("/kpi/trends").then(setTrends).catch(() => {});
  }, [reload]);

  const names = useMemo(() => Object.fromEntries(members.map((m) => [m.member_id, m.name])), [members]);
  const gcalEnabled = !!gcalStatus?.enabled;
  const actions = useEventActions(reloadNow, gcalEnabled);

  // ---- URL で開く・閉じる
  const patchParams = (fn: (p: URLSearchParams) => void) =>
    setParams((p) => {
      const n = new URLSearchParams(p);
      fn(n);
      return n;
    });
  const openDetail = (id: string) => patchParams((p) => { p.set("id", id); p.delete("new"); });
  const closeDetail = () => patchParams((p) => p.delete("id"));
  const openCreate = () => patchParams((p) => { p.set("new", "1"); p.delete("id"); });
  const closeCreate = () => patchParams((p) => p.delete("new"));

  // ---- 絞り込み
  const today = todayKey();
  const filtered = useMemo(() => {
    const word = q.trim().toLowerCase();
    return (events ?? []).filter((e) => {
      if (typeFilter && e.type !== typeFilter) return false;
      if (statusFilter !== "all" && e.status !== statusFilter) return false;
      if (word && !`${calTitle(e)} ${e.location ?? ""}`.toLowerCase().includes(word)) return false;
      return true;
    });
  }, [events, typeFilter, statusFilter, q]);
  const upcoming = useMemo(
    () => filtered.filter((e) => dayKey(e.datetime_start) >= today).sort((a, b) => a.datetime_start.localeCompare(b.datetime_start)),
    [filtered, today]
  );
  const past = useMemo(
    () => filtered.filter((e) => dayKey(e.datetime_start) < today).sort((a, b) => b.datetime_start.localeCompare(a.datetime_start)),
    [filtered, today]
  );
  const filtering = !!(typeFilter || statusFilter !== "all" || q.trim());

  // ---- 表示中で出欠を集計できる予定の出欠を取る（4件ずつ）
  const needIds = useMemo(
    () =>
      [...upcoming, ...(showPast ? past : [])]
        .filter((e) => e.status === "open" || e.status === "closed")
        .map((e) => e.event_id),
    [upcoming, past, showPast]
  );
  const needKey = needIds.join(",");
  useEffect(() => {
    let stop = false;
    const todo = needIds.filter((id) => !(id in briefsRef.current));
    let i = 0;
    const worker = async () => {
      while (!stop && i < todo.length) {
        const id = todo[i++];
        try {
          const r = await api<{ summary: Summary }>(`/events/${id}`);
          if (stop) return;
          briefsRef.current = { ...briefsRef.current, [id]: r.summary };
          setBriefs(briefsRef.current);
        } catch {
          /* 一覧の出欠が出ないだけなので無視 */
        }
      }
    };
    Promise.all(Array.from({ length: Math.min(4, todo.length) }, worker));
    return () => {
      stop = true;
    };
  }, [needKey, briefsVersion]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- カレンダー
  async function pullNow() {
    setGcalBusy(true);
    try {
      const r = await api<{ created: number; updated: number; cancelled: number }>("/gcal/pull", { method: "POST" });
      toast(
        `カレンダーから取り込みました（新規 ${r.created}件・更新 ${r.updated}件・中止 ${r.cancelled}件）` +
          (r.created ? "。新しい予定は下書きです。出欠を受け付けるときは「受付を始める」を押してください" : "")
      );
      reloadNow();
    } catch (e) {
      toast(friendlyError(e), "error");
    } finally {
      setGcalBusy(false);
    }
  }
  async function backfill() {
    setGcalBusy(true);
    try {
      const r = await api<{ retried: number; synced: number; failed: number }>("/gcal/backfill", { method: "POST" });
      if (r.failed) toast(`カレンダーへ ${r.synced}件反映しました（${r.failed}件は反映できませんでした）`, "error");
      else toast(`カレンダーへ ${r.synced}件反映しました`);
      reloadNow();
    } catch (e) {
      toast(friendlyError(e), "error");
    } finally {
      setGcalBusy(false);
    }
  }

  // ---- 行のおもな操作（1つだけ）
  function rowAction(e: EventRow, isPast: boolean): ReactNode {
    if (e.status === "cancelled") return null;
    if (gcalIssue(e, gcalEnabled)) {
      return <Button size="sm" onClick={() => actions.resync(e)}>カレンダーに反映</Button>;
    }
    if (isPast) return null;
    if (e.status === "draft") {
      return <Button size="sm" onClick={() => actions.startAccepting(e)}>受付を始める</Button>;
    }
    const b = briefs[e.event_id];
    if (e.status === "open" && b && b.unanswered > 0) {
      const who = (b.unanswered_member_ids ?? []).map((id) => names[id] ?? "会員");
      return <Button size="sm" onClick={() => actions.remind(e, b.unanswered, who)}>催促する</Button>;
    }
    return null;
  }

  function renderRows(list: EventRow[], isPast: boolean) {
    return groupByMonth(list).map(([month, rows]) => (
      <tbody key={month}>
        <tr>
          <td colSpan={5} className="!border-t-0 !bg-white px-3 pb-1 pt-5 text-sm font-extrabold">
            {monthLabel(month)}
            <span className="ml-2 text-xs font-medium text-ink-sub">{rows.length}件</span>
          </td>
        </tr>
        {rows.map((e) => {
          const st = statusInfo(e.status);
          const b = briefs[e.event_id];
          const cancelled = e.status === "cancelled";
          const isToday = dayKey(e.datetime_start) === today;
          return (
            <tr key={e.event_id} className={cancelled || isPast ? "text-ink-sub" : ""}>
              <td className="whitespace-nowrap">
                <div className="num font-bold">
                  {fmtDateTime(e.datetime_start, false)}
                  {isToday && <span className="ml-1.5"><Badge tone="green">今日</Badge></span>}
                </div>
                <div className="num text-xs text-ink-sub">{timeOf(e.datetime_start)}</div>
              </td>
              <td className="min-w-[16rem]">
                <button
                  type="button"
                  className={`text-left font-bold hover:underline ${cancelled ? "text-ink-faint line-through" : ""}`}
                  onClick={() => openDetail(e.event_id)}
                >
                  {calTitle(e)}
                </button>
                <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-ink-sub">
                  {e.location && <span>{e.location}</span>}
                  {e.origin === "gcal" && <span>カレンダーから</span>}
                  {e.origin === "line" && <span>LINEから</span>}
                  <GcalIssueBadge issue={gcalIssue(e, gcalEnabled)} error={e.gcal_error} />
                </div>
              </td>
              <td className="whitespace-nowrap"><Badge tone={st.tone}>{st.label}</Badge></td>
              <td className="w-40">
                {b && b.total_targets > 0 ? (
                  <>
                    <div className="num mb-1 text-xs text-ink-sub">回答 {b.answered}/{b.total_targets}人</div>
                    <Progress value={b.answered} max={b.total_targets} />
                  </>
                ) : (e.status === "open" || e.status === "closed") && !b ? (
                  <span className="text-xs text-ink-faint">…</span>
                ) : (
                  <span className="text-xs text-ink-faint">—</span>
                )}
              </td>
              <td className="whitespace-nowrap text-right">
                <div className="inline-flex items-center gap-1">
                  {rowAction(e, isPast)}
                  <Button size="sm" variant="ghost" onClick={() => openDetail(e.event_id)}>詳細</Button>
                </div>
              </td>
            </tr>
          );
        })}
      </tbody>
    ));
  }

  const table = (list: EventRow[], isPast: boolean) => (
    <div className="overflow-x-auto">
      <table className="tbl min-w-[760px]">
        <thead>
          <tr>
            <th className="w-32">日時</th>
            <th>予定</th>
            <th className="w-28">状態</th>
            <th className="w-40">出欠</th>
            <th className="w-48" />
          </tr>
        </thead>
        {renderRows(list, isPast)}
      </table>
    </div>
  );

  return (
    <div>
      <PageHeader
        title="予定・出欠"
        description={<GcalLine status={gcalStatus} busy={gcalBusy} onPull={pullNow} onBackfill={backfill} />}
        actions={<Button variant="primary" onClick={openCreate}>予定を登録</Button>}
      />

      {loadErr && <ErrorNote>{loadErr}</ErrorNote>}

      {/* 絞り込み */}
      <section className="mb-4 rounded-card bg-white px-5 py-4">
        <div className="flex flex-wrap items-end gap-3">
          <Field label="種別">
            <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
              <option value="">すべて</option>
              {EVENT_TYPES.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </Field>
          <Field label="さがす" className="min-w-[12rem] flex-1">
            <input
              type="search"
              className="w-full"
              placeholder="予定の名前・場所で検索"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
          </Field>
        </div>
        <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
          <Segmented value={statusFilter} options={STATUS_FILTERS} onChange={setStatusFilter} />
          <label className="flex items-center gap-2 text-xs text-ink-sub">
            <input
              type="checkbox"
              checked={showPrevious}
              onChange={(e) => {
                setShowPrevious(e.target.checked);
                reload(e.target.checked);
              }}
            />
            運用を始める前（前年度以前）の予定も表示する
          </label>
        </div>
      </section>

      {/* 一覧 */}
      {events === null && !loadErr ? (
        <Loading />
      ) : events && events.length === 0 ? (
        <Panel>
          <EmptyState
            title="まだ予定がありません"
            action={
              <div className="flex flex-wrap justify-center gap-2">
                <Button onClick={openCreate}>予定を登録</Button>
                {gcalEnabled && <Button variant="ghost" onClick={pullNow} disabled={gcalBusy}>Googleカレンダーから取り込む</Button>}
              </div>
            }
          >
            予定を登録すると、会員が LINE から出欠を回答できるようになります。
          </EmptyState>
        </Panel>
      ) : events ? (
        <>
          <Panel title="これからの予定" action={<span className="num text-xs text-ink-sub">{upcoming.length}件</span>} flush>
            {upcoming.length === 0 ? (
              <div className="px-5 pb-3">
                {filtering ? (
                  <EmptyState
                    title="条件に合う予定はありません"
                    action={
                      <Button size="sm" onClick={() => { setTypeFilter(""); setStatusFilter("all"); setQ(""); }}>
                        条件をクリア
                      </Button>
                    }
                  />
                ) : (
                  <EmptyState title="これからの予定はありません" action={<Button size="sm" onClick={openCreate}>予定を登録</Button>}>
                    次の例会や理事会を登録しておくと、出欠の受付と催促ができます。
                  </EmptyState>
                )}
              </div>
            ) : (
              table(upcoming, false)
            )}
          </Panel>

          {past.length > 0 && (
            <Panel
              title="終わった予定"
              action={
                <Button size="sm" variant="ghost" onClick={() => setShowPast((v) => !v)}>
                  {showPast ? "隠す" : `表示する（${past.length}件）`}
                </Button>
              }
              flush={showPast}
            >
              {showPast ? table(past, true) : (
                <p className="text-sm text-ink-sub">新しい順に表示します。出欠の記録や事前共有の資料は各予定の「詳細」から見られます。</p>
              )}
            </Panel>
          )}
        </>
      ) : null}

      {trends.length > 0 && (
        <Panel title="出席率の推移">
          <div style={{ width: "100%", height: 220 }}>
            <ResponsiveContainer>
              <LineChart data={trends.map((t) => ({ ...t, pct: Math.round(t.attendance_rate * 100) }))}>
                <CartesianGrid strokeDasharray="3 3" stroke="#E5E7EB" />
                <XAxis dataKey="title" tick={{ fontSize: 11, fill: "#6B7280" }} />
                <YAxis domain={[0, 100]} unit="%" tick={{ fontSize: 11, fill: "#6B7280" }} width={44} />
                <Tooltip formatter={(v) => [`${v}%`, "出席率"]} />
                <Line type="monotone" dataKey="pct" name="出席率" stroke="#06C755" strokeWidth={2} dot={{ r: 3 }} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </Panel>
      )}

      {creating && (
        <CreateDialog
          members={members}
          policies={policies}
          onClose={closeCreate}
          onCreated={(title) => {
            toast(`「${title}」を登録しました`);
            closeCreate();
            reloadNow();
          }}
        />
      )}

      {selId && (
        <DetailDrawer
          eventId={selId}
          members={members}
          names={names}
          policies={policies}
          gcalEnabled={gcalEnabled}
          onClose={closeDetail}
          onChanged={reloadNow}
        />
      )}
    </div>
  );
}
