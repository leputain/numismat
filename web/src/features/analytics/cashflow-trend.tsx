import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { apiClient } from "../../app/providers";
import type { TimeSeriesResponse } from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { formatPeriod } from "../../shared/format/date-time";
import { formatMoney } from "../../shared/finance/money";
import { queryKeys } from "../../shared/queries/query-keys";
import {
  buildCashflowSeries,
  scaleMinorToPixels,
  summarizeCashflow,
  timeSeriesCurrencies,
  type CashflowPoint,
  type CashflowSummary,
} from "./timeseries-math";

const GRAIN = "day" as const;
const CHART_WIDTH = 640;
const CHART_HEIGHT = 224;
const PLOT_LEFT = 18;
const PLOT_RIGHT = 622;
const ZERO_Y = 108;
const VERTICAL_EXTENT = 82;

function timeseriesPath(start: string, end: string): string {
  const query = new URLSearchParams({ start, end, grain: GRAIN });
  return `/api/v1/reports/timeseries?${query.toString()}`;
}

function CashflowSkeleton() {
  return (
    <div aria-busy="true" aria-label="Загрузка денежного потока" className="cashflow-loading" role="status">
      <div className="skeleton h-11 w-full" />
      <div className="skeleton h-56 w-full" />
      <div className="cashflow-counts">
        <div className="skeleton h-16 w-full" />
        <div className="skeleton h-16 w-full" />
        <div className="skeleton h-16 w-full" />
      </div>
      <span className="sr-only">Строим график по дням…</span>
    </div>
  );
}

