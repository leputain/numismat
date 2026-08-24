import { Link } from "react-router";

import type { Budget } from "../../shared/api/types";
import { formatMoney } from "../../shared/finance/money";
import {
  BUDGET_STATE_PRESENTATION,
  BudgetStateBadge,
  budgetStatePriority,
} from "../budgets/budget-progress";
import { budgetTransactionsPath } from "../transactions/transaction-filter-model";

function mostUrgentBudget(budgets: readonly Budget[]): Budget | undefined {
  let selected: Budget | undefined;
  for (const candidate of budgets) {
    if (
      selected === undefined ||
      budgetStatePriority(candidate.progress.state) >
        budgetStatePriority(selected.progress.state)
    ) {
      selected = candidate;
    }
  }
  return selected;
}

export function DashboardBudgetSignal({
  budgets,
  complete = true,
  locale,
}: {
  readonly budgets: readonly Budget[];
  readonly complete?: boolean;
  readonly locale: string;
}) {
  if (!complete) {
    return (
      <section className="notice notice--warning">
        <div>
          <p className="notice__title">Проверьте список бюджетов</p>
          <p className="notice__text">
            Активных лимитов больше безопасного размера обзора, поэтому здесь не выбирается случайный статус.
          </p>
        </div>
        <Link className="button button--secondary" to="/budgets">Все бюджеты</Link>
      </section>
    );
  }
  const urgent = mostUrgentBudget(budgets);
  if (urgent === undefined) {
    return (
      <section className="notice">
        <div>
          <p className="notice__title">Лимит расходов пока не задан</p>
          <p className="notice__text">Бюджет покажет остаток и безопасный темп трат.</p>
        </div>
        <Link className="button button--secondary" to="/budgets/new">Настроить</Link>
      </section>
    );
  }

  const progress = urgent.progress;
  const state = progress.state;
  return (
    <section className="budget-signal notice" data-state={state}>
      <div className="budget-signal__content">
        <div className="budget-signal__heading">
          <div>
            <BudgetStateBadge state={state} />
            <p className="notice__title">{BUDGET_STATE_PRESENTATION[state].dashboardTitle}</p>
          </div>
          <span className="budget-signal__currency">{urgent.currency}</span>
        </div>
        <p className="notice__text">
          {urgent.name}. Каждая валюта считается отдельно.
        </p>
        <dl aria-label="Ключевые показатели бюджета" className="budget-signal__metrics">
          <div>
            <dt>Безопасно в день</dt>
            <dd>{formatMoney(progress.safe_daily_minor, urgent.currency, locale)}</dd>
          </div>
          <div>
            <dt>Известные регулярные</dt>
            <dd>{formatMoney(progress.known_recurring_minor, urgent.currency, locale)}</dd>
          </div>
        </dl>
      </div>
      <div className="flex flex-wrap gap-2">
        <Link className="button button--primary" to={budgetTransactionsPath(urgent)}>
          Показать расходы
        </Link>
        <Link className="button button--secondary" to={`/budgets/${urgent.id}`}>Подробнее</Link>
      </div>
    </section>
  );
}
