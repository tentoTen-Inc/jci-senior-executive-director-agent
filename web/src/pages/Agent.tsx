import { useEffect, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../api/client";
import { Card } from "../components/Card";

type Cost = {
  month_start: string;
  calls: number;
  failed_calls: number;
  input_tokens: number;
  output_tokens: number;
  llm_cost_usd: number;
  infra_cost_usd: number;
  monthly_cost_usd: number;
};
type Overview = {
  delivery_success_rate: number;
  delivery_total: number;
  avg_attendance_rate: number;
  avg_answer_rate: number;
  reminder_count: number;
  cost: Cost;
};
type Trend = { title: string; answer_rate: number; attendance_rate: number };
type DeliveryLog = {
  log_id: string;
  member_id: string;
  result: string;
  reason: string | null;
  sent_at: string;
};
type Audit = { at: string; actor: string; action: string; target: string | null };

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-white rounded-lg shadow-sm p-4 flex-1 min-w-[150px]">
      <div className="text-xs text-slate-500">{label}</div>
      <div className="text-2xl font-bold text-navy">{value}</div>
    </div>
  );
}

export default function Agent() {
  const [ov, setOv] = useState<Overview | null>(null);
  const [trends, setTrends] = useState<Trend[]>([]);
  const [logs, setLogs] = useState<DeliveryLog[]>([]);
  const [audit, setAudit] = useState<Audit[]>([]);
  const [filter, setFilter] = useState("");
  const [msg, setMsg] = useState<string | null>(null);

  const loadLogs = (result: string) =>
    api<DeliveryLog[]>(`/delivery-logs${result ? `?result=${result}` : ""}`).then(setLogs);

  useEffect(() => {
    api<Overview>("/kpi/overview").then(setOv).catch((e) => setMsg(e.message));
    api<Trend[]>("/kpi/trends").then(setTrends).catch(() => {});
    loadLogs("").catch(() => {});
    api<Audit[]>("/audit-logs").then(setAudit).catch(() => {});
  }, []);

  return (
    <div>
      <h1 className="text-lg font-semibold text-navy mb-3">エージェントKPI</h1>
      {msg && <div className="mb-2 text-sm text-red-600">{msg}</div>}

      {ov && (
        <div className="flex flex-wrap gap-3 mb-4">
          <Stat label="配信成功率" value={`${Math.round(ov.delivery_success_rate * 100)}%`} />
          <Stat label="平均出席率" value={`${Math.round(ov.avg_attendance_rate * 100)}%`} />
          <Stat label="平均回答率" value={`${Math.round(ov.avg_answer_rate * 100)}%`} />
          <Stat label="配信総数" value={`${ov.delivery_total}`} />
          <Stat label="当月コスト" value={`$${ov.cost.monthly_cost_usd.toFixed(2)}`} />
        </div>
      )}

      {ov && (
        <Card title="当月のAIコスト（概算）">
          <table className="text-sm">
            <tbody>
              <tr className="border-t">
                <td className="py-1 pr-4 text-slate-500">集計期間</td>
                <td>{ov.cost.month_start.slice(0, 7)} 〜 現在</td>
              </tr>
              <tr className="border-t">
                <td className="py-1 pr-4 text-slate-500">Gemini呼び出し</td>
                <td>
                  {ov.cost.calls} 回
                  {ov.cost.failed_calls > 0 && (
                    <span className="text-red-600">（失敗 {ov.cost.failed_calls}）</span>
                  )}
                </td>
              </tr>
              <tr className="border-t">
                <td className="py-1 pr-4 text-slate-500">トークン</td>
                <td>
                  入力 {ov.cost.input_tokens.toLocaleString()} / 出力{" "}
                  {ov.cost.output_tokens.toLocaleString()}
                </td>
              </tr>
              <tr className="border-t">
                <td className="py-1 pr-4 text-slate-500">内訳</td>
                <td>
                  Gemini ${ov.cost.llm_cost_usd.toFixed(2)} ＋ インフラ概算 $
                  {ov.cost.infra_cost_usd.toFixed(2)}
                </td>
              </tr>
            </tbody>
          </table>
          <p className="text-xs text-slate-400 mt-2">
            ※ 記録したトークン数×単価による概算です。実請求額は Cloud Billing を参照してください。
          </p>
        </Card>
      )}

      {trends.length > 0 && (
        <Card title="回答率・出席率（イベント別）">
          <div style={{ width: "100%", height: 220 }}>
            <ResponsiveContainer>
              <BarChart
                data={trends.map((t) => ({
                  title: t.title,
                  回答率: Math.round(t.answer_rate * 100),
                  出席率: Math.round(t.attendance_rate * 100),
                }))}
              >
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="title" tick={{ fontSize: 11 }} />
                <YAxis domain={[0, 100]} unit="%" tick={{ fontSize: 11 }} />
                <Tooltip />
                <Bar dataKey="回答率" fill="#2e5a88" />
                <Bar dataKey="出席率" fill="#7da7d9" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>
      )}

      <Card title="配信ログ">
        <div className="mb-2 text-sm">
          <select
            className="border rounded p-1"
            value={filter}
            onChange={(e) => {
              setFilter(e.target.value);
              loadLogs(e.target.value);
            }}
          >
            <option value="">すべて</option>
            <option value="ok">成功</option>
            <option value="blocked">ブロック</option>
            <option value="failed">失敗</option>
          </select>
        </div>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-slate-500">
              <th className="py-1">日時</th>
              <th>会員</th>
              <th>結果</th>
              <th>理由</th>
            </tr>
          </thead>
          <tbody>
            {logs.slice(0, 50).map((l) => (
              <tr key={l.log_id} className="border-t">
                <td className="py-1 text-slate-500">{l.sent_at.replace("T", " ").slice(0, 16)}</td>
                <td>{l.member_id}</td>
                <td>{l.result}</td>
                <td className="text-slate-500">{l.reason || "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {logs.length === 0 && <p className="text-slate-500 text-sm">配信ログはありません。</p>}
      </Card>

      <Card title="監査ログ">
        <table className="w-full text-sm">
          <tbody>
            {audit.slice(0, 30).map((a, i) => (
              <tr key={i} className="border-t">
                <td className="py-1 text-slate-500">{a.at.replace("T", " ").slice(0, 16)}</td>
                <td>{a.actor}</td>
                <td>{a.action}</td>
                <td className="text-slate-500">{a.target || ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {audit.length === 0 && <p className="text-slate-500 text-sm">監査ログはありません。</p>}
      </Card>
    </div>
  );
}
