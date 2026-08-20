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
      className="budget-card surface-panel block outline-none transition-colors hover:border-[var(--nm-accent)] focus-visible:ring-2 focus-visible:ring-[var(--nm-focus)]"
      to={`/budgets/${budget.id}`}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="truncate text-base font-semibold text-[var(--nm-text)]">{budget.name}</p>
          <p className="mt-1 text-xs text-[var(--nm-muted)]">
            {formatBudgetDate(budget.starts_on, locale)} — {formatBudgetDate(budget.ends_on, locale)}
          </p>
        </div>
        <span className={overspent ? "text-sm font-semibold text-[var(--nm-danger)]" : "text-sm font-semibold text-[var(--nm-accent)]"}>
          {String(Math.floor(progress.progress_bps / 100))}%
        </span>
      </div>
      <div
        aria-label={`Использовано ${String(Math.floor(progress.progress_bps / 100))}%`}
        aria-valuemax={100}
        aria-valuemin={0}
        aria-valuenow={width}
        className="mt-4 h-2 overflow-hidden rounded-full bg-[var(--nm-line)]"
        role="progressbar"
      >
        <span
          className={`block h-full rounded-full ${overspent ? "bg-[var(--nm-danger)]" : "bg-[var(--nm-accent)]"}`}
          style={{ width: `${String(width)}%` }}
        />
      </div>
      <div className="mt-4 flex items-end justify-between gap-4 text-sm">
        <div>
          <p className="text-[var(--nm-muted)]">Потрачено</p>
          <p className="mt-1 font-medium text-[var(--nm-text)]">
            {formatMoney(progress.spent_minor, budget.currency, locale)}
          </p>
        </div>
        <div className="text-right">
          <p className="text-[var(--nm-muted)]">{overspent ? "Перерасход" : "Осталось"}</p>
          <p className={overspent ? "mt-1 font-medium text-[var(--nm-danger)]" : "mt-1 font-medium text-[var(--nm-text)]"}>
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
