import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
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
  useUi,
} from "../components/ui";

// 設定（docs/admin-ui-design.md §3・§4: キルスイッチ→送信の一時停止、seed→標準の設定に戻す）

type Settings = {
  kill_switch: boolean;
  operation_start: string | null;
  quiet_hours: { start: string; end: string; tz: string };
  rate_limit: { per_member_per_day: number; global_per_min: number };
};
type Stage = { name: string; offset_minutes: number; audience: string; template: string };
type Policy = { policy_id: string; event_type: string; stages: Stage[] };

const DAY = 24 * 60;

const AUDIENCE: Record<string, string> = {
  all: "対象の全員",
  unanswered: "まだ回答していない人",
  attendees: "出席と答えた人",
};

/** 締切からのずれ（分・負=締切前）を「締切の7日前」のように書く。 */
function offsetLabel(minutes: number): string {
  if (minutes === 0) return "締切の時刻";
  const before = minutes < 0;
  const abs = Math.abs(minutes);
  const days = Math.floor(abs / DAY);
  const hours = Math.floor((abs % DAY) / 60);
  const mins = abs % 60;
  if (before && days === 1 && !hours && !mins) return "締切の前日";
  const parts = [days && `${days}日`, hours && `${hours}時間`, mins && `${mins}分`].filter(Boolean);
  return `締切の${parts.join("")}${before ? "前" : "後"}`;
}

function fmtDate(iso: string | null): string {
  if (!iso) return "未設定";
  const [y, m, d] = iso.split("-").map(Number);
  return `${y}年${m}月${d}日`;
}

const tzLabel = (tz: string) => (tz === "Asia/Tokyo" ? "日本時間" : tz);

