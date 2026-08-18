import { Link } from "react-router";

import type { Budget } from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { formatMoney } from "../../shared/finance/money";
import { formatBudgetDate } from "./budget-window";

export function BudgetCard({ budget }: { readonly budget: Budget }) {
  const { locale } = useSessionFormat();
  const progress = budget.progress;
  const overspent = progress.overspent_minor !== "0";
  const width = Math.max(0, Math.min(100, Math.floor(progress.progress_bps / 100)));
  return (
    <Link
      className="block rounded-[1.4rem] border border-white/8 bg-white/[0.035] p-5 outline-none transition hover:border-amber-200/25 hover:bg-white/[0.055] focus-visible:ring-2 focus-visible:ring-amber-200"
      to={`/budgets/${budget.id}`}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="truncate text-base font-semibold text-stone-100">{budget.name}</p>
          <p className="mt-1 text-xs text-stone-500">
            {formatBudgetDate(budget.starts_on, locale)} — {formatBudgetDate(budget.ends_on, locale)}
          </p>
        </div>
        <span className={overspent ? "text-sm font-semibold text-[#df9a94]" : "text-sm font-semibold text-amber-100"}>
          {String(Math.floor(progress.progress_bps / 100))}%
        </span>
      </div>
      <div
        aria-label={`Использовано ${String(Math.floor(progress.progress_bps / 100))}%`}
        aria-valuemax={100}
        aria-valuemin={0}
        aria-valuenow={width}
        className="mt-4 h-2 overflow-hidden rounded-full bg-white/8"
        role="progressbar"
      >
        <span
          className={`block h-full rounded-full ${overspent ? "bg-[#c97970]" : "bg-amber-200"}`}
          style={{ width: `${String(width)}%` }}
        />
      </div>
      <div className="mt-4 flex items-end justify-between gap-4 text-sm">
        <div>
          <p className="text-stone-500">Потрачено</p>
          <p className="mt-1 font-medium text-stone-200">
            {formatMoney(progress.spent_minor, budget.currency, locale)}
          </p>
        </div>
        <div className="text-right">
          <p className="text-stone-500">{overspent ? "Перерасход" : "Осталось"}</p>
          <p className={overspent ? "mt-1 font-medium text-[#df9a94]" : "mt-1 font-medium text-stone-200"}>
            {formatMoney(
              overspent ? progress.overspent_minor : progress.remaining_minor,
              budget.currency,
              locale,
            )}
          </p>
        </div>
      </div>
    </Link>
  );
}
