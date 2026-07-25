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
  title: string;
  datetime_start: string;
  status: string;
};
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
        <table className="w-full text-sm">
          <tbody>
            {events.map((e) => (
              <tr key={e.event_id} className="border-t">
                <td className="py-1">{e.title}</td>
                <td className="text-slate-500">{e.datetime_start.replace("T", " ").slice(0, 16)}</td>
                <td>{e.status}</td>
                <td>
                  <button
                    className="text-xs bg-slate-200 text-navy rounded px-2 py-1"
                    onClick={() => open(e.event_id)}
                  >
                    詳細
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {events.length === 0 && <p className="text-slate-500 text-sm">イベントがありません。</p>}
      </Card>

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
