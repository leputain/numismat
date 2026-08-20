import { useQuery } from "@tanstack/react-query";
import { lazy, Suspense, useMemo, useState } from "react";

import { apiClient } from "../../app/providers";
import type { DashboardResponse, TimeSeriesResponse } from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { formatExclusivePeriod, formatPeriod } from "../../shared/format/date-time";
import { formatMoney } from "../../shared/finance/money";
import { queryKeys } from "../../shared/queries/query-keys";
import { formatBasisPoints, savingsRateBasisPoints } from "./analytics-math";
import {
  ANALYTICS_PERIOD_OPTIONS,
  buildAnalyticsPeriod,
  type AnalyticsPeriodId,
  type AnalyticsPeriodSpec,
} from "./analytics-period";
import {
  buildCashflowSeries,
  summarizeCashflow,
  timeSeriesCurrencies,
  type CashflowPoint,
  type CashflowSummary,
} from "./timeseries-math";

const FinancialCharts = lazy(async () => {
  const module = await import("./financial-charts");
  return { default: module.FinancialCharts };
});

type PeriodTotals = DashboardResponse["current_period"];

function timeseriesPath(period: AnalyticsPeriodSpec): string {
  const query = new URLSearchParams({
    start: period.currentStart,
    end: period.currentEnd,
    grain: period.grain,
  });
  return `/api/v1/reports/timeseries?${query.toString()}`;
}

function CashflowSkeleton() {
  return (
    <div aria-busy="true" aria-label="Загрузка денежного потока" className="cashflow-loading" role="status">
      <div className="skeleton h-11 w-full" />
      <div className="analytics-kpis">
        <div className="analytics-kpi-hero analytics-kpi-hero--skeleton skeleton h-28 w-full" />
        <div className="analytics-kpi-strip analytics-kpi-strip--skeleton">
          <div className="skeleton h-20 w-full" />
          <div className="skeleton h-20 w-full" />
          <div className="skeleton h-20 w-full" />
        </div>
      </div>
      <div className="skeleton h-80 w-full" />
      <div className="skeleton h-44 w-full" />
      <span className="sr-only">Строим графики…</span>
    </div>
  );
}

function ChartsSkeleton() {
  return (
    <div aria-busy="true" aria-label="Подготовка интерактивных графиков" className="analytics-charts" role="status">
      <div className="skeleton h-80 w-full" />
      <div className="skeleton h-44 w-full" />
    </div>
  );
}

