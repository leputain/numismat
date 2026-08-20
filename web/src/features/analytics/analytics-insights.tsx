import type { DashboardResponse } from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { EmptyState } from "../../shared/components/async-state";
import { formatMoney, moneyMagnitude } from "../../shared/finance/money";
import {
  buildCurrencyComparisons,
  compareMinor,
  formatBasisPoints,
  relativeVisualPercent,
  savingsRateBasisPoints,
  shareBasisPoints,
  visualPercentFromBasisPoints,
} from "./analytics-math";

type PeriodTotals = DashboardResponse["current_period"];
type CurrencyTotals = PeriodTotals["totals"];
type CategoryTotal = DashboardResponse["top_categories"][number];

interface TrendPresentation {
  readonly className: string;
  readonly text: string;
}

function expenseTrend(currentMinor: string, comparableMinor: string): TrendPresentation {
  const comparison = compareMinor(currentMinor, comparableMinor);
  if (comparison.changeBasisPoints === null) {
    return { className: "trend-chip--neutral", text: "Впервые есть расходы" };
  }
  if (comparison.direction === "flat") {
    return { className: "trend-chip--neutral", text: "Расходы как месяц назад" };
  }
  const absoluteBasisPoints =
    comparison.changeBasisPoints < 0n
      ? -comparison.changeBasisPoints
      : comparison.changeBasisPoints;
  return {
    className:
      comparison.direction === "up" ? "trend-chip--expense" : "trend-chip--income",
    text: `Расходы ${comparison.direction === "up" ? "выше" : "ниже"} на ${formatBasisPoints(absoluteBasisPoints)}`,
  };
}

function signedMoney(value: bigint, currency: string, locale: string): string {
  const formatted = formatMoney(value.toString(), currency, locale);
  return value > 0n ? `+${formatted}` : formatted;
}

export function PeriodTotalsCards({
  totals,
  emptyText = "За этот период операций ещё нет.",
}: {
  readonly totals: CurrencyTotals;
  readonly emptyText?: string;
}) {
  const { locale } = useSessionFormat();
  if (totals.length === 0) {
    return <p className="quiet-empty">{emptyText}</p>;
  }
  return (
    <div className="today-grid">
      {totals.map((total) => (
        <article className="today-card" key={total.currency}>
          <span className="summary-card__currency">{total.currency}</span>
          <p className="today-card__net">{formatMoney(total.net_minor, total.currency, locale)}</p>
          <p className="summary-card__net-label today-card__net-label">
            Доходы минус расходы
          </p>
          <div className="today-card__split">
            <span className="money-income">+ {formatMoney(total.income_minor, total.currency, locale)}</span>
            <span className="money-expense">− {formatMoney(total.expense_minor, total.currency, locale)}</span>
          </div>
        </article>
      ))}
    </div>
  );
}

export function MonthlySummaryCards({
  current,
  comparable,
}: {
  readonly current: PeriodTotals;
  readonly comparable: PeriodTotals;
}) {
  const { locale } = useSessionFormat();
  const comparisons = buildCurrencyComparisons(current.totals, comparable.totals);
  if (comparisons.length === 0) {
    return <p className="quiet-empty">В этом и прошлом месяце операций ещё нет.</p>;
  }
  return (
    <div className="summary-grid monthly-results">
      {comparisons.map(({ currency, current: currentTotal, comparable: comparableTotal }) => {
        const trend = expenseTrend(currentTotal.expense_minor, comparableTotal.expense_minor);
        return (
          <article
            className="summary-card summary-card--featured monthly-result-card"
            key={currency}
          >
            <div className="summary-card__heading">
              <span className="summary-card__currency">{currency}</span>
              <span className={`trend-chip ${trend.className}`}>{trend.text}</span>
            </div>
            <p className="summary-card__net">
              {formatMoney(currentTotal.net_minor, currency, locale)}
            </p>
            <p className="summary-card__net-label">Доходы минус расходы</p>
            <dl className="summary-card__split">
              <div>
                <dt>Доходы</dt>
                <dd className="money-income">
                  {formatMoney(currentTotal.income_minor, currency, locale)}
                </dd>
              </div>
              <div>
                <dt>Расходы</dt>
                <dd className="money-expense">
                  {formatMoney(currentTotal.expense_minor, currency, locale)}
                </dd>
              </div>
            </dl>
          </article>
        );
      })}
    </div>
  );
}

function categoryGroups(categories: readonly CategoryTotal[]): Array<[string, CategoryTotal[]]> {
  const groups = new Map<string, CategoryTotal[]>();
  for (const category of categories) {
    const items = groups.get(category.currency) ?? [];
    items.push(category);
    groups.set(category.currency, items);
  }
  return [...groups.entries()].sort(([left], [right]) => left.localeCompare(right));
}

