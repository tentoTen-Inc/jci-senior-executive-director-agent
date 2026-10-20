import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import ContactTabs from "../components/ContactTabs";
import {
  Badge,
  Button,
  EmptyState,
  ErrorNote,
  Field,
  Loading,
  PageHeader,
  Panel,
  Progress,
  Segmented,
  Stat,
  fmtDateTime,
  useUi,
} from "../components/ui";

// アンケート（docs/admin-ui-design.md §3「連絡・アンケート」）
// Google フォームを取り込み → 集計・AI 要約 → 未提出者へ催促・五役へレポート

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
  reminder_count: number;
  reminded_at?: string | null;
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
type Aggregate = { responses: number; scales: ScaleStat[]; texts: TextStat[] };
type Pending = {
  total_targets: number;
  answered: number;
  unmatched: number;
  pending_member_ids: string[];
  pending: { member_id: string; name: string }[];
};
type TrendPoint = { survey_id: string; title: string; kind: string; responses: number; overall_average: number | null };
type EventBrief = { event_id: string; title: string; datetime_start: string };
type Member = {
  member_id: string;
  name: string;
  status: string;
  member_type: string;
  officer_role: string | null;
  line_user_id: string | null;
};
type SendResult = { sent: number; blocked: number; deferred: number; failed: number };

type Kind = "internal" | "external";
const KIND: Record<string, { label: string; tone: "green" | "blue" }> = {
  internal: { label: "対内", tone: "green" },
  external: { label: "対外", tone: "blue" },
};

// 五役（app/summary.py OFFICER_ROLES）と配信の対象（app/models.py Member.is_deliverable）
const OFFICER_ROLES = new Set(["理事長", "直前理事長", "副理事長", "専務理事", "監事"]);
const DELIVERABLE_TYPES = new Set(["regular", "external_auditor", "office", "youth_support"]);
const isDeliverable = (m: Member) => m.status === "active" && !!m.line_user_id && DELIVERABLE_TYPES.has(m.member_type);

const defaultReminder = (title: string) =>
  `【アンケートのお願い】${title}\nまだご回答をいただいていません。お手数ですが、事務局からご案内したGoogleフォームからご回答をお願いします。`;

type Rule = [RegExp, string];
const COMMON_RULES: Rule[] = [
  [/ガードレール/, "安全のため送信を止めました（送信の一時停止中・静音時間・送信の上限など）。設定と送信の記録を確認してください。"],
  [/not found/i, "対象のアンケートが見つかりませんでした。画面を読み込み直してください。"],
];
/** サーバーのエラーのうち、開発用の言葉を含むものを画面向けの言葉にする（それ以外はそのまま出す） */
function friendly(e: unknown, rules: Rule[] = []): string {
  const m = e instanceof Error ? e.message : String(e);
  for (const [re, text] of [...rules, ...COMMON_RULES]) if (re.test(m)) return text;
  return m;
}

const FORM_ERR: Rule[] = [
  [/フォームの取得に失敗|drive-reader/, "フォームを読めませんでした。URL が正しいか、フォームの共有ができているかを確認してください（取り込みの「連携の準備」）。"],
];

function resultLine(r: SendResult, head: string): string {
  const extra = [r.deferred && `時間をおいて送る ${r.deferred}人`, r.blocked && `届かない ${r.blocked}人`, r.failed && `失敗 ${r.failed}人`]
    .filter(Boolean)
    .join("・");
  return `${head}（送信 ${r.sent}人${extra ? `・${extra}` : ""}）`;
}