export default function Settings() {
  const { confirm, toast } = useUi();
  const [settings, setSettings] = useState<Settings | null>(null);
  const [opStart, setOpStart] = useState("");
  const [quiet, setQuiet] = useState({ start: "", end: "" });
  const [rate, setRate] = useState({ per_member_per_day: 0, global_per_min: 0 });
  const [policies, setPolicies] = useState<Policy[] | null>(null);
  const [editing, setEditing] = useState<Policy | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const applySettings = (s: Settings) => {
    setSettings(s);
    setOpStart(s.operation_start ?? "");
    setQuiet({ start: s.quiet_hours.start, end: s.quiet_hours.end });
    setRate({ ...s.rate_limit });
  };
  const loadPolicies = () => api<Policy[]>("/policies").then(setPolicies).catch(() => setPolicies([]));

  useEffect(() => {
    api<Settings>("/settings").then(applySettings).catch((e) => setErr(e.message));
    loadPolicies();
  }, []);

  /** 保存済みの設定に、その欄の変更だけを重ねて保存する（ほかの欄の書きかけは送らない）。 */
  async function save(patch: Partial<Settings>, done: string) {
    if (!settings) return;
    setSaving(true);
    try {
      const saved = await api<Settings>("/settings", {
        method: "PUT",
        body: JSON.stringify({ ...settings, ...patch }),
      });
      setSettings(saved);
      // 保存した欄だけ表示を合わせる
      if ("operation_start" in patch) setOpStart(saved.operation_start ?? "");
      if ("quiet_hours" in patch) setQuiet({ start: saved.quiet_hours.start, end: saved.quiet_hours.end });
      if ("rate_limit" in patch) setRate({ ...saved.rate_limit });
      toast(done);
    } catch (e) {
      toast(`保存できませんでした（${(e as Error).message}）`, "error");
    } finally {
      setSaving(false);
    }
  }

  async function toggleSending() {
    if (!settings) return;
    const stop = !settings.kill_switch;
    const ok = await confirm(
      stop
        ? {
            title: "LINE への送信を一時停止しますか？",
            body: (
              <>
                会員への自動の送信（出欠の依頼・催促、連絡やアンケートのお知らせなど）と、予約したグループへの投稿が止まります。
                <br />
                止めている間に送れなかったものは、再開したあとの毎時の自動処理で送り直します。
              </>
            ),
            confirmLabel: "一時停止する",
            danger: true,
          }
        : {
            title: "LINE への送信を再開しますか？",
            body: "止めている間に送れなかった出欠の依頼・催促や予約投稿は、次の毎時の自動処理でまとめて送られます。",
            confirmLabel: "再開する",
          }
    );
    if (!ok) return;
    await save({ kill_switch: stop }, stop ? "LINE への送信を一時停止しました" : "LINE への送信を再開しました");
  }

  async function savePolicy(p: Policy) {
    try {
      await api<Policy>(`/policies/${encodeURIComponent(p.policy_id)}`, { method: "PUT", body: JSON.stringify(p) });
      toast(`${p.event_type}の催促のタイミングを保存しました`);
      setEditing(null);
      loadPolicies();
    } catch (e) {
      toast(`保存できませんでした（${(e as Error).message}）`, "error");
    }
  }

  async function resetPolicies() {
    const ok = await confirm({
      title: "催促のタイミングを標準の設定に戻しますか？",
      body: (
        <>
          例会（締切の7日前・3日前・前日）と理事会（締切の7日前・5日前・3日前）を標準のタイミングに戻します。
          ここで変更した内容は上書きされます。
        </>
      ),
      confirmLabel: "標準に戻す",
    });
    if (!ok) return;
    try {
      await api("/policies/seed", { method: "POST" });
      toast("催促のタイミングを標準の設定に戻しました");
      setEditing(null);
      loadPolicies();
    } catch (e) {
      toast(`戻せませんでした（${(e as Error).message}）`, "error");
    }
  }

  if (err) return <ErrorNote>{err}</ErrorNote>;
  if (!settings) return <Loading />;

  const opDirty = (settings.operation_start ?? "") !== opStart;
  const quietDirty = quiet.start !== settings.quiet_hours.start || quiet.end !== settings.quiet_hours.end;
  const rateDirty =
    rate.per_member_per_day !== settings.rate_limit.per_member_per_day ||
    rate.global_per_min !== settings.rate_limit.global_per_min;
  const sendDirty = quietDirty || rateDirty;
  const rateValid = rate.per_member_per_day >= 1 && rate.global_per_min >= 1;
  const quietValid = /^\d{2}:\d{2}$/.test(quiet.start) && /^\d{2}:\d{2}$/.test(quiet.end);

  return (
    <div className="max-w-4xl">
      <PageHeader title="設定" description="年度の始まり、LINE への送信の止め方・送り方、出欠の催促のタイミングを決めます。" />

      {/* ------------------------------------------------ 年度 */}
      <Panel title="年度（運用開始日）" action={<span className="text-ink-sub">いま: {fmtDate(settings.operation_start)}</span>}>
        <div className="flex flex-wrap items-end gap-3">
          <Field label="運用開始日（年度の始まり）">
            <input type="date" value={opStart} onChange={(e) => setOpStart(e.target.value)} />
          </Field>
          <Button
            disabled={saving || !opDirty}
            onClick={() => save({ operation_start: opStart || null }, "運用開始日を保存しました")}
          >
            保存
          </Button>
        </div>
        <p className="mt-3 text-xs leading-relaxed text-ink-sub">
          この日より前の予定は、予定・出欠の一覧、出席率の推移、カレンダーからの取り込みの対象外になります（データは消えません）。
        </p>
      </Panel>

      {/* ------------------------------------------------ 送信の一時停止 */}
      <Panel
        title="LINE への送信の一時停止"
        action={settings.kill_switch ? <Badge tone="red">停止中</Badge> : <Badge tone="green">送信中</Badge>}
        className={settings.kill_switch ? "ring-2 ring-danger/40" : ""}
      >
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="min-w-[260px] flex-1 text-sm leading-relaxed">
            {settings.kill_switch ? (
              <>
                <div className="font-bold text-danger">いま LINE への自動の送信を止めています。</div>
                <div className="mt-1 text-ink-sub">
                  出欠の依頼・催促、連絡やアンケートのお知らせ、予約したグループへの投稿は送られません。再開すると、止めている間に送れなかったものを毎時の自動処理で送り直します。
                </div>
              </>
            ) : (
              <>
                <div className="font-bold">いつもどおり送信しています。</div>
                <div className="mt-1 text-ink-sub">
                  間違った内容が送られそうなときなど、急いで止めたいときに使います。止めている間は、会員への自動の送信と予約したグループへの投稿がすべて止まります。
                </div>
              </>
            )}
          </div>
          {settings.kill_switch ? (
            <Button variant="primary" disabled={saving} onClick={toggleSending}>送信を再開する</Button>
          ) : (
            <Button
              variant="secondary"
              className="!bg-danger !text-white hover:!bg-danger/90"
              disabled={saving}
              onClick={toggleSending}
            >
              送信を一時停止する
            </Button>
          )}
        </div>
      </Panel>

      {/* ------------------------------------------------ 静音時間・レート */}
      <Panel title="送らない時間帯と送信の上限">
        <div className="grid gap-5 md:grid-cols-2">
          <div>
            <div className="mb-2 text-sm font-bold">送らない時間帯（{tzLabel(settings.quiet_hours.tz)}）</div>
            <div className="flex flex-wrap items-end gap-2">
              <Field label="開始">
                <input type="time" className="w-32" value={quiet.start} onChange={(e) => setQuiet({ ...quiet, start: e.target.value })} />
              </Field>
              <span className="pb-2 text-sm text-ink-sub">〜</span>
              <Field label="終了">
                <input type="time" className="w-32" value={quiet.end} onChange={(e) => setQuiet({ ...quiet, end: e.target.value })} />
              </Field>
            </div>
            <p className="mt-2 text-xs leading-relaxed text-ink-sub">
              この時間帯に送る予定のものは、終了の時刻のあとに送ります。
            </p>
          </div>
          <div>
            <div className="mb-2 text-sm font-bold">送信の上限</div>
            <div className="space-y-2">
              <label className="flex flex-wrap items-center gap-2">
                <span className="w-32 text-sm text-ink-sub">1人あたり 1日</span>
                <input
                  type="number"
                  min={1}
                  className="num w-20"
                  value={rate.per_member_per_day}
                  onChange={(e) => setRate({ ...rate, per_member_per_day: Number(e.target.value) })}
                />
                <span className="text-sm">通まで</span>
              </label>
              <label className="flex flex-wrap items-center gap-2">
                <span className="w-32 text-sm text-ink-sub">全体で 1分あたり</span>
                <input
                  type="number"
                  min={1}
                  className="num w-20"
                  value={rate.global_per_min}
                  onChange={(e) => setRate({ ...rate, global_per_min: Number(e.target.value) })}
                />
                <span className="text-sm">通まで</span>
              </label>
            </div>
            <p className="mt-2 text-xs leading-relaxed text-ink-sub">
              同じ人に何度も送りすぎないための上限です。上限を超える分は、その時点では送りません。
            </p>
          </div>
        </div>
        <div className="mt-4 flex flex-wrap items-center justify-end gap-2 border-t border-gray-100 pt-4">
          {!rateValid && <span className="text-xs text-danger">上限は1以上にしてください</span>}
          {!quietValid && <span className="text-xs text-danger">時刻を入れてください</span>}
          {sendDirty && (
            <Button
              variant="ghost"
              onClick={() => {
                setQuiet({ start: settings.quiet_hours.start, end: settings.quiet_hours.end });
                setRate({ ...settings.rate_limit });
              }}
            >
              元に戻す
            </Button>
          )}
          <Button
            disabled={saving || !sendDirty || !rateValid || !quietValid}
            onClick={() =>
              save(
                { quiet_hours: { ...settings.quiet_hours, ...quiet }, rate_limit: { ...rate } },
                "送らない時間帯と送信の上限を保存しました"
              )
            }
          >
            保存
          </Button>
        </div>
      </Panel>

      {/* ------------------------------------------------ 催促のタイミング */}
      <Panel
        title="催促のタイミング"
        action={policies && policies.length > 0 && <Button size="sm" variant="ghost" onClick={resetPolicies}>標準の設定に戻す</Button>}
      >
        <p className="mb-4 text-xs leading-relaxed text-ink-sub">
          予定の種別ごとに、出欠の依頼と催促を LINE で送るタイミングです。締切が決まっていない予定は、開催日時を基準にします。
        </p>
        {policies === null ? (
          <Loading />
        ) : policies.length === 0 ? (
          <EmptyState
            title="催促のタイミングがまだ決まっていません"
            action={<Button onClick={resetPolicies}>標準の設定を入れる</Button>}
          >
            標準の設定（例会・理事会）を入れると、締切に合わせて自動で依頼・催促を送ります。
          </EmptyState>
        ) : (
          <div className="space-y-3">
            {policies.map((p) =>
              editing?.policy_id === p.policy_id ? (
                <PolicyEditor key={p.policy_id} policy={editing} onChange={setEditing} onCancel={() => setEditing(null)} onSave={savePolicy} />
              ) : (
                <div key={p.policy_id} className="rounded-2xl bg-gray-50 px-4 py-3">
                  <div className="flex items-center justify-between gap-2">
                    <div className="font-extrabold">{p.event_type}</div>
                    <Button size="sm" onClick={() => setEditing(structuredClone(p))} disabled={!!editing}>
                      変更する
                    </Button>
                  </div>
                  <ol className="mt-2 space-y-1.5">
                    {[...p.stages]
                      .map((s, i) => ({ s, i }))
                      .sort((a, b) => a.s.offset_minutes - b.s.offset_minutes)
                      .map(({ s, i }, n) => (
                        <li key={i} className="flex items-baseline gap-2 text-sm">
                          <span className="num flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-line text-[11px] font-bold text-white">
                            {n + 1}
                          </span>
                          <span>
                            <span className="font-bold">{offsetLabel(s.offset_minutes)}</span>に
                            {AUDIENCE[s.audience] ?? "対象の人"}へ「{s.name}」
                          </span>
                        </li>
                      ))}
                  </ol>
                </div>
              )
            )}
          </div>
        )}
      </Panel>

      {/* ------------------------------------------------ 操作の記録は送信の記録へ */}
      <Panel>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="text-sm text-ink-sub">設定の変更などの操作の記録は「送信の記録」で見られます。</div>
          <Link to="/agent" className="text-sm font-bold text-line-ink hover:underline">送信の記録へ</Link>
        </div>
      </Panel>
    </div>
  );
}

