import { useEffect, useState } from "react";
import { api } from "../api/client";
import {
  Badge,
  Button,
  EmptyState,
  ErrorNote,
  Loading,
  PageHeader,
  Panel,
  Segmented,
  Stat,
  fmtDateTime,
  useUi,
} from "../components/ui";

// 記録: AI の回答（docs/admin-ui-design.md §3・§4「距離」は関連度 高/中/低 で出す）

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

type Source = { chunk_id: string; title: string | null; scope_type: string | null; distance: number | null };

type Filter = "" | "down" | "ungrounded" | "escalated" | "unrated" | "up";
const FILTERS: { value: Filter; label: string }[] = [
  { value: "", label: "すべて" },
  { value: "down", label: "👎 違う" },
  { value: "ungrounded", label: "根拠なし" },
  { value: "escalated", label: "人の対応が必要" },
  { value: "unrated", label: "未評価" },
  { value: "up", label: "👍 役に立った" },
];

const SCOPE: Record<string, string> = {
  user: "個人のトーク",
  group: "グループ",
  room: "複数人トーク",
};

/** 近さ（小さいほど関連が強い）を 高・中・低 にする。数値そのものは画面に出さない。 */
function relevance(distance: number | null | undefined): { label: string; tone: "green" | "blue" | "gray" } | null {
  if (distance == null || Number.isNaN(distance)) return null;
  if (distance < 0.3) return { label: "関連度 高", tone: "green" };
  if (distance < 0.45) return { label: "関連度 中", tone: "blue" };
  return { label: "関連度 低", tone: "gray" };
}

function sources(a: Answer): Source[] {
  try {
    return a.sources ? (JSON.parse(a.sources) as Source[]) : [];
  } catch {
    return [];
  }
}

function RatingBadge({ rating }: { rating: Answer["rating"] }) {
  if (rating === "up") return <Badge tone="green">👍 役に立った</Badge>;
  if (rating === "down") return <Badge tone="red">👎 違う</Badge>;
  return <Badge tone="gray">未評価</Badge>;
}