/** フォームの URL か ID から、編集用の ID を取り出す。回答用 URL（/d/e/…）は使えないので null。 */
function formIdFrom(input: string): string | null {
  const s = input.trim();
  if (/\/forms\/d\/e\//.test(s)) return null;
  const m = s.match(/\/forms\/d\/([A-Za-z0-9_-]+)/);
  return m ? m[1] : s;
}

function overall(scales: ScaleStat[]): number | null {
  const avgs = scales.map((s) => s.average).filter((v): v is number => v !== null);
  return avgs.length ? avgs.reduce((a, b) => a + b, 0) / avgs.length : null;
}

/** 点数の分布を横並びの棒で表す */
function Distribution({ stat }: { stat: ScaleStat }) {
  const high = stat.scale_high ?? 5;
  const low = stat.scale_low ?? 1;
  const max = Math.max(1, ...Object.values(stat.distribution));
  const keys: number[] = [];
  for (let i = low; i <= high; i++) keys.push(i);
  return (
    <div className="flex h-10 items-end gap-1.5">
      {keys.map((k) => {
        const n = stat.distribution[String(k)] ?? 0;
        return (
          <div key={k} className="flex flex-col items-center" title={`${k}点: ${n}件`}>
            <div className="w-4 rounded-sm bg-line" style={{ height: `${(n / max) * 28}px`, minHeight: n ? "2px" : "0" }} />
            <span className="num mt-0.5 text-[10px] text-ink-faint">{k}</span>
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------- 画面
export default function Surveys() {
  const [params, setParams] = useSearchParams();
  const [items, setItems] = useState<Survey[] | null>(null);
  const [trends, setTrends] = useState<TrendPoint[]>([]);
  const [events, setEvents] = useState<EventBrief[]>([]);
  const [members, setMembers] = useState<Member[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [showImport, setShowImport] = useState(false);

  const load = () => {
    api<Survey[]>("/surveys")
      .then((s) => {
        setItems(s);
        setErr(null);
      })
      .catch((e) => setErr(friendly(e)));
    api<TrendPoint[]>("/surveys/trends").then(setTrends).catch(() => {});
  };
  useEffect(() => {
    load();
    api<EventBrief[]>("/events").then(setEvents).catch(() => {});
    api<Member[]>("/members").then(setMembers).catch(() => {});
  }, []);

  const selId = params.get("id");
  const openDetail = (id: string | null) => {
    const next = new URLSearchParams(params);
    if (id) next.set("id", id);
    else next.delete("id");
    setParams(next);
    window.scrollTo({ top: 0 });
  };
  const eventTitle = useMemo(() => new Map(events.map((e) => [e.event_id, e.title])), [events]);

  if (selId) {
    return (
      <div>
        <ContactTabs />
        <SurveyDetail
          key={selId}
          id={selId}
          members={members}
          eventTitle={eventTitle}
          onBack={() => openDetail(null)}
          onChanged={load}
        />
      </div>
    );
  }

  const sorted = [...(items ?? [])].sort((a, b) => (b.synced_at ?? "").localeCompare(a.synced_at ?? ""));

  return (
    <div>
      <ContactTabs />
      <PageHeader
        title="アンケート"
        description="Google フォームのアンケートを取り込み、集計・AI の要約・未提出の人への催促ができます。"
        actions={
          <Button variant="primary" onClick={() => setShowImport(!showImport)}>
            フォームを取り込む
          </Button>
        }
      />
      {err && <ErrorNote>{err}</ErrorNote>}

      {showImport && (
        <ImportPanel
          events={events}
          onClose={() => setShowImport(false)}
          onImported={(s) => {
            setShowImport(false);
            load();
            openDetail(s.survey_id);
          }}
        />
      )}

      <Panel title="取り込んだアンケート" flush>
        {!items ? (
          <Loading />
        ) : sorted.length === 0 ? (
          <div className="px-5 pb-3">
            <EmptyState
              title="まだアンケートはありません"
              action={!showImport && <Button onClick={() => setShowImport(true)}>フォームを取り込む</Button>}
            >
              アンケートの配布はこれまでどおり Google フォームで行い、回答をここに取り込んで集計します。
            </EmptyState>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="tbl min-w-[560px]">
              <thead>
                <tr>
                  <th>タイトル</th>
                  <th>区分</th>
                  <th className="!text-right">回答数</th>
                  <th>取込日</th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((s) => {
                  const k = KIND[s.kind] ?? { label: "—", tone: "green" as const };
                  return (
                    <tr key={s.survey_id} className="cursor-pointer" onClick={() => openDetail(s.survey_id)}>
                      <td>
                        <button type="button" className="text-left font-bold hover:underline" onClick={() => openDetail(s.survey_id)}>
                          {s.title}
                        </button>
                        {s.event_id && eventTitle.get(s.event_id) && (
                          <div className="text-xs text-ink-sub">{eventTitle.get(s.event_id)}</div>
                        )}
                      </td>
                      <td>
                        <Badge tone={k.tone}>{k.label}</Badge>
                      </td>
                      <td className="num text-right font-bold">{s.responses.length}</td>
                      <td className="num whitespace-nowrap text-ink-sub">{fmtDateTime(s.synced_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {trends.length > 1 && (
        <Panel title="回ごとの満足度の推移">
          <p className="mb-3 text-xs text-ink-sub">設問の文は回ごとに変わるため、点数で答える設問すべての平均で比べています。</p>
          <ul className="space-y-2">
            {trends.map((t) => {
              const k = KIND[t.kind];
              const max = 5;
              return (
                <li key={t.survey_id} className="grid grid-cols-[1fr_auto] items-center gap-x-3 gap-y-1 sm:grid-cols-[minmax(0,16rem)_1fr_auto]">
                  <div className="truncate text-sm">
                    {k && <span className="mr-1.5"><Badge tone={k.tone}>{k.label}</Badge></span>}
                    {t.title}
                  </div>
                  <div className="col-span-2 row-start-2 sm:col-span-1 sm:row-start-auto">
                    <Progress value={t.overall_average ?? 0} max={max} />
                  </div>
                  <div className="num text-right text-sm">
                    <b>{t.overall_average === null ? "—" : t.overall_average.toFixed(1)}</b>
                    <span className="ml-2 text-xs text-ink-sub">{t.responses}件</span>
                  </div>
                </li>
              );
            })}
          </ul>
        </Panel>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 取り込み
function ImportPanel({
  events,
  onClose,
  onImported,
}: {
  events: EventBrief[];
  onClose: () => void;
  onImported: (s: Survey) => void;
}) {
  const { toast } = useUi();
  const [url, setUrl] = useState("");
  const [kind, setKind] = useState<Kind>("internal");
  const [eventId, setEventId] = useState("");
  const [busy, setBusy] = useState<"check" | "import" | null>(null);
  const [preview, setPreview] = useState<{ title: string; questions: number; responses: number } | null>(null);

  const upcoming = [...events].sort((a, b) => b.datetime_start.localeCompare(a.datetime_start));

  function body(dryRun: boolean): string | null {
    const id = formIdFrom(url);
    if (id === null) {
      toast("回答用の URL ではなく、フォームの編集画面の URL を貼り付けてください", "error");
      return null;
    }
    if (!id) {
      toast("Google フォームの URL を入力してください", "error");
      return null;
    }
    return JSON.stringify({ form_id: id, kind, event_id: eventId || null, dry_run: dryRun });
  }

  async function check() {
    const b = body(true);
    if (!b) return;
    setBusy("check");
    try {
      setPreview(await api("/surveys/import-form", { method: "POST", body: b }));
    } catch (e) {
      toast(friendly(e, FORM_ERR), "error");
    } finally {
      setBusy(null);
    }
  }

  async function doImport() {
    const b = body(false);
    if (!b) return;
    setBusy("import");
    try {
      const s = await api<Survey>("/surveys/import-form", { method: "POST", body: b });
      toast(`「${s.title}」を取り込みました（回答 ${s.responses.length}件）`);
      onImported(s);
    } catch (e) {
      toast(friendly(e, FORM_ERR), "error");
    } finally {
      setBusy(null);
    }
  }

  const reset = () => setPreview(null);

  return (
    <Panel title="Google フォームを取り込む" action={<button type="button" className="font-bold text-ink-sub" onClick={onClose}>閉じる</button>}>
      <div className="grid gap-4 md:grid-cols-2">
        <Field label="フォームの URL" hint="フォームの編集画面を開いたときのアドレスを貼り付けてください。同じフォームをもう一度取り込むと、回答が最新になります。" className="md:col-span-2">
          <input
            className="w-full"
            placeholder="https://docs.google.com/forms/d/…/edit"
            value={url}
            onChange={(e) => {
              setUrl(e.target.value);
              reset();
            }}
          />
        </Field>
        <div>
          <span className="field-label">区分</span>
          <Segmented<Kind>
            value={kind}
            onChange={(k) => {
              setKind(k);
              reset();
            }}
            options={[
              { value: "internal", label: "対内（会員向け）" },
              { value: "external", label: "対外（一般の方向け）" },
            ]}
          />
          <p className="mt-1 text-xs text-ink-faint">
            {kind === "internal" ? "回答を会員と照らし合わせ、まだ答えていない人がわかります。" : "会員以外が答えるため、未提出の人の確認や催促はできません。"}
          </p>
        </div>
        <Field label="対象の予定（任意）" hint={kind === "internal" ? "選ぶと、その予定の対象の人だけを未提出の判定に使います。" : undefined}>
          <select className="w-full" value={eventId} onChange={(e) => { setEventId(e.target.value); reset(); }}>
            <option value="">選ばない</option>
            {upcoming.map((e) => (
              <option key={e.event_id} value={e.event_id}>
                {fmtDateTime(e.datetime_start, false)} {e.title}
              </option>
            ))}
          </select>
        </Field>
      </div>

      <div className="mt-4 flex flex-wrap justify-end gap-2">
        <Button disabled={!!busy || !url.trim()} onClick={check}>
          {busy === "check" ? "確認中…" : "取り込む前に確認"}
        </Button>
        <Button variant="primary" disabled={!!busy || !url.trim()} onClick={doImport}>
          {busy === "import" ? "取り込み中…" : "取り込む"}
        </Button>
      </div>

      {preview && (
        <div className="mt-4 rounded-2xl bg-gray-50 px-4 py-3 text-sm">
          <div className="font-bold">{preview.title}</div>
          <div className="num mt-1 text-ink-sub">
            設問 {preview.questions}件・回答 {preview.responses}件
          </div>
          <div className="mt-1 text-xs text-ink-sub">まだ取り込んでいません。よければ「取り込む」を押してください。</div>
        </div>
      )}

      <details className="mt-4 rounded-2xl bg-gray-50 px-4 py-3 text-sm">
        <summary className="cursor-pointer font-bold text-ink-sub">連携の準備（初回のみ）</summary>
        <div className="mt-2 space-y-1 text-ink-sub">
          <p>このシステムがフォームを読めるように、Google フォームの「共同編集者を追加」から次のアドレスを追加してください。</p>
          <p className="select-all break-all rounded-lg bg-white px-2 py-1 font-mono text-xs text-ink">
            drive-reader@jci-sed-agent.iam.gserviceaccount.com
          </p>
          <p>アンケートの配布はこれまでどおり Google フォームで行います。</p>
        </div>
      </details>
    </Panel>
  );
}

// ---------------------------------------------------------------- 詳細
function SurveyDetail({
  id,
  members,
  eventTitle,
  onBack,
  onChanged,
}: {
  id: string;
  members: Member[];
  eventTitle: Map<string, string>;
  onBack: () => void;
  onChanged: () => void;
}) {
  const { confirm, toast } = useUi();
  const [sel, setSel] = useState<Survey | null>(null);
  const [agg, setAgg] = useState<Aggregate | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [report, setReport] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [reminder, setReminder] = useState<string | null>(null);

  async function open() {
    try {
      const data = await api<{ survey: Survey; aggregate: Aggregate }>(`/surveys/${id}`);
      setSel(data.survey);
      setAgg(data.aggregate);
      if (data.survey.kind === "internal") {
        api<Pending>(`/surveys/${id}/pending`).then(setPending).catch(() => setPending(null));
      } else {
        setPending(null);
      }
    } catch (e) {
      setErr(friendly(e));
    }
  }
  useEffect(() => {
    open();
  }, [id]);

  const officers = members.filter((m) => isDeliverable(m) && !!m.officer_role && OFFICER_ROLES.has(m.officer_role));

  if (err) return (
    <div>
      <BackLink onBack={onBack} />
      <ErrorNote>{err}</ErrorNote>
    </div>
  );
  if (!sel || !agg) return <Loading />;

  const k = KIND[sel.kind] ?? { label: "—", tone: "green" as const };
  const avg = overall(agg.scales);
  const pendingN = pending?.pending_member_ids.length ?? 0;
  const message = reminder ?? defaultReminder(sel.title);

  async function act(label: string, path: string, done: string, errMap?: Rule[]) {
    setBusy(label);
    try {
      await api<Survey>(`/surveys/${id}/${path}`, { method: "POST" });
      toast(done);
      onChanged();
      await open();
    } catch (e) {
      toast(friendly(e, errMap), "error");
    } finally {
      setBusy(null);
    }
  }

  async function remind() {
    if (!pending || !sel) return;
    const names = pending.pending.map((p) => p.name);
    const ok = await confirm({
      title: `まだの ${pendingN}人に LINE で催促しますか？`,
      body: (
        <>
          <div className="text-xs">
            {names.slice(0, 15).join("、")}
            {names.length > 15 ? ` ほか${names.length - 15}人` : ""}
          </div>
          <div className="mt-2 whitespace-pre-wrap rounded-xl bg-gray-50 px-3 py-2 text-xs text-ink">{message}</div>
          <div className="mt-2 text-xs">送ったメッセージは取り消せません。</div>
        </>
      ),
      confirmLabel: "催促を送る",
    });
    if (!ok) return;
    setBusy("remind");
    try {
      const custom = reminder !== null && reminder.trim() !== defaultReminder(sel.title) ? reminder.trim() : null;
      const r = await api<SendResult & { pending: number }>(`/surveys/${id}/remind`, {
        method: "POST",
        body: JSON.stringify(custom ? { body_text: custom } : {}),
      });
      toast(resultLine(r, `${r.pending}人に催促しました`));
      await open();
    } catch (e) {
      toast(friendly(e), "error");
    } finally {
      setBusy(null);
    }
  }

  async function makeReport(notify: boolean) {
    if (notify) {
      const ok = await confirm({
        title: `五役 ${officers.length}人にレポートを LINE で送りますか？`,
        body: (
          <>
            <div>
              送り先: <b className="text-ink">{officers.length ? officers.map((m) => `${m.name}（${m.officer_role}）`).join("、") : "五役（役職が登録された人）"}</b>
            </div>
            <div className="mt-2 text-xs">集計の要点をまとめて送ります。送ったメッセージは取り消せません。</div>
          </>
        ),
        confirmLabel: "五役に送る",
      });
      if (!ok) return;
    }
    setBusy(notify ? "notify" : "report");
    try {
      const r = await api<{ report: string; notified: boolean; sent?: number; blocked?: number; deferred?: number; failed?: number }>(
        `/surveys/${id}/report`,
        { method: "POST", body: JSON.stringify({ notify }) }
      );
      setReport(r.report);
      toast(
        r.notified
          ? resultLine({ sent: r.sent ?? 0, blocked: r.blocked ?? 0, deferred: r.deferred ?? 0, failed: r.failed ?? 0 }, "五役に送りました")
          : "レポートを作りました"
      );
    } catch (e) {
      toast(friendly(e), "error");
    } finally {
      setBusy(null);
    }
  }

  async function copyReport() {
    if (!report) return;
    try {
      await navigator.clipboard.writeText(report);
      toast("レポートをコピーしました");
    } catch {
      toast("コピーできませんでした。文章を選んでコピーしてください", "error");
    }
  }

  return (
    <div>
      <BackLink onBack={onBack} />
      <PageHeader
        title={sel.title}
        description={
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <Badge tone={k.tone}>{k.label}</Badge>
            {sel.event_id && eventTitle.get(sel.event_id) && <span>{eventTitle.get(sel.event_id)}</span>}
            <span className="num">取込 {fmtDateTime(sel.synced_at)}</span>
          </span>
        }
        actions={
          <Button disabled={!!busy} onClick={() => act("sync", "sync", "回答を最新にしました", FORM_ERR)}>
            {busy === "sync" ? "取り込み中…" : "回答を取り直す"}
          </Button>
        }
      />

      <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="回答" value={agg.responses} unit="件" />
        {pending ? (
          <Stat
            label="提出した会員"
            value={`${pending.answered}/${pending.total_targets}`}
            unit="人"
            sub={pendingN ? `まだ ${pendingN}人` : "全員提出済み"}
            highlight={pendingN > 0}
          />
        ) : (
          <Stat label="提出した会員" value="—" sub={sel.kind === "external" ? "対外のため確認できません" : "確認中…"} />
        )}
        <Stat label="総合の平均点" value={avg === null ? "—" : avg.toFixed(1)} sub={agg.scales.length ? `点数の設問 ${agg.scales.length}問` : "点数の設問なし"} />
        <Stat
          label="催促"
          value={sel.reminder_count}
          unit="回"
          sub={sel.reminded_at ? `最後 ${fmtDateTime(sel.reminded_at)}` : "まだ送っていません"}
        />
      </div>

      {/* 未提出の人 */}
      {pending && (
        <Panel title="まだ答えていない人">
          {pendingN === 0 ? (
            <EmptyState title="対象の人は全員答えています" />
          ) : (
            <>
              <div className="mb-3">
                <Progress value={pending.answered} max={pending.total_targets} />
              </div>
              <div className="flex flex-wrap gap-1.5">
                {pending.pending.map((p) => (
                  <span key={p.member_id} className="rounded-full bg-gray-100 px-3 py-1 text-sm">
                    {p.name}
                  </span>
                ))}
              </div>
              <details className="mt-4 text-sm" onToggle={(e) => (e.currentTarget.open && reminder === null ? setReminder(defaultReminder(sel.title)) : undefined)}>
                <summary className="cursor-pointer font-bold text-ink-sub">催促の文面を見る・直す</summary>
                <textarea className="mt-2 h-24 w-full" value={message} onChange={(e) => setReminder(e.target.value)} />
              </details>
              <div className="mt-3 flex justify-end">
                <Button variant="primary" disabled={!!busy || !message.trim()} onClick={remind}>
                  {busy === "remind" ? "送信中…" : `まだの ${pendingN}人に催促する`}
                </Button>
              </div>
            </>
          )}
          {pending.unmatched > 0 && (
            <p className="mt-3 rounded-xl bg-hold-soft px-3 py-2 text-xs text-hold">
              会員と照らし合わせられなかった回答が {pending.unmatched}件 あります（名前の書き方の違い・会員以外の回答など）。この回答の人は催促の対象から外しています。
            </p>
          )}
        </Panel>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        {/* 点数の集計 */}
        <Panel title="点数の集計">
          {agg.scales.length === 0 ? (
            <EmptyState title="点数で答える設問はありません" />
          ) : (
            <ul className="divide-y divide-gray-100">
              {agg.scales.map((s) => (
                <li key={s.question_id} className="flex items-center justify-between gap-3 py-2.5">
                  <div className="min-w-0">
                    {s.section && <div className="text-[11px] text-ink-faint">{s.section}</div>}
                    <div className="text-sm">{s.title}</div>
                    <div className="num text-xs text-ink-sub">{s.answered}人が回答</div>
                  </div>
                  <div className="flex shrink-0 items-end gap-3">
                    <Distribution stat={s} />
                    <span className="num w-10 text-right text-2xl font-extrabold">{s.average === null ? "—" : s.average.toFixed(1)}</span>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        {/* AI の要約 */}
        <Panel
          title="自由記述の AI 要約"
          action={
            <Button
              size="sm"
              disabled={!!busy}
              onClick={() =>
                act("digest", "digest", sel.digest ? "要約を作り直しました" : "要約を作りました", [
                  [/生成できませんでした|LLM/, "AI の要約を作れませんでした。少し時間をおいてもう一度お試しください。"],
                ])
              }
            >
              {busy === "digest" ? "AI が読んでいます…" : sel.digest ? "作り直す" : "AI で要約する"}
            </Button>
          }
        >
          {sel.digest ? (
            <div className="space-y-3 text-sm">
              <p className="leading-relaxed">{sel.digest.summary}</p>
              <div className="flex flex-wrap gap-1.5">
                <Badge tone="green">好意的 {sel.digest.sentiment.positive ?? 0}</Badge>
                <Badge tone="gray">どちらでもない {sel.digest.sentiment.neutral ?? 0}</Badge>
                <Badge tone="red">否定的 {sel.digest.sentiment.negative ?? 0}</Badge>
              </div>
              {sel.digest.themes.length > 0 && (
                <div>
                  <div className="field-label">よく出た話題</div>
                  <ul className="space-y-1.5">
                    {sel.digest.themes.map((t, i) => (
                      <li key={i}>
                        <span className="font-bold">{t.label}</span>
                        <span className="num ml-1 text-xs text-ink-sub">{t.count}件</span>
                        {t.examples.length > 0 && <div className="text-xs text-ink-sub">「{t.examples.join("」「")}」</div>}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {sel.digest.improvements.length > 0 && (
                <div>
                  <div className="field-label">次に向けた改善のヒント</div>
                  <ul className="list-disc space-y-0.5 pl-5 text-hold">
                    {sel.digest.improvements.map((x, i) => (
                      <li key={i}>{x}</li>
                    ))}
                  </ul>
                </div>
              )}
              <p className="text-xs text-ink-faint">※ AI の要約です。判断するときは下の原文も確認してください。</p>
            </div>
          ) : (
            <EmptyState title="まだ要約していません">自由記述の回答を AI が読み、話題ごとにまとめます。</EmptyState>
          )}
        </Panel>
      </div>

      {/* 自由記述の原文 */}
      {agg.texts.length > 0 && (
        <details className="group mb-4 rounded-card bg-white px-5 py-4">
          <summary className="cursor-pointer list-none font-extrabold">
            <span className="mr-1 inline-block transition group-open:rotate-90">›</span>
            自由記述の原文
            <span className="num ml-2 text-xs font-normal text-ink-sub">
              {agg.texts.reduce((n, t) => n + t.answers.length, 0)}件
            </span>
          </summary>
          <div className="mt-3 space-y-4">
            {agg.texts.map((t) => (
              <div key={t.question_id}>
                <div className="field-label">{t.title}</div>
                {t.answers.length === 0 ? (
                  <span className="text-xs text-ink-faint">回答なし</span>
                ) : (
                  <ul className="space-y-1 text-sm">
                    {t.answers.map((a, i) => (
                      <li key={i} className="rounded-xl bg-gray-50 px-3 py-2">{a}</li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
          </div>
        </details>
      )}

      {/* レポート */}
      <Panel title="集計レポート">
        <p className="mb-3 text-sm text-ink-sub">集計と要約をひとつの文章にまとめます。五役へ LINE で送ることもできます。</p>
        <div className="flex flex-wrap gap-2">
          <Button disabled={!!busy} onClick={() => makeReport(false)}>
            {busy === "report" ? "作成中…" : "レポートを作る"}
          </Button>
          <Button disabled={!!busy} onClick={() => makeReport(true)}>
            {busy === "notify" ? "送信中…" : "五役に LINE で送る"}
          </Button>
          {report && (
            <Button variant="ghost" onClick={copyReport}>
              コピーする
            </Button>
          )}
        </div>
        {report && (
          <pre className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap rounded-xl bg-gray-50 p-3 font-sans text-sm leading-relaxed">
            {report}
          </pre>
        )}
      </Panel>
    </div>
  );
}

function BackLink({ onBack }: { onBack: () => void }) {
  return (
    <button type="button" onClick={onBack} className="mb-3 text-sm font-bold text-line-ink hover:underline">
      ‹ 一覧に戻る
    </button>
  );
}
