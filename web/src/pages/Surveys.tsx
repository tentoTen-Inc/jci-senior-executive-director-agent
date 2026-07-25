import { useEffect, useState } from "react";
import { api } from "../api/client";
import { Card } from "../components/Card";

type Question = {
  question_id: string;
  title: string;
  type: string;
  section: string | null;
  scale_low: number | null;
  scale_high: number | null;
};
type Theme = { label: string; count: number; examples: string[] };
type Digest = {
  summary: string;
  themes: Theme[];
  sentiment: Record<string, number>;
  improvements: string[];
  generated_at: string | null;
};
type Survey = {
  survey_id: string;
  form_id: string;
  title: string;
  kind: string;
  event_id: string | null;
  questions: Question[];
  responses: { response_id: string }[];
  digest: Digest | null;
  synced_at: string | null;
};
type ScaleStat = {
  question_id: string;
  title: string;
  section: string | null;
  scale_low: number | null;
  scale_high: number | null;
  answered: number;
  average: number | null;
  distribution: Record<string, number>;
};
type TextStat = { question_id: string; title: string; answers: string[] };
type Aggregate = {
  responses: number;
  scales: ScaleStat[];
  texts: TextStat[];
};

const KIND_LABEL: Record<string, string> = { internal: "対内", external: "対外" };

/** 分布を簡易バーで表す（1..high の件数）。 */
function Distribution({ stat }: { stat: ScaleStat }) {
  const high = stat.scale_high ?? 5;
  const low = stat.scale_low ?? 1;
  const max = Math.max(1, ...Object.values(stat.distribution));
  const keys: number[] = [];
  for (let i = low; i <= high; i++) keys.push(i);
  return (
    <div className="flex items-end gap-1 h-8">
      {keys.map((k) => {
        const n = stat.distribution[String(k)] ?? 0;
        return (
          <div key={k} className="flex flex-col items-center" title={`${k}: ${n}件`}>
            <div
              className="w-3 bg-brand rounded-sm"
              style={{ height: `${(n / max) * 24}px`, minHeight: n ? "2px" : "0" }}
            />
            <span className="text-[10px] text-slate-400">{k}</span>
          </div>
        );
      })}
    </div>
  );
}

