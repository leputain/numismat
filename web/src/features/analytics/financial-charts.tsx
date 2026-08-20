import { useId, useMemo } from "react";
import {
  Area,
  AreaChart,
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipContentProps,
} from "recharts";

import type { TimeSeriesGrain } from "../../shared/api/types";
import { formatExclusivePeriod } from "../../shared/format/date-time";
import { formatMoney } from "../../shared/finance/money";
import {
  buildCashflowChartSeries,
  type CashflowChartPoint,
  type CashflowPoint,
} from "./timeseries-math";

interface ChartDatum extends CashflowChartPoint {
  readonly label: string;
}

function safeLocale(locale: string): string {
  try {
    return Intl.getCanonicalLocales(locale)[0] ?? "ru-RU";
  } catch {
    return "ru-RU";
  }
}

function bucketLabel(
  start: string,
  grain: TimeSeriesGrain,
  locale: string,
  timeZone: string,
): string {
  const instant = new Date(start);
  if (!Number.isFinite(instant.getTime())) {
    return "—";
  }
  return new Intl.DateTimeFormat(safeLocale(locale), {
    day: grain === "month" ? undefined : "numeric",
    month: "short",
    timeZone,
    year: grain === "month" ? "2-digit" : undefined,
  }).format(instant);
}

function isChartDatum(value: unknown): value is ChartDatum {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as Partial<ChartDatum>;
  return (
    typeof candidate.start === "string" &&
    typeof candidate.end === "string" &&
    typeof candidate.incomeMinor === "string" &&
    typeof candidate.expenseMinor === "string" &&
    typeof candidate.netMinor === "string" &&
    typeof candidate.cumulativeMinor === "string"
  );
}

function FinancialTooltip({
  active,
  currency,
  locale,
  payload,
  timeZone,
}: TooltipContentProps & {
  readonly currency: string;
  readonly locale: string;
  readonly timeZone: string;
}) {
  const datum = payload[0]?.payload;
  if (!active || !isChartDatum(datum)) {
    return null;
  }
  return (
    <div className="analytics-chart-tooltip">
      <strong>{formatExclusivePeriod(datum.start, datum.end, locale, timeZone)}</strong>
      <dl>
        <div className="analytics-chart-tooltip__row analytics-chart-tooltip__row--income">
          <dt>Доходы</dt>
          <dd>{formatMoney(datum.incomeMinor, currency, locale)}</dd>
        </div>
        <div className="analytics-chart-tooltip__row analytics-chart-tooltip__row--expense">
          <dt>Расходы</dt>
          <dd>{formatMoney(datum.expenseMinor, currency, locale)}</dd>
        </div>
        <div className="analytics-chart-tooltip__row">
          <dt>Итог</dt>
          <dd>{formatMoney(datum.netMinor, currency, locale)}</dd>
        </div>
        <div className="analytics-chart-tooltip__row analytics-chart-tooltip__row--cumulative">
          <dt>С начала периода</dt>
          <dd>{formatMoney(datum.cumulativeMinor, currency, locale)}</dd>
        </div>
      </dl>
    </div>
  );
}

