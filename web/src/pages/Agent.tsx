import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
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
} from "../components/ui";

// 記録: 送信の記録（docs/admin-ui-design.md §3）
// LINE への送信の結果と、管理画面・自動処理で行った操作の記録。ID は画面に出さず名前・タイトルで見せる。

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
  content?: string;
  result: string;
  reason: string | null;
  sent_at: string;
};
type Audit = { at: string; actor: string; action: string; target: string | null; detail?: string | null };
type Member = {
  member_id: string;
  name: string;
  line_user_id: string | null;
  contact?: { email?: string | null } | null;
};
type Titled = { title: string };
type Group = { group_id: string; group_name: string | null; source_type?: string };

type ResultFilter = "" | "ok" | "blocked" | "failed";
const RESULT: Record<string, { label: string; tone: "green" | "amber" | "red" | "gray" }> = {
  ok: { label: "成功", tone: "green" },
  blocked: { label: "見送り", tone: "amber" },
  failed: { label: "失敗", tone: "red" },
};

const REASON: Record<string, string> = {
  kill_switch: "送信を一時停止していたため送っていません",
  quiet_hours: "送らない時間帯だったため、あとで送ります",
  rate_limited: "送信の上限に達したため送っていません",
  send_error: "LINE に届けられませんでした（LINE 未連携など）",
};

// ---------------------------------------------------------------- 操作の名前
const ACTION: Record<string, string> = {
  "event.create": "予定を登録",
  "event.update": "予定を変更",
  "event.cancel": "予定を中止",
  "event.restore": "予定を元に戻す",
  "event.close": "出欠を締め切り",
  "event.import_from_gcal": "カレンダーから予定を取り込み",
  "event.update_from_gcal": "カレンダーの変更を反映",
  "event.cancel_from_gcal": "カレンダーで消された予定を中止",
  "policy.update": "催促のタイミングを変更",
  "settings.update": "設定を変更",
  "escalation.handled": "問い合わせを対応済みに",
  "member.create": "会員を追加",
  "member.update": "会員の情報を変更",
  "member.invite": "招待コードを発行",
  "link.request": "LINE 連携を申請",
  "link.approve": "LINE 連携を承認",
  "link.reject": "LINE 連携をお断り",
  "post.schedule": "投稿を予約",
  "post.send": "予約した投稿を送信",
  "post.cancel": "投稿の予約を取り消し",
  "poll.create": "日程調整を作成",
  "poll.announce": "日程調整を案内",
  "poll.remind": "日程調整を催促",
  "poll.decide": "日程を決定",
  "poll.close": "日程調整を締め切り",
  "notice.create": "連絡を登録",
  "notice.import_gmail": "メールから連絡を取り込み",
  "notice.digest": "連絡を要約",
  "notice.deliver": "連絡を送信",
  "notice.deliver.halted": "連絡の送信を中止（安全確認）",
  "notice.archive": "連絡を片付け",
  "notice.actions.create": "対応事項を作成",
  "notice.actions.done": "対応事項を完了に",
  "notice.actions.remind": "対応事項を催促",
  "notice.actions.remind.halted": "対応事項の催促を中止（安全確認）",
  "proposal.create": "議案を登録",
  "proposal.import_drive": "ドライブから議案を取り込み",
  "proposal.llm_review": "AI が議案を確認",
  "proposal.return": "議案を差し戻し",
  "proposal.stage": "議案の段階を変更",
  "proposal.approve": "議案を承認",
  "survey.import": "アンケートを取り込み",
  "survey.report": "アンケートの結果を報告",
  "survey.report.halted": "アンケート結果の通知を中止（安全確認）",
  "survey.remind": "アンケートを催促",
  "survey.remind.halted": "アンケートの催促を中止（安全確認）",
  "survey.digest": "アンケートの回答を要約",
};

/** 変更した項目の名前（event.update・member.update の detail は項目名のカンマ区切り） */
const FIELD: Record<string, string> = {
  title: "タイトル",
  type: "種別",
  datetime_start: "開始日時",
  datetime_end: "終了日時",
  start: "開始日時",
  end: "終了日時",
  location: "場所",
  target_scope: "対象者",
  attendance_deadline: "出欠の締切",
  reminder_policy_id: "催促のタイミング",
  quorum: "定足数",
  status: "状態",
  fiscal_year: "年度",
  description: "説明",
  name: "名前",
  kana: "ふりがな",
  birthday: "生年月日",
  member_type: "会員の種別",
  committee: "所属委員会",
  committees: "所属委員会",
  committee_role: "委員会での役割",
  officer_role: "役職",
  secondments: "出向",
  contact: "連絡先",
  line_user_id: "LINE 連携",
};

