import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
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

// 日程調整（docs/schedule-poll-design.md・画面は docs/admin-ui-design.md）

type Answer = "ok" | "maybe" | "ng";
type Candidate = { candidate_id: string; start: string; end: string | null; label: string };
type Poll = {
  poll_id: string;
  title: string;
  event_type: string;
  fiscal_year: number | null;
  location: string | null;
  audience: string | null;
  group_id: string;
  deadline: string | null;
  status: "open" | "closed" | "decided";
  decided_candidate_id: string | null;
  event_id: string | null;
  announced_at: string | null;
  reminded_at: string | null;
  created_at: string | null;
};
type Row = {
  member_id: string | null;
  name: string;
  display_name: string | null;
  answers: Record<string, Answer> | null;
  comment: string | null;
};
type Summary = {
  poll: Poll;
  candidates: Candidate[];
  rows: Row[];
  others: Row[];
  counts: Record<string, Record<Answer, number>>;
  pending: string[];
  liff_url: string | null;
};
type ListItem = Poll & { pending: number };
type Member = {
  member_id: string;
  name: string;
  status: string;
  officer_role: string | null;
  committees: { committee: string; role: string | null }[];
};
type Group = { group_id: string; group_name: string | null; joined_at?: string | null; left_at?: string | null };
type Brief = { answered: number; targets: number; leading: string | null };

const MARK: Record<Answer, string> = { ok: "○", maybe: "△", ng: "×" };
const MARK_CLASS: Record<Answer, string> = {
  ok: "bg-line-soft text-line-ink",
  maybe: "bg-hold-soft text-hold",
  ng: "bg-gray-100 text-ink-faint",
};
const STATUS: Record<Poll["status"], { label: string; tone: "green" | "gray" | "blue" }> = {
  open: { label: "受付中", tone: "green" },
  closed: { label: "締切", tone: "gray" },
  decided: { label: "決定", tone: "blue" },
};
const TYPES = ["五役会", "理事会", "委員会", "例会", "総会", "行事", "対外", "個別"];
const GOYAKU_ROLES = ["理事長", "副理事長", "専務理事", "監事"];

type Target = "goyaku" | "riji" | "all";
const TARGETS: { value: Target; label: string; hint: string; audience: string; group: string }[] = [
  { value: "goyaku", label: "五役", hint: "理事長・副理事長・専務理事・監事", audience: "五役の皆さま", group: "2027年度　五役" },
  { value: "riji", label: "理事", hint: "五役＋委員長・副委員長", audience: "理事の皆さま", group: "2027 猪苗代青年会議所 理事会" },
  { value: "all", label: "会員全員", hint: "在籍している会員すべて", audience: "会員の皆さま", group: "【JCI猪苗代】" },
];

// ---------------------------------------------------------------- 対象者・グループ
function isGoyaku(m: Member): boolean {
  return GOYAKU_ROLES.includes(m.officer_role ?? "");
}
function isRiji(m: Member): boolean {
  return isGoyaku(m) || m.committees.some((c) => c.role === "委員長" || c.role === "副委員長");
}
/** 対象者のプレビュー（サーバ側の対象の決め方と同じ） */
function targetMembers(members: Member[], t: Target): Member[] {
  const active = members.filter((m) => m.status === "active");
  if (t === "goyaku") return active.filter(isGoyaku);
  if (t === "riji") return active.filter(isRiji);
  return active;
}

const norm = (s: string) => s.replace(/[\s　]/g, "");
/** 名前でグループを探す（完全一致 → 空白を無視して一致 → 含む） */
function findGroup(groups: Group[], name: string): string {
  const n = norm(name);
  const hit =
    groups.find((g) => g.group_name === name) ??
    groups.find((g) => g.group_name && norm(g.group_name) === n) ??
    groups.find((g) => g.group_name && norm(g.group_name).includes(n));
  return hit?.group_id ?? "";
}
function isActiveGroup(g: Group): boolean {
  if (!g.left_at) return true;
  return !!g.joined_at && new Date(g.joined_at) > new Date(g.left_at);
}
const groupLabel = (g: Group | undefined) => g?.group_name ?? "（名前を取得できていないグループ）";

