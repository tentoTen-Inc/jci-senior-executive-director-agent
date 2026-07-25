import { useEffect, useState } from "react";
import { api } from "../api/client";
import { Card } from "../components/Card";

type FormatCheck = { passed: boolean; issues: string[] } | null;
type LlmReview = { summary: string; points: string[]; concerns: string[] } | null;
type SedApproval = { status: string; by: string | null; comment: string | null };
type Proposal = {
  proposal_id: string;
  title: string;
  number: string | null;
  committee: string | null;
  stage: string;
  doc_type: string;
  content: string | null;
  format_check: FormatCheck;
  llm_review: LlmReview;
  sed_approval: SedApproval;
};

type CellState = "done" | "pending" | "unset" | "soon" | "overdue";
type MatrixCell = {
  kind: string;
  total: number;
  counts: Record<CellState, number>;
  state: CellState;
  next_deadline: string | null;
  samples: string[];
};
type Matrix = {
  kinds: { key: string; label: string }[];
  rows: { committee: string; total: number; cells: Record<string, MatrixCell> }[];
  totals: Record<string, Record<CellState, number>>;
};

// セル=状態色（docs/dashboard-design.md §3.2）
const CELL_STYLE: Record<CellState, { cls: string; label: string }> = {
  overdue: { cls: "bg-red-100 text-red-700", label: "遅延" },
  soon: { cls: "bg-amber-100 text-amber-800", label: "締切間近" },
  pending: { cls: "bg-white text-slate-600", label: "期限内" },
  unset: { cls: "bg-slate-100 text-slate-500", label: "締切未設定" },
  done: { cls: "bg-green-50 text-green-700", label: "完了" },
};

const STAGES: { key: string; label: string }[] = [
  { key: "entry", label: "エントリー" },
  { key: "submitted", label: "資料提出" },
  { key: "sed_review", label: "専務レビュー" },
  { key: "goyaku", label: "五役会" },
  { key: "board", label: "理事会上程" },
  { key: "decided", label: "審議結果" },
  { key: "executing", label: "事業実施" },
  { key: "reported", label: "報告" },
  { key: "verified", label: "検証" },
];