export function CategoryShareList({
  categories,
  totals,
}: {
  readonly categories: readonly CategoryTotal[];
  readonly totals: CurrencyTotals;
}) {
  const { locale } = useSessionFormat();
  if (categories.length === 0) {
    return (
      <EmptyState title="Расходов по категориям пока нет">
        Структура появится после первого подтверждённого расхода.
      </EmptyState>
    );
  }
  return (
    <div className="category-share-groups">
      {categoryGroups(categories).map(([currency, items]) => {
        const expenseTotal =
          totals.find((total) => total.currency === currency)?.expense_minor ?? "0";
        return (
          <section aria-labelledby={`category-share-${currency}`} key={currency}>
            <div className="category-share-heading">
              <h3 id={`category-share-${currency}`}>{currency}</h3>
              <span>Доля расходов</span>
            </div>
            <div className="category-share-list">
              {items.map((item, index) => {
                const share = shareBasisPoints(item.amount_minor, expenseTotal);
                return (
                  <article className="category-share-row" key={`${currency}:${item.category_id}`}>
                    <span aria-hidden="true" className="category-share-row__rank">
                      {String(index + 1).padStart(2, "0")}
                    </span>
                    <span aria-hidden="true" className="category-share-row__emoji">
                      {item.emoji || "·"}
                    </span>
                    <span className="category-share-row__body">
                      <span className="category-share-row__label">
                        <strong>{item.name}</strong>
                        <span>{formatBasisPoints(share)}</span>
                      </span>
                      <span aria-hidden="true" className="category-share-row__track">
                        <span
                          className="category-share-row__fill"
                          style={{ width: `${String(visualPercentFromBasisPoints(share))}%` }}
                        />
                      </span>
                      <span className="category-share-row__amount">
                        {formatMoney(item.amount_minor, currency, locale)}
                      </span>
                    </span>
                  </article>
                );
              })}
            </div>
          </section>
        );
      })}
      <p className="analytics-footnote">
        Показаны крупнейшие категории. Валюты считаются отдельно и не складываются между собой.
      </p>
    </div>
  );
}

export function PeriodComparisonCards({
  current,
  comparable,
}: {
  readonly current: PeriodTotals;
  readonly comparable: PeriodTotals;
}) {
  const { locale } = useSessionFormat();
  const comparisons = buildCurrencyComparisons(current.totals, comparable.totals);
  if (comparisons.length === 0) {
    return (
      <EmptyState title="Сравнивать пока нечего">
        После первых операций здесь появится динамика по каждой валюте.
      </EmptyState>
    );
  }
  return (
    <div className="comparison-grid analytics-comparison-grid">
      {comparisons.map(({ currency, current: currentTotal, comparable: comparableTotal }) => {
        const expenseComparison = compareMinor(
          currentTotal.expense_minor,
          comparableTotal.expense_minor,
        );
        const maximumExpense =
          moneyMagnitude(currentTotal.expense_minor) > moneyMagnitude(comparableTotal.expense_minor)
            ? currentTotal.expense_minor
            : comparableTotal.expense_minor;
        const savingsRate = savingsRateBasisPoints(
          currentTotal.income_minor,
          currentTotal.expense_minor,
        );
        const trend = expenseTrend(currentTotal.expense_minor, comparableTotal.expense_minor);
        return (
          <article className="comparison-card analytics-comparison-card" key={currency}>
            <div className="comparison-card__topline">
              <span className="comparison-card__currency">{currency}</span>
              <span className={`trend-chip ${trend.className}`}>{trend.text}</span>
            </div>
            <div className="comparison-card__hero">
              <div>
                <span>Расходы за месяц</span>
                <strong>{formatMoney(currentTotal.expense_minor, currency, locale)}</strong>
              </div>
              <div>
                <span>К прошлому месяцу</span>
                <strong className={expenseComparison.delta > 0n ? "money-expense" : "money-income"}>
                  {signedMoney(expenseComparison.delta, currency, locale)}
                </strong>
              </div>
            </div>
            <div className="comparison-bars" aria-label={`Сравнение расходов в ${currency}`}>
              <div>
                <span>Этот месяц</span>
                <i aria-hidden="true">
                  <b
                    style={{
                      width: `${String(relativeVisualPercent(currentTotal.expense_minor, maximumExpense))}%`,
                    }}
                  />
                </i>
              </div>
              <div>
                <span>Прошлый</span>
                <i aria-hidden="true">
                  <b
                    style={{
                      width: `${String(relativeVisualPercent(comparableTotal.expense_minor, maximumExpense))}%`,
                    }}
                  />
                </i>
              </div>
            </div>
            <dl className="comparison-card__metrics">
              <div>
                <dt>Доходы</dt>
                <dd>{formatMoney(currentTotal.income_minor, currency, locale)}</dd>
              </div>
              <div>
                <dt>Итог</dt>
                <dd>{formatMoney(currentTotal.net_minor, currency, locale)}</dd>
              </div>
              <div>
                <dt>Остаток от доходов</dt>
                <dd>{savingsRate === null ? "Нет доходов" : formatBasisPoints(savingsRate)}</dd>
              </div>
            </dl>
          </article>
        );
      })}
    </div>
  );
}
