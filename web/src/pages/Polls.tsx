import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { Card } from "../components/Card";

// 日程調整（docs/schedule-poll-design.md）

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
type Group = { group_id: string; group_name: string | null };

const MARK: Record<Answer, string> = { ok: "○", maybe: "△", ng: "×" };
const MARK_CLASS: Record<Answer, string> = {
  ok: "text-green-600 font-bold",
  maybe: "text-amber-600 font-bold",
  ng: "text-slate-400",
};
const STATUS: Record<Poll["status"], string> = { open: "受付中", closed: "締切", decided: "決定" };
const TYPES = ["五役会", "理事会", "委員会", "例会", "総会", "行事", "対外", "個別"];
const GOYAKU_ROLES = ["理事長", "副理事長", "専務理事", "監事"];

/** 理事 = 五役（監事含む）＋委員長・副委員長（2027_理事者出欠管理と同じ範囲）。 */
function rijiIds(members: Member[]): string[] {
  return members
    .filter((m) => m.status === "active")
    .filter(
      (m) =>
        GOYAKU_ROLES.includes(m.officer_role ?? "") ||
        m.committees.some((c) => c.role === "委員長" || c.role === "副委員長")
    )
    .map((m) => m.member_id);
}

function when(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${d.getMonth() + 1}/${d.getDate()} ${d.toTimeString().slice(0, 5)}`;
}

type Draft = {
  title: string;
  event_type: string;
  fiscal_year: string;
  location: string;
  audience: string;
  target: "goyaku" | "riji" | "all";
  group_id: string;
  deadline: string;
  candidates: string[];
};

const EMPTY: Draft = {
  title: "",
  event_type: "五役会",
  fiscal_year: "",
  location: "JC会館 or オンライン",
  audience: "五役の皆さま",
  target: "goyaku",
  group_id: "",
  deadline: "",
  candidates: ["", "", ""],
};

export default function Polls() {
  const [list, setList] = useState<ListItem[]>([]);
  const [selected, setSelected] = useState<Summary | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [groups, setGroups] = useState<Group[]>([]);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [creating, setCreating] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => api<ListItem[]>("/polls").then(setList).catch((e) => setMsg(e.message));
  useEffect(() => {
    load();
    api<Member[]>("/members").then(setMembers).catch(() => {});
    api<Group[]>("/line/groups").then(setGroups).catch(() => {});
  }, []);

  const groupName = useMemo(
    () => Object.fromEntries(groups.map((g) => [g.group_id, g.group_name ?? g.group_id])),
    [groups]
  );

  const open = (pollId: string) =>
    api<Summary>(`/polls/${pollId}`).then(setSelected).catch((e) => setMsg(e.message));

  async function act(path: string, confirmText: string, body?: unknown) {
    if (!selected || !window.confirm(confirmText)) return;
    setBusy(true);
    setMsg(null);
    try {
      await api(`/polls/${selected.poll.poll_id}/${path}`, {
        method: "POST",
        body: body ? JSON.stringify(body) : undefined,
      });
      await open(selected.poll.poll_id);
      await load();
      setMsg("完了しました。");
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function create() {
    const candidates = draft.candidates.filter(Boolean).map((start) => ({ start }));
    if (!draft.title || !draft.group_id || candidates.length === 0) {
      setMsg("タイトル・案内先グループ・候補日を入力してください。");
      return;
    }
    const target_scope =
      draft.target === "goyaku"
        ? { kind: "officer", value: GOYAKU_ROLES }
        : draft.target === "riji"
          ? { kind: "custom", value: rijiIds(members) }
          : { kind: "all", value: [] };
    setBusy(true);
    try {
      const res = await api<Summary>("/polls", {
        method: "POST",
        body: JSON.stringify({
          title: draft.title,
          event_type: draft.event_type,
          fiscal_year: draft.fiscal_year ? Number(draft.fiscal_year) : null,
          location: draft.location || null,
          audience: draft.audience || null,
          candidates,
          target_scope,
          group_id: draft.group_id,
          deadline: draft.deadline || null,
        }),
      });
      setCreating(false);
      setDraft(EMPTY);
      setSelected(res);
      await load();
      setMsg("作成しました。内容を確認して「グループに案内する」を押してください。");
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const s = selected;
  const target = s ? groupName[s.poll.group_id] ?? s.poll.group_id : "";

  return (
    <div>
      <h1 className="text-xl font-semibold mb-3">日程調整</h1>
      {msg && <div className="mb-3 text-sm bg-amber-50 border border-amber-200 rounded p-2">{msg}</div>}

      <Card title="日程調整の一覧">
        <div className="mb-2">
          <button className="bg-brand text-white rounded px-3 py-1 text-sm" onClick={() => setCreating(!creating)}>
            {creating ? "作成をやめる" : "新しく作る"}
          </button>
        </div>
        {creating && (
          <div className="grid grid-cols-2 gap-2 text-sm mb-3 pb-3 border-b">
            <label className="col-span-2">
              タイトル
              <input className="border rounded p-1 w-full" placeholder="第2回五役予定者会議"
                value={draft.title} onChange={(e) => setDraft({ ...draft, title: e.target.value })} />
            </label>
            <label>
              種別（決定時のイベント）
              <select className="border rounded p-1 w-full" value={draft.event_type}
                onChange={(e) => setDraft({ ...draft, event_type: e.target.value })}>
                {TYPES.map((t) => <option key={t}>{t}</option>)}
              </select>
            </label>
            <label>
              年度（翌年度の会議のときだけ）
              <input className="border rounded p-1 w-full" type="number" placeholder="2027"
                value={draft.fiscal_year} onChange={(e) => setDraft({ ...draft, fiscal_year: e.target.value })} />
            </label>
            <label>
              対象者
              <select className="border rounded p-1 w-full" value={draft.target}
                onChange={(e) => {
                  const t = e.target.value as Draft["target"];
                  const audience = t === "goyaku" ? "五役の皆さま" : t === "riji" ? "理事の皆さま" : "会員の皆さま";
                  setDraft({ ...draft, target: t, audience });
                }}>
                <option value="goyaku">五役（理事長・副理事長・専務・監事）</option>
                <option value="riji">理事（五役＋委員長・副委員長）</option>
                <option value="all">会員全員</option>
              </select>
            </label>
            <label>
              宛名（案内に表示）
              <input className="border rounded p-1 w-full" value={draft.audience}
                onChange={(e) => setDraft({ ...draft, audience: e.target.value })} />
            </label>
            <label>
              案内先の LINE グループ
              <select className="border rounded p-1 w-full" value={draft.group_id}
                onChange={(e) => setDraft({ ...draft, group_id: e.target.value })}>
                <option value="">選んでください</option>
                {groups.map((g) => <option key={g.group_id} value={g.group_id}>{g.group_name ?? g.group_id}</option>)}
              </select>
            </label>
            <label>
              場所
              <input className="border rounded p-1 w-full" value={draft.location}
                onChange={(e) => setDraft({ ...draft, location: e.target.value })} />
            </label>
            <label>
              回答締切
              <input className="border rounded p-1 w-full" type="datetime-local" value={draft.deadline}
                onChange={(e) => setDraft({ ...draft, deadline: e.target.value })} />
            </label>
            <div className="col-span-2">
              候補日時
              {draft.candidates.map((c, i) => (
                <input key={i} className="border rounded p-1 w-full mb-1" type="datetime-local" value={c}
                  onChange={(e) => {
                    const candidates = [...draft.candidates];
                    candidates[i] = e.target.value;
                    setDraft({ ...draft, candidates });
                  }} />
              ))}
              <button className="text-brand text-xs" onClick={() => setDraft({ ...draft, candidates: [...draft.candidates, ""] })}>
                ＋ 候補を追加
              </button>
            </div>
            <div className="col-span-2">
              <button className="bg-brand text-white rounded px-3 py-1" disabled={busy} onClick={create}>
                作成する（まだ送信しません）
              </button>
            </div>
          </div>
        )}
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-slate-500 border-b">
              <th className="py-1">タイトル</th><th>状態</th><th>未回答</th><th>案内</th><th></th>
            </tr>
          </thead>
          <tbody>
            {list.map((p) => (
              <tr key={p.poll_id} className="border-b">
                <td className="py-1">{p.title}</td>
                <td>{STATUS[p.status]}</td>
                <td>{p.pending}人</td>
                <td>{p.announced_at ? when(p.announced_at) : "未送信"}</td>
                <td><button className="text-brand" onClick={() => open(p.poll_id)}>開く</button></td>
              </tr>
            ))}
            {list.length === 0 && <tr><td colSpan={5} className="py-2 text-slate-500">まだありません。</td></tr>}
          </tbody>
        </table>
      </Card>

      {s && (
        <Card title={`${s.poll.title}（${STATUS[s.poll.status]}）`}>
          <div className="text-sm text-slate-600 mb-2 space-y-0.5">
            <div>案内先: {target}／{s.poll.audience ?? ""}</div>
            <div>締切: {when(s.poll.deadline)}　案内: {s.poll.announced_at ? when(s.poll.announced_at) : "未送信"}
              {s.poll.reminded_at && `　催促: ${when(s.poll.reminded_at)}`}</div>
            {s.liff_url && <div>回答画面: <a className="text-brand underline" href={s.liff_url} target="_blank" rel="noreferrer">{s.liff_url}</a></div>}
            {!s.liff_url && <div className="text-red-600">LIFF が未設定のため、まだ案内できません。</div>}
          </div>
          <div className="overflow-x-auto">
            <table className="text-sm border-collapse">
              <thead>
                <tr className="border-b">
                  <th className="text-left pr-3 py-1">名前</th>
                  {s.candidates.map((c) => (
                    <th key={c.candidate_id} className={`px-2 ${s.poll.decided_candidate_id === c.candidate_id ? "bg-green-50" : ""}`}>
                      {c.label}
                    </th>
                  ))}
                  <th className="text-left pl-2">コメント</th>
                </tr>
              </thead>
              <tbody>
                {[...s.rows, ...s.others].map((r, i) => (
                  <tr key={i} className="border-b">
                    <td className="pr-3 py-1 whitespace-nowrap">
                      {r.name}
                      {r.display_name && r.member_id && <span className="text-xs text-slate-400">（{r.display_name}）</span>}
                    </td>
                    {s.candidates.map((c) => {
                      const a = r.answers?.[c.candidate_id];
                      return <td key={c.candidate_id} className={`text-center ${a ? MARK_CLASS[a] : "text-slate-300"}`}>{a ? MARK[a] : "—"}</td>;
                    })}
                    <td className="pl-2 text-slate-600">{r.comment}</td>
                  </tr>
                ))}
                <tr className="font-semibold">
                  <td className="pr-3 py-1">集計</td>
                  {s.candidates.map((c) => (
                    <td key={c.candidate_id} className="text-center whitespace-nowrap px-2">
                      ○{s.counts[c.candidate_id].ok} △{s.counts[c.candidate_id].maybe} ×{s.counts[c.candidate_id].ng}
                    </td>
                  ))}
                  <td></td>
                </tr>
              </tbody>
            </table>
          </div>
          <div className="text-sm mt-2">未回答: {s.pending.length ? s.pending.join("・") : "なし"}</div>

          {s.poll.status === "open" && (
            <div className="flex flex-wrap gap-2 mt-3">
              <button className="bg-brand text-white rounded px-3 py-1 text-sm" disabled={busy || !s.liff_url}
                onClick={() => act("announce", `「${target}」に日程調整の案内を送ります。よろしいですか？`)}>
                {s.poll.announced_at ? "もう一度案内する" : "グループに案内する"}
              </button>
              <button className="border rounded px-3 py-1 text-sm" disabled={busy || !s.liff_url || s.pending.length === 0}
                onClick={() => act("remind", `「${target}」で未回答の ${s.pending.join("・")} さんに催促します。よろしいですか？`)}>
                未回答者に催促する
              </button>
              <button className="border rounded px-3 py-1 text-sm" disabled={busy}
                onClick={() => act("close", "回答の受付を締め切ります（日程は決めません）。よろしいですか？")}>
                締め切る
              </button>
            </div>
          )}
          {s.poll.status !== "decided" && (
            <div className="mt-3 text-sm">
              <div className="mb-1">この日に決定（イベントを下書きで作り、カレンダーに登録して、グループに連絡します）</div>
              <div className="flex flex-wrap gap-2">
                {s.candidates.map((c) => (
                  <button key={c.candidate_id} className="border border-green-600 text-green-700 rounded px-3 py-1" disabled={busy}
                    onClick={() => act("decide", `${c.label} に決定し、「${target}」に連絡します。よろしいですか？`, { candidate_id: c.candidate_id })}>
                    {c.label}
                  </button>
                ))}
              </div>
            </div>
          )}
          {s.poll.status === "decided" && (
            <div className="mt-3 text-sm text-green-700">
              決定済み（{s.candidates.find((c) => c.candidate_id === s.poll.decided_candidate_id)?.label}）。出欠管理にイベントを下書きで作成しました。
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