export default function Proposals() {
  const [items, setItems] = useState<Proposal[]>([]);
  const [sel, setSel] = useState<Proposal | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [form, setForm] = useState({ title: "", committee: "", content: "" });
  const [folderId, setFolderId] = useState("");
  const [matrix, setMatrix] = useState<Matrix | null>(null);

  const load = () => {
    api<Proposal[]>("/proposals").then(setItems).catch((e) => setMsg(e.message));
    api<Matrix>("/proposals/matrix").then(setMatrix).catch((e) => setMsg(e.message));
  };
  useEffect(() => {
    load();
  }, []);

  async function create() {
    if (!form.title.trim()) return;
    await api("/proposals", { method: "POST", body: JSON.stringify(form) });
    setForm({ title: "", committee: "", content: "" });
    load();
  }

  async function importDrive(dryRun: boolean) {
    if (!folderId.trim()) {
      setMsg("DriveフォルダIDを入力してください。");
      return;
    }
    try {
      const r = await api<{ total: number; created: number; updated: number; dry_run: boolean }>(
        "/proposals/import-drive",
        { method: "POST", body: JSON.stringify({ folder_id: folderId.trim(), dry_run: dryRun }) }
      );
      setMsg(
        `${r.dry_run ? "[確認]" : "[取込]"} 対象${r.total}件 / 新規${r.created} 更新${r.updated}`
      );
      if (!dryRun) load();
    } catch (e) {
      setMsg((e as Error).message);
    }
  }

  async function act(id: string, path: string, body?: object) {
    try {
      await api(`/proposals/${id}/${path}`, {
        method: "POST",
        body: body ? JSON.stringify(body) : undefined,
      });
      const fresh = await api<Proposal>(`/proposals/${id}`);
      setSel(fresh);
      load();
    } catch (e) {
      setMsg((e as Error).message);
    }
  }

  return (
    <div>
      <h1 className="text-lg font-semibold text-navy mb-3">議案ライフサイクル</h1>
      {msg && <div className="mb-2 text-sm text-red-600">{msg}</div>}

      <Card title="議案を追加">
        <div className="flex flex-wrap gap-2 items-end text-sm">
          <input
            className="border rounded p-1 flex-1 min-w-[180px]"
            placeholder="議案名"
            value={form.title}
            onChange={(e) => setForm({ ...form, title: e.target.value })}
          />
          <input
            className="border rounded p-1 w-40"
            placeholder="委員会"
            value={form.committee}
            onChange={(e) => setForm({ ...form, committee: e.target.value })}
          />
          <button className="bg-brand text-white rounded px-3 py-1" onClick={create}>
            追加
          </button>
        </div>
        <div className="flex flex-wrap gap-2 items-end text-sm mt-3 pt-3 border-t">
          <input
            className="border rounded p-1 flex-1 min-w-[220px]"
            placeholder="DriveフォルダID（議案資料の入ったフォルダ）"
            value={folderId}
            onChange={(e) => setFolderId(e.target.value)}
          />
          <button className="bg-slate-200 text-navy rounded px-3 py-1" onClick={() => importDrive(true)}>
            取込確認(dry-run)
          </button>
          <button className="bg-brand text-white rounded px-3 py-1" onClick={() => importDrive(false)}>
            Driveから取込
          </button>
        </div>
        <p className="text-xs text-slate-400 mt-1">
          ※ フォルダを drive-reader@jci-sed-agent.iam.gserviceaccount.com に共有しておく必要があります。
        </p>
      </Card>

      <Card title="カンバン">
        <div className="overflow-x-auto">
          <div className="flex gap-2 min-w-max">
            {STAGES.map((s) => {
              const cards = items.filter((p) => p.stage === s.key);
              return (
                <div key={s.key} className="w-40 shrink-0 bg-slate-50 rounded p-2">
                  <div className="text-xs font-semibold text-navy mb-1">
                    {s.label} ({cards.length})
                  </div>
                  {cards.map((p) => (
                    <button
                      key={p.proposal_id}
                      onClick={() => setSel(p)}
                      className="block w-full text-left bg-white rounded shadow-sm p-2 mb-1 text-xs hover:ring-1 ring-brand"
                    >
                      <div className="font-medium truncate">{p.title}</div>
                      <div className="text-slate-500">{p.committee || "-"}</div>
                      {p.format_check && (
                        <span className={p.format_check.passed ? "text-green-600" : "text-red-600"}>
                          形式{p.format_check.passed ? "OK" : `NG(${p.format_check.issues.length})`}
                        </span>
                      )}
                    </button>
                  ))}
                </div>
              );
            })}
          </div>
        </div>
      </Card>

      {matrix && matrix.rows.length > 0 && (
        <Card title="委員会別 提出マトリクス">
          <div className="overflow-x-auto">
            <table className="text-sm w-full min-w-[560px]">
              <thead>
                <tr className="text-xs text-slate-500">
                  <th className="text-left font-medium py-1 pr-3">委員会</th>
                  <th className="font-medium py-1 px-2">件数</th>
                  {matrix.kinds.map((k) => (
                    <th key={k.key} className="font-medium py-1 px-2">
                      {k.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {matrix.rows.map((row) => (
                  <tr key={row.committee} className="border-t align-top">
                    <td className="py-1 pr-3">{row.committee}</td>
                    <td className="text-center py-1 px-2 text-slate-500">{row.total}</td>
                    {matrix.kinds.map((k) => {
                      const cell = row.cells[k.key];
                      const style = CELL_STYLE[cell.state];
                      return (
                        <td key={k.key} className="py-1 px-1">
                          <div className={`rounded p-1 ${style.cls}`} title={cell.samples.join(" / ")}>
                            <div className="text-xs font-medium">
                              {style.label}
                              {cell.state !== "done" && cell.counts[cell.state] > 1 &&
                                ` ${cell.counts[cell.state]}件`}
                            </div>
                            {cell.next_deadline && (
                              <div className="text-xs opacity-75">
                                次〆{new Date(cell.next_deadline).toLocaleDateString("ja-JP", {
                                  month: "numeric",
                                  day: "numeric",
                                })}
                              </div>
                            )}
                            <div className="text-xs opacity-60 truncate">
                              完了 {cell.counts.done}/{cell.total}
                            </div>
                          </div>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-slate-400 mt-2">
            列=締切区分（エントリー/提出/配信）。セル色は最も深刻な状態（遅延＞締切間近＞締切未設定＞期限内＞完了）。
            進行中（open）の議案のみ集計。
          </p>
        </Card>
      )}

      {sel && (
        <Card title={`議案詳細: ${sel.title}`}>
          <div className="text-sm space-y-2">
            <div>
              委員会: {sel.committee || "-"} / ステージ: {sel.stage} / 承認:{" "}
              <b>{sel.sed_approval.status}</b>
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                className="bg-slate-200 text-navy rounded px-2 py-1 text-xs"
                onClick={() => act(sel.proposal_id, "format-check")}
              >
                形式チェック
              </button>
              <button
                className="bg-slate-200 text-navy rounded px-2 py-1 text-xs"
                onClick={() => act(sel.proposal_id, "llm-review")}
              >
                AIレビュー
              </button>
              <button
                className="bg-green-600 text-white rounded px-2 py-1 text-xs"
                onClick={() => act(sel.proposal_id, "approve", { comment: "" })}
              >
                承認
              </button>
              <button
                className="bg-red-600 text-white rounded px-2 py-1 text-xs"
                onClick={() => act(sel.proposal_id, "return", { comment: "" })}
              >
                差戻し
              </button>
              <button
                className="text-slate-500 underline text-xs"
                onClick={() => setSel(null)}
              >
                閉じる
              </button>
            </div>

            {sel.format_check && (
              <div>
                <div className="font-semibold">
                  形式チェック: {sel.format_check.passed ? "OK" : "要修正"}
                </div>
                <ul className="list-disc ml-5 text-red-600">
                  {sel.format_check.issues.map((i, idx) => (
                    <li key={idx}>{i}</li>
                  ))}
                </ul>
              </div>
            )}

            {sel.llm_review && (
              <div className="bg-slate-50 rounded p-2">
                <div className="font-semibold">AIレビュー（助言）</div>
                <div>要約: {sel.llm_review.summary}</div>
                <div className="mt-1">論点:</div>
                <ul className="list-disc ml-5">
                  {sel.llm_review.points.map((p, i) => (
                    <li key={i}>{p}</li>
                  ))}
                </ul>
                <div className="mt-1">要確認点:</div>
                <ul className="list-disc ml-5 text-amber-700">
                  {sel.llm_review.concerns.map((p, i) => (
                    <li key={i}>{p}</li>
                  ))}
                </ul>
                <div className="text-xs text-slate-400 mt-1">※最終判断は専務理事が行います</div>
              </div>
            )}
          </div>
        </Card>
      )}
    </div>
  );
}