// ---------------------------------------------------------------- 集計
/** いちばん集まりやすい候補: ○が多い → ×が少ない → △が多い → 日付が早い（サーバーの home と同じ順）。 */
function leadingId(s: Summary): string | null {
  const score = (c: Candidate) => {
    const n = s.counts[c.candidate_id] ?? { ok: 0, maybe: 0, ng: 0 };
    return [n.ok, -n.ng, n.maybe];
  };
  let best: Candidate | null = null;
  for (const c of s.candidates) {
    if ((s.counts[c.candidate_id]?.ok ?? 0) === 0) continue;
    if (!best) { best = c; continue; }
    const [a, b] = [score(c), score(best)];
    const cmp = a[0] - b[0] || a[1] - b[1] || a[2] - b[2] || (c.start < best.start ? 1 : -1);
    if (cmp > 0) best = c;
  }
  return best?.candidate_id ?? null;
}
function toBrief(s: Summary): Brief {
  const lead = leadingId(s);
  return {
    targets: s.rows.length,
    answered: s.rows.length - s.pending.length,
    leading: s.candidates.find((c) => c.candidate_id === lead)?.label ?? null,
  };
}

// ---------------------------------------------------------------- 日付
const pad = (n: number) => String(n).padStart(2, "0");
function localInput(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function daysLater(days: number, hour = 21): string {
  const d = new Date();
  d.setDate(d.getDate() + days);
  d.setHours(hour, 0, 0, 0);
  return localInput(d);
}

// ---------------------------------------------------------------- 小さな部品
function CopyLink({ label, url, note }: { label: string; url: string; note?: ReactNode }) {
  const { toast } = useUi();
  async function copy() {
    try {
      await navigator.clipboard.writeText(url);
      toast("リンクをコピーしました");
    } catch {
      toast("コピーできませんでした。「開く」から開いてアドレスをコピーしてください", "error");
    }
  }
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-2xl bg-gray-50 px-4 py-3">
      <div className="min-w-0 flex-1">
        <div className="text-sm font-bold">{label}</div>
        {note && <div className="text-xs text-ink-sub">{note}</div>}
      </div>
      <div className="flex gap-1.5">
        <Button size="sm" onClick={copy}>リンクをコピー</Button>
        <a href={url} target="_blank" rel="noreferrer">
          <Button size="sm" variant="ghost">開く</Button>
        </a>
      </div>
    </div>
  );
}

function Step({ no, title, children, hint }: { no: number; title: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex gap-3 border-t border-gray-100 py-5 first:border-t-0 first:pt-1">
      <div className="num flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-line text-sm font-extrabold text-white">
        {no}
      </div>
      <div className="min-w-0 flex-1">
        <div className="font-extrabold">{title}</div>
        {hint && <div className="mt-0.5 text-xs text-ink-sub">{hint}</div>}
        <div className="mt-3">{children}</div>
      </div>
    </div>
  );
}

function Mark({ a }: { a: Answer | undefined }) {
  if (!a) return <span className="text-ink-faint">—</span>;
  return (
    <span className={`inline-flex h-7 w-7 items-center justify-center rounded-full text-sm font-extrabold ${MARK_CLASS[a]}`}>
      {MARK[a]}
    </span>
  );
}

/** 「11/5(木) 19:00〜」を日付と時刻の2行に */
function CandLabel({ label }: { label: string }) {
  const i = label.indexOf(" ");
  if (i < 0) return <>{label}</>;
  return (
    <span className="num leading-tight">
      <span className="block font-bold text-ink">{label.slice(0, i)}</span>
      <span className="block">{label.slice(i + 1)}</span>
    </span>
  );
}

// ---------------------------------------------------------------- 作成フォーム
type Cand = { date: string; start: string; end: string };
type Draft = {
  title: string;
  event_type: string;
  fiscal_year: string;
  location: string;
  audience: string;
  target: Target;
  group_id: string;
  deadline: string;
  candidates: Cand[];
};
const newCand = (start = "19:00"): Cand => ({ date: "", start, end: "" });
const EMPTY: Draft = {
  title: "",
  event_type: "五役会",
  fiscal_year: "",
  location: "JC会館 or オンライン",
  audience: "五役の皆さま",
  target: "goyaku",
  group_id: "",
  deadline: "",
  candidates: [newCand(), newCand(), newCand()],
};