/** detail の「key=value」の key */
const KEY: Record<string, string> = {
  sent: "送信",
  blocked: "見送り",
  deferred: "あとで送信",
  failed: "失敗",
  created: "新規",
  updated: "更新",
  pending: "未回答",
  stages: "段階",
  texts: "回答",
  notify: "通知",
};

const EVENT_STATUS: Record<string, string> = {
  draft: "下書き",
  open: "出欠受付中",
  closed: "締切",
  cancelled: "中止",
};

const POST_KIND: Record<string, string> = {
  link_invite: "LINE 連携のお願い",
  poll_announce: "日程調整の案内",
};

const AUTO_ACTORS = new Set(["", "unknown", "scheduled", "system", "tick", "scheduler"]);
const LINE_ID = /\b[UCR][0-9a-f]{32}\b/g;
const IS_LINE_ID = /^[UCR][0-9a-f]{32}$/;
const SYS_ID = /\b(?:ev|poll|post|prop|esc|nta|ntc|ntca|svy|svyrep|mem|manual|rp)_[^\s,、・→@()（）]+/g;
const MEMBER_ID = /\bm\d+\b/g;

type Lookup = {
  memberById: Map<string, string>;
  memberByLine: Map<string, string>;
  memberByEmail: Map<string, string>;
  titles: Map<string, string>;
  groups: Map<string, string>;
};

function actorLabel(raw: string, lk: Lookup): { label: string; auto: boolean } {
  const a = (raw ?? "").replace(/^accounts\.google\.com:/, "").trim();
  if (AUTO_ACTORS.has(a.toLowerCase())) return { label: "自動処理", auto: true };
  if (a === "gcal") return { label: "カレンダー連携（自動）", auto: true };
  if (a.startsWith("line:")) {
    const id = a.slice(5);
    const name = lk.memberById.get(id) ?? lk.memberByLine.get(id);
    return { label: name ? `${name}（LINE から）` : "会員（LINE から）", auto: false };
  }
  const byMail = lk.memberByEmail.get(a.toLowerCase());
  if (byMail) return { label: byMail, auto: false };
  if (IS_LINE_ID.test(a)) return { label: "メンバー", auto: false };
  return { label: a, auto: false };
}

/** 監査の対象（target）を名前・タイトルにする。分からない ID は出さない。 */
function targetLabel(a: Audit, lk: Lookup): string | null {
  const t = a.target;
  if (!t) return null;
  if (lk.titles.has(t)) return lk.titles.get(t)!;
  if (lk.memberById.has(t)) return `${lk.memberById.get(t)} さん`;
  if (lk.memberByLine.has(t)) return `${lk.memberByLine.get(t)} さん`;
  if (lk.groups.has(t)) return lk.groups.get(t)!;
  const rp = /^rp_(.+?)_default$/.exec(t);
  if (rp) return `${rp[1]}の催促`;
  return null;
}

/** 文中に残った ID を名前に置き換える（分からないものは消す）。 */
function scrub(text: string, lk: Lookup): string {
  return text
    .replace(LINE_ID, (id) => lk.groups.get(id) ?? lk.memberByLine.get(id) ?? (id.startsWith("U") ? "メンバー" : "グループ"))
    .replace(SYS_ID, (id) => lk.titles.get(id) ?? "")
    .replace(MEMBER_ID, (id) => lk.memberById.get(id) ?? "会員")
    .replace(/\s{2,}/g, " ")
    .trim();
}

function keyValues(detail: string): string | null {
  const pairs = detail.split(/\s+/).map((p) => /^([a-z_]+)=(.*)$/.exec(p));
  if (pairs.length === 0 || pairs.some((m) => !m)) return null;
  return pairs
    .map((m) => {
      const [, k, v] = m!;
      const val = v === "True" || v === "true" ? "あり" : v === "False" || v === "false" ? "なし" : v;
      return `${KEY[k] ?? "件数"} ${val}`;
    })
    .join("・");
}