export default function AiAnswers() {
  const { toast } = useUi();
  const [rows, setRows] = useState<Answer[] | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [filter, setFilter] = useState<Filter>("");
  const [notReady, setNotReady] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api<Summary>("/ai/summary")
      .then(setSummary)
      .catch((e) => {
        if (String(e.message).includes("503")) setNotReady(true);
        else setErr(e.message);
      });
  }, []);

  useEffect(() => {
    setRows(null);
    const q = filter ? `?filter=${filter}&limit=200` : "?limit=200";
    api<Answer[]>(`/ai/answers${q}`)
      .then(setRows)
      .catch(() => setRows([]));
  }, [filter]);

  async function downloadCsv() {
    const res = await fetch("/api/ai/answers.csv", { credentials: "include" });
    if (!res.ok) {
      toast(`CSV を保存できませんでした（${res.status}）`, "error");
      return;
    }
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = "ai_answers.csv";
    a.click();
    URL.revokeObjectURL(url);
    toast("評価のデータを CSV で保存しました");
  }

  if (notReady) {
    return (
      <div>
        <PageHeader title="AI の回答" />
        <Panel>
          <EmptyState title="まだ AI の回答の記録を表示できません">
            LINE のやり取りを保存する準備が終わると、ここに表示されます。
          </EmptyState>
        </Panel>
      </div>
    );
  }

  return (
    <div>
      <PageHeader
        title="AI の回答"
        description="会員からの質問に AI がどう答えたかの振り返りです。違う回答は内容を直すきっかけにしてください。"
        actions={<Button onClick={downloadCsv}>評価のデータを CSV で保存</Button>}
      />

      {err && <ErrorNote>{err}</ErrorNote>}

      {summary && (
        <>
          <div className="mb-2 text-xs font-bold text-ink-sub">直近30日</div>
          <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            <Stat
              label="回答数"
              value={summary.answers}
              unit="件"
              sub={summary.used_line_sources ? `LINE の発言を参考 ${summary.used_line_sources}件` : undefined}
            />
            <Stat label="👍 役に立った" value={summary.up} unit="件" />
            <Stat label="👎 違う" value={summary.down} unit="件" highlight={summary.down > 0} sub={summary.down > 0 ? "内容を確認してください" : undefined} />
            <Stat label="未評価" value={summary.unrated} unit="件" />
            <Stat label="根拠なし" value={summary.ungrounded} unit="件" sub="資料が見つからなかった回答" />
            <Stat label="人の対応が必要" value={summary.escalated} unit="件" sub="［意見箱に送る］を付けた回答" />
          </div>
        </>
      )}

      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <Segmented value={filter} options={FILTERS} onChange={setFilter} />
        {rows && rows.length > 0 && (
          <span className="num text-xs text-ink-sub">
            {rows.length}件{rows.length >= 200 ? "（新しい順に200件まで）" : ""}
          </span>
        )}
      </div>

      {rows === null ? (
        <Loading />
      ) : rows.length === 0 ? (
        <Panel>
          <EmptyState title="該当する回答はありません">
            {filter ? "ほかの切り替えも見てください。" : "会員が LINE で質問すると、ここに AI の回答が並びます。"}
          </EmptyState>
        </Panel>
      ) : (
        <ul className="space-y-3">
          {rows.map((a) => (
            <li key={a.answer_id}>
              <AnswerCard a={a} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 回答のカード
function AnswerCard({ a }: { a: Answer }) {
  const src = sources(a);
  return (
    <article className="rounded-card bg-white px-5 py-4">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <span className="num text-sm font-bold">{fmtDateTime(a.answered_at)}</span>
        <RatingBadge rating={a.rating} />
        {!a.grounded && <Badge tone="amber">根拠なし</Badge>}
        {a.needs_human && <Badge tone="blue">人の対応が必要</Badge>}
      </div>

      {/* 会員の質問（左）と AI の回答（右）を LINE の吹き出しで */}
      <div className="flex justify-start">
        <div className="max-w-[90%] sm:max-w-[75%]">
          <div className="mb-0.5 px-1 text-xs text-ink-sub">会員の質問</div>
          <div className="whitespace-pre-wrap break-words rounded-2xl rounded-tl-md bg-gray-100 px-3.5 py-2 text-sm leading-relaxed">
            {a.question}
          </div>
        </div>
      </div>
      <div className="mt-2 flex justify-end">
        <div className="max-w-[90%] sm:max-w-[75%]">
          <div className="mb-0.5 px-1 text-right text-xs text-ink-sub">AI の回答</div>
          <div className="whitespace-pre-wrap break-words rounded-2xl rounded-tr-md bg-line-soft px-3.5 py-2 text-sm leading-relaxed">
            {a.answer}
          </div>
        </div>
      </div>

      <div className="mt-3 rounded-2xl bg-gray-50 px-4 py-3">
        <div className="mb-1 text-xs font-bold text-ink-sub">参考にした情報</div>
        {src.length === 0 ? (
          <div className="text-sm text-ink-sub">
            {a.grounded ? "（記録がありません）" : "参考にできる資料が見つかりませんでした。"}
          </div>
        ) : (
          <ol className="space-y-1">
            {src.map((s, i) => {
              const r = relevance(s.distance);
              return (
                <li key={s.chunk_id ?? i} className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
                  <span className="num w-5 shrink-0 text-xs text-ink-faint">{i + 1}.</span>
                  <span className="min-w-0 break-words">{s.title ?? "LINE の発言"}</span>
                  {s.scope_type && SCOPE[s.scope_type] && (
                    <span className="text-xs text-ink-sub">（{SCOPE[s.scope_type]}）</span>
                  )}
                  {r && <Badge tone={r.tone}>{r.label}</Badge>}
                </li>
              );
            })}
          </ol>
        )}
      </div>
    </article>
  );
}
