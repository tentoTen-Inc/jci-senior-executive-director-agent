import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import {
  Badge,
  Button,
  EmptyState,
  ErrorNote,
  Field,
  Loading,
  PageHeader,
  Panel,
  Stat,
  fmtDateTime,
  useUi,
} from "../components/ui";

// 議案（docs/admin-ui-design.md §3）: 段階ごとのカンバン・詳細・Drive からの取り込み

type FormatCheck = { passed: boolean; issues: string[]; checked_at?: string | null } | null;
type LlmReview = { summary: string; points: string[]; concerns: string[]; reviewed_at?: string | null } | null;
type SedApproval = { status: string; by: string | null; at?: string | null; comment: string | null };
type Deadlines = { entry: string | null; submit: string | null; deliver: string | null };
type History = { at: string; stage: string; by: string };
type Proposal = {
  proposal_id: string;
  title: string;
  number: string | null;
  committee: string | null;
  stage: string;
  doc_type: string;
  content: string | null;
  storage_uri: string | null;
  deadlines: Deadlines;
  format_check: FormatCheck;
  llm_review: LlmReview;
  sed_approval: SedApproval;
  history: History[];
  status: string;
  // 事業計画書（作業文書）から同期した内容（docs/proposal-ledger-design.md §4）
  doc_id?: string | null;
  synced_at?: string | null;
  basics?: Record<string, string>;
  sections?: Record<string, string>;
  feedback?: { meeting: string; kind: string; no: number; person: string | null; text: string }[];
  event_date?: string | null;
  venue?: string | null;
  participants?: string | null;
  // 上程先の会議（日程調整中なら「〇〇（日程調整中）」）と空欄の欄の数（サーバーが添える）
  meeting_label?: string | null;
  meeting_poll_id?: string | null;
  blank_count?: number | null;
};
const SUMMARY_TYPE = "年間計画（サマリー）";

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
type ImportResult = {
  total: number;
  created: number;
  updated: number;
  dry_run: boolean;
  items: { name: string; action: string }[];
};

type Tone = "green" | "gray" | "red" | "amber" | "blue";

const STAGES: { key: string; label: string; hint: string }[] = [
  { key: "entry", label: "エントリー", hint: "委員会が議案を出すと宣言した段階" },
  { key: "submitted", label: "資料提出", hint: "議案書が届いた段階" },
  { key: "sed_review", label: "専務レビュー", hint: "専務が内容を確認する段階" },
  { key: "goyaku", label: "五役会", hint: "五役会で協議する段階" },
  { key: "board", label: "理事会上程", hint: "理事会に出す段階" },
  { key: "decided", label: "審議結果", hint: "理事会の結果が出た段階" },
  { key: "executing", label: "事業実施", hint: "事業を行っている段階" },
  { key: "reported", label: "報告", hint: "事業の報告を出した段階" },
  { key: "verified", label: "検証", hint: "振り返りまで終わった段階" },
];
const STAGE_INDEX: Record<string, number> = Object.fromEntries(STAGES.map((s, i) => [s.key, i]));
const stageLabel = (key: string) => STAGES[STAGE_INDEX[key]]?.label ?? "（段階不明）";

// 締切区分と、それを「済み」とみなす段階（app/proposal_matrix.py と同じ考え方）
const DEADLINE_KINDS: { key: keyof Deadlines; label: string; doneFrom: string }[] = [
  { key: "entry", label: "エントリー", doneFrom: "submitted" },
  { key: "submit", label: "資料提出", doneFrom: "sed_review" },
  { key: "deliver", label: "配信", doneFrom: "board" },
];
const SOON_DAYS = 3;

const CELL: Record<CellState, { tone: Tone; label: string }> = {
  overdue: { tone: "red", label: "遅れ" },
  soon: { tone: "amber", label: "締切間近" },
  pending: { tone: "blue", label: "期限内" },
  unset: { tone: "gray", label: "締切未設定" },
  done: { tone: "green", label: "済み" },
};

const APPROVAL: Record<string, { label: string; tone: Tone }> = {
  pending: { label: "未判断", tone: "gray" },
  approved: { label: "承認済み", tone: "green" },
  returned: { label: "差し戻し", tone: "red" },
};

/** 次に来る（まだ済んでいない）締切。締切が無ければ null。 */
function nextDeadline(p: Proposal): { label: string; due: string; state: "overdue" | "soon" | "pending" } | null {
  const idx = STAGE_INDEX[p.stage] ?? 0;
  for (const k of DEADLINE_KINDS) {
    if (idx >= STAGE_INDEX[k.doneFrom]) continue;
    const due = p.deadlines?.[k.key];
    if (!due) return null;
    const ms = new Date(due).getTime() - Date.now();
    const state = ms < 0 ? "overdue" : ms <= SOON_DAYS * 86400000 ? "soon" : "pending";
    return { label: k.label, due, state };
  }
  return null;
}

function DeadlineBadge({ p }: { p: Proposal }) {
  const d = nextDeadline(p);
  if (!d) return null;
  const tone: Tone = d.state === "overdue" ? "red" : d.state === "soon" ? "amber" : "gray";
  return (
    <Badge tone={tone}>
      {d.label} 〆{fmtDateTime(d.due, false)}
      {d.state === "overdue" ? " 過ぎ" : ""}
    </Badge>
  );
}

