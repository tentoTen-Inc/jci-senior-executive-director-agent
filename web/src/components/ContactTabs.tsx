import { NavLink } from "react-router-dom";

// 「連絡・アンケート」の上部切替（対外連絡 / アンケート）。見た目は ui.tsx の Segmented と同じ（背景の上に置くため、選んでいない方は白）。
const TABS = [
  { to: "/notices", label: "対外連絡" },
  { to: "/surveys", label: "アンケート" },
];

export default function ContactTabs() {
  return (
    <nav aria-label="連絡・アンケートの切替" className="mb-4 inline-flex flex-wrap gap-1.5">
      {TABS.map((t) => (
        <NavLink
          key={t.to}
          to={t.to}
          className={({ isActive }) =>
            `rounded-full px-3.5 py-1.5 text-sm font-bold transition ${
              isActive ? "bg-ink text-white" : "bg-white text-ink-sub hover:bg-gray-100"
            }`
          }
        >
          {t.label}
        </NavLink>
      ))}
    </nav>
  );
}
