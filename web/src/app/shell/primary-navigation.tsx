import { NavLink } from "react-router";

const NAVIGATION = [
  { to: "/", label: "Обзор", end: true },
  { to: "/transactions", label: "Операции", end: false },
  { to: "/budgets", label: "Бюджеты", end: false },
  { to: "/recurring", label: "Регулярные", end: false },
  { to: "/rates", label: "Курсы", end: false },
  { to: "/imports", label: "Импорт", end: false },
  { to: "/draft", label: "Черновик", end: true },
] as const;

export function PrimaryNavigation() {
  return (
    <nav
      aria-label="Основная навигация"
      className="border-t border-white/8 bg-[#0a1214]/95 px-[max(1rem,var(--tg-content-safe-area-left,0px))] pb-[max(0.5rem,var(--tg-content-safe-area-bottom,0px))] pt-2 backdrop-blur-xl"
    >
      <div className="mx-auto flex max-w-xl gap-1 overflow-x-auto">
        {NAVIGATION.map((item) => (
          <NavLink
            className={({ isActive }) =>
              `grid min-h-11 min-w-20 flex-1 place-items-center rounded-2xl px-2 text-xs font-semibold outline-none transition-colors focus-visible:ring-2 focus-visible:ring-amber-200 ${
                isActive ? "bg-white/10 text-stone-50" : "text-stone-500 hover:text-stone-200"
              }`
            }
            end={item.end}
            key={item.to}
            to={item.to}
          >
            {item.label}
          </NavLink>
        ))}
      </div>
    </nav>
  );
}