function CreateForm({
  members,
  groups,
  onCancel,
  onCreated,
}: {
  members: Member[];
  groups: Group[];
  onCancel: () => void;
  onCreated: (s: Summary) => void;
}) {
  const { toast } = useUi();
  const [d, setD] = useState<Draft>(EMPTY);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  // グループ一覧が届いたら、対象者に合わせた案内先を選んでおく
  useEffect(() => {
    if (!d.group_id && groups.length) {
      const preset = TARGETS.find((t) => t.value === d.target)!;
      const id = findGroup(groups, preset.group);
      if (id) setD((x) => ({ ...x, group_id: id }));
    }
  }, [groups]); // eslint-disable-line react-hooks/exhaustive-deps

  const preview = useMemo(() => targetMembers(members, d.target), [members, d.target]);
  const filled = d.candidates.filter((c) => c.date);
  const group = groups.find((g) => g.group_id === d.group_id);

  function setTarget(t: Target) {
    const preset = TARGETS.find((x) => x.value === t)!;
    const id = findGroup(groups, preset.group);
    setD((x) => ({
      ...x,
      target: t,
      audience: preset.audience,
      group_id: id || x.group_id,
      event_type:
        x.event_type === "五役会" || x.event_type === "理事会"
          ? t === "goyaku" ? "五役会" : t === "riji" ? "理事会" : x.event_type
          : x.event_type,
    }));
  }
  function setCand(i: number, patch: Partial<Cand>) {
    setD((x) => ({ ...x, candidates: x.candidates.map((c, j) => (j === i ? { ...c, ...patch } : c)) }));
  }

  async function create() {
    setProblem(null);
    const cands = d.candidates.filter((c) => c.date);
    if (!d.title.trim()) return setProblem("1. 会議の名前を入力してください。");
    if (cands.length === 0) return setProblem("2. 候補日時を1つ以上入力してください。");
    if (cands.some((c) => !c.start)) return setProblem("2. 候補日時の開始時刻を入力してください。");
    if (cands.some((c) => c.end && c.end <= c.start)) return setProblem("2. 終了時刻は開始時刻より後にしてください。");
    const keys = cands.map((c) => `${c.date}T${c.start}`);
    if (new Set(keys).size !== keys.length) return setProblem("2. 同じ候補日時が重なっています。");
    if (!d.group_id) return setProblem("3. 案内先の LINE グループを選んでください。");
    if (d.target === "riji" && preview.length === 0) return setProblem("3. 理事にあたる会員が見つかりません。");

    const target_scope =
      d.target === "goyaku"
        ? { kind: "officer", value: GOYAKU_ROLES }
        : d.target === "riji"
          ? { kind: "custom", value: preview.map((m) => m.member_id) }
          : { kind: "all", value: [] };
    setBusy(true);
    try {
      const res = await api<Summary>("/polls", {
        method: "POST",
        body: JSON.stringify({
          title: d.title.trim(),
          event_type: d.event_type,
          fiscal_year: d.fiscal_year ? Number(d.fiscal_year) : null,
          location: d.location.trim() || null,
          audience: d.audience.trim() || null,
          candidates: cands.map((c) => ({
            start: `${c.date}T${c.start}`,
            end: c.end ? `${c.date}T${c.end}` : null,
          })),
          target_scope,
          group_id: d.group_id,
          deadline: d.deadline || null,
        }),
      });
      toast("作成しました。内容を確かめて「グループに案内する」を押してください");
      onCreated(res);
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  }

  const firstCand = filled.map((c) => `${c.date}T${c.start}`).sort()[0];
  const deadlineLate = d.deadline && firstCand && d.deadline >= firstCand;

  return (
    <Panel title="日程調整を作る" action={<Button size="sm" variant="ghost" onClick={onCancel}>やめる</Button>}>
      <p className="mb-2 text-sm text-ink-sub">作っただけでは LINE に送りません。作ったあとで内容を確かめてから案内できます。</p>

      <Step no={1} title="会議の内容">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="会議の名前" className="sm:col-span-2">
            <input className="w-full" placeholder="例: 第2回五役予定者会議" value={d.title}
              onChange={(e) => setD({ ...d, title: e.target.value })} />
          </Field>
          <Field label="種類" hint="日程が決まったとき、この種類で予定に登録します">
            <select className="w-full" value={d.event_type} onChange={(e) => setD({ ...d, event_type: e.target.value })}>
              {TYPES.map((t) => <option key={t}>{t}</option>)}
            </select>
          </Field>
          <Field label="場所">
            <input className="w-full" value={d.location} onChange={(e) => setD({ ...d, location: e.target.value })} />
          </Field>
          <Field label="年度（翌年度の会議のときだけ）" hint="空欄なら今の年度です">
            <input className="w-full" type="number" placeholder="例: 2027" value={d.fiscal_year}
              onChange={(e) => setD({ ...d, fiscal_year: e.target.value })} />
          </Field>
        </div>
      </Step>

      <Step no={2} title="候補日時" hint="時刻は 19:00 にしてあります。終了時刻は分かるときだけ入れてください。">
        <div className="space-y-2">
          {d.candidates.map((c, i) => (
            <div key={i} className="flex flex-wrap items-center gap-2">
              <span className="num w-6 text-right text-sm font-bold text-ink-sub">{i + 1}</span>
              <input type="date" aria-label={`候補${i + 1}の日付`} value={c.date}
                onChange={(e) => setCand(i, { date: e.target.value })} />
              <input type="time" aria-label={`候補${i + 1}の開始時刻`} className="w-28" value={c.start}
                onChange={(e) => setCand(i, { start: e.target.value })} />
              <span className="text-sm text-ink-sub">〜</span>
              <input type="time" aria-label={`候補${i + 1}の終了時刻`} className="w-28" value={c.end}
                onChange={(e) => setCand(i, { end: e.target.value })} />
              <Button size="sm" variant="danger" disabled={d.candidates.length <= 1}
                onClick={() => setD({ ...d, candidates: d.candidates.filter((_, j) => j !== i) })}>
                削除
              </Button>
            </div>
          ))}
        </div>
        <Button size="sm" variant="ghost" className="mt-2" disabled={d.candidates.length >= 10}
          onClick={() => setD({ ...d, candidates: [...d.candidates, newCand(d.candidates[d.candidates.length - 1]?.start || "19:00")] })}>
          ＋ 候補を追加
        </Button>
        <span className="ml-2 text-xs text-ink-faint">最大10件</span>
      </Step>

      <Step no={3} title="対象者と案内先">
        <Segmented value={d.target} onChange={setTarget}
          options={TARGETS.map((t) => ({ value: t.value, label: t.label }))} />
        <div className="mt-2 text-xs text-ink-sub">{TARGETS.find((t) => t.value === d.target)!.hint}</div>
        <div className="mt-3 rounded-2xl bg-gray-50 px-4 py-3 text-sm">
          <span className="font-bold">対象 <span className="num">{preview.length}</span> 人</span>
          <span className="ml-2 text-ink-sub">{preview.map((m) => m.name).join("・") || "（該当する会員がいません）"}</span>
        </div>
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          <Field label="案内を送る LINE グループ" hint="対象者に合わせて選んであります。違うときは選び直してください。">
            <select className="w-full" value={d.group_id} onChange={(e) => setD({ ...d, group_id: e.target.value })}>
              <option value="">選んでください</option>
              {groups.map((g) => <option key={g.group_id} value={g.group_id}>{groupLabel(g)}</option>)}
            </select>
          </Field>
          <Field label="宛名（案内の最初に出ます）">
            <input className="w-full" value={d.audience} onChange={(e) => setD({ ...d, audience: e.target.value })} />
          </Field>
        </div>
      </Step>

      <Step no={4} title="回答の締切" hint="案内と催促のメッセージに表示します。無くてもかまいません。">
        <div className="flex flex-wrap items-center gap-2">
          <input type="datetime-local" aria-label="回答の締切" value={d.deadline}
            onChange={(e) => setD({ ...d, deadline: e.target.value })} />
          <Button size="sm" onClick={() => setD({ ...d, deadline: daysLater(3) })}>3日後の21時</Button>
          <Button size="sm" onClick={() => setD({ ...d, deadline: daysLater(7) })}>1週間後の21時</Button>
          {d.deadline && <Button size="sm" variant="ghost" onClick={() => setD({ ...d, deadline: "" })}>締切なし</Button>}
        </div>
        {deadlineLate && <div className="mt-2 text-xs text-hold">締切が最初の候補日より後になっています。</div>}
      </Step>

      {problem && <ErrorNote>{problem}</ErrorNote>}
      <div className="flex flex-wrap items-center justify-end gap-3 border-t border-gray-100 pt-4">
        <span className="text-xs text-ink-sub">
          {filled.length} 件の候補・対象 {preview.length} 人・案内先 {group ? `「${groupLabel(group)}」` : "未選択"}
        </span>
        <Button onClick={onCancel}>やめる</Button>
        <Button variant="primary" disabled={busy} onClick={create}>作成する（まだ送りません）</Button>
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------- 詳細
function PollDetail({
  s,
  groupName,
  onChanged,
  onClose,
}: {
  s: Summary;
  groupName: string;
  onChanged: () => Promise<void>;
  onClose: () => void;
}) {
  const { confirm, toast } = useUi();
  const [busy, setBusy] = useState(false);
  const [notify, setNotify] = useState(true);
  const p = s.poll;
  const lead = leadingId(s);
  const answered = s.rows.length - s.pending.length;
  const allRows = [...s.rows, ...s.others];
  const canSend = !!s.liff_url;
  const where = <b className="text-ink">「{groupName}」</b>;

  async function run(path: string, body: unknown, done: (res: unknown) => string) {
    setBusy(true);
    try {
      const res = await api(`/polls/${p.poll_id}/${path}`, {
        method: "POST",
        body: body ? JSON.stringify(body) : undefined,
      });
      toast(done(res));
      await onChanged();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function announce() {
    const ok = await confirm({
      title: p.announced_at ? "もう一度案内を送りますか？" : "グループに案内を送りますか？",
      body: (
        <>
          送り先: {where}
          <br />
          対象 {s.rows.length} 人（{p.audience ?? "宛名なし"}）・候補 {s.candidates.length} 件
          {p.deadline && <>・締切 {fmtDateTime(p.deadline)}</>}
          <br />
          LINE への送信は取り消せません。送り先のグループ名をよく確かめてください。
        </>
      ),
      confirmLabel: `「${groupName}」に送る`,
    });
    if (ok) run("announce", undefined, () => `「${groupName}」に案内を送りました`);
  }
  async function remind() {
    const ok = await confirm({
      title: `未回答の ${s.pending.length} 人に催促しますか？`,
      body: (
        <>
          送り先: {where}
          <br />
          グループの中で、次の方に名前を挙げて回答をお願いします: {s.pending.join("・")}
          <br />
          LINE への送信は取り消せません。
        </>
      ),
      confirmLabel: `「${groupName}」に送る`,
    });
    if (ok)
      run("remind", undefined, (r) => {
        const n = (r as { reminded?: string[] }).reminded?.length ?? s.pending.length;
        return `${n} 人に催促しました`;
      });
  }
  async function close() {
    const ok = await confirm({
      title: "回答の受付を締め切りますか？",
      body: "LINE には何も送りません。日程はまだ決まりません（締め切ったあとでも「この日に決定」できます）。",
      confirmLabel: "締め切る",
    });
    if (ok) run("close", undefined, () => "締め切りました");
  }
  async function decide(c: Candidate) {
    const n = s.counts[c.candidate_id] ?? { ok: 0, maybe: 0, ng: 0 };
    const ok = await confirm({
      title: `${c.label} に決定しますか？`,
      body: (
        <>
          ○{n.ok}・△{n.maybe}・×{n.ng}
          <br />
          「予定・出欠」に下書きで登録し、カレンダーにも入れます。
          <br />
          {notify ? (
            <>決定のお知らせを {where}（対象 {s.rows.length} 人）に送ります。LINE への送信は取り消せません。</>
          ) : (
            <>LINE には送りません。</>
          )}
        </>
      ),
      confirmLabel: notify ? `決定して「${groupName}」に送る` : "決定する",
    });
    if (ok) run("decide", { candidate_id: c.candidate_id, notify }, () => `${c.label} に決定しました`);
  }

  const decided = s.candidates.find((c) => c.candidate_id === p.decided_candidate_id);

  return (
    <div>
      <Panel>
        <div className="flex flex-wrap items-start justify-between gap-2 pt-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-xl font-extrabold">{p.title}</h2>
              <Badge tone={STATUS[p.status].tone}>{STATUS[p.status].label}</Badge>
              {p.status === "open" && !p.announced_at && <Badge tone="amber">まだ案内していません</Badge>}
            </div>
            <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
              <dt className="text-ink-sub">案内先</dt>
              <dd className="font-bold">{groupName}<span className="ml-2 font-normal text-ink-sub">{p.audience}</span></dd>
              <dt className="text-ink-sub">種類・場所</dt>
              <dd>{p.event_type}{p.fiscal_year ? `（${p.fiscal_year}年度）` : ""}・{p.location ?? "未定"}</dd>
              <dt className="text-ink-sub">締切</dt>
              <dd className="num">{p.deadline ? fmtDateTime(p.deadline) : "なし"}</dd>
              <dt className="text-ink-sub">送った記録</dt>
              <dd className="num">
                {p.announced_at ? `案内 ${fmtDateTime(p.announced_at)}` : "案内はまだ"}
                {p.reminded_at && `・催促 ${fmtDateTime(p.reminded_at)}`}
              </dd>
            </dl>
          </div>
          <Button size="sm" variant="ghost" onClick={onClose}>閉じる</Button>
        </div>

        {p.status === "open" && (
          <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-gray-100 pt-4">
            {!p.announced_at ? (
              <Button variant="primary" disabled={busy || !canSend} onClick={announce}>グループに案内する</Button>
            ) : (
              <>
                <Button variant="primary" disabled={busy || !canSend || s.pending.length === 0} onClick={remind}>
                  未回答者に催促する{s.pending.length ? `（${s.pending.length}人）` : ""}
                </Button>
                <Button disabled={busy || !canSend} onClick={announce}>もう一度案内する</Button>
              </>
            )}
            <Button variant="ghost" disabled={busy} onClick={close}>締め切る</Button>
          </div>
        )}
        {p.status === "open" && !canSend && (
          <div className="mt-3 rounded-2xl bg-danger-soft px-4 py-3 text-sm text-danger">
            LINE で回答する画面の準備ができていないため、まだ案内できません。設定の担当者に確認してください。
          </div>
        )}
        {decided && (
          <div className="mt-4 flex flex-wrap items-center gap-3 rounded-2xl bg-line-soft px-4 py-3 text-sm text-line-ink">
            <span className="font-bold">{decided.label} に決定しました。</span>
            <span>「予定・出欠」に下書きで登録してあります。</span>
            {p.event_id && (
              <Link to={`/events?id=${p.event_id}`} className="font-bold underline">予定・出欠で開く</Link>
            )}
          </div>
        )}
        {s.liff_url && (
          <div className="mt-4">
            <CopyLink label="回答画面のリンク" url={s.liff_url}
              note="LINE の中で開くと回答できます。案内を見落とした方へ個別に送るときに使えます。" />
          </div>
        )}
      </Panel>

      <Panel title="回答の状況" flush>
        <div className="grid gap-3 px-5 pb-4 sm:grid-cols-3">
          <div className="rounded-2xl bg-gray-50 px-4 py-3">
            <div className="text-xs text-ink-sub">回答</div>
            <div className="num text-2xl font-extrabold">{answered}<span className="text-sm font-bold">/{s.rows.length}人</span></div>
            <div className="mt-1"><Progress value={answered} max={s.rows.length} /></div>
          </div>
          <div className="rounded-2xl bg-gray-50 px-4 py-3 sm:col-span-2">
            <div className="text-xs text-ink-sub">いちばん○が多い候補</div>
            <div className="mt-1 font-extrabold">
              {lead ? s.candidates.find((c) => c.candidate_id === lead)?.label : "まだ回答がありません"}
              {lead && <span className="num ml-2 text-sm font-bold text-line-ink">○{s.counts[lead]?.ok}</span>}
            </div>
            <div className="mt-1 text-xs text-ink-sub">
              未回答: {s.pending.length ? s.pending.join("・") : "なし（全員回答済み）"}
            </div>
          </div>
        </div>

        {allRows.length === 0 ? (
          <div className="px-5 pb-3"><EmptyState title="対象の会員がいません" /></div>
        ) : (
          <div className="overflow-x-auto">
            <table className="tbl">
              <thead>
                <tr>
                  <th className="sticky left-0 z-10 bg-white">名前</th>
                  {s.candidates.map((c) => (
                    <th key={c.candidate_id}
                      className={`text-center ${c.candidate_id === p.decided_candidate_id ? "bg-info-soft" : c.candidate_id === lead ? "bg-line-soft" : ""}`}>
                      <CandLabel label={c.label} />
                    </th>
                  ))}
                  <th>コメント</th>
                </tr>
              </thead>
              <tbody>
                {allRows.map((r, i) => {
                  const other = i >= s.rows.length;
                  return (
                    <tr key={i}>
                      <td className="sticky left-0 z-10 whitespace-nowrap bg-white">
                        <div className="font-bold">{r.name}</div>
                        {other && <div className="text-xs text-hold">対象外の方の回答</div>}
                        {!other && !r.answers && <div className="text-xs text-ink-faint">未回答</div>}
                        {r.display_name && r.member_id && <div className="text-xs text-ink-faint">LINE名: {r.display_name}</div>}
                      </td>
                      {s.candidates.map((c) => (
                        <td key={c.candidate_id} className="text-center">
                          <Mark a={r.answers?.[c.candidate_id]} />
                        </td>
                      ))}
                      <td className="min-w-[10rem] text-ink-sub">{r.comment}</td>
                    </tr>
                  );
                })}
                <tr>
                  <td className="sticky left-0 z-10 bg-white font-extrabold">集計</td>
                  {s.candidates.map((c) => {
                    const n = s.counts[c.candidate_id] ?? { ok: 0, maybe: 0, ng: 0 };
                    return (
                      <td key={c.candidate_id} className="num whitespace-nowrap text-center">
                        <div className="font-extrabold text-line-ink">○{n.ok}</div>
                        <div className="text-xs text-ink-sub">△{n.maybe} ×{n.ng}</div>
                      </td>
                    );
                  })}
                  <td />
                </tr>
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {p.status !== "decided" && (
        <Panel title="日程を決める">
          <p className="mb-3 text-sm text-ink-sub">
            決めた日を「予定・出欠」に下書きで登録し、カレンダーにも入れます。
          </p>
          <label className="mb-3 flex items-center gap-2">
            <input type="checkbox" checked={notify} onChange={(e) => setNotify(e.target.checked)} />
            <span>決定のお知らせを「{groupName}」に送る</span>
          </label>
          <ul className="divide-y divide-gray-100">
            {s.candidates.map((c) => {
              const n = s.counts[c.candidate_id] ?? { ok: 0, maybe: 0, ng: 0 };
              return (
                <li key={c.candidate_id} className="flex flex-wrap items-center gap-3 py-2.5">
                  <span className="num min-w-[9rem] font-bold">{c.label}</span>
                  <span className="num text-sm text-ink-sub">○{n.ok}・△{n.maybe}・×{n.ng}</span>
                  {c.candidate_id === lead && <Badge tone="green">いちばん多い</Badge>}
                  <Button size="sm" className="ml-auto" disabled={busy} onClick={() => decide(c)}>この日に決定</Button>
                </li>
              );
            })}
          </ul>
        </Panel>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 画面
type Filter = "open" | "done" | "all";

export default function Polls() {
  const [params, setParams] = useSearchParams();
  const selectedId = params.get("id");
  const creating = params.get("new") === "1";

  const [list, setList] = useState<ListItem[] | null>(null);
  const [briefs, setBriefs] = useState<Record<string, Brief>>({});
  const [selected, setSelected] = useState<Summary | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [groups, setGroups] = useState<Group[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("open");

  const load = useCallback(async () => {
    try {
      const items = await api<ListItem[]>("/polls");
      setList(items);
      // 一覧の「回答 n/m」「いちばん多い候補」は個別の集計から作る
      const entries = await Promise.all(
        items.map((p) =>
          api<Summary>(`/polls/${p.poll_id}`)
            .then((s) => [p.poll_id, toBrief(s)] as const)
            .catch(() => null)
        )
      );
      setBriefs(Object.fromEntries(entries.filter((e): e is readonly [string, Brief] => e !== null)));
    } catch (e) {
      setErr((e as Error).message);
    }
  }, []);

  const loadSelected = useCallback(async (id: string) => {
    try {
      const s = await api<Summary>(`/polls/${id}`);
      setSelected(s);
      setBriefs((b) => ({ ...b, [id]: toBrief(s) }));
    } catch (e) {
      setSelected(null);
      setErr((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
    api<Member[]>("/members").then(setMembers).catch(() => {});
    api<Group[]>("/line/groups").then((gs) => setGroups(gs.filter(isActiveGroup))).catch(() => {});
  }, [load]);

  useEffect(() => {
    if (selectedId) loadSelected(selectedId);
    else setSelected(null);
  }, [selectedId, loadSelected]);

  const groupName = useMemo(() => {
    const map = new Map(groups.map((g) => [g.group_id, groupLabel(g)]));
    return (id: string) => map.get(id) ?? "（参加していないグループ）";
  }, [groups]);

  const openPoll = (id: string) => setParams({ id });
  const closeDetail = () => setParams({});

  if (err && !list) return <ErrorNote>{err}</ErrorNote>;

  const sorted = [...(list ?? [])].sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""));
  const openCount = sorted.filter((p) => p.status === "open").length;
  const shown = sorted.filter((p) =>
    filter === "all" ? true : filter === "open" ? p.status === "open" : p.status !== "open"
  );
  const showDetail = creating || !!selectedId;

  return (
    <div>
      <PageHeader
        title="日程調整"
        description="候補日を LINE で聞いて、回答を見ながら日程を決めます。"
        actions={!creating && <Button onClick={() => setParams({ new: "1" })}>＋ 日程調整を作る</Button>}
      />
      {err && list && <ErrorNote>{err}</ErrorNote>}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,340px)_minmax(0,1fr)]">
        <div className={showDetail ? "hidden lg:block" : ""}>
          <Panel flush>
            <div className="px-5 pb-3 pt-4">
              <Segmented value={filter} onChange={setFilter}
                options={[
                  { value: "open", label: `受付中 ${openCount}` },
                  { value: "done", label: "締切・決定" },
                  { value: "all", label: "すべて" },
                ]} />
            </div>
            {!list ? (
              <Loading />
            ) : shown.length === 0 ? (
              <div className="px-5 pb-3">
                <EmptyState title={filter === "open" ? "受付中の日程調整はありません" : "まだありません"}
                  action={<Button size="sm" onClick={() => setParams({ new: "1" })}>日程調整を作る</Button>} />
              </div>
            ) : (
              <ul className="space-y-2 px-3 pb-3">
                {shown.map((p) => {
                  const b = briefs[p.poll_id];
                  const active = p.poll_id === selectedId;
                  return (
                    <li key={p.poll_id}>
                      <button type="button" onClick={() => openPoll(p.poll_id)}
                        className={`block w-full rounded-2xl px-4 py-3 text-left transition ${
                          active ? "bg-line-soft ring-2 ring-line/40" : "bg-gray-50 hover:bg-gray-100"
                        }`}>
                        <div className="flex items-start justify-between gap-2">
                          <div className="font-bold">{p.title}</div>
                          <Badge tone={STATUS[p.status].tone}>{STATUS[p.status].label}</Badge>
                        </div>
                        {b && (
                          <div className="num mt-2 flex items-center gap-3 text-xs text-ink-sub">
                            <span className="whitespace-nowrap">回答 {b.answered}/{b.targets}人</span>
                            <Progress value={b.answered} max={b.targets} />
                          </div>
                        )}
                        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-sub">
                          {p.status === "open" && p.deadline && <span className="num">締切 {fmtDateTime(p.deadline)}</span>}
                          {p.status === "open" &&
                            (p.announced_at ? <span className="num">案内 {fmtDateTime(p.announced_at, false)}</span> : <Badge tone="amber">未案内</Badge>)}
                          {b?.leading && p.status !== "decided" && <span>いちばん多い: {b.leading}</span>}
                        </div>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </Panel>
        </div>

        <div className="min-w-0">
          {showDetail && (
            <Button size="sm" variant="ghost" className="mb-2 lg:hidden" onClick={closeDetail}>← 一覧に戻る</Button>
          )}
          {creating ? (
            <CreateForm members={members} groups={groups} onCancel={closeDetail}
              onCreated={(s) => {
                setSelected(s);
                setParams({ id: s.poll.poll_id });
                load();
              }} />
          ) : selectedId ? (
            selected && selected.poll.poll_id === selectedId ? (
              <PollDetail s={selected} groupName={groupName(selected.poll.group_id)} onClose={closeDetail}
                onChanged={async () => {
                  await loadSelected(selectedId);
                  await load();
                }} />
            ) : (
              <Panel><Loading /></Panel>
            )
          ) : (
            <Panel className="hidden lg:block">
              <EmptyState title="左の一覧から日程調整を選んでください">
                回答の表、案内・催促・決定はここに出ます。
              </EmptyState>
            </Panel>
          )}
        </div>
      </div>
    </div>
  );
}
