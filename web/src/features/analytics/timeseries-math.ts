import type { TimeSeriesBucket } from "../../shared/api/types";
import { parseMinorUnits } from "./analytics-math";

export interface CashflowPoint {
  readonly start: string;
  readonly end: string;
  readonly incomeMinor: bigint;
  readonly expenseMinor: bigint;
  readonly netMinor: bigint;
  readonly incomeCount: number;
  readonly expenseCount: number;
}

export interface CashflowSummary {
  readonly incomeMinor: bigint;
  readonly expenseMinor: bigint;
  readonly netMinor: bigint;
  readonly incomeCount: number;
  readonly expenseCount: number;
  readonly activeDays: number;
  readonly maximumMagnitude: bigint;
}

export interface CashflowChartPoint {
  readonly start: string;
  readonly end: string;
  readonly incomeMinor: string;
  readonly expenseMinor: string;
  readonly netMinor: string;
  readonly cumulativeMinor: string;
  readonly incomeCount: number;
  readonly expenseCount: number;
  readonly incomeVisual: number;
  readonly expenseVisual: number;
  readonly netVisual: number;
  readonly cumulativeVisual: number;
}

const CHART_VISUAL_LIMIT = 10_000;

function magnitude(value: bigint): bigint {
  return value < 0n ? -value : value;
}

export function timeSeriesCurrencies(buckets: readonly TimeSeriesBucket[]): string[] {
  const currencies = new Set<string>();
  for (const bucket of buckets) {
    for (const total of bucket.totals) {
      currencies.add(total.currency);
    }
  }
  return [...currencies].sort((left, right) => left.localeCompare(right));
}

export function buildCashflowSeries(
  buckets: readonly TimeSeriesBucket[],
  currency: string,
): CashflowPoint[] {
  return buckets.map((bucket) => {
    const total = bucket.totals.find((candidate) => candidate.currency === currency);
    return {
      start: bucket.start,
      end: bucket.end,
      incomeMinor: parseMinorUnits(total?.income_minor ?? "0"),
      expenseMinor: parseMinorUnits(total?.expense_minor ?? "0"),
      netMinor: parseMinorUnits(total?.net_minor ?? "0"),
      incomeCount: total?.income_count ?? 0,
      expenseCount: total?.expense_count ?? 0,
    };
  });
}

export function summarizeCashflow(points: readonly CashflowPoint[]): CashflowSummary {
  let incomeMinor = 0n;
  let expenseMinor = 0n;
  let netMinor = 0n;
  let incomeCount = 0;
  let expenseCount = 0;
  let activeDays = 0;
  let maximumMagnitude = 0n;

  for (const point of points) {
    incomeMinor += point.incomeMinor;
    expenseMinor += point.expenseMinor;
    netMinor += point.netMinor;
    incomeCount += point.incomeCount;
    expenseCount += point.expenseCount;
    if (point.incomeCount > 0 || point.expenseCount > 0) {
      activeDays += 1;
    }
    for (const value of [point.incomeMinor, point.expenseMinor, point.netMinor]) {
      const absolute = magnitude(value);
      if (absolute > maximumMagnitude) {
        maximumMagnitude = absolute;
      }
    }
  }

  return {
    incomeMinor,
    expenseMinor,
    netMinor,
    incomeCount,
    expenseCount,
    activeDays,
    maximumMagnitude,
  };
}

export function scaleMinorToPixels(value: bigint, maximum: bigint, extent: number): number {
  if (maximum <= 0n || extent <= 0) {
    return 0;
  }
  const absolute = magnitude(value);
  const bounded = absolute > maximum ? maximum : absolute;
  const scaledHundredths = (bounded * BigInt(Math.trunc(extent) * 100)) / maximum;
  // Only the bounded SVG coordinate is converted; the financial value never becomes Number.
  const pixels = Number(scaledHundredths) / 100;
  return value < 0n ? -pixels : pixels;
}

export function scaleMinorToChartUnit(value: bigint, maximum: bigint): number {
  if (maximum <= 0n || value === 0n) {
    return 0;
  }
  const absolute = magnitude(value);
  const bounded = absolute > maximum ? maximum : absolute;
  const scaled = (bounded * BigInt(CHART_VISUAL_LIMIT)) / maximum;
  const visual = Number(scaled);
  return value < 0n ? -visual : visual;
}

export function buildCashflowChartSeries(
  points: readonly CashflowPoint[],
): CashflowChartPoint[] {
  let cumulative = 0n;
  let maximumFlowMagnitude = 0n;
  let maximumCumulativeMagnitude = 0n;
  const cumulativeValues: bigint[] = [];

  for (const point of points) {
    cumulative += point.netMinor;
    cumulativeValues.push(cumulative);
    const cumulativeMagnitude = magnitude(cumulative);
    if (cumulativeMagnitude > maximumCumulativeMagnitude) {
      maximumCumulativeMagnitude = cumulativeMagnitude;
    }
    for (const value of [point.incomeMinor, point.expenseMinor, point.netMinor]) {
      const absolute = magnitude(value);
      if (absolute > maximumFlowMagnitude) {
        maximumFlowMagnitude = absolute;
      }
    }
  }

  return points.map((point, index) => {
    const cumulativeMinor = cumulativeValues[index] ?? 0n;
    return {
      start: point.start,
      end: point.end,
      incomeMinor: point.incomeMinor.toString(),
      expenseMinor: point.expenseMinor.toString(),
      netMinor: point.netMinor.toString(),
      cumulativeMinor: cumulativeMinor.toString(),
      incomeCount: point.incomeCount,
      expenseCount: point.expenseCount,
      incomeVisual: scaleMinorToChartUnit(point.incomeMinor, maximumFlowMagnitude),
      expenseVisual: scaleMinorToChartUnit(-point.expenseMinor, maximumFlowMagnitude),
      netVisual: scaleMinorToChartUnit(point.netMinor, maximumFlowMagnitude),
      cumulativeVisual: scaleMinorToChartUnit(cumulativeMinor, maximumCumulativeMagnitude),
    };
  });
}
