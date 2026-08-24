import { Link } from "react-router";

import type { Budget } from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { formatMoney } from "../../shared/finance/money";
import { BudgetProgressOverview } from "./budget-progress";
import { formatBudgetDate } from "./budget-window";

export function BudgetCard({ budget }: { readonly budget: Budget }) {
  const { locale } = useSessionFormat();
  return (
    <Link
      className="budget-card surface-panel"
      data-state={budget.progress.state}
      to={`/budgets/${budget.id}`}
    >
      <div className="budget-card__header">
        <div className="min-w-0">
          <p className="budget-card__name">{budget.name}</p>
          <p className="budget-card__period">
            {formatBudgetDate(budget.starts_on, locale)} — {formatBudgetDate(budget.ends_on, locale)}
          </p>
        </div>
        <div className="budget-card__limit">
          <span>Лимит · {budget.currency}</span>
          <strong>{formatMoney(budget.limit_minor, budget.currency, locale)}</strong>
        </div>
      </div>
      <BudgetProgressOverview budget={budget} compact locale={locale} />
    </Link>
  );
}