function detailLabel(a: Audit, lk: Lookup): string | null {
  const d = (a.detail ?? "").trim();
  if (!d) return null;
  switch (a.action) {
    case "settings.update": {
      const on = /kill_switch=(True|true)/.test(d);
      return `送信の一時停止: ${on ? "停止中" : "送信中"}`;
    }
    case "policy.update": {
      const n = /stages=(\d+)/.exec(d);
      return n ? `${n[1]}段階` : null;
    }
    case "event.update":
    case "event.update_from_gcal":
    case "member.update": {
      const fields = d.split(",").map((s) => s.trim()).filter(Boolean);
      const known = [...new Set(fields.map((f) => FIELD[f]).filter(Boolean))];
      const rest = fields.filter((f) => !FIELD[f]).length;
      if (known.length === 0) return rest ? "内容を変更" : null;
      return `${known.join("・")}${rest ? " ほか" : ""}を変更`;
    }
    case "event.restore": {
      const s = d.replace(/^EventStatus\./, "");
      return `状態: ${EVENT_STATUS[s] ?? "元に戻しました"}`;
    }
    case "post.schedule":
    case "post.send": {
      // 「{kind} → {group_id} @ {send_at}」
      const m = /^(\S+)\s*→\s*(\S+)(?:\s*@\s*(.+))?$/.exec(d);
      if (m) {
        const kind = POST_KIND[m[1]] ?? "投稿";
        const group = lk.groups.get(m[2]) ?? "グループ";
        const at = m[3] ? `（${fmtDateTime(m[3].trim(), m[3].includes("T"))} に送信）` : "";
        return `${kind} → ${group}${at}`;
      }
      break;
    }
    case "link.approve": {
      // 「{会員名} ← {LINE の表示名 または LINE の内部ID}」
      const m = /^(.*?)\s*←\s*(.*)$/.exec(d);
      const shown = m ? m[2].trim() : "";
      return shown && !IS_LINE_ID.test(shown) ? `LINE の表示名: ${shown}` : null;
    }
    case "link.request":
    case "link.reject":
      return `LINE の表示名: ${scrub(d, lk) || "（なし）"}`;
    case "notice.actions.done":
      return lk.memberById.get(d) ? `${lk.memberById.get(d)} さん` : null;
  }
  const kv = keyValues(d);
  if (kv) return kv;
  const s = scrub(d, lk);
  return s || null;
}

