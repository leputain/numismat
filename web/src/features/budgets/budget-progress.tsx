import type { Budget } from "../../shared/api/types";
import { formatMoney } from "../../shared/finance/money";

export type BudgetProgressState = Budget["progress"]["state"];

interface BudgetStatePresentation {
  readonly label: string;
  readonly description: string;
  readonly dashboardTitle: string;
}

export const BUDGET_STATE_PRESENTATION: Readonly<
  Record<BudgetProgressState, BudgetStatePresentation>
> = Object.freeze({
  on_track: {
    label: "В плане",
    description: "После известных регулярных расходов остаётся запас.",
    dashboardTitle: "Расходы под контролем",
  },
  watch: {
    label: "Нужен запас",
    description: "Известные регулярные расходы исчерпывают доступный лимит.",
    dashboardTitle: "Регулярные расходы требуют внимания",
  },
  over: {
    label: "Лимит превышен",
    description: "Фактические расходы уже выше установленного лимита.",
    dashboardTitle: "Лимит уже превышен",
  },
});

const BUDGET_STATE_PRIORITY: Readonly<Record<BudgetProgressState, number>> = Object.freeze({
  on_track: 0,
  watch: 1,
  over: 2,
});

export function budgetStatePriority(state: BudgetProgressState): number {
  return BUDGET_STATE_PRIORITY[state];
}

export function BudgetStateBadge({ state }: { readonly state: BudgetProgressState }) {
  return (
    <span
      aria-label={`Статус бюджета: ${BUDGET_STATE_PRESENTATION[state].label}`}
      className="budget-state-badge"
      data-state={state}
    >
      <span aria-hidden="true" className="budget-state-badge__dot" />
      {BUDGET_STATE_PRESENTATION[state].label}
    </span>
  );
}

export function BudgetStateLegend() {
  const states: readonly BudgetProgressState[] = ["on_track", "watch", "over"];
  return (
    <aside aria-label="Легенда статусов бюджета" className="budget-state-legend">
      <span className="budget-state-legend__title">Статус прогноза</span>
      <span className="budget-state-legend__items">
        {states.map((state) => (
          <span className="budget-state-legend__item" data-state={state} key={state}>
            <span aria-hidden="true" className="budget-state-legend__dot" />
            {BUDGET_STATE_PRESENTATION[state].label}
          </span>
        ))}
      </span>
    </aside>
  );
}

export function BudgetProgressOverview({
  budget,
  locale,
  compact = false,
}: {
  readonly budget: Budget;
  readonly locale: string;
  readonly compact?: boolean;
}) {
  const progress = budget.progress;
  const state = progress.state;
  const percentage = Math.floor(progress.progress_bps / 100);
  const width = Math.max(0, Math.min(100, percentage));
  const spent = formatMoney(progress.spent_minor, budget.currency, locale);
  const limit = formatMoney(budget.limit_minor, budget.currency, locale);

  return (
    <div
      className={`budget-progress-overview${compact ? " budget-progress-overview--compact" : ""}`}
      data-state={state}
    >
      <div className="budget-progress-overview__status">
        <BudgetStateBadge state={state} />
        <span className="budget-progress-overview__percentage">{String(percentage)}% лимита</span>
      </div>
      <p className="budget-progress-overview__description">
        {BUDGET_STATE_PRESENTATION[state].description}
      </p>
      <div
        aria-label={`Фактически потрачено ${spent} из лимита ${limit}`}
        aria-valuemax={100}
        aria-valuemin={0}
        aria-valuenow={width}
        aria-valuetext={`${String(percentage)}% лимита`}
        className="budget-progress-meter"
        role="progressbar"
      >
        <span className="budget-progress-meter__fill" style={{ width: `${String(width)}%` }} />
      </div>
      <dl className="budget-progress-metrics" data-mobile-layout="stack-to-grid">
        <div data-metric="spent">
          <dt>Фактически потрачено</dt>
          <dd>{spent}</dd>
        </div>
        <div data-metric="recurring">
          <dt>Известные регулярные</dt>
          <dd>{formatMoney(progress.known_recurring_minor, budget.currency, locale)}</dd>
        </div>
        <div data-metric="forecast">
          <dt>Прогноз к концу периода</dt>
          <dd>{formatMoney(progress.forecast_minor, budget.currency, locale)}</dd>
        </div>
        <div data-metric="daily">
          <dt>Безопасно в день</dt>
          <dd>{formatMoney(progress.safe_daily_minor, budget.currency, locale)}</dd>
        </div>
        <div data-metric={state === "over" ? "overspent" : "remaining"}>
          <dt>{state === "over" ? "Перерасход" : "Остаток лимита"}</dt>
          <dd>
            {formatMoney(
              state === "over" ? progress.overspent_minor : progress.remaining_minor,
              budget.currency,
              locale,
            )}
          </dd>
        </div>
      </dl>
      {compact ? null : (
        <p className="budget-progress-overview__note">
          Прогноз складывает фактические траты и известные регулярные обязательства. Повседневные
          расходы вперёд не экстраполируются; валюты не смешиваются.
        </p>
      )}
    </div>
  );
}
