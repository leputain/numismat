import type { DashboardResponse } from "../../shared/api/types";

const CANONICAL_MINOR_UNITS = /^-?\d+$/u;
const BASIS_POINTS_PER_PERCENT = 100n;
const FULL_SHARE_BASIS_POINTS = 10_000n;

type CurrencyTotal = DashboardResponse["current_period"]["totals"][number];

export interface MinorComparison {
  readonly current: bigint;
  readonly comparable: bigint;
  readonly delta: bigint;
  readonly direction: "up" | "down" | "flat";
  readonly changeBasisPoints: bigint | null;
}

export interface CurrencyComparison {
  readonly currency: string;
  readonly current: CurrencyTotal;
  readonly comparable: CurrencyTotal;
}

export function parseMinorUnits(value: string): bigint {
  if (!CANONICAL_MINOR_UNITS.test(value)) {
    return 0n;
  }
  return BigInt(value);
}

function zeroTotal(currency: string): CurrencyTotal {
  return {
    currency,
    expense_minor: "0",
    income_minor: "0",
    net_minor: "0",
  };
}

export function compareMinor(currentMinor: string, comparableMinor: string): MinorComparison {
  const current = parseMinorUnits(currentMinor);
  const comparable = parseMinorUnits(comparableMinor);
  const delta = current - comparable;
  const direction = delta > 0n ? "up" : delta < 0n ? "down" : "flat";
  const changeBasisPoints =
    comparable === 0n
      ? current === 0n
        ? 0n
        : null
      : (delta * FULL_SHARE_BASIS_POINTS) / (comparable < 0n ? -comparable : comparable);

  return { current, comparable, delta, direction, changeBasisPoints };
}

export function formatBasisPoints(value: bigint): string {
  const negative = value < 0n;
  const absolute = negative ? -value : value;
  const whole = absolute / BASIS_POINTS_PER_PERCENT;
  const fraction = (absolute % BASIS_POINTS_PER_PERCENT).toString().padStart(2, "0").replace(/0+$/u, "");
  return `${negative ? "−" : ""}${whole.toString()}${fraction.length === 0 ? "" : `,${fraction}`}%`;
}

export function shareBasisPoints(amountMinor: string, totalMinor: string): bigint {
  const amount = parseMinorUnits(amountMinor);
  const total = parseMinorUnits(totalMinor);
  const positiveAmount = amount < 0n ? -amount : amount;
  const positiveTotal = total < 0n ? -total : total;
  if (positiveAmount === 0n || positiveTotal === 0n) {
    return 0n;
  }
  const share = (positiveAmount * FULL_SHARE_BASIS_POINTS) / positiveTotal;
  return share > FULL_SHARE_BASIS_POINTS ? FULL_SHARE_BASIS_POINTS : share;
}

export function visualPercentFromBasisPoints(value: bigint): number {
  const bounded = value < 0n ? 0n : value > FULL_SHARE_BASIS_POINTS ? FULL_SHARE_BASIS_POINTS : value;
  // The conversion is safe: the value is bounded to 0..10_000 and is used only for CSS width.
  return Number(bounded) / 100;
}

export function relativeVisualPercent(valueMinor: string, maximumMinor: string): number {
  return visualPercentFromBasisPoints(shareBasisPoints(valueMinor, maximumMinor));
}

export function savingsRateBasisPoints(incomeMinor: string, expenseMinor: string): bigint | null {
  const income = parseMinorUnits(incomeMinor);
  const expense = parseMinorUnits(expenseMinor);
  if (income <= 0n) {
    return null;
  }
  return ((income - expense) * FULL_SHARE_BASIS_POINTS) / income;
}

export function buildCurrencyComparisons(
  currentTotals: readonly CurrencyTotal[],
  comparableTotals: readonly CurrencyTotal[],
): CurrencyComparison[] {
  const currencies = new Set([
    ...currentTotals.map((total) => total.currency),
    ...comparableTotals.map((total) => total.currency),
  ]);
  return [...currencies]
    .sort((left, right) => left.localeCompare(right))
    .map((currency) => ({
      currency,
      current: currentTotals.find((total) => total.currency === currency) ?? zeroTotal(currency),
      comparable:
        comparableTotals.find((total) => total.currency === currency) ?? zeroTotal(currency),
    }));
}