// ---------------------------------------------------------------- 画面
export default function Agent() {
  const [ov, setOv] = useState<Overview | null>(null);
  const [trends, setTrends] = useState<Trend[]>([]);
  const [logs, setLogs] = useState<DeliveryLog[] | null>(null);
  const [audit, setAudit] = useState<Audit[] | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [titles, setTitles] = useState<Map<string, string>>(new Map());
  const [groups, setGroups] = useState<Map<string, string>>(new Map());
  const [filter, setFilter] = useState<ResultFilter>("");
  const [logLimit, setLogLimit] = useState(50);
  const [auditLimit, setAuditLimit] = useState(30);
  const [err, setErr] = useState<string | null>(null);

  const loadLogs = (result: ResultFilter) => {
    setLogs(null);
    api<DeliveryLog[]>(`/delivery-logs${result ? `?result=${result}` : ""}`)
      .then(setLogs)
      .catch(() => setLogs([]));
  };

  useEffect(() => {
    api<Overview>("/kpi/overview").then(setOv).catch((e) => setErr(e.message));
    api<Trend[]>("/kpi/trends").then(setTrends).catch(() => {});
    loadLogs("");
    api<Audit[]>("/audit-logs?limit=200").then(setAudit).catch(() => setAudit([]));
    api<Member[]>("/members").then(setMembers).catch(() => {});
    // 操作の対象を名前で出すための参照（読めなくても画面は出す）
    Promise.allSettled([
      api<(Titled & { event_id: string })[]>("/events?include_previous=true"),
      api<(Titled & { poll_id: string })[]>("/polls"),
    ]).then(([ev, po]) => {
      const m = new Map<string, string>();
      if (ev.status === "fulfilled") for (const e of ev.value) m.set(e.event_id, e.title);
      if (po.status === "fulfilled") for (const p of po.value) m.set(p.poll_id, p.title);
      setTitles(m);
    });
    api<Group[]>("/line/groups")
      .then((gs) => setGroups(new Map(gs.filter((g) => g.group_name).map((g) => [g.group_id, g.group_name!]))))
      .catch(() => {});
  }, []);

  const lk: Lookup = useMemo(() => {
    const memberById = new Map<string, string>();
    const memberByLine = new Map<string, string>();
    const memberByEmail = new Map<string, string>();
    for (const m of members) {
      memberById.set(m.member_id, m.name);
      if (m.line_user_id) memberByLine.set(m.line_user_id, m.name);
      if (m.contact?.email) memberByEmail.set(m.contact.email.toLowerCase(), m.name);
    }
    return { memberById, memberByLine, memberByEmail, titles, groups };
  }, [members, titles, groups]);

  const pct = (x: number) => `${Math.round(x * 100)}`;

  return (
    <div>
      <PageHeader
        title="送信の記録"
        description="LINE への送信の結果と、管理画面や自動処理で行った操作の記録です。"
      />

      {err && <ErrorNote>{err}</ErrorNote>}

      {ov && (
        <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
          <Stat label="送信の成功率" value={pct(ov.delivery_success_rate)} unit="%" sub={`送信 ${ov.delivery_total.toLocaleString()}件`} />
          <Stat label="出欠の平均回答率" value={pct(ov.avg_answer_rate)} unit="%" />
          <Stat label="平均出席率" value={pct(ov.avg_attendance_rate)} unit="%" />
          <Stat label="催促の回数" value={ov.reminder_count.toLocaleString()} unit="回" />
          <Stat label="今月の AI の費用" value={`$${ov.cost.monthly_cost_usd.toFixed(2)}`} sub="概算" />
        </div>
      )}

      {/* ------------------------------------------------ 送信の記録 */}
      <Panel title="LINE への送信" flush>
        <div className="px-5 pb-3">
          <Segmented
            value={filter}
            onChange={(v) => {
              setFilter(v);
              setLogLimit(50);
              loadLogs(v);
            }}
            options={[
              { value: "", label: "すべて" },
              { value: "ok", label: "成功" },
              { value: "blocked", label: "見送り" },
              { value: "failed", label: "失敗" },
            ]}
          />
        </div>
        {logs === null ? (
          <Loading />
        ) : logs.length === 0 ? (
          <div className="px-5 pb-3">
            <EmptyState title={filter ? "該当する送信はありません" : "まだ送信の記録はありません"} />
          </div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="tbl">
                <thead>
                  <tr>
                    <th>日時</th>
                    <th>会員</th>
                    <th>結果</th>
                    <th>理由</th>
                    <th className="hidden lg:table-cell">送った内容</th>
                  </tr>
                </thead>
                <tbody>
                  {logs.slice(0, logLimit).map((l) => {
                    const r = RESULT[l.result] ?? { label: "その他", tone: "gray" as const };
                    return (
                      <tr key={l.log_id}>
                        <td className="num whitespace-nowrap text-ink-sub">{fmtDateTime(l.sent_at)}</td>
                        <td className="whitespace-nowrap font-bold">
                          {lk.memberById.get(l.member_id) ?? <span className="font-normal text-ink-sub">名簿にない会員</span>}
                        </td>
                        <td><Badge tone={r.tone}>{r.label}</Badge></td>
                        <td className="min-w-[180px] text-ink-sub">
                          {l.reason ? REASON[l.reason] ?? "その他の理由" : l.result === "ok" ? "—" : "理由の記録なし"}
                        </td>
                        <td className="hidden max-w-[320px] lg:table-cell">
                          {l.content && <div className="truncate text-xs text-ink-sub" title={l.content}>{l.content}</div>}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <ListFooter shown={Math.min(logLimit, logs.length)} total={logs.length} onMore={() => setLogLimit((n) => n + 50)} />
          </>
        )}
      </Panel>

      {/* ------------------------------------------------ 操作の記録 */}
      <Panel title="操作の記録" flush>
        {audit === null ? (
          <Loading />
        ) : audit.length === 0 ? (
          <div className="px-5 pb-3">
            <EmptyState title="まだ操作の記録はありません" />
          </div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="tbl">
                <thead>
                  <tr>
                    <th>日時</th>
                    <th>操作した人</th>
                    <th>操作</th>
                    <th>対象</th>
                  </tr>
                </thead>
                <tbody>
                  {audit.slice(0, auditLimit).map((a, i) => {
                    const who = actorLabel(a.actor, lk);
                    const what = ACTION[a.action];
                    const target = targetLabel(a, lk);
                    const detail = detailLabel(a, lk);
                    return (
                      <tr key={`${a.at}-${i}`}>
                        <td className="num whitespace-nowrap text-ink-sub">{fmtDateTime(a.at)}</td>
                        <td className="whitespace-nowrap">
                          {who.auto ? <Badge tone="gray">{who.label}</Badge> : <span className="font-bold">{who.label}</span>}
                        </td>
                        <td className="whitespace-nowrap">
                          {what ?? (
                            <>
                              操作 <span className="text-xs text-ink-faint">{a.action}</span>
                            </>
                          )}
                        </td>
                        <td className="min-w-[200px]">
                          {target ? (
                            <>
                              <div className="font-bold">{target}</div>
                              {detail && detail !== target && <div className="text-xs text-ink-sub">{detail}</div>}
                            </>
                          ) : detail ? (
                            <div>{detail}</div>
                          ) : (
                            <span className="text-ink-faint">—</span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <ListFooter shown={Math.min(auditLimit, audit.length)} total={audit.length} onMore={() => setAuditLimit((n) => n + 30)} />
          </>
        )}
      </Panel>

      {/* ------------------------------------------------ 推移・費用 */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
        {trends.length > 0 && (
          <Panel title="予定ごとの回答率・出席率" className="lg:col-span-3">
            <div style={{ width: "100%", height: 240 }}>
              <ResponsiveContainer>
                <BarChart
                  data={trends.map((t) => ({
                    title: t.title,
                    回答率: Math.round(t.answer_rate * 100),
                    出席率: Math.round(t.attendance_rate * 100),
                  }))}
                  margin={{ top: 8, right: 8, left: -16, bottom: 0 }}
                >
                  <CartesianGrid vertical={false} stroke="#F0F0F2" />
                  <XAxis dataKey="title" tick={{ fontSize: 11, fill: "#6B7280" }} tickLine={false} axisLine={{ stroke: "#E5E7EB" }} />
                  <YAxis domain={[0, 100]} unit="%" tick={{ fontSize: 11, fill: "#6B7280" }} tickLine={false} axisLine={false} />
                  <Tooltip formatter={(v) => `${v}%`} cursor={{ fill: "#F7F7F8" }} />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  <Bar dataKey="回答率" fill="#06C755" radius={[4, 4, 0, 0]} />
                  <Bar dataKey="出席率" fill="#9BE3B9" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </Panel>
        )}

        {ov && (
          <Panel title="今月の AI の費用（概算）" className={trends.length > 0 ? "lg:col-span-2" : "lg:col-span-5"}>
            <dl className="divide-y divide-gray-100 text-sm">
              <Row label="集計期間">{ov.cost.month_start.slice(0, 7).replace("-", "年")}月1日 〜 今日</Row>
              <Row label="合計">
                <span className="num text-lg font-extrabold">${ov.cost.monthly_cost_usd.toFixed(2)}</span>
              </Row>
              <Row label="内訳">
                <span className="num">AI ${ov.cost.llm_cost_usd.toFixed(2)} ＋ サーバー ${ov.cost.infra_cost_usd.toFixed(2)}</span>
              </Row>
              <Row label="AI の利用">
                <span className="num">{ov.cost.calls.toLocaleString()}回</span>
                {ov.cost.failed_calls > 0 && <span className="ml-2"><Badge tone="red">うまくいかなかった {ov.cost.failed_calls}回</Badge></span>}
              </Row>
              <Row label="処理した量">
                <span className="num text-ink-sub">
                  読み込み {ov.cost.input_tokens.toLocaleString()}・書き出し {ov.cost.output_tokens.toLocaleString()}
                </span>
              </Row>
            </dl>
            <p className="mt-3 text-xs text-ink-faint">
              記録した利用量から計算した目安です。実際の請求額は Google Cloud の請求の画面で確認してください。
            </p>
          </Panel>
        )}
      </div>
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 py-2">
      <dt className="w-24 shrink-0 text-xs text-ink-sub">{label}</dt>
      <dd className="min-w-0 flex-1">{children}</dd>
    </div>
  );
}

function ListFooter({ shown, total, onMore }: { shown: number; total: number; onMore: () => void }) {
  return (
    <div className="flex items-center justify-between gap-2 px-5 pt-2 text-xs text-ink-sub">
      <span className="num">{total}件中 {shown}件を表示</span>
      {shown < total && <Button size="sm" variant="ghost" onClick={onMore}>もっと見る</Button>}
    </div>
  );
}