export function FinancialCharts({
  currency,
  grain,
  locale,
  points,
  timeZone,
}: {
  readonly currency: string;
  readonly grain: TimeSeriesGrain;
  readonly locale: string;
  readonly points: readonly CashflowPoint[];
  readonly timeZone: string;
}) {
  const instanceId = useId().replaceAll(":", "");
  const expensePatternId = `expense-${instanceId}`;
  const cumulativeGradientId = `cumulative-${instanceId}`;
  const data = useMemo<ChartDatum[]>(
    () =>
      buildCashflowChartSeries(points).map((point) => ({
        ...point,
        label: bucketLabel(point.start, grain, locale, timeZone),
      })),
    [grain, locale, points, timeZone],
  );
  const tooltip = (props: TooltipContentProps) => (
    <FinancialTooltip
      {...props}
      currency={currency}
      locale={locale}
      timeZone={timeZone}
    />
  );

  return (
    <section aria-label="Графики движения денег" className="analytics-charts analytics-chart-suite">
      <div
        aria-labelledby="cashflow-chart-heading"
        className="analytics-chart-section"
        role="group"
      >
        <div className="analytics-chart-section__heading">
          <div>
            <span>Динамика</span>
            <h3 id="cashflow-chart-heading">Доходы, расходы и итог</h3>
          </div>
          <span className="analytics-chart-section__currency">{currency}</span>
        </div>
        <div aria-label={`Денежный поток в ${currency}`} className="analytics-chart-canvas" role="group">
          <ResponsiveContainer height={300} minWidth={0} width="100%">
            <ComposedChart accessibilityLayer data={data} margin={{ bottom: 4, left: 0, right: 4, top: 14 }}>
              <defs>
                <pattern
                  height="6"
                  id={expensePatternId}
                  patternUnits="userSpaceOnUse"
                  width="6"
                >
                  <rect fill="var(--nm-expense)" fillOpacity="0.2" height="6" width="6" />
                  <path
                    d="M-1 1 1-1M1 7 7 1M5 7 7 5"
                    stroke="var(--nm-expense)"
                    strokeOpacity="0.88"
                    strokeWidth="1.5"
                  />
                </pattern>
              </defs>
              <CartesianGrid stroke="var(--nm-line)" strokeDasharray="3 7" vertical={false} />
              <XAxis
                axisLine={false}
                dataKey="label"
                minTickGap={24}
                tick={{ fill: "var(--nm-muted)", fontSize: 10 }}
                tickLine={false}
                tickMargin={10}
              />
              <YAxis domain={[-10_000, 10_000]} hide />
              <ReferenceLine stroke="var(--nm-line-strong, var(--nm-line))" y={0} />
              <Tooltip
                content={tooltip}
                cursor={{ fill: "var(--nm-chart-hover, transparent)" }}
                isAnimationActive={false}
              />
              <Bar
                dataKey="incomeVisual"
                fill="var(--nm-income)"
                fillOpacity={0.88}
                isAnimationActive={false}
                maxBarSize={15}
                name="Доходы"
                radius={[5, 5, 2, 2]}
              />
              <Bar
                dataKey="expenseVisual"
                fill={`url(#${expensePatternId})`}
                isAnimationActive={false}
                maxBarSize={15}
                name="Расходы"
                radius={[2, 2, 5, 5]}
              />
              <Line
                activeDot={{ fill: "var(--nm-chart-net)", r: 5, stroke: "var(--nm-raised)", strokeWidth: 3 }}
                dataKey="netVisual"
                dot={false}
                isAnimationActive={false}
                name="Чистый итог"
                stroke="var(--nm-chart-net)"
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2.5}
                type="monotone"
              />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
        <div aria-label="Обозначения графика" className="analytics-chart-legend">
          <span><i className="analytics-chart-legend__income" />Доходы</span>
          <span><i className="analytics-chart-legend__expense" />Расходы</span>
          <span><i className="analytics-chart-legend__net" />Чистый итог</span>
        </div>
      </div>

      <div
        aria-labelledby="cumulative-chart-heading"
        className="analytics-chart-section analytics-chart-section--cumulative"
        role="group"
      >
        <div className="analytics-chart-section__heading">
          <div>
            <span>Накопительный результат</span>
            <h3 id="cumulative-chart-heading">Результат с начала периода</h3>
          </div>
        </div>
        <div aria-label={`Накопительный итог в ${currency}`} className="analytics-chart-canvas analytics-chart-canvas--compact" role="group">
          <ResponsiveContainer height={150} minWidth={0} width="100%">
            <AreaChart accessibilityLayer data={data} margin={{ bottom: 2, left: 0, right: 4, top: 10 }}>
              <defs>
                <linearGradient id={cumulativeGradientId} x1="0" x2="0" y1="0" y2="1">
                  <stop offset="0%" stopColor="var(--nm-chart-net)" stopOpacity="0.22" />
                  <stop offset="100%" stopColor="var(--nm-chart-net)" stopOpacity="0" />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="var(--nm-line)" strokeDasharray="3 7" vertical={false} />
              <XAxis
                axisLine={false}
                dataKey="label"
                minTickGap={28}
                tick={{ fill: "var(--nm-muted)", fontSize: 10 }}
                tickLine={false}
                tickMargin={8}
              />
              <YAxis domain={[-10_000, 10_000]} hide />
              <ReferenceLine stroke="var(--nm-line-strong, var(--nm-line))" y={0} />
              <Tooltip
                content={tooltip}
                cursor={{ stroke: "var(--nm-chart-net)", strokeDasharray: "3 4", strokeOpacity: 0.45 }}
                isAnimationActive={false}
              />
              <Area
                activeDot={{ fill: "var(--nm-chart-net)", r: 4, stroke: "var(--nm-raised)", strokeWidth: 2 }}
                dataKey="cumulativeVisual"
                dot={false}
                fill={`url(#${cumulativeGradientId})`}
                isAnimationActive={false}
                name="Накопительный итог"
                stroke="var(--nm-chart-net)"
                strokeWidth={2.2}
                type="monotone"
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </div>
    </section>
  );
}
