import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { api } from "./api/client";

// メニュー構成は docs/admin-ui-design.md §3
type Counts = { home: number; polls: number; members: number; feedback: number };
type NavItem = { to: string; label: string; end?: boolean; count?: keyof Counts; also?: string[] };

const MAIN: NavItem[] = [
  { to: "/", label: "ホーム", end: true, count: "home" },
  { to: "/events", label: "予定・出欠" },
  { to: "/polls", label: "日程調整", count: "polls" },
  { to: "/members", label: "会員・LINE連携", count: "members" },
  { to: "/proposals", label: "議案" },
  { to: "/notices", label: "連絡・アンケート", also: ["/surveys"] },
  { to: "/feedback", label: "意見箱", count: "feedback" },
];
const RECORDS: NavItem[] = [
  { to: "/line-groups", label: "LINE のやり取り" },
  { to: "/ai-answers", label: "AI の回答" },
  { to: "/agent", label: "送信の記録" },
  { to: "/settings", label: "設定" },
];

type HomeCounts = {
  action_required: { open_escalations: number; link_requests?: number; new_feedback?: number };
  polls?: unknown[];
};

function useCounts(): Counts | null {
  const { pathname } = useLocation();
  const [counts, setCounts] = useState<Counts | null>(null);
  useEffect(() => {
    api<HomeCounts>("/home")
      .then((h) => {
        const a = h.action_required;
        setCounts({
          home: a.open_escalations + (a.link_requests ?? 0),
          polls: h.polls?.length ?? 0,
          members: a.link_requests ?? 0,
          feedback: a.new_feedback ?? 0,
        });
      })
      .catch(() => {});
  }, [pathname]);
  return counts;
}

function Item({ item, counts, onNavigate }: { item: NavItem; counts: Counts | null; onNavigate: () => void }) {
  const { pathname } = useLocation();
  const n = item.count && counts ? counts[item.count] : 0;
  const alsoActive = item.also?.some((p) => pathname.startsWith(p));
  return (
    <NavLink
      to={item.to}
      end={item.end}
      onClick={onNavigate}
      className={({ isActive }) =>
        `mb-0.5 flex items-center justify-between rounded-full px-4 py-2.5 text-sm font-bold transition ${
          isActive || alsoActive ? "bg-ink text-white" : "text-ink hover:bg-gray-100"
        }`
      }
    >
      <span>{item.label}</span>
      {n > 0 && <span className="num rounded-full bg-line px-2 py-0.5 text-[11px] text-white">{n}</span>}
    </NavLink>
  );
}

export default function App() {
  const counts = useCounts();
  const [open, setOpen] = useState(false);
  const { pathname } = useLocation();
  useEffect(() => setOpen(false), [pathname]);

  const nav = (
    <nav className="flex h-full flex-col px-3 py-4">
      <div className="mb-5 flex items-center gap-2.5 px-2">
        <div className="grid h-9 w-9 place-items-center rounded-full bg-line text-sm font-extrabold text-white">専</div>
        <div>
          <div className="text-[15px] font-extrabold leading-tight">猪苗代専務AI</div>
          <div className="text-[11px] text-ink-sub">管理画面</div>
        </div>
      </div>
      {MAIN.map((i) => (
        <Item key={i.to} item={i} counts={counts} onNavigate={() => setOpen(false)} />
      ))}
      <div className="mb-1 mt-5 px-4 text-[11px] font-bold tracking-wider text-ink-faint">記録・設定</div>
      {RECORDS.map((i) => (
        <Item key={i.to} item={i} counts={counts} onNavigate={() => setOpen(false)} />
      ))}
    </nav>
  );

  return (
    <div className="min-h-screen lg:flex">
      {/* PC: 左メニュー */}
      <aside className="sticky top-0 hidden h-screen w-60 shrink-0 overflow-y-auto border-r border-gray-100 bg-white lg:block">
        {nav}
      </aside>

      {/* スマホ・タブレット: 上部バー＋引き出し */}
      <header className="sticky top-0 z-30 flex items-center justify-between border-b border-gray-100 bg-white px-4 py-3 lg:hidden">
        <div className="flex items-center gap-2">
          <div className="grid h-8 w-8 place-items-center rounded-full bg-line text-xs font-extrabold text-white">専</div>
          <span className="font-extrabold">猪苗代専務AI</span>
        </div>
        <button
          type="button"
          className="rounded-full bg-gray-100 px-4 py-1.5 text-sm font-bold"
          onClick={() => setOpen(true)}
          aria-label="メニューを開く"
        >
          メニュー{counts && counts.home > 0 ? `（${counts.home}）` : ""}
        </button>
      </header>
      {open && (
        <div className="fixed inset-0 z-40 lg:hidden" onClick={() => setOpen(false)}>
          <div className="absolute inset-0 bg-black/30" />
          <aside className="absolute left-0 top-0 h-full w-72 overflow-y-auto bg-white" onClick={(e) => e.stopPropagation()}>
            {nav}
          </aside>
        </div>
      )}

      <main className="min-w-0 flex-1 px-4 py-6 sm:px-6 lg:px-10 lg:py-8">
        <div className="mx-auto max-w-6xl">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