function CashflowTable({
  currency,
  grain,
  points,
  timeZone,
}: {
  readonly currency: string;
  readonly grain: TimeSeriesResponse["grain"];
  readonly points: readonly CashflowPoint[];
  readonly timeZone: string;
}) {
  const { locale } = useSessionFormat();
  const unit = grain === "month" ? "месяцам" : "дням";
  return (
    <details className="cashflow-table-details">
      <summary>Точные значения по {unit}</summary>
      <div className="cashflow-table-scroll">
        <table className="cashflow-table">
          <caption className="sr-only">Доходы и расходы по {unit} в валюте {currency}</caption>
          <thead>
            <tr>
              <th scope="col">Период</th>
              <th scope="col">Доходы</th>
              <th scope="col">Расходы</th>
              <th scope="col">Итог</th>
              <th scope="col">Записей</th>
            </tr>
          </thead>
          <tbody>
            {points.map((point) => (
              <tr key={point.start}>
                <th scope="row">
                  <time dateTime={point.start}>{formatPeriod(point.start, locale, timeZone)}</time>
                </th>
                <td>{formatMoney(point.incomeMinor.toString(), currency, locale)}</td>
                <td>{formatMoney(point.expenseMinor.toString(), currency, locale)}</td>
                <td>{formatMoney(point.netMinor.toString(), currency, locale)}</td>
                <td>{point.incomeCount + point.expenseCount}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function CashflowKpis({
  currency,
  summary,
}: {
  readonly currency: string;
  readonly summary: CashflowSummary;
}) {
  const { locale } = useSessionFormat();
  const savingsRate = savingsRateBasisPoints(
    summary.incomeMinor.toString(),
    summary.expenseMinor.toString(),
  );
  const totalOperations = summary.incomeCount + summary.expenseCount;
  return (
    <div className="analytics-kpis">
      <article aria-label="Итог выбранного периода" className="analytics-kpi-hero">
        <span className="analytics-kpi-hero__label">Итог периода</span>
        <strong className="analytics-kpi-hero__value">
          {formatMoney(summary.netMinor.toString(), currency, locale)}
        </strong>
        <small className="analytics-kpi-hero__caption">Доходы минус расходы</small>
      </article>
      <dl aria-label="Показатели выбранного периода" className="analytics-kpi-strip">
        <div className="analytics-kpi-metric analytics-kpi-metric--income">
          <dt>Доходы</dt>
          <dd className="analytics-kpi-metric__value">
            {formatMoney(summary.incomeMinor.toString(), currency, locale)}
          </dd>
          <dd className="analytics-kpi-metric__meta">
            <small>{summary.incomeCount} подтверждённых операций</small>
          </dd>
        </div>
        <div className="analytics-kpi-metric analytics-kpi-metric--expense">
          <dt>Расходы</dt>
          <dd className="analytics-kpi-metric__value">
            {formatMoney(summary.expenseMinor.toString(), currency, locale)}
          </dd>
          <dd className="analytics-kpi-metric__meta">
            <small>{summary.expenseCount} подтверждённых операций</small>
          </dd>
        </div>
        <div className="analytics-kpi-metric analytics-kpi-metric--savings">
          <dt>Остаток от доходов</dt>
          <dd className="analytics-kpi-metric__value">
            {savingsRate === null ? "—" : formatBasisPoints(savingsRate)}
          </dd>
          <dd className="analytics-kpi-metric__meta">
            <small>
              {savingsRate === null ? "Появится после дохода" : `${totalOperations} операций всего`}
            </small>
          </dd>
        </div>
      </dl>
    </div>
  );
}

function CashflowContent({
  period,
  response,
}: {
  readonly period: AnalyticsPeriodSpec;
  readonly response: TimeSeriesResponse;
}) {
  const { locale } = useSessionFormat();
  const currencies = useMemo(() => timeSeriesCurrencies(response.buckets), [response.buckets]);
  const [requestedCurrency, setRequestedCurrency] = useState("");
  const selectedCurrency = currencies.includes(requestedCurrency)
    ? requestedCurrency
    : (currencies[0] ?? "");
  const points = useMemo(
    () => buildCashflowSeries(response.buckets, selectedCurrency),
    [response.buckets, selectedCurrency],
  );
  const summary = useMemo(() => summarizeCashflow(points), [points]);

  if (selectedCurrency.length === 0) {
    return (
      <div className="cashflow-empty">
        <span aria-hidden="true">⌁</span>
        <strong>За выбранный период движения пока нет</strong>
        <p>Графики появятся после первой подтверждённой операции.</p>
      </div>
    );
  }

  return (
    <div className="cashflow-content">
      <div className="analytics-range-summary">
        <div aria-label="Валюта графиков" className="currency-chips" role="group">
          {currencies.map((currency) => (
            <button
              aria-pressed={currency === selectedCurrency}
              className="currency-chip"
              key={currency}
              onClick={() => setRequestedCurrency(currency)}
              type="button"
            >
              {currency}
            </button>
          ))}
        </div>
        <p aria-live="polite" className="analytics-range-caption">
          {formatExclusivePeriod(response.period.start, response.period.end, locale, response.timezone)}
          <span>{period.shortDescription}</span>
        </p>
      </div>

      {summary.incomeCount + summary.expenseCount === 0 ? (
        <div className="cashflow-empty">
          <span aria-hidden="true">⌁</span>
          <strong>В этой валюте пока нет движения</strong>
          <p>Выберите другую валюту или добавьте подтверждённую операцию.</p>
        </div>
      ) : (
        <>
          <CashflowKpis currency={selectedCurrency} summary={summary} />

          <Suspense fallback={<ChartsSkeleton />}>
            <FinancialCharts
              currency={selectedCurrency}
              grain={response.grain}
              locale={locale}
              points={points}
              timeZone={response.timezone}
            />
          </Suspense>

          <div className="cashflow-counts" aria-label="Активность в выбранном периоде">
            <div><span>Доходных операций</span><strong>{summary.incomeCount}</strong></div>
            <div><span>Расходных операций</span><strong>{summary.expenseCount}</strong></div>
            <div>
              <span>{response.grain === "month" ? "Месяцев" : "Дней"} с движением</span>
              <strong>{summary.activeDays}</strong>
            </div>
          </div>

          <CashflowTable
            currency={selectedCurrency}
            grain={response.grain}
            points={points}
            timeZone={response.timezone}
          />
        </>
      )}
    </div>
  );
}

export function CashflowTrend({
  comparable,
  current,
  timeZone,
}: {
  readonly comparable: PeriodTotals;
  readonly current: PeriodTotals;
  readonly timeZone: string;
}) {
  const [periodId, setPeriodId] = useState<AnalyticsPeriodId>("month");
  const period = useMemo(
    () =>
      buildAnalyticsPeriod(periodId, {
        currentStart: current.start,
        currentEnd: current.end,
        comparableStart: comparable.start,
        comparableEnd: comparable.end,
        timeZone,
      }),
    [comparable.end, comparable.start, current.end, current.start, periodId, timeZone],
  );
  const timeseries = useQuery({
    queryKey: queryKeys.timeseries(period.currentStart, period.currentEnd, period.grain),
    queryFn: ({ signal }) =>
      apiClient.get<TimeSeriesResponse>(timeseriesPath(period), { signal }),
    staleTime: 30_000,
  });

  return (
    <section
      aria-labelledby="cashflow-title"
      className="cashflow-section analytics-cashflow"
    >
      <div className="section-heading analytics-cashflow__heading">
        <div>
          <p className="eyebrow">Динамика бюджета</p>
          <h2 className="section-title" id="cashflow-title">Движение за период</h2>
          <p className="section-description">
            Выберите горизонт и валюту. Коснитесь столбца или точки, чтобы увидеть точные суммы.
          </p>
        </div>
      </div>

      <div className="analytics-toolbar">
        <div aria-label="Период аналитики" className="analytics-period-tabs" role="group">
          {ANALYTICS_PERIOD_OPTIONS.map((option) => (
            <button
              aria-pressed={periodId === option.id}
              className="analytics-period-tab"
              key={option.id}
              onClick={() => setPeriodId(option.id)}
              type="button"
            >
              <strong>{option.label}</strong>
              <span>{option.shortDescription}</span>
            </button>
          ))}
        </div>
      </div>

      {timeseries.isPending ? (
        <CashflowSkeleton />
      ) : timeseries.isError ? (
        <div className="inline-state" role="alert">
          <div>
            <strong>Графики не загрузились</strong>
            <span>Смените период или повторите запрос. Остальная аналитика доступна.</span>
          </div>
          <button className="button button--secondary" onClick={() => void timeseries.refetch()} type="button">
            Повторить
          </button>
        </div>
      ) : (
        <CashflowContent period={period} response={timeseries.data} />
      )}
    </section>
  );
}