type Rule = [RegExp, string];
const COMMON_RULES: Rule[] = [
  [/ガードレール/, "安全のため送信を止めました（送信の一時停止中・静音時間・送信の上限など）。設定と送信の記録を確認してください。"],
  [/not found/i, "対象の議案が見つかりませんでした。画面を読み込み直してください。"],
];
/** サーバーのエラーのうち、開発用の言葉を含むものを画面向けの言葉にする（それ以外はそのまま出す） */
function friendly(e: unknown, rules: Rule[] = []): string {
  const m = e instanceof Error ? e.message : String(e);
  for (const [re, text] of [...rules, ...COMMON_RULES]) if (re.test(m)) return text;
  return m;
}

const toDateInput = (iso: string | null | undefined) => (iso ? iso.slice(0, 10) : "");
const fromDateInput = (v: string) => (v ? `${v}T23:59:00` : null);
const actorLabel = (by: string | null | undefined) => (!by || by === "unknown" || by === "system" ? "自動処理" : by);

/** Drive フォルダの URL か ID を受け取り、ID を返す */
function folderIdFrom(input: string): string {
  const s = input.trim();
  const m = s.match(/\/folders\/([A-Za-z0-9_-]+)/) ?? s.match(/[?&]id=([A-Za-z0-9_-]+)/);
  return m ? m[1] : s;
}

