import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import {
  Badge,
  Button,
  EmptyState,
  ErrorNote,
  Loading,
  PageHeader,
  Panel,
  Progress,
  Stat,
  fmtDateTime,
  useUi,
} from "../components/ui";

// ホーム「今日やること」（docs/admin-ui-design.md §3）

type AgendaItem = {
  event_id: string;
  title: string;
  type: string;
  status: "draft" | "open" | "closed" | "cancelled";
  datetime_start: string;
  location: string | null;
};
type PollBrief = {
  poll_id: string;
  title: string;
  answered: number;
  targets: number;
  deadline: string | null;
  leading: string | null;
};
type HomeData = {
  kill_switch: boolean;
  member_count: number;
  action_required: {
    unlinked_members: number;
    open_escalations: number;
    unanswered_total: number;
    open_notice_actions: number;
    link_requests: number;
  };
  agenda: AgendaItem[];
  polls: PollBrief[];
};
type Escalation = {
  escalation_id: string;
  member_name: string | null;
  member_id: string;
  kind: string;
  text: string | null;
  created_at: string;
};
type LinkItem = {
  line_user_id: string;
  display_name: string | null;
  member_id: string;
  member_name: string | null;
  requested_at?: string | null;
  source?: string;
};
type LinkData = { requests: LinkItem[]; candidates: LinkItem[] };

const STATUS: Record<AgendaItem["status"], { label: string; tone: "gray" | "green" | "blue" }> = {
  draft: { label: "下書き", tone: "gray" },
  open: { label: "出欠受付中", tone: "green" },
  closed: { label: "締切", tone: "blue" },
  cancelled: { label: "中止", tone: "gray" },
};