// ---------------------------------------------------------------- 催促の段階の編集
function splitOffset(minutes: number) {
  const before = minutes <= 0;
  const abs = Math.abs(minutes);
  return { before, days: Math.floor(abs / DAY), hours: Math.floor((abs % DAY) / 60), rest: abs % 60 };
}
function joinOffset(o: { before: boolean; days: number; hours: number; rest: number }) {
  const abs = o.days * DAY + o.hours * 60 + o.rest;
  return o.before ? -abs : abs;
}

function PolicyEditor({
  policy,
  onChange,
  onCancel,
  onSave,
}: {
  policy: Policy;
  onChange: (p: Policy) => void;
  onCancel: () => void;
  onSave: (p: Policy) => void;
}) {
  const setStage = (i: number, offset: number) =>
    onChange({ ...policy, stages: policy.stages.map((s, j) => (j === i ? { ...s, offset_minutes: offset } : s)) });

  return (
    <div className="rounded-2xl border-2 border-line/40 bg-white px-4 py-3">
      <div className="mb-2 font-extrabold">{policy.event_type}の催促のタイミングを変更</div>
      <div className="space-y-3">
        {policy.stages.map((s, i) => {
          const o = splitOffset(s.offset_minutes);
          const num = (v: string) => Math.max(0, Math.floor(Number(v) || 0));
          return (
            <div key={i} className="rounded-xl bg-gray-50 px-3 py-2.5">
              <div className="mb-1.5 text-sm">
                <span className="font-bold">「{s.name}」</span>
                <span className="ml-1 text-xs text-ink-sub">送る相手: {AUDIENCE[s.audience] ?? "対象の人"}</span>
              </div>
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span>締切の</span>
                <input
                  type="number"
                  min={0}
                  aria-label="日"
                  className="num w-16"
                  value={o.days}
                  onChange={(e) => setStage(i, joinOffset({ ...o, days: num(e.target.value) }))}
                />
                <span>日</span>
                <input
                  type="number"
                  min={0}
                  max={23}
                  aria-label="時間"
                  className="num w-16"
                  value={o.hours}
                  onChange={(e) => setStage(i, joinOffset({ ...o, hours: Math.min(23, num(e.target.value)) }))}
                />
                <span>時間</span>
                <select
                  aria-label="前か後か"
                  value={o.before ? "before" : "after"}
                  onChange={(e) => setStage(i, joinOffset({ ...o, before: e.target.value === "before" }))}
                >
                  <option value="before">前</option>
                  <option value="after">後</option>
                </select>
                <span className="text-xs text-ink-sub">→ {offsetLabel(s.offset_minutes)}</span>
              </div>
            </div>
          );
        })}
      </div>
      <div className="mt-3 flex justify-end gap-2">
        <Button variant="ghost" onClick={onCancel}>やめる</Button>
        <Button variant="primary" onClick={() => onSave(policy)}>保存</Button>
      </div>
    </div>
  );
}