function docUrl(uri: string | null): string | null {
  if (!uri) return null;
  if (/^https?:\/\//.test(uri)) return uri;
  if (/^[A-Za-z0-9_-]{20,}$/.test(uri)) return `https://docs.google.com/document/d/${uri}/edit`;
  return null;
}

// ---------------------------------------------------------------- 画面

const EMPTY_FORM = { title: "", committee: "", number: "", entry: "", submit: "", deliver: "", content: "" };

export default function Proposals() {
  const { confirm, toast } = useUi();
  const [params, setParams] = useSearchParams();
  const [items, setItems] = useState<Proposal[] | null>(null);
  const [matrix, setMatrix] = useState<Matrix | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [committee, setCommittee] = useState("");
  const [panel, setPanel] = useState<"none" | "add" | "drive">("none");
  const [showMatrix, setShowMatrix] = useState(false);

  const selId = params.get("id");
  const sel = items?.find((p) => p.proposal_id === selId) ?? null;
  const openDetail = (id: string | null) => {
    const next = new URLSearchParams(params);
    if (id) next.set("id", id);
    else next.delete("id");
    setParams(next, { replace: true });
  };

  const load = () => {
    api<Proposal[]>("/proposals")
      .then((ps) => {
        setItems(ps);
        setErr(null);
      })
      .catch((e) => setErr(friendly(e)));
    api<Matrix>("/proposals/matrix").then(setMatrix).catch(() => setMatrix(null));
  };
  useEffect(load, []);

  const committees = useMemo(
    () => [...new Set((items ?? []).map((p) => p.committee).filter((c): c is string => !!c))].sort(),
    [items]
  );
  const shown = (items ?? []).filter((p) => !committee || p.committee === committee);
  const open = (items ?? []).filter((p) => p.status !== "closed");
  const waiting = open.filter((p) => p.stage === "sed_review").length;
  const overdue = open.filter((p) => nextDeadline(p)?.state === "overdue").length;
  const soon = open.filter((p) => nextDeadline(p)?.state === "soon").length;

  if (err && !items) return <ErrorNote>{err}</ErrorNote>;
  if (!items) return <Loading />;

  return (
    <div>
      <PageHeader
        title="議案"
        description="エントリーから検証まで、議案がいまどの段階にあるかを確認できます。"
        actions={
          <>
            <Button onClick={() => setPanel(panel === "drive" ? "none" : "drive")}>Drive から取り込む</Button>
            <Button variant="primary" onClick={() => setPanel(panel === "add" ? "none" : "add")}>
              議案を追加
            </Button>
          </>
        }
      />
      {err && <ErrorNote>{err}</ErrorNote>}

      {panel === "add" && (
        <AddPanel
          committees={committees}
          onClose={() => setPanel("none")}
          onCreated={(p) => {
            setPanel("none");
            load();
            openDetail(p.proposal_id);
          }}
        />
      )}
      {panel === "drive" && <DrivePanel onClose={() => setPanel("none")} onImported={load} />}

      <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="進行中の議案" value={open.length} unit="件" />
        <Stat
          label="専務の確認待ち"
          value={waiting}
          unit="件"
          highlight={waiting > 0}
          sub={waiting ? "「専務レビュー」の列から確認できます" : "ありません"}
        />
        <Stat label="締切を過ぎた" value={overdue} unit="件" sub={overdue ? "カードに赤い印が付いています" : "ありません"} />
        <Stat label={`${SOON_DAYS}日以内に締切`} value={soon} unit="件" />
      </div>

      {items.length === 0 ? (
        <Panel>
          <EmptyState
            title="まだ議案がありません"
            action={
              <div className="flex flex-wrap justify-center gap-2">
                <Button onClick={() => setPanel("drive")}>Drive から取り込む</Button>
                <Button variant="primary" onClick={() => setPanel("add")}>議案を追加</Button>
              </div>
            }
          >
            <p>議案は「エントリー → 資料提出 → 専務レビュー → 五役会 → 理事会上程 → 審議結果 → 事業実施 → 報告 → 検証」の順に進みます。</p>
            <p className="mt-1">議案書が Google ドライブのフォルダにあれば、まとめて取り込めます。</p>
          </EmptyState>
        </Panel>
      ) : (
        <Panel
          title="段階ごとの議案"
          action={
            committees.length > 0 && (
              <select
                aria-label="委員会で絞り込む"
                value={committee}
                onChange={(e) => setCommittee(e.target.value)}
                className="!py-1.5"
              >
                <option value="">すべての委員会</option>
                {committees.map((c) => (
                  <option key={c} value={c}>{c}</option>
                ))}
              </select>
            )
          }
          flush
        >
          <div className="overflow-x-auto px-5 pb-4">
            <div className="flex min-w-max gap-3">
              {STAGES.map((s) => {
                const cards = shown.filter((p) => p.stage === s.key);
                const mine = s.key === "sed_review";
                return (
                  <div
                    key={s.key}
                    className={`w-56 shrink-0 rounded-2xl p-2.5 ${mine ? "bg-line-soft" : "bg-gray-50"}`}
                  >
                    <div className="mb-2 flex items-center justify-between px-1" title={s.hint}>
                      <span className={`text-sm font-extrabold ${mine ? "text-line-ink" : ""}`}>{s.label}</span>
                      <span className="num rounded-full bg-white px-2 py-0.5 text-xs font-bold text-ink-sub">
                        {cards.length}
                      </span>
                    </div>
                    {mine && cards.length > 0 && (
                      <div className="mb-2 px-1 text-[11px] font-bold text-line-ink">専務の確認待ち</div>
                    )}
                    <div className="space-y-2">
                      {cards.map((p) => (
                        <KanbanCard key={p.proposal_id} p={p} active={p.proposal_id === selId} onClick={() => openDetail(p.proposal_id)} />
                      ))}
                      {cards.length === 0 && <div className="px-1 py-3 text-center text-xs text-ink-faint">なし</div>}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </Panel>
      )}

      {matrix && matrix.rows.length > 0 && (
        <Panel
          title="委員会ごとの提出状況"
          action={
            <button type="button" className="font-bold text-line-ink" onClick={() => setShowMatrix(!showMatrix)}>
              {showMatrix ? "閉じる" : "表を開く"}
            </button>
          }
          flush={showMatrix}
        >
          {!showMatrix ? (
            <p className="text-sm text-ink-sub">
              委員会ごとに、エントリー・資料提出・配信の締切に間に合っているかを一覧で確認できます。
            </p>
          ) : (
            <>
              <div className="overflow-x-auto">
                <table className="tbl min-w-[560px]">
                  <thead>
                    <tr>
                      <th>委員会</th>
                      <th className="!text-right">件数</th>
                      {matrix.kinds.map((k) => (
                        <th key={k.key}>{k.key === "submit" ? "資料提出" : k.label}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {matrix.rows.map((row) => (
                      <tr key={row.committee}>
                        <td className="font-bold">{row.committee === "(未設定)" ? "委員会未設定" : row.committee}</td>
                        <td className="num text-right text-ink-sub">{row.total}</td>
                        {matrix.kinds.map((k) => {
                          const cell = row.cells[k.key];
                          const st = CELL[cell.state];
                          const n = cell.counts[cell.state];
                          return (
                            <td key={k.key} title={cell.samples.join(" / ")}>
                              <div className="flex flex-wrap items-center gap-1.5">
                                <Badge tone={st.tone}>
                                  {st.label}
                                  {cell.state !== "done" && n > 1 ? ` ${n}件` : ""}
                                </Badge>
                                {cell.next_deadline && (
                                  <span className="num text-xs text-ink-sub">〆{fmtDateTime(cell.next_deadline, false)}</span>
                                )}
                              </div>
                              <div className="num mt-0.5 text-[11px] text-ink-faint">
                                済み {cell.counts.done}/{cell.total}
                              </div>
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="px-5 pt-2 text-xs text-ink-sub">
                色は、その委員会でいちばん急ぎの状態です（遅れ ＞ 締切間近 ＞ 締切未設定 ＞ 期限内 ＞ 済み）。進行中の議案だけを数えています。
              </p>
            </>
          )}
        </Panel>
      )}

      {sel && (
        <Drawer onClose={() => openDetail(null)} title={sel.title}>
          <Detail
            key={sel.proposal_id}
            p={sel}
            committees={committees}
            onChanged={load}
            confirm={confirm}
            toast={toast}
          />
        </Drawer>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- カンバンのカード
function KanbanCard({ p, active, onClick }: { p: Proposal; active: boolean; onClick: () => void }) {
  const ap = APPROVAL[p.sed_approval?.status] ?? APPROVAL.pending;
  return (
    <button
      type="button"
      onClick={onClick}
      className={`block w-full rounded-xl bg-white px-3 py-2.5 text-left transition hover:ring-2 hover:ring-line/40 ${
        active ? "ring-2 ring-line" : ""
      }`}
    >
      <div className="line-clamp-2 text-sm font-bold leading-snug">{p.title}</div>
      <div className="mt-0.5 truncate text-xs text-ink-sub">
        {[p.number, p.committee].filter(Boolean).join("・") || "委員会未設定"}
      </div>
      {p.meeting_label && <div className="mt-0.5 truncate text-xs text-ink-sub">上程先: {p.meeting_label}</div>}
      <div className="mt-2 flex flex-wrap gap-1">
        <DeadlineBadge p={p} />
        {p.sed_approval?.status !== "pending" && <Badge tone={ap.tone}>{ap.label}</Badge>}
        {p.format_check &&
          (p.format_check.passed ? (
            <Badge tone="green">形式OK</Badge>
          ) : (
            <Badge tone="amber">要修正 {p.format_check.issues.length}</Badge>
          ))}
        {p.llm_review && <Badge tone="blue">AI確認済み</Badge>}
      </div>
    </button>
  );
}

// ---------------------------------------------------------------- 右から出る詳細
function Drawer({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/20" onClick={onClose}>
      <aside
        role="dialog"
        aria-label={title}
        className="flex h-full w-full max-w-[600px] flex-col bg-canvas"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3 bg-white px-5 py-4">
          <h2 className="text-lg font-extrabold leading-snug">{title}</h2>
          <Button variant="ghost" size="sm" onClick={onClose} aria-label="閉じる">
            閉じる
          </Button>
        </div>
        <div className="flex-1 overflow-y-auto p-4">{children}</div>
      </aside>
    </div>
  );
}

// ---------------------------------------------------------------- 詳細
type UiFns = ReturnType<typeof useUi>;

function Detail({
  p,
  committees,
  onChanged,
  confirm,
  toast,
}: {
  p: Proposal;
  committees: string[];
  onChanged: () => void;
  confirm: UiFns["confirm"];
  toast: UiFns["toast"];
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [comment, setComment] = useState("");
  const [stage, setStage] = useState(p.stage);
  const [editing, setEditing] = useState(false);
  useEffect(() => setStage(p.stage), [p.stage]);

  const idx = STAGE_INDEX[p.stage] ?? 0;
  const next = STAGES[idx + 1];
  const ap = APPROVAL[p.sed_approval?.status] ?? APPROVAL.pending;
  const url = docUrl(p.storage_uri);

  async function run(label: string, path: string, body?: object, done?: string, errMap?: Rule[]) {
    setBusy(label);
    try {
      await api(`/proposals/${p.proposal_id}/${path}`, {
        method: "POST",
        body: body ? JSON.stringify(body) : undefined,
      });
      if (done) toast(done);
      onChanged();
      return true;
    } catch (e) {
      toast(friendly(e, errMap), "error");
      return false;
    } finally {
      setBusy(null);
    }
  }

  async function approve() {
    const ok = await confirm({
      title: "この議案を承認しますか？",
      body:
        p.stage === "sed_review" ? (
          <>承認すると「五役会」の段階へ進みます。{comment.trim() && <>コメント「{comment.trim()}」も記録します。</>}</>
        ) : (
          <>いまの段階（{stageLabel(p.stage)}）はそのままで、承認したことを記録します。</>
        ),
      confirmLabel: "承認する",
    });
    if (!ok) return;
    if (await run("approve", "approve", { comment: comment.trim() || null }, "承認しました")) setComment("");
  }

  async function sendBack() {
    const ok = await confirm({
      title: "この議案を差し戻しますか？",
      body: (
        <>
          差し戻すと「資料提出」の段階に戻ります。
          {comment.trim() ? <>コメント「{comment.trim()}」を記録します。</> : <>直してほしい点をコメントに書いておくと記録に残ります。</>}
          <br />※ 委員会への連絡は自動では送られません。
        </>
      ),
      confirmLabel: "差し戻す",
      danger: true,
    });
    if (!ok) return;
    if (await run("return", "return", { comment: comment.trim() || null }, "差し戻しました")) setComment("");
  }

  async function changeStage(to: string) {
    if (to === p.stage) return;
    const ok = await confirm({
      title: `「${stageLabel(to)}」に変えますか？`,
      body: <>いまの段階「{stageLabel(p.stage)}」から「{stageLabel(to)}」に変えます。変更は履歴に残ります。</>,
      confirmLabel: "変える",
    });
    if (!ok) {
      setStage(p.stage);
      return;
    }
    await run("stage", "stage", { stage: to }, `「${stageLabel(to)}」に変えました`);
  }

  return (
    <div className="space-y-4">
      {/* 概要 */}
      <section className="rounded-card bg-white p-5">
        <div className="flex flex-wrap items-center gap-2 text-sm text-ink-sub">
          {p.number && <span className="font-bold text-ink">{p.number}</span>}
          <span>
            {p.committee || "委員会未設定"}・{p.doc_type}
          </span>
          <DeadlineBadge p={p} />
          {p.blank_count != null && (
            <Badge tone={p.blank_count ? "amber" : "green"}>
              {p.blank_count ? `空欄 ${p.blank_count} 欄` : "すべて記入済み"}
            </Badge>
          )}
        </div>
        {p.meeting_label && <div className="mt-1 text-sm">上程先: <span className="font-bold">{p.meeting_label}</span></div>}
        {url && (
          <a href={url} target="_blank" rel="noreferrer" className="mt-2 inline-block text-sm font-bold text-line-ink hover:underline">
            議案書を開く（Google ドキュメント）
          </a>
        )}

        <div className="mt-4">
          <div className="field-label">いまの段階</div>
          <ol className="flex flex-wrap gap-1">
            {STAGES.map((s, i) => (
              <li
                key={s.key}
                className={`rounded-full px-2.5 py-0.5 text-xs font-bold ${
                  i === idx ? "bg-line text-white" : i < idx ? "bg-line-soft text-line-ink" : "bg-gray-100 text-ink-faint"
                }`}
              >
                {s.label}
              </li>
            ))}
          </ol>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            {next && (
              <Button size="sm" disabled={!!busy} onClick={() => changeStage(next.key)}>
                「{next.label}」へ進める
              </Button>
            )}
            <label className="flex items-center gap-2 text-xs text-ink-sub">
              ほかの段階に変える
              <select
                value={stage}
                disabled={!!busy}
                onChange={(e) => {
                  setStage(e.target.value);
                  changeStage(e.target.value);
                }}
                className="!py-1 !text-xs"
              >
                {STAGES.map((s) => (
                  <option key={s.key} value={s.key}>{s.label}</option>
                ))}
              </select>
            </label>
          </div>
        </div>
      </section>

      {p.doc_id && <PlanContent p={p} />}

      {/* 専務の判断 */}
      <section className="rounded-card bg-white p-5">
        <div className="mb-2 flex items-center justify-between gap-2">
          <h3 className="font-extrabold">専務の判断</h3>
          <Badge tone={ap.tone}>{ap.label}</Badge>
        </div>
        {p.sed_approval?.status !== "pending" && (
          <div className="mb-3 rounded-xl bg-gray-50 px-3 py-2 text-sm">
            <div className="text-xs text-ink-sub">
              {p.sed_approval.at ? fmtDateTime(p.sed_approval.at) : ""} {actorLabel(p.sed_approval.by)}
            </div>
            {p.sed_approval.comment ? <div className="mt-0.5">{p.sed_approval.comment}</div> : <div className="mt-0.5 text-ink-faint">コメントなし</div>}
          </div>
        )}
        <Field label="コメント（任意）" hint="差し戻すときは、直してほしい点を書いておくと記録に残ります。">
          <textarea className="h-20 w-full" value={comment} onChange={(e) => setComment(e.target.value)} />
        </Field>
        <div className="mt-3 flex flex-wrap justify-end gap-2">
          <Button variant="danger" disabled={!!busy} onClick={sendBack}>
            {busy === "return" ? "差し戻し中…" : "差し戻す"}
          </Button>
          <Button variant="primary" disabled={!!busy} onClick={approve}>
            {busy === "approve" ? "承認中…" : "承認する"}
          </Button>
        </div>
      </section>

      {/* チェック */}
      <section className="rounded-card bg-white p-5">
        <h3 className="mb-1 font-extrabold">議案書のチェック</h3>
        <p className="mb-3 text-xs text-ink-sub">
          形式チェックは必須項目と半角・全角の表記を調べます。AI の確認は内容の要約と気になる点を出します（最終判断は専務が行います）。
        </p>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" disabled={!!busy} onClick={() => run("format", "format-check", undefined, "形式チェックをしました")}>
            {busy === "format" ? "チェック中…" : p.format_check ? "形式チェックをやり直す" : "形式チェック"}
          </Button>
          <Button
            size="sm"
            disabled={!!busy}
            onClick={() =>
              run("review", "llm-review", undefined, "AI の確認が終わりました", [
                [/生成できませんでした|LLM/, "AI の確認ができませんでした。議案書の本文が空か、AI が一時的に使えない状態です。"],
              ])
            }
          >
            {busy === "review" ? "AI が読んでいます…" : p.llm_review ? "AI の確認をやり直す" : "AI に確認してもらう"}
          </Button>
        </div>

        {p.format_check && (
          <div className="mt-4">
            <div className="flex items-center gap-2 text-sm font-bold">
              形式チェック
              {p.format_check.passed ? <Badge tone="green">問題なし</Badge> : <Badge tone="amber">直す点 {p.format_check.issues.length}件</Badge>}
            </div>
            {p.format_check.issues.length > 0 && (
              <ul className="mt-1 list-disc space-y-0.5 pl-5 text-sm text-hold">
                {p.format_check.issues.map((i, k) => (
                  <li key={k}>{i.replace("本文(content)", "本文")}</li>
                ))}
              </ul>
            )}
          </div>
        )}

        {p.llm_review && (
          <div className="mt-4 rounded-xl bg-gray-50 p-4 text-sm">
            <div className="mb-1 font-bold">AI の確認（参考）</div>
            <p className="leading-relaxed">{p.llm_review.summary}</p>
            {p.llm_review.points.length > 0 && (
              <>
                <div className="mt-3 text-xs font-bold text-ink-sub">論点</div>
                <ul className="list-disc space-y-0.5 pl-5">
                  {p.llm_review.points.map((x, i) => (
                    <li key={i}>{x}</li>
                  ))}
                </ul>
              </>
            )}
            {p.llm_review.concerns.length > 0 && (
              <>
                <div className="mt-3 text-xs font-bold text-ink-sub">確認したほうがよい点</div>
                <ul className="list-disc space-y-0.5 pl-5 text-hold">
                  {p.llm_review.concerns.map((x, i) => (
                    <li key={i}>{x}</li>
                  ))}
                </ul>
              </>
            )}
            <div className="mt-3 text-xs text-ink-faint">※ AI の意見です。最終判断は専務理事が行います。</div>
          </div>
        )}
      </section>

      {/* 内容の編集 */}
      <section className="rounded-card bg-white p-5">
        <div className="flex items-center justify-between gap-2">
          <h3 className="font-extrabold">議案の情報・締切</h3>
          {!editing && (
            <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>
              編集する
            </Button>
          )}
        </div>
        {editing ? (
          <EditForm p={p} committees={committees} onDone={(saved) => { setEditing(false); if (saved) onChanged(); }} toast={toast} />
        ) : (
          <dl className="mt-2 grid grid-cols-[7rem_1fr] gap-y-1.5 text-sm">
            {DEADLINE_KINDS.map((k) => (
              <div key={k.key} className="contents">
                <dt className="text-ink-sub">{k.label}の締切</dt>
                <dd className="num">{p.deadlines?.[k.key] ? fmtDateTime(p.deadlines[k.key], false) : <span className="text-ink-faint">未設定</span>}</dd>
              </div>
            ))}
            <dt className="text-ink-sub">本文</dt>
            <dd>{p.content?.trim() ? `${p.content.trim().length.toLocaleString()}文字` : <span className="text-ink-faint">なし</span>}</dd>
          </dl>
        )}
      </section>

      {/* 履歴 */}
      {p.history?.length > 0 && (
        <section className="rounded-card bg-white p-5">
          <h3 className="mb-2 font-extrabold">これまでの流れ</h3>
          <ol className="space-y-1.5 text-sm">
            {[...p.history].reverse().map((h, i) => (
              <li key={i} className="flex flex-wrap gap-x-3">
                <span className="num w-28 shrink-0 text-ink-sub">{fmtDateTime(h.at)}</span>
                <span className="font-bold">{stageLabel(h.stage)}</span>
                <span className="text-xs text-ink-faint">{actorLabel(h.by)}</span>
              </li>
            ))}
          </ol>
        </section>
      )}
    </div>
  );
}

function EditForm({
  p,
  committees,
  onDone,
  toast,
}: {
  p: Proposal;
  committees: string[];
  onDone: (saved: boolean) => void;
  toast: UiFns["toast"];
}) {
  const [f, setF] = useState({
    title: p.title,
    number: p.number ?? "",
    committee: p.committee ?? "",
    entry: toDateInput(p.deadlines?.entry),
    submit: toDateInput(p.deadlines?.submit),
    deliver: toDateInput(p.deadlines?.deliver),
    content: p.content ?? "",
  });
  const [saving, setSaving] = useState(false);

  async function save() {
    if (!f.title.trim()) {
      toast("議案名を入力してください", "error");
      return;
    }
    setSaving(true);
    try {
      await api(`/proposals/${p.proposal_id}`, {
        method: "PUT",
        body: JSON.stringify({
          title: f.title.trim(),
          number: f.number.trim() || null,
          committee: f.committee.trim() || null,
          content: f.content,
          deadlines: {
            entry: fromDateInput(f.entry),
            submit: fromDateInput(f.submit),
            deliver: fromDateInput(f.deliver),
          },
        }),
      });
      toast("保存しました");
      onDone(true);
    } catch (e) {
      toast(friendly(e), "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="mt-3 space-y-3">
      <Field label="議案名">
        <input className="w-full" value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} />
      </Field>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="委員会">
          <input className="w-full" list="proposal-committees" value={f.committee} onChange={(e) => setF({ ...f, committee: e.target.value })} />
        </Field>
        <Field label="議案番号（任意）">
          <input className="w-full" placeholder="例: 第3号議案" value={f.number} onChange={(e) => setF({ ...f, number: e.target.value })} />
        </Field>
      </div>
      <CommitteeList committees={committees} />
      <div className="grid gap-3 sm:grid-cols-3">
        {DEADLINE_KINDS.map((k) => (
          <Field key={k.key} label={`${k.label}の締切`}>
            <input type="date" className="w-full" value={f[k.key]} onChange={(e) => setF({ ...f, [k.key]: e.target.value })} />
          </Field>
        ))}
      </div>
      <Field label="本文" hint="形式チェックと AI の確認はこの本文を読みます。">
        <textarea className="h-40 w-full" value={f.content} onChange={(e) => setF({ ...f, content: e.target.value })} />
      </Field>
      <div className="flex justify-end gap-2">
        <Button onClick={() => onDone(false)}>やめる</Button>
        <Button variant="primary" disabled={saving} onClick={save}>
          {saving ? "保存中…" : "保存する"}
        </Button>
      </div>
    </div>
  );
}

function CommitteeList({ committees }: { committees: string[] }) {
  return (
    <datalist id="proposal-committees">
      {committees.map((c) => (
        <option key={c} value={c} />
      ))}
    </datalist>
  );
}

// ---------------------------------------------------------------- 追加
function AddPanel({
  committees,
  onClose,
  onCreated,
}: {
  committees: string[];
  onClose: () => void;
  onCreated: (p: Proposal) => void;
}) {
  const { toast } = useUi();
  const [f, setF] = useState(EMPTY_FORM);
  const [saving, setSaving] = useState(false);

  async function create() {
    if (!f.title.trim()) {
      toast("議案名を入力してください", "error");
      return;
    }
    setSaving(true);
    try {
      const p = await api<Proposal>("/proposals", {
        method: "POST",
        body: JSON.stringify({
          title: f.title.trim(),
          committee: f.committee.trim() || null,
          number: f.number.trim() || null,
          content: f.content.trim() || null,
          deadlines: {
            entry: fromDateInput(f.entry),
            submit: fromDateInput(f.submit),
            deliver: fromDateInput(f.deliver),
          },
        }),
      });
      toast(`「${p.title}」を追加しました（エントリー）`);
      setF(EMPTY_FORM);
      onCreated(p);
    } catch (e) {
      toast(friendly(e), "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Panel title="議案を追加" action={<button type="button" className="font-bold text-ink-sub" onClick={onClose}>閉じる</button>}>
      <p className="mb-3 text-sm text-ink-sub">追加した議案は「エントリー」の段階から始まります。</p>
      <div className="grid gap-3 md:grid-cols-3">
        <Field label="議案名" className="md:col-span-2">
          <input className="w-full" placeholder="例: 10月例会 事業計画" value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} />
        </Field>
        <Field label="委員会">
          <input className="w-full" list="proposal-committees" value={f.committee} onChange={(e) => setF({ ...f, committee: e.target.value })} />
        </Field>
        <Field label="議案番号（任意）">
          <input className="w-full" placeholder="例: 第3号議案" value={f.number} onChange={(e) => setF({ ...f, number: e.target.value })} />
        </Field>
        {DEADLINE_KINDS.map((k) => (
          <Field key={k.key} label={`${k.label}の締切（任意）`}>
            <input type="date" className="w-full" value={f[k.key]} onChange={(e) => setF({ ...f, [k.key]: e.target.value })} />
          </Field>
        ))}
        <Field label="本文（任意）" hint="あとから Drive の取り込みや編集で入れることもできます。" className="md:col-span-3">
          <textarea className="h-24 w-full" value={f.content} onChange={(e) => setF({ ...f, content: e.target.value })} />
        </Field>
      </div>
      <CommitteeList committees={committees} />
      <div className="mt-3 flex justify-end gap-2">
        <Button onClick={onClose}>やめる</Button>
        <Button variant="primary" disabled={saving} onClick={create}>
          {saving ? "追加中…" : "追加する"}
        </Button>
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------- Drive から取り込む
function DrivePanel({ onClose, onImported }: { onClose: () => void; onImported: () => void }) {
  const { confirm, toast } = useUi();
  const [folder, setFolder] = useState("");
  const [preview, setPreview] = useState<ImportResult | null>(null);
  const [busy, setBusy] = useState<"check" | "import" | null>(null);

  const errMap: Rule[] = [
    [/Drive取込に失敗/, "フォルダを読めませんでした。URL が正しいか、フォルダの共有ができているかを確認してください（下の「連携の準備」）。"],
  ];

  async function call(dryRun: boolean) {
    const id = folderIdFrom(folder);
    if (!id) {
      toast("Drive フォルダの URL を入力してください", "error");
      return null;
    }
    return api<ImportResult>("/proposals/import-drive", {
      method: "POST",
      body: JSON.stringify({ folder_id: id, dry_run: dryRun }),
    });
  }

  async function check() {
    setBusy("check");
    try {
      const r = await call(true);
      if (r) setPreview(r);
    } catch (e) {
      toast(friendly(e, errMap), "error");
    } finally {
      setBusy(null);
    }
  }

  async function doImport() {
    const ok = await confirm({
      title: "Drive から取り込みますか？",
      body: preview ? (
        <>新しく {preview.created}件 を「資料提出」の段階で追加し、{preview.updated}件 の本文を最新にします。</>
      ) : (
        <>フォルダ内の Google ドキュメントを議案として取り込みます。同じ書類は本文だけ最新にします。</>
      ),
      confirmLabel: "取り込む",
    });
    if (!ok) return;
    setBusy("import");
    try {
      const r = await call(false);
      if (!r) return;
      toast(`取り込みました（新しく ${r.created}件・更新 ${r.updated}件）`);
      setPreview(null);
      setFolder("");
      onImported();
    } catch (e) {
      toast(friendly(e, errMap), "error");
    } finally {
      setBusy(null);
    }
  }

  return (
    <Panel title="Drive から取り込む" action={<button type="button" className="font-bold text-ink-sub" onClick={onClose}>閉じる</button>}>
      <p className="mb-3 text-sm text-ink-sub">
        議案書（Google ドキュメント）が入ったフォルダの URL を貼り付けてください。新しい書類は「資料提出」の段階で追加し、取り込み済みの書類は本文を最新にします。
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <Field label="Drive フォルダの URL" className="min-w-[240px] flex-1">
          <input
            className="w-full"
            placeholder="https://drive.google.com/drive/folders/…"
            value={folder}
            onChange={(e) => {
              setFolder(e.target.value);
              setPreview(null);
            }}
          />
        </Field>
        <Button disabled={!!busy || !folder.trim()} onClick={check}>
          {busy === "check" ? "確認中…" : "取り込む前に確認"}
        </Button>
        <Button variant={preview ? "primary" : "secondary"} disabled={!!busy || !folder.trim()} onClick={doImport}>
          {busy === "import" ? "取り込み中…" : "取り込む"}
        </Button>
      </div>

      {preview && (
        <div className="mt-4 rounded-2xl bg-gray-50 p-4">
          <div className="text-sm font-bold">
            {preview.total === 0 ? "フォルダに取り込める書類がありませんでした" : `${preview.total}件の書類が見つかりました`}
            {preview.total > 0 && (
              <span className="ml-2 font-normal text-ink-sub">新しく {preview.created}件・更新 {preview.updated}件</span>
            )}
          </div>
          {preview.items?.length > 0 && (
            <ul className="mt-2 max-h-56 space-y-1 overflow-y-auto text-sm">
              {preview.items.map((it, i) => (
                <li key={i} className="flex items-center gap-2">
                  <Badge tone={it.action === "created" ? "green" : "gray"}>{it.action === "created" ? "新規" : "更新"}</Badge>
                  <span className="truncate">{it.name}</span>
                </li>
              ))}
            </ul>
          )}
          <div className="mt-2 text-xs text-ink-sub">まだ取り込んでいません。よければ「取り込む」を押してください。</div>
        </div>
      )}

      <details className="mt-4 rounded-2xl bg-gray-50 px-4 py-3 text-sm">
        <summary className="cursor-pointer font-bold text-ink-sub">連携の準備（初回のみ）</summary>
        <div className="mt-2 space-y-1 text-ink-sub">
          <p>このシステムがフォルダを読めるように、Google ドライブでフォルダを次のアドレスに「閲覧者」として共有してください。</p>
          <p className="select-all break-all rounded-lg bg-white px-2 py-1 font-mono text-xs text-ink">
            drive-reader@jci-sed-agent.iam.gserviceaccount.com
          </p>
          <p>共有は一度だけで、そのフォルダに後から入れた書類も取り込めます。</p>
        </div>
      </details>
    </Panel>
  );
}

// 事業計画書から同期した内容。台帳は文書から作るので、ここでは見るだけ（直すときは文書で）
function PlanContent({ p }: { p: Proposal }) {
  const sections = Object.entries(p.sections ?? {}).filter(([, v]) => v.trim());
  const meetings = new Map<string, NonNullable<Proposal["feedback"]>>();
  for (const f of p.feedback ?? []) meetings.set(f.meeting, [...(meetings.get(f.meeting) ?? []), f]);
  const summary = p.doc_type === SUMMARY_TYPE;
  const facts: [string, string | null | undefined][] = summary ? [] : [
    ["実施日", p.event_date],
    ["場所", p.venue],
    ["対象・人数", p.participants],
    ["委員長", p.basics?.["委員長"]],
    ["文書作成人", p.basics?.["文書作成人（役職/氏名）"]],
  ];
  return (
    <section className="rounded-card bg-white p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="font-extrabold">{p.doc_type}の内容</h3>
        {p.synced_at && <span className="text-xs text-ink-faint">文書から読み取り: {fmtDateTime(p.synced_at)}</span>}
      </div>
      <dl className="mt-2 grid grid-cols-[7rem_1fr] gap-y-1.5 text-sm">
        {facts.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-ink-sub">{k}</dt>
            <dd>{v?.trim() ? v : <span className="text-ink-faint">未記入</span>}</dd>
          </div>
        ))}
      </dl>
      <details className="mt-3 text-sm">
        <summary className="cursor-pointer font-bold text-line-ink">
          {summary ? "記入欄" : "事業要項"}（記入済み {sections.length} / {Object.keys(p.sections ?? {}).length} 項目）
        </summary>
        <div className="mt-2 space-y-3">
          {sections.map(([k, v]) => (
            <div key={k}>
              <div className="text-xs font-bold text-ink-sub">{k}</div>
              <p className="whitespace-pre-wrap leading-relaxed">{v}</p>
            </div>
          ))}
        </div>
      </details>
      {meetings.size > 0 && (
        <details className="mt-3 text-sm" open>
          <summary className="cursor-pointer font-bold text-line-ink">審議の記録</summary>
          <div className="mt-2 space-y-3">
            {[...meetings.entries()].map(([meeting, items]) => (
              <div key={meeting}>
                <div className="text-xs font-bold text-ink-sub">{meeting}</div>
                <ul className="mt-1 space-y-1">
                  {items.map((f) => (
                    <li key={`${f.kind}${f.no}`} className="flex gap-2">
                      <Badge tone={f.kind === "意見" ? "amber" : "green"}>{f.kind}{f.no}</Badge>
                      <span className="whitespace-pre-wrap">{f.text}{f.person && <span className="text-ink-sub">（{f.person}）</span>}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </details>
      )}
      <p className="mt-3 text-xs text-ink-faint">内容を直すときは、議案書（Google ドキュメント）を編集してください。1時間ごとに読み取ります。</p>
    </section>
  );
}
