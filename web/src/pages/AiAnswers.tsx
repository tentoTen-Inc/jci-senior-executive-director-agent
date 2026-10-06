import { useEffect, useState } from "react";
import { api } from "../api/client";
import { Card } from "../components/Card";

type Answer = {
  answer_id: string;
  answered_at: string;
  question: string;
  answer: string;
  sources: string | null;
  source_count: number;
  grounded: boolean;
  needs_human: boolean;
  rating: "up" | "down" | null;
  model: string | null;
};

type Summary = {
  answers: number;
  up: number;
  down: number;
  unrated: number;
  ungrounded: number;
  escalated: number;
  used_line_sources: number;
};

type Source = { chunk_id: string; title: string | null; scope_type: string | null; distance: number };

const FILTERS = [
  { key: "", label: "すべて" },
  { key: "down", label: "👎 違う" },
  { key: "ungrounded", label: "根拠なし" },
  { key: "escalated", label: "取次あり" },
  { key: "unrated", label: "未評価" },
  { key: "up", label: "👍 役に立った" },
];

function when(iso: string): string {
  return new Date(iso).toLocaleString("ja-JP", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Asia/Tokyo",
  });
}

function sources(a: Answer): Source[] {
  try {
    return a.sources ? (JSON.parse(a.sources) as Source[]) : [];
  } catch {
    return [];
  }
}

export default function AiAnswers() {
  const [rows, setRows] = useState<Answer[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [filter, setFilter] = useState("");
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    api<Summary>("/ai/summary")
      .then(setSummary)
      .catch((e) =>
        setMsg(
          String(e.message).includes("503")
            ? "データレイクがまだ有効になっていません。"
            : e.message
        )
      );
  }, []);

  useEffect(() => {
    const q = filter ? `?filter=${filter}&limit=200` : "?limit=200";
    api<Answer[]>(`/ai/answers${q}`).then(setRows).catch(() => {});
  }, [filter]);

  async function downloadCsv() {
    const res = await fetch("/api/ai/answers.csv", { credentials: "include" });
    if (!res.ok) {
      setMsg(`CSVを出力できませんでした (${res.status})`);
      return;
    }
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = "ai_answers.csv";
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div>
      <h1 className="text-lg font-semibold text-navy mb-3">AI応答の振り返り</h1>
      {msg && <div className="mb-2 text-sm text-brand">{msg}</div>}

      {summary && (
        <Card title="直近30日">
          <div className="grid grid-cols-3 md:grid-cols-7 gap-2 text-center text-sm">
            {[
              ["回答", summary.answers],
              ["👍", summary.up],
              ["👎", summary.down],
              ["未評価", summary.unrated],
              ["根拠なし", summary.ungrounded],
              ["取次", summary.escalated],
              ["LINE資料を使用", summary.used_line_sources],
            ].map(([label, n]) => (
              <div key={label as string} className="rounded bg-slate-50 py-2">
                <div className="text-xs text-slate-500">{label}</div>
                <div className="text-lg font-semibold text-navy">{n}</div>
              </div>
            ))}
          </div>
        </Card>
      )}

      <Card title="回答一覧（新しい順）">
        <div className="flex flex-wrap gap-2 items-center mb-3">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              className={`text-xs rounded px-2 py-1 ${
                filter === f.key ? "bg-brand text-white" : "bg-slate-200 text-navy"
              }`}
              onClick={() => setFilter(f.key)}
            >
              {f.label}
            </button>
          ))}
          <button
            className="ml-auto text-xs bg-slate-200 text-navy rounded px-2 py-1"
            onClick={downloadCsv}
          >
            評価データをCSV出力
          </button>
        </div>

        {rows.length === 0 ? (
          <p className="text-slate-500 text-sm">該当する回答はありません。</p>
        ) : (
          <ul className="space-y-3">
            {rows.map((a) => (
              <li key={a.answer_id} className="border-t pt-2 text-sm">
                <div className="text-xs text-slate-500 flex flex-wrap gap-2">
                  <span>{when(a.answered_at)}</span>
                  {a.rating === "up" && <span>👍</span>}
                  {a.rating === "down" && <span className="text-red-600">👎</span>}
                  {!a.grounded && <span className="text-amber-700">根拠なし</span>}
                  {a.needs_human && <span className="text-amber-700">事務局へ取次</span>}
                </div>
                <div className="mt-1">
                  <span className="text-xs text-slate-500 mr-1">質問</span>
                  {a.question}
                </div>
                <div className="mt-1 whitespace-pre-wrap rounded bg-blue-50 px-2 py-1">{a.answer}</div>
                {sources(a).length > 0 && (
                  <div className="mt-1 text-xs text-slate-500">
                    出典:{" "}
                    {sources(a).map((s, i) => (
                      <span key={s.chunk_id} className="mr-2">
                        [{i + 1}] {s.title ?? "LINEの発言"}（距離 {s.distance?.toFixed(2)}）
                      </span>
                    ))}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
