import { NavLink, Outlet } from "react-router-dom";

const nav = [
  { to: "/", label: "ホーム", end: true },
  { to: "/members", label: "会員管理" },
  { to: "/events", label: "出欠管理" },
  { to: "/proposals", label: "議案" },
  { to: "/notices", label: "対外連絡" },
  { to: "/agent", label: "エージェントKPI" },
  { to: "/settings", label: "設定" },
];

export default function App() {
  return (
    <div className="min-h-screen">
      <header className="bg-navy text-white px-5 py-3 text-lg font-semibold">
        JCI専務理事エージェント — 管理ダッシュボード
      </header>
      <div className="flex">
        <nav className="w-48 shrink-0 bg-white border-r min-h-[calc(100vh-52px)] p-2">
          {nav.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              className={({ isActive }) =>
                `block px-3 py-2 rounded-md text-sm mb-1 ${
                  isActive ? "bg-brand text-white" : "text-slate-700 hover:bg-slate-100"
                }`
              }
            >
              {n.label}
            </NavLink>
          ))}
        </nav>
        <main className="flex-1 p-5 max-w-5xl">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
