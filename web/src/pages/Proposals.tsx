import { useEffect, useState } from "react";
import { api } from "../api/client";
import { Card } from "../components/Card";

type FormatCheck = { passed: boolean; issues: string[]; checked_at?: string | null } | null;
type LlmReview = { summary: string; points: string[]; concerns: string[]; reviewed_at?: string | null } | null;
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

const COMMITTEE_MATRIX_STAGES = [
  { key: "entry", label: "エントリー" },
  { key: "submitted", label: "資料提出" },
  { key: "other", label: "その他" },
] as const;

type MatrixGridRow = {
  committee: string;
  counts: Record<string, number>;
  samples: Record<string, string[]>;
  latest: Record<string, string | null>;
};

type MatrixResponse = {
  committees: string[];
  deadlines: { entry: string | null; submit: string | null; deliver: string | null };
  proposals: Proposal[];
  grid: MatrixGridRow[];
};

export default function Proposals() {
  const [items, setItems] = useState<Proposal[]>([]);
  const [sel, setSel] = useState<Proposal | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [form, setForm] = useState({ title: "", committee: "", content: "" });
  const [folderId, setFolderId] = useState("");
  const [matrix, setMatrix] = useState<MatrixResponse | null>(null);

  const load = () => {
    api<Proposal[]>("/proposals")
      .then(setItems)
      .catch((e) => setMsg(e.message));
    api<MatrixResponse>("/proposals/matrix")
      .then(setMatrix)
      .catch((e) => setMsg(e.message));
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

  const committees = Array.from(new Set(items.map((p) => p.committee || "(未設定)")));

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

      {committees.length > 0 && (
        <Card title="委員会別 件数">
          <table className="text-sm">
            <tbody>
              {committees.map((c) => (
                <tr key={c} className="border-t">
                  <td className="py-1 pr-4">{c}</td>
                  <td>{items.filter((p) => (p.committee || "(未設定)") === c).length} 件</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      {matrix && matrix.grid.length > 0 && (
        <Card title="委員会 × 締切状態">
          <div className="overflow-x-auto">
            <table className="text-sm border-collapse">
              <thead>
                <tr className="border-t">
                  <th className="text-left py-1 pr-3">委員会</th>
                  {COMMITTEE_MATRIX_STAGES.map(({ key, label }) => (
                    <th key={key} className="text-center py-1 px-3">
                      {label}
                      {matrix.deadlines[key as keyof typeof matrix.deadlines] && (
                        <div className="text-xs font-normal text-slate-500">
                          〆{new Date(matrix.deadlines[key as keyof typeof matrix.deadlines] as string).toLocaleDateString("ja-JP")}
                        </div>
                      )}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {matrix.grid.map((row) => (
                  <tr key={row.committee}>
                    <td className="py-1 pr-3">{row.committee}</td>
                    {COMMITTEE_MATRIX_STAGES.map(({ key }) => {
                      const count = row.counts[key] ?? 0;
                      const sample = (row.samples[key] ?? []).join(" / ");
                      return (
                        <td key={key} className="text-center py-1 px-3 border-t">
                          <div className="font-medium">{count} 件</div>
                          {!!sample && <div className="text-xs text-slate-500 truncate">{sample}</div>}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
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
