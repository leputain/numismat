import { describe, expect, it } from "vitest";

import {
  buildCurrencyComparisons,
  compareMinor,
  formatBasisPoints,
  savingsRateBasisPoints,
  shareBasisPoints,
} from "./analytics-math";
import { buildCashflowSeries, scaleMinorToPixels, summarizeCashflow } from "./timeseries-math";

describe("analytics money math", () => {
  it("keeps comparisons and shares in exact integer arithmetic", () => {
    expect(compareMinor("12500", "10000")).toMatchObject({
      delta: 2500n,
      direction: "up",
      changeBasisPoints: 2500n,
    });
    expect(formatBasisPoints(-1234n)).toBe("−12,34%");
    expect(shareBasisPoints("333", "1000")).toBe(3330n);
    expect(savingsRateBasisPoints("10000", "7500")).toBe(2500n);
  });

  it("keeps currencies separate and supplies exact zero totals for an absent period", () => {
    const comparisons = buildCurrencyComparisons(
      [{ currency: "RUB", expense_minor: "100", income_minor: "0", net_minor: "-100" }],
      [{ currency: "USD", expense_minor: "25", income_minor: "0", net_minor: "-25" }],
    );

    expect(comparisons.map((item) => item.currency)).toEqual(["RUB", "USD"]);
    expect(comparisons[0]?.comparable.expense_minor).toBe("0");
    expect(comparisons[1]?.current.expense_minor).toBe("0");
  });

  it("builds an exact per-currency cashflow series and bounds only SVG coordinates", () => {
    const points = buildCashflowSeries(
      [
        {
          start: "2026-08-01T00:00:00Z",
          end: "2026-08-02T00:00:00Z",
          totals: [
            {
              currency: "RUB",
              income_minor: "900719925474099300",
              expense_minor: "300000000000000000",
              net_minor: "600719925474099300",
              income_count: 1,
              expense_count: 2,
            },
          ],
        },
        {
          start: "2026-08-02T00:00:00Z",
          end: "2026-08-03T00:00:00Z",
          totals: [],
        },
      ],
      "RUB",
    );
    const summary = summarizeCashflow(points);

    expect(summary.incomeMinor).toBe(900719925474099300n);
    expect(summary.netMinor).toBe(600719925474099300n);
    expect(summary.incomeCount).toBe(1);
    expect(summary.expenseCount).toBe(2);
    expect(summary.activeDays).toBe(1);
    expect(points[1]?.incomeMinor).toBe(0n);
    expect(scaleMinorToPixels(summary.maximumMagnitude, summary.maximumMagnitude, 88)).toBe(88);
  });
});
