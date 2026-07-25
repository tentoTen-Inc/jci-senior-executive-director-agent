import { useEffect, useState } from "react";
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
import { Card } from "../components/Card";

type EventRow = {
  event_id: string;
  type: string;
  title: string;
  datetime_start: string;
  location: string | null;
  attendance_deadline: string | null;
  target_scope: { kind: string; value: string[] };
  quorum: number | null;
  status: string;
};

const EVENT_TYPES = ["例会", "理事会", "五役会", "委員会", "総会", "イベント"];
const SCOPE_KINDS = [
  { key: "all", label: "全員" },
  { key: "committee", label: "委員会" },
  { key: "officer", label: "役職" },
  { key: "custom", label: "会員ID指定" },
];

/** datetime-local 用（"YYYY-MM-DDTHH:MM"）に整える。 */
function dtLocal(iso: string | null): string {
  return iso ? iso.slice(0, 16) : "";
}
type Summary = {
  total_targets: number;
  answered: number;
  unanswered: number;
  attendance_rate: number;
  counts: Record<string, number>;
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

const STATUSES = ["出席", "Web出席", "欠席", "委任", "未回答"];

export default function Events() {
  const [events, setEvents] = useState<EventRow[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [att, setAtt] = useState<Attendance[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [names, setNames] = useState<Record<string, string>>({});
  const [trends, setTrends] = useState<Trend[]>([]);
  const [msg, setMsg] = useState<string | null>(null);
  const [edit, setEdit] = useState<EventRow | null>(null);
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState({
    type: "例会",
    title: "",
    datetime_start: "",
    location: "",
    quorum: "",
  });

  useEffect(() => {
    api<EventRow[]>("/events").then(setEvents).catch((e) => setMsg(e.message));
    api<Member[]>("/members").then((ms) =>
      setNames(Object.fromEntries(ms.map((m) => [m.member_id, m.name])))
    );
    api<Trend[]>("/kpi/trends").then(setTrends).catch(() => {});
  }, []);

  async function open(id: string) {
    setSel(id);
    const data = await api<{ attendances: Attendance[]; summary: Summary }>(
      `/events/${id}/attendances`
    );
    setAtt(data.attendances);
    setSummary(data.summary);
  }

  async function setStatus(memberId: string, status: string) {
    if (!sel) return;
    await api(`/events/${sel}/attendances/${memberId}`, {
      method: "PUT",
      body: JSON.stringify({ status }),
    });
    open(sel);
  }

  async function remind() {
    if (!sel) return;
    const r = await api<{ targets: string[] }>(`/events/${sel}/remind`, { method: "POST" });
    setMsg(`未回答 ${r.targets.length} 名に催促ジョブを作成しました。`);
  }

  async function downloadCsv() {
    if (!sel) return;
    const res = await fetch(`/api/events/${sel}/attendances.csv`, { credentials: "include" });
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `attendances_${sel}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function createEvent() {
    if (!draft.title.trim() || !draft.datetime_start) {
      setMsg("イベント名と開催日時は必須です。");
      return;
    }
    try {
      await api("/events", {
        method: "POST",
        body: JSON.stringify({
          type: draft.type,
          title: draft.title.trim(),
          datetime_start: draft.datetime_start,
          location: draft.location.trim() || null,
          quorum: draft.quorum ? Number(draft.quorum) : null,
          target_scope: { kind: "all", value: [] },
          status: "open",
        }),
      });
      setMsg(`${draft.title} を登録しました。`);
      setDraft({ type: "例会", title: "", datetime_start: "", location: "", quorum: "" });
      setCreating(false);
      api<EventRow[]>("/events").then(setEvents);
    } catch (e) {
      setMsg((e as Error).message);
    }
  }

  async function saveEvent() {
    if (!edit) return;
    try {
      await api(`/events/${edit.event_id}`, {
        method: "PUT",
        body: JSON.stringify({
          type: edit.type,
          title: edit.title,
          datetime_start: edit.datetime_start,
          location: edit.location,
          attendance_deadline: edit.attendance_deadline,
          target_scope: edit.target_scope,
          quorum: edit.quorum,
          status: edit.status,
        }),
      });
      setMsg(`${edit.title} を更新しました。`);
      setEdit(null);
      api<EventRow[]>("/events").then(setEvents);
      if (sel === edit.event_id) open(edit.event_id);
    } catch (e) {
      setMsg((e as Error).message);
    }
  }

  async function downloadPackage() {
    if (!sel) return;
    const res = await fetch(`/api/events/${sel}/package?download=true`, {
      credentials: "include",
    });
    if (!res.ok) {
      setMsg("事前共有パッケージの作成に失敗しました。");
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `package_${sel}.md`;
    a.click();
    URL.revokeObjectURL(url);
  }

  const attByMember = Object.fromEntries(att.map((a) => [a.member_id, a.status]));
  const proxyByMember: Record<string, string | null> = Object.fromEntries(
    att.map((a) => [a.member_id, a.proxy_member_id])
  );

  return (
    <div>
      <h1 className="text-lg font-semibold text-navy mb-3">出欠管理</h1>
      {msg && <div className="mb-2 text-sm text-brand">{msg}</div>}

      {trends.length > 0 && (
        <Card title="出席率の推移">
          <div style={{ width: "100%", height: 220 }}>
            <ResponsiveContainer>
              <LineChart data={trends.map((t) => ({ ...t, pct: Math.round(t.attendance_rate * 100) }))}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="title" tick={{ fontSize: 11 }} />
                <YAxis domain={[0, 100]} unit="%" tick={{ fontSize: 11 }} />
                <Tooltip />
                <Line type="monotone" dataKey="pct" name="出席率%" stroke="#2e5a88" />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </Card>
      )}

      <Card title="イベント一覧">
        {creating ? (
          <div className="flex flex-wrap gap-2 items-center text-sm mb-3 pb-3 border-b">
            <select
              className="border rounded p-1"
              value={draft.type}
              onChange={(e) => setDraft({ ...draft, type: e.target.value })}
            >
              {EVENT_TYPES.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
            <input
              className="border rounded p-1 w-48"
              placeholder="イベント名"
              value={draft.title}
              onChange={(e) => setDraft({ ...draft, title: e.target.value })}
            />
            <input
              type="datetime-local"
              className="border rounded p-1"
              value={draft.datetime_start}
              onChange={(e) => setDraft({ ...draft, datetime_start: e.target.value })}
            />
            <input
              className="border rounded p-1 w-36"
              placeholder="場所"
              value={draft.location}
              onChange={(e) => setDraft({ ...draft, location: e.target.value })}
            />
            <input
              type="number"
              className="border rounded p-1 w-24"
              placeholder="定足数"
              value={draft.quorum}
              onChange={(e) => setDraft({ ...draft, quorum: e.target.value })}
            />
            <button className="bg-brand text-white rounded px-3 py-1" onClick={createEvent}>
              登録
            </button>
            <button className="text-slate-500 underline text-xs" onClick={() => setCreating(false)}>
              やめる
            </button>
          </div>
        ) : (
          <button
            className="bg-slate-200 text-navy rounded px-3 py-1 text-sm mb-3"
            onClick={() => setCreating(true)}
          >
            イベントを登録
          </button>
        )}
        <table className="w-full text-sm">
          <tbody>
            {events.map((e) => (
              <tr key={e.event_id} className="border-t">
                <td className="py-1">{e.title}</td>
                <td className="text-slate-500">{e.datetime_start.replace("T", " ").slice(0, 16)}</td>
                <td>{e.status}</td>
                <td className="space-x-2 whitespace-nowrap">
                  <button
                    className="text-xs bg-slate-200 text-navy rounded px-2 py-1"
                    onClick={() => open(e.event_id)}
                  >
                    詳細
                  </button>
                  <button
                    className="text-xs bg-slate-200 text-navy rounded px-2 py-1"
                    onClick={() => setEdit({ ...e, datetime_start: dtLocal(e.datetime_start) })}
                  >
                    編集
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {events.length === 0 && <p className="text-slate-500 text-sm">イベントがありません。</p>}
      </Card>

      {edit && (
        <Card title={`イベントを編集: ${edit.title}`}>
          <div className="grid gap-2 md:grid-cols-2 text-sm">
            <label className="block">
              <span className="text-xs text-slate-500">種別</span>
              <select
                className="border rounded p-1 w-full"
                value={edit.type}
                onChange={(ev) => setEdit({ ...edit, type: ev.target.value })}
              >
                {EVENT_TYPES.map((t) => (
                  <option key={t} value={t}>{t}</option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="text-xs text-slate-500">イベント名</span>
              <input
                className="border rounded p-1 w-full"
                value={edit.title}
                onChange={(ev) => setEdit({ ...edit, title: ev.target.value })}
              />
            </label>
            <label className="block">
              <span className="text-xs text-slate-500">開催日時</span>
              <input
                type="datetime-local"
                className="border rounded p-1 w-full"
                value={dtLocal(edit.datetime_start)}
                onChange={(ev) => setEdit({ ...edit, datetime_start: ev.target.value })}
              />
            </label>
            <label className="block">
              <span className="text-xs text-slate-500">出欠締切</span>
              <input
                type="datetime-local"
                className="border rounded p-1 w-full"
                value={dtLocal(edit.attendance_deadline)}
                onChange={(ev) =>
                  setEdit({ ...edit, attendance_deadline: ev.target.value || null })
                }
              />
            </label>
            <label className="block">
              <span className="text-xs text-slate-500">場所</span>
              <input
                className="border rounded p-1 w-full"
                value={edit.location ?? ""}
                onChange={(ev) => setEdit({ ...edit, location: ev.target.value || null })}
              />
            </label>
            <label className="block">
              <span className="text-xs text-slate-500">定足数（人数）</span>
              <input
                type="number"
                className="border rounded p-1 w-full"
                value={edit.quorum ?? ""}
                onChange={(ev) =>
                  setEdit({ ...edit, quorum: ev.target.value ? Number(ev.target.value) : null })
                }
              />
            </label>
            <label className="block">
              <span className="text-xs text-slate-500">対象範囲</span>
              <select
                className="border rounded p-1 w-full"
                value={edit.target_scope.kind}
                onChange={(ev) =>
                  setEdit({ ...edit, target_scope: { kind: ev.target.value, value: [] } })
                }
              >
                {SCOPE_KINDS.map((k) => (
                  <option key={k.key} value={k.key}>{k.label}</option>
                ))}
              </select>
            </label>
            {edit.target_scope.kind !== "all" && (
              <label className="block">
                <span className="text-xs text-slate-500">対象（カンマ区切り）</span>
                <input
                  className="border rounded p-1 w-full"
                  placeholder="例: 総務委員会, コト創り委員会"
                  value={edit.target_scope.value.join(", ")}
                  onChange={(ev) =>
                    setEdit({
                      ...edit,
                      target_scope: {
                        kind: edit.target_scope.kind,
                        value: ev.target.value
                          .split(",")
                          .map((x) => x.trim())
                          .filter(Boolean),
                      },
                    })
                  }
                />
              </label>
            )}
            <label className="block">
              <span className="text-xs text-slate-500">状態</span>
              <select
                className="border rounded p-1 w-full"
                value={edit.status}
                onChange={(ev) => setEdit({ ...edit, status: ev.target.value })}
              >
                <option value="draft">下書き</option>
                <option value="open">受付中</option>
                <option value="closed">終了</option>
              </select>
            </label>
          </div>
          <div className="mt-3 flex gap-2 items-center">
            <button className="bg-brand text-white rounded px-3 py-1 text-sm" onClick={saveEvent}>
              保存
            </button>
            <button className="text-slate-500 underline text-xs" onClick={() => setEdit(null)}>
              閉じる
            </button>
          </div>
        </Card>
      )}

      {sel && summary && (
        <Card title="出欠詳細">
          <div className="text-sm mb-2 flex flex-wrap gap-3 items-center">
            <span>回答 {summary.answered}/{summary.total_targets}</span>
            <span className="font-semibold">出席率 {Math.round(summary.attendance_rate * 100)}%</span>
            <button className="text-xs bg-brand text-white rounded px-2 py-1" onClick={remind}>
              未回答に催促
            </button>
            <button className="text-xs bg-slate-200 text-navy rounded px-2 py-1" onClick={downloadCsv}>
              CSV出力
            </button>
            <button
              className="text-xs bg-slate-200 text-navy rounded px-2 py-1"
              onClick={downloadPackage}
            >
              事前共有パッケージ(md)
            </button>
          </div>

          {summary.quorum !== null && (
            <div className="text-sm mb-2 p-2 rounded bg-slate-50">
              <span className="text-slate-500 text-xs mr-2">定足数 {summary.quorum}名</span>
              <span className={summary.quorum_met ? "text-green-700" : "text-red-600"}>
                委任含む {summary.present_with_proxies}名 →{" "}
                {summary.quorum_met ? "充足" : "不足"}
              </span>
              <span className="mx-2 text-slate-300">/</span>
              <span
                className={summary.quorum_met_without_proxies ? "text-green-700" : "text-red-600"}
              >
                委任除く {summary.present}名 →{" "}
                {summary.quorum_met_without_proxies ? "充足" : "不足"}
              </span>
              <p className="text-xs text-slate-400 mt-1">
                ※ 委任を定足数に数えるかは規約に依ります。両方を表示しています（判定の補助）。
              </p>
            </div>
          )}
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-slate-500">
                <th className="py-1">会員</th>
                <th>状態</th>
                <th>委任先</th>
                <th>手動修正</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(names).map(([id, name]) => (
                <tr key={id} className="border-t">
                  <td className="py-1">{name}</td>
                  <td className="font-semibold">{attByMember[id] ?? "未回答"}</td>
                  <td className="text-slate-500">
                    {proxyByMember[id] ? names[proxyByMember[id]!] ?? proxyByMember[id] : "-"}
                  </td>
                  <td>
                    <select
                      className="text-xs border rounded p-1"
                      value={attByMember[id] ?? "未回答"}
                      onChange={(e) => setStatus(id, e.target.value)}
                    >
                      {STATUSES.map((s) => (
                        <option key={s} value={s}>{s}</option>
                      ))}
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}
