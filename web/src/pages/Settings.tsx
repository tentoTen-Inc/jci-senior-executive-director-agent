import { useEffect, useState } from "react";
import { api } from "../api/client";
import { Card } from "../components/Card";

type Settings = {
  kill_switch: boolean;
  quiet_hours: { start: string; end: string; tz: string };
  rate_limit: { per_member_per_day: number; global_per_min: number };
};
type Stage = { name: string; offset_minutes: number; audience: string; template: string };
type Policy = { policy_id: string; event_type: string; stages: Stage[] };
type Audit = { at: string; actor: string; action: string; target: string | null; detail: string | null };

/** 締切からのオフセット（分・負=締切前）を人が読める表記にする。 */
function offsetLabel(minutes: number): string {
  if (minutes === 0) return "締切時刻";
  const before = minutes < 0;
  const abs = Math.abs(minutes);
  const days = Math.floor(abs / (60 * 24));
  const hours = Math.floor((abs % (60 * 24)) / 60);
  const mins = abs % 60;
  const parts = [days && `${days}日`, hours && `${hours}時間`, mins && `${mins}分`].filter(Boolean);
  return `締切${before ? "前" : "後"} ${parts.join("")}`;
}

export default function Settings() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [audit, setAudit] = useState<Audit[]>([]);
  const [msg, setMsg] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const loadAll = () => {
    api<Settings>("/settings").then(setSettings).catch((e) => setMsg(e.message));
    api<Policy[]>("/policies").then(setPolicies).catch(() => {});
    api<Audit[]>("/audit-logs").then(setAudit).catch(() => {});
  };
  useEffect(loadAll, []);

  async function save(next: Settings) {
    setSaving(true);
    try {
      const saved = await api<Settings>("/settings", {
        method: "PUT",
        body: JSON.stringify(next),
      });
      setSettings(saved);
      setMsg("保存しました。");
      api<Audit[]>("/audit-logs").then(setAudit).catch(() => {});
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function savePolicy(p: Policy) {
    try {
      await api<Policy>(`/policies/${p.policy_id}`, { method: "PUT", body: JSON.stringify(p) });
      setMsg(`催促ポリシー(${p.policy_id})を保存しました。`);
      loadAll();
    } catch (e) {
      setMsg((e as Error).message);
    }
  }

  async function seed() {
    try {
      await api("/policies/seed", { method: "POST" });
      setMsg("既定の催促ポリシーを投入しました。");
      loadAll();
    } catch (e) {
      setMsg((e as Error).message);
    }
  }

  return (
    <div>
      <h1 className="text-lg font-semibold text-navy mb-3">設定・監査</h1>
      {msg && <div className="mb-2 text-sm text-brand">{msg}</div>}

      {settings && (
        <>
          <Card title="キルスイッチ（緊急停止）">
            <div className="flex items-center gap-3 text-sm">
              <span
                className={`px-2 py-1 rounded-full text-xs ${
                  settings.kill_switch ? "bg-red-100 text-red-700" : "bg-green-50 text-green-700"
                }`}
              >
                {settings.kill_switch ? "ON（全自動配信 停止中）" : "OFF（稼働中）"}
              </span>
              <button
                className={`rounded px-3 py-1 text-white ${
                  settings.kill_switch ? "bg-brand" : "bg-red-600"
                }`}
                disabled={saving}
                onClick={() => save({ ...settings, kill_switch: !settings.kill_switch })}
              >
                {settings.kill_switch ? "配信を再開する" : "全配信を停止する"}
              </button>
            </div>
            <p className="text-xs text-slate-400 mt-2">
              ON の間、tick もクローズ通知も送信されません（ブロックされた対象は OFF 後の次 tick で再送）。
            </p>
          </Card>

          <Card title="静音時間・レート上限">
            <div className="flex flex-wrap gap-4 text-sm items-end">
              <label className="block">
                <span className="text-xs text-slate-500 block">静音開始</span>
                <input
                  className="border rounded p-1 w-24"
                  value={settings.quiet_hours.start}
                  onChange={(e) =>
                    setSettings({
                      ...settings,
                      quiet_hours: { ...settings.quiet_hours, start: e.target.value },
                    })
                  }
                />
              </label>
              <label className="block">
                <span className="text-xs text-slate-500 block">静音終了</span>
                <input
                  className="border rounded p-1 w-24"
                  value={settings.quiet_hours.end}
                  onChange={(e) =>
                    setSettings({
                      ...settings,
                      quiet_hours: { ...settings.quiet_hours, end: e.target.value },
                    })
                  }
                />
              </label>
              <label className="block">
                <span className="text-xs text-slate-500 block">1人あたり/日</span>
                <input
                  type="number"
                  className="border rounded p-1 w-20"
                  value={settings.rate_limit.per_member_per_day}
                  onChange={(e) =>
                    setSettings({
                      ...settings,
                      rate_limit: {
                        ...settings.rate_limit,
                        per_member_per_day: Number(e.target.value),
                      },
                    })
                  }
                />
              </label>
              <label className="block">
                <span className="text-xs text-slate-500 block">全体/分</span>
                <input
                  type="number"
                  className="border rounded p-1 w-20"
                  value={settings.rate_limit.global_per_min}
                  onChange={(e) =>
                    setSettings({
                      ...settings,
                      rate_limit: {
                        ...settings.rate_limit,
                        global_per_min: Number(e.target.value),
                      },
                    })
                  }
                />
              </label>
              <button
                className="bg-brand text-white rounded px-3 py-1"
                disabled={saving}
                onClick={() => save(settings)}
              >
                保存
              </button>
            </div>
            <p className="text-xs text-slate-400 mt-2">
              静音時間は {settings.quiet_hours.tz} 基準。時刻は HH:MM 形式。
            </p>
          </Card>
        </>
      )}

      <Card title="催促ポリシー">
        {policies.length === 0 && (
          <p className="text-sm text-slate-500 mb-2">
            ポリシーが未投入です。既定（例会・理事会）を投入してください。
          </p>
        )}
        {policies.map((p) => (
          <div key={p.policy_id} className="border-t py-2">
            <div className="text-sm font-medium">
              {p.event_type}
              <span className="text-xs text-slate-400 ml-2">{p.policy_id}</span>
            </div>
            <table className="text-sm mt-1">
              <thead>
                <tr className="text-xs text-slate-500 text-left">
                  <th className="pr-3 font-medium">段階</th>
                  <th className="pr-3 font-medium">タイミング（分・負=締切前）</th>
                  <th className="pr-3 font-medium">対象</th>
                </tr>
              </thead>
              <tbody>
                {p.stages.map((s, i) => (
                  <tr key={i}>
                    <td className="pr-3 py-1">{s.name}</td>
                    <td className="pr-3 py-1">
                      <input
                        type="number"
                        className="border rounded p-1 w-24"
                        value={s.offset_minutes}
                        onChange={(e) => {
                          const stages = p.stages.map((x, j) =>
                            j === i ? { ...x, offset_minutes: Number(e.target.value) } : x
                          );
                          setPolicies(
                            policies.map((q) =>
                              q.policy_id === p.policy_id ? { ...q, stages } : q
                            )
                          );
                        }}
                      />
                      <span className="text-xs text-slate-500 ml-2">
                        {offsetLabel(s.offset_minutes)}
                      </span>
                    </td>
                    <td className="pr-3 py-1 text-slate-500">{s.audience}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <button
              className="bg-brand text-white rounded px-3 py-1 text-xs mt-2"
              onClick={() => savePolicy(p)}
            >
              このポリシーを保存
            </button>
          </div>
        ))}
        <button className="bg-slate-200 text-navy rounded px-3 py-1 text-sm mt-3" onClick={seed}>
          既定ポリシーを投入（seed）
        </button>
      </Card>

      <Card title="監査ログ">
        <table className="w-full text-sm">
          <tbody>
            {audit.slice(0, 30).map((a, i) => (
              <tr key={i} className="border-t">
                <td className="py-1 text-slate-500 whitespace-nowrap">
                  {a.at.replace("T", " ").slice(0, 16)}
                </td>
                <td>{a.actor}</td>
                <td>{a.action}</td>
                <td className="text-slate-500">{a.target || ""}</td>
                <td className="text-slate-500">{a.detail || ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {audit.length === 0 && <p className="text-slate-500 text-sm">監査ログはありません。</p>}
      </Card>
    </div>
  );
}