function CashflowChart({
  currency,
  points,
  summary,
  timeZone,
}: {
  readonly currency: string;
  readonly points: readonly CashflowPoint[];
  readonly summary: CashflowSummary;
  readonly timeZone: string;
}) {
  const { locale } = useSessionFormat();
  const plotWidth = PLOT_RIGHT - PLOT_LEFT;
  const step = plotWidth / Math.max(points.length, 1);
  const barWidth = Math.min(8, Math.max(2.5, step * 0.28));
  const netPoints = points
    .map((point, index) => {
      const x = PLOT_LEFT + step * (index + 0.5);
      const y = ZERO_Y - scaleMinorToPixels(point.netMinor, summary.maximumMagnitude, VERTICAL_EXTENT);
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
  const middleIndex = Math.floor((points.length - 1) / 2);
  const axisPoints = [points[0], points[middleIndex], points.at(-1)].filter(
    (point, index, items): point is CashflowPoint =>
      point !== undefined && items.findIndex((candidate) => candidate?.start === point.start) === index,
  );

  return (
    <div className="cashflow-chart-frame">
      <div className="cashflow-legend" aria-label="Обозначения графика">
        <span><i className="cashflow-legend__income" />Доходы</span>
        <span><i className="cashflow-legend__expense" />Расходы</span>
        <span><i className="cashflow-legend__net" />Итог дня</span>
      </div>
      <svg
        aria-labelledby="cashflow-chart-title cashflow-chart-description"
        className="cashflow-chart"
        role="img"
        viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
      >
        <title id="cashflow-chart-title">Доходы и расходы по дням, {currency}</title>
        <desc id="cashflow-chart-description">
          Доходы направлены вверх от нулевой линии, расходы вниз, а линия показывает разницу за день.
          Точные значения доступны в таблице под графиком.
        </desc>
        <line className="cashflow-chart__grid" x1={PLOT_LEFT} x2={PLOT_RIGHT} y1={ZERO_Y - 41} y2={ZERO_Y - 41} />
        <line className="cashflow-chart__zero" x1={PLOT_LEFT} x2={PLOT_RIGHT} y1={ZERO_Y} y2={ZERO_Y} />
        <line className="cashflow-chart__grid" x1={PLOT_LEFT} x2={PLOT_RIGHT} y1={ZERO_Y + 41} y2={ZERO_Y + 41} />
        {points.map((point, index) => {
          const center = PLOT_LEFT + step * (index + 0.5);
          const incomeHeight = scaleMinorToPixels(
            point.incomeMinor,
            summary.maximumMagnitude,
            VERTICAL_EXTENT,
          );
          const expenseHeight = scaleMinorToPixels(
            point.expenseMinor,
            summary.maximumMagnitude,
            VERTICAL_EXTENT,
          );
          return (
            <g key={point.start}>
              {incomeHeight === 0 ? null : (
                <rect
                  className="cashflow-chart__income"
                  height={incomeHeight}
                  rx={Math.min(2, barWidth / 2)}
                  width={barWidth}
                  x={center - barWidth - 1}
                  y={ZERO_Y - incomeHeight}
                />
              )}
              {expenseHeight === 0 ? null : (
                <rect
                  className="cashflow-chart__expense"
                  height={expenseHeight}
                  rx={Math.min(2, barWidth / 2)}
                  width={barWidth}
                  x={center + 1}
                  y={ZERO_Y}
                />
              )}
            </g>
          );
        })}
        {netPoints.length === 0 ? null : (
          <polyline className="cashflow-chart__net" points={netPoints} />
        )}
        {points.map((point, index) => {
          const x = PLOT_LEFT + step * (index + 0.5);
          const y = ZERO_Y - scaleMinorToPixels(point.netMinor, summary.maximumMagnitude, VERTICAL_EXTENT);
          return point.netMinor === 0n ? null : (
            <circle className="cashflow-chart__net-point" cx={x} cy={y} key={point.start} r="2.25" />
          );
        })}
      </svg>
      <div aria-hidden="true" className="cashflow-axis">
        {axisPoints.map((point) => (
          <span key={point.start}>{formatPeriod(point.start, locale, timeZone)}</span>
        ))}
      </div>
    </div>
  );
}

function CashflowTable({
  currency,
  points,
  timeZone,
}: {
  readonly currency: string;
  readonly points: readonly CashflowPoint[];
  readonly timeZone: string;
}) {
  const { locale } = useSessionFormat();
  return (
    <details className="cashflow-table-details">
      <summary>Точные значения по дням</summary>
      <div className="cashflow-table-scroll">
        <table className="cashflow-table">
          <caption className="sr-only">Доходы и расходы по дням в валюте {currency}</caption>
          <thead>
            <tr>
              <th scope="col">Дата</th>
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

function CashflowContent({ response }: { readonly response: TimeSeriesResponse }) {
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
        <strong>В этом месяце пока нет движения</strong>
        <p>График появится после первой подтверждённой операции.</p>
      </div>
    );
  }

  return (
    <div className="cashflow-content">
      <div aria-label="Валюта графика" className="currency-chips" role="group">
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

      {summary.incomeCount + summary.expenseCount === 0 ? (
        <div className="cashflow-empty">
          <span aria-hidden="true">⌁</span>
          <strong>В этой валюте пока нет движения</strong>
          <p>Выберите другую валюту или добавьте подтверждённую операцию.</p>
        </div>
      ) : (
        <>
          <div className="cashflow-totals">
            <div>
              <span>Доходы</span>
              <strong className="money-income">{formatMoney(summary.incomeMinor.toString(), selectedCurrency, locale)}</strong>
            </div>
            <div>
              <span>Расходы</span>
              <strong className="money-expense">{formatMoney(summary.expenseMinor.toString(), selectedCurrency, locale)}</strong>
            </div>
            <div>
              <span>Итог периода</span>
              <strong>{formatMoney(summary.netMinor.toString(), selectedCurrency, locale)}</strong>
            </div>
          </div>

          <CashflowChart
            currency={selectedCurrency}
            points={points}
            summary={summary}
            timeZone={response.timezone}
          />

          <div className="cashflow-counts" aria-label="Количество операций в периоде">
            <div><span>Доходов</span><strong>{summary.incomeCount}</strong></div>
            <div><span>Расходов</span><strong>{summary.expenseCount}</strong></div>
            <div><span>Дней с движением</span><strong>{summary.activeDays}</strong></div>
          </div>

          <CashflowTable
            currency={selectedCurrency}
            points={points}
            timeZone={response.timezone}
          />
        </>
      )}
    </div>
  );
}

export function CashflowTrend({ start, end }: { readonly start: string; readonly end: string }) {
  const timeseries = useQuery({
    queryKey: queryKeys.timeseries(start, end, GRAIN),
    queryFn: ({ signal }) =>
      apiClient.get<TimeSeriesResponse>(timeseriesPath(start, end), { signal }),
    staleTime: 30_000,
  });

  return (
    <section
      aria-labelledby="cashflow-title"
      className="surface-panel surface-panel--roomy cashflow-section"
    >
      <div className="section-heading section-heading--inside">
        <div>
          <p className="eyebrow">Движение внутри месяца</p>
          <h2 className="section-title" id="cashflow-title">Доходы и расходы по дням</h2>
        </div>
        <span className="period-caption">Текущий месяц</span>
      </div>
      {timeseries.isPending ? (
        <CashflowSkeleton />
      ) : timeseries.isError ? (
        <div className="inline-state" role="alert">
          <div>
            <strong>График не загрузился</strong>
            <span>Остальная аналитика доступна.</span>
          </div>
          <button className="button button--secondary" onClick={() => void timeseries.refetch()} type="button">
            Повторить
          </button>
        </div>
      ) : (
        <CashflowContent response={timeseries.data} />
      )}
    </section>
  );
}