export default function Surveys() {
  const [items, setItems] = useState<Survey[]>([]);
  const [sel, setSel] = useState<Survey | null>(null);
  const [agg, setAgg] = useState<Aggregate | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({ form_id: "", kind: "internal", event_id: "" });

  const load = () =>
    api<Survey[]>("/surveys").then(setItems).catch((e) => setMsg(e.message));
  useEffect(() => {
    load();
  }, []);

  async function open(id: string) {
    try {
      const data = await api<{ survey: Survey; aggregate: Aggregate }>(`/surveys/${id}`);
      setSel(data.survey);
      setAgg(data.aggregate);
    } catch (e) {
      setMsg((e as Error).message);
    }
  }

  async function importForm(dryRun: boolean) {
    if (!form.form_id.trim()) {
      setMsg("フォームIDを入力してください。");
      return;
    }
    setBusy(true);
    try {
      const body = JSON.stringify({
        form_id: form.form_id.trim(),
        kind: form.kind,
        event_id: form.event_id.trim() || null,
        dry_run: dryRun,
      });
      if (dryRun) {
        const r = await api<{ title: string; questions: number; responses: number }>(
          "/surveys/import-form",
          { method: "POST", body }
        );
        setMsg(`[確認] ${r.title} / 設問${r.questions}件 / 回答${r.responses}件`);
      } else {
        const s = await api<Survey>("/surveys/import-form", { method: "POST", body });
        setMsg(`取込しました: ${s.title}`);
        setForm({ form_id: "", kind: form.kind, event_id: "" });
        await load();
        await open(s.survey_id);
      }
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function act(id: string, path: string) {
    setBusy(true);
    try {
      await api<Survey>(`/surveys/${id}/${path}`, { method: "POST" });
      setMsg(path === "sync" ? "回答を再取得しました。" : "AI要約を生成しました。");
      await load();
      await open(id);
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h1 className="text-lg font-semibold text-navy mb-3">アンケート</h1>
      {msg && <div className="mb-2 text-sm text-brand">{msg}</div>}

      <Card title="Googleフォームから取込">
        <div className="flex flex-wrap gap-2 items-center text-sm">
          <input
            className="border rounded p-1 flex-1 min-w-[240px]"
            placeholder="フォームID（URLの /d/ と /edit の間）"
            value={form.form_id}
            onChange={(e) => setForm({ ...form, form_id: e.target.value })}
          />
          <select
            className="border rounded p-1"
            value={form.kind}
            onChange={(e) => setForm({ ...form, kind: e.target.value })}
          >
            <option value="internal">対内</option>
            <option value="external">対外</option>
          </select>
          <input
            className="border rounded p-1 w-40"
            placeholder="対象イベントID(任意)"
            value={form.event_id}
            onChange={(e) => setForm({ ...form, event_id: e.target.value })}
          />
          <button
            className="bg-slate-200 text-navy rounded px-3 py-1"
            disabled={busy}
            onClick={() => importForm(true)}
          >
            取込確認(dry-run)
          </button>
          <button
            className="bg-brand text-white rounded px-3 py-1"
            disabled={busy}
            onClick={() => importForm(false)}
          >
            取込
          </button>
        </div>
        <p className="text-xs text-slate-400 mt-1">
          ※ フォームを drive-reader@jci-sed-agent.iam.gserviceaccount.com
          に共有しておく必要があります。配信は現行どおり Google フォームで行います。
        </p>
      </Card>

      <Card title="一覧">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-slate-500">
              <th className="py-1">アンケート</th>
              <th>区分</th>
              <th>回答</th>
              <th>取込</th>
            </tr>
          </thead>
          <tbody>
            {items.map((s) => (
              <tr
                key={s.survey_id}
                className="border-t hover:bg-slate-50 cursor-pointer"
                onClick={() => open(s.survey_id)}
              >
                <td className="py-1 font-medium">{s.title}</td>
                <td className="text-slate-500">{KIND_LABEL[s.kind] ?? s.kind}</td>
                <td>{s.responses.length}</td>
                <td className="text-slate-500">
                  {s.synced_at ? s.synced_at.replace("T", " ").slice(0, 16) : "-"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {items.length === 0 && (
          <p className="text-slate-500 text-sm">取込済みのアンケートはありません。</p>
        )}
      </Card>

      {sel && agg && (
        <Card title={`集計: ${sel.title}`}>
          <div className="flex flex-wrap gap-2 items-center text-sm mb-3">
            <span>回答 {agg.responses}件</span>
            <button
              className="bg-slate-200 text-navy rounded px-2 py-1 text-xs"
              disabled={busy}
              onClick={() => act(sel.survey_id, "sync")}
            >
              回答を再取得
            </button>
            <button
              className="bg-slate-200 text-navy rounded px-2 py-1 text-xs"
              disabled={busy}
              onClick={() => act(sel.survey_id, "digest")}
            >
              {sel.digest ? "AI要約を再生成" : "自由記述をAIで要約"}
            </button>
            <button className="text-slate-500 underline text-xs" onClick={() => setSel(null)}>
              閉じる
            </button>
          </div>

          <div className="grid gap-3 md:grid-cols-2 text-sm">
            <div>
              <div className="font-semibold text-navy mb-1">5段階評価</div>
              {agg.scales.length === 0 && <p className="text-slate-500">スケール設問はありません。</p>}
              {agg.scales.map((s) => (
                <div key={s.question_id} className="border-t py-2">
                  {s.section && <div className="text-[10px] text-slate-400">{s.section}</div>}
                  <div className="text-xs">{s.title}</div>
                  <div className="flex items-center gap-3">
                    <span className="text-lg font-bold text-navy">
                      {s.average === null ? "-" : s.average.toFixed(1)}
                    </span>
                    <span className="text-xs text-slate-400">n={s.answered}</span>
                    <Distribution stat={s} />
                  </div>
                </div>
              ))}
            </div>

            <div>
              <div className="font-semibold text-navy mb-1">AI要約（参考）</div>
              {sel.digest ? (
                <div className="bg-slate-50 rounded p-2">
                  <div>{sel.digest.summary}</div>
                  <div className="text-xs text-slate-500 mt-2">
                    感情傾向: 肯定 {sel.digest.sentiment.positive ?? 0} / 中立{" "}
                    {sel.digest.sentiment.neutral ?? 0} / 否定 {sel.digest.sentiment.negative ?? 0}
                  </div>
                  {sel.digest.themes.length > 0 && (
                    <>
                      <div className="text-xs text-slate-500 mt-2">分類</div>
                      <ul className="list-disc ml-5">
                        {sel.digest.themes.map((t, i) => (
                          <li key={i}>
                            {t.label}（{t.count}件）
                            {t.examples.length > 0 && (
                              <span className="text-slate-500">：{t.examples.join(" / ")}</span>
                            )}
                          </li>
                        ))}
                      </ul>
                    </>
                  )}
                  {sel.digest.improvements.length > 0 && (
                    <>
                      <div className="text-xs text-slate-500 mt-2">改善提案</div>
                      <ul className="list-disc ml-5 text-amber-700">
                        {sel.digest.improvements.map((x, i) => (
                          <li key={i}>{x}</li>
                        ))}
                      </ul>
                    </>
                  )}
                  <div className="text-xs text-slate-400 mt-2">
                    ※ AIの要約です。判断は原文（下記）も確認のうえ行ってください。
                  </div>
                </div>
              ) : (
                <p className="text-slate-500">未生成です。「自由記述をAIで要約」を押してください。</p>
              )}

              <div className="font-semibold text-navy mt-3 mb-1">自由記述（原文）</div>
              {agg.texts.map((t) => (
                <div key={t.question_id} className="border-t py-2">
                  <div className="text-xs text-slate-500">{t.title}</div>
                  <ul className="list-disc ml-5">
                    {t.answers.map((a, i) => (
                      <li key={i}>{a}</li>
                    ))}
                  </ul>
                  {t.answers.length === 0 && <span className="text-slate-400 text-xs">回答なし</span>}
                </div>
              ))}
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}
