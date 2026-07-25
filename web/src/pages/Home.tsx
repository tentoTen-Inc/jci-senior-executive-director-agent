import { useEffect, useState } from "react";
import { api } from "../api/client";
import { Card } from "../components/Card";

type EventBrief = {
  event_id: string;
  title: string;
  datetime_start: string;
  location: string | null;
  total_targets: number;
  answered: number;
  attendance_rate: number;
  unanswered: number;
};

type HomeData = {
  kill_switch: boolean;
  delivery_success_rate: number;
  member_count: number;
  action_required: {
    unlinked_members: number;
    open_escalations: number;
    unanswered_total: number;
    open_notice_actions: number;
  };
  upcoming_events: EventBrief[];
  this_week: EventBrief[];
};

type Escalation = {
  escalation_id: string;
  member_id: string;
  member_name: string | null;
  kind: string;
  text: string | null;
  created_at: string;
  status: string;
};

function Stat({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return (
    <div className="bg-white rounded-lg shadow-sm p-4 flex-1 min-w-[140px]">
      <div className="text-xs text-slate-500">{label}</div>
      <div className={`text-2xl font-bold ${warn ? "text-red-600" : "text-navy"}`}>{value}</div>
    </div>
  );
}

function fmt(dt: string) {
  return dt.replace("T", " ").slice(0, 16);
}

export default function Home() {
  const [data, setData] = useState<HomeData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [escalations, setEscalations] = useState<Escalation[]>([]);

  const loadAll = () => {
    api<HomeData>("/home").then(setData).catch((e) => setErr(e.message));
    api<Escalation[]>("/escalations?status=open").then(setEscalations).catch(() => {});
  };
  useEffect(loadAll, []);

  async function markHandled(id: string) {
    try {
      await api(`/escalations/${id}/handled`, { method: "POST" });
      loadAll();
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  if (err) return <Card title="ホーム"><p className="text-red-600 text-sm">{err}</p></Card>;
  if (!data) return <Card title="ホーム"><p className="text-slate-500 text-sm">読み込み中…</p></Card>;

  const a = data.action_required;
  return (
    <div>
      <h1 className="text-lg font-semibold text-navy mb-3">専務ダッシュボード</h1>

      <div className="flex flex-wrap gap-3 mb-4">
        <Stat label="会員数" value={`${data.member_count} 名`} />
        <Stat label="配信成功率" value={`${Math.round(data.delivery_success_rate * 100)}%`} />
        <Stat
          label="キルスイッチ"
          value={data.kill_switch ? "ON(停止中)" : "OFF(稼働)"}
          warn={data.kill_switch}
        />
        <Stat label="未回答(全体)" value={`${a.unanswered_total}`} warn={a.unanswered_total > 0} />
      </div>

      <Card title="要対応">
        <ul className="text-sm space-y-1">
          <li>未対応の事務局連絡: <b>{a.open_escalations}</b> 件</li>
          <li>LINE未連携の会員: <b>{a.unlinked_members}</b> 名</li>
          <li>出欠未回答(対象延べ): <b>{a.unanswered_total}</b> 名</li>
          <li>対外連絡の未完了タスク: <b>{a.open_notice_actions}</b> 件</li>
        </ul>
      </Card>

      {escalations.length > 0 && (
        <Card title="取次・問い合わせ（会員から）">
          <table className="w-full text-sm">
            <tbody>
              {escalations.map((e) => (
                <tr key={e.escalation_id} className="border-t align-top">
                  <td className="py-1 text-slate-500 whitespace-nowrap">{fmt(e.created_at)}</td>
                  <td className="py-1 whitespace-nowrap">{e.member_name || e.member_id}</td>
                  <td className="py-1 text-slate-500 whitespace-nowrap">
                    {e.kind === "question" ? "質問" : "連絡希望"}
                  </td>
                  <td className="py-1">{e.text || "（内容なし）"}</td>
                  <td className="py-1">
                    <button
                      className="bg-slate-200 text-navy rounded px-2 py-0.5 text-xs"
                      onClick={() => markHandled(e.escalation_id)}
                    >
                      対応済み
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="text-xs text-slate-400 mt-2">
            エージェントが答えられなかった質問と、会員からの連絡希望です。
          </p>
        </Card>
      )}

      <Card title="直近の予定">
        {data.upcoming_events.length === 0 ? (
          <p className="text-slate-500 text-sm">予定されているイベントはありません。</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-slate-500">
                <th className="py-1">イベント</th>
                <th>日時</th>
                <th>回答/対象</th>
                <th>出席率</th>
              </tr>
            </thead>
            <tbody>
              {data.upcoming_events.map((e) => (
                <tr key={e.event_id} className="border-t">
                  <td className="py-1">{e.title}</td>
                  <td className="text-slate-500">{fmt(e.datetime_start)}</td>
                  <td>{e.answered}/{e.total_targets}</td>
                  <td className="font-semibold">{Math.round(e.attendance_rate * 100)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