function today(): string {
  const d = new Date();
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日(${"日月火水木金土"[d.getDay()]})`;
}

/** 日付ごとにまとめる（「10/30(金)」→ 予定の配列） */
function byDay(items: AgendaItem[]): [string, AgendaItem[]][] {
  const map = new Map<string, AgendaItem[]>();
  for (const it of items) {
    const key = fmtDateTime(it.datetime_start, false);
    map.set(key, [...(map.get(key) ?? []), it]);
  }
  return [...map.entries()];
}

export default function Home() {
  const { confirm, toast } = useUi();
  const [data, setData] = useState<HomeData | null>(null);
  const [escalations, setEscalations] = useState<Escalation[]>([]);
  const [links, setLinks] = useState<LinkData>({ requests: [], candidates: [] });
  const [err, setErr] = useState<string | null>(null);

  const load = () => {
    api<HomeData>("/home")
      // 古いサーバー（agenda・polls が無い）でも表示できるようにする
      .then((h) => setData({ ...h, agenda: h.agenda ?? [], polls: h.polls ?? [] }))
      .catch((e) => setErr(e.message));
    api<Escalation[]>("/escalations?status=open").then(setEscalations).catch(() => {});
    api<LinkData>("/line-link").then(setLinks).catch(() => {});
  };
  useEffect(load, []);

  async function approve(item: LinkItem) {
    const ok = await confirm({
      title: `${item.member_name} さんの LINE 連携を承認しますか？`,
      body: <>LINE の表示名「{item.display_name ?? "（なし）"}」が本人であることを確認してください。承認すると本人に完了のお知らせが届きます。</>,
      confirmLabel: "承認する",
    });
    if (!ok) return;
    try {
      await api(`/line-link/${item.line_user_id}/approve`, {
        method: "POST",
        body: JSON.stringify({ member_id: item.member_id }),
      });
      toast(`${item.member_name} さんの LINE 連携を承認しました`);
      load();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  }

  async function handled(e: Escalation) {
    const ok = await confirm({
      title: "対応済みにしますか？",
      body: <>{e.member_name ?? "会員"} さんからの「{e.text ?? "（内容なし）"}」</>,
      confirmLabel: "対応済みにする",
    });
    if (!ok) return;
    try {
      await api(`/escalations/${e.escalation_id}/handled`, { method: "POST" });
      toast("対応済みにしました");
      load();
    } catch (err) {
      toast((err as Error).message, "error");
    }
  }

  if (err) return <ErrorNote>{err}</ErrorNote>;
  if (!data) return <Loading />;

  const a = data.action_required;
  const linkRows = [...links.requests, ...links.candidates];
  const todoCount = linkRows.length + escalations.length;
  const pollAnswered = data.polls.reduce((n, p) => n + p.answered, 0);
  const pollTargets = data.polls.reduce((n, p) => n + p.targets, 0);
  const linked = data.member_count - a.unlinked_members;

  return (
    <div>
      <PageHeader
        title="今日やること"
        description={today()}
        actions={
          <>
            <Link to="/polls?new=1"><Button>日程調整を作る</Button></Link>
            <Link to="/events?new=1"><Button variant="primary">予定を登録</Button></Link>
          </>
        }
      />

      {data.kill_switch && (
        <ErrorNote>
          いま LINE への送信を一時停止しています。出欠の催促やお知らせは送られません。{" "}
          <Link to="/settings" className="font-bold underline">設定で再開する</Link>
        </ErrorNote>
      )}

      <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="対応が必要" value={todoCount} unit="件" highlight={todoCount > 0} sub={todoCount ? "下の一覧から対応できます" : "ありません"} />
        <Stat label="日程調整の回答" value={pollTargets ? `${pollAnswered}/${pollTargets}` : "—"} unit={pollTargets ? "人" : undefined}
          sub={data.polls.length ? `受付中 ${data.polls.length}件` : "受付中なし"} to="/polls" />
        <Stat label="LINE 連携" value={`${linked}/${data.member_count}`} unit="人" sub={a.unlinked_members ? `未連携 ${a.unlinked_members}人` : "全員連携済み"} to="/members" />
        <Stat label="今後2週間の予定" value={data.agenda.length} unit="件" to="/events" />
      </div>

      <div className="grid gap-4 lg:grid-cols-5">
        <Panel title="対応が必要なこと" className="lg:col-span-3" flush>
          {todoCount === 0 ? (
            <div className="px-5 pb-3"><EmptyState title="対応が必要なものはありません" /></div>
          ) : (
            <div className="overflow-x-auto">
              <table className="tbl">
                <thead><tr><th>内容</th><th>相手</th><th>受付</th><th /></tr></thead>
                <tbody>
                  {linkRows.map((r) => (
                    <tr key={r.line_user_id}>
                      <td className="whitespace-nowrap"><Badge tone="green">LINE 連携</Badge></td>
                      <td>
                        <div className="font-bold">{r.member_name}</div>
                        <div className="text-xs text-ink-sub">LINE名: {r.display_name ?? "（なし）"}{r.source ? `・${r.source}` : ""}</div>
                      </td>
                      <td className="num whitespace-nowrap text-ink-sub">{r.requested_at ? fmtDateTime(r.requested_at) : "—"}</td>
                      <td className="text-right"><Button size="sm" variant="primary" onClick={() => approve(r)}>承認</Button></td>
                    </tr>
                  ))}
                  {escalations.map((e) => (
                    <tr key={e.escalation_id}>
                      <td className="whitespace-nowrap"><Badge tone="amber">{e.kind === "question" ? "AI が答えられなかった質問" : e.kind === "contact" ? "連絡希望" : "その他"}</Badge></td>
                      <td>
                        <div className="font-bold">{e.member_name ?? "会員"}</div>
                        <div className="text-xs text-ink-sub">「{e.text ?? "（内容なし）"}」</div>
                      </td>
                      <td className="num whitespace-nowrap text-ink-sub">{fmtDateTime(e.created_at)}</td>
                      <td className="text-right"><Button size="sm" onClick={() => handled(e)}>対応済み</Button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel title="日程調整" action={<Link to="/polls" className="font-bold text-line-ink">一覧へ</Link>} className="lg:col-span-2">
          {data.polls.length === 0 ? (
            <EmptyState title="受付中の日程調整はありません" action={<Link to="/polls?new=1"><Button size="sm">日程調整を作る</Button></Link>} />
          ) : (
            <ul className="space-y-3">
              {data.polls.map((p) => (
                <li key={p.poll_id}>
                  <Link to={`/polls?id=${p.poll_id}`} className="block rounded-2xl bg-gray-50 px-4 py-3 transition hover:bg-gray-100">
                    <div className="flex items-start justify-between gap-2">
                      <div className="font-bold">{p.title}</div>
                      {p.deadline && <Badge tone="red">締切 {fmtDateTime(p.deadline, false)}</Badge>}
                    </div>
                    <div className="num mt-2 flex items-center gap-3 text-xs text-ink-sub">
                      <span className="whitespace-nowrap">回答 {p.answered}/{p.targets}人</span>
                      <Progress value={p.answered} max={p.targets} />
                    </div>
                    <div className="mt-1 text-xs text-ink-sub">{p.leading ? `いちばん多い: ${p.leading}` : "まだ回答がありません"}</div>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>

      <Panel title="今後2週間の予定" action={<Link to="/events" className="font-bold text-line-ink">予定・出欠へ</Link>}>
        {data.agenda.length === 0 ? (
          <EmptyState title="2週間以内の予定はありません" />
        ) : (
          <div className="divide-y divide-gray-100">
            {byDay(data.agenda).map(([day, items]) => (
              <div key={day} className="flex gap-4 py-3">
                <div className="num w-20 shrink-0 pt-0.5 text-sm font-extrabold">{day}</div>
                <ul className="flex-1 space-y-2">
                  {items.map((e) => (
                    <li key={e.event_id} className="flex flex-wrap items-center gap-x-3 gap-y-1">
                      <span className="num text-sm text-ink-sub">{new Date(e.datetime_start).toTimeString().slice(0, 5)}</span>
                      <Link to={`/events?id=${e.event_id}`} className="font-bold hover:underline">{e.title}</Link>
                      {e.location && <span className="text-xs text-ink-sub">{e.location}</span>}
                      <Badge tone={STATUS[e.status].tone}>{STATUS[e.status].label}</Badge>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        )}
      </Panel>
    </div>
  );
}
