import { describe, expect, it } from "vitest";

import { buildAnalyticsPeriod, type AnalyticsPeriodAnchor } from "./analytics-period";

const MOSCOW_ANCHOR: AnalyticsPeriodAnchor = {
  currentStart: "2026-07-31T21:00:00.000Z",
  currentEnd: "2026-08-20T09:00:00.000Z",
  comparableStart: "2026-06-30T21:00:00.000Z",
  comparableEnd: "2026-07-20T09:00:00.000Z",
  timeZone: "Europe/Moscow",
};

describe("analytics calendar periods", () => {
  it("uses authoritative month-to-date boundaries", () => {
    expect(buildAnalyticsPeriod("month", MOSCOW_ANCHOR)).toMatchObject({
      currentStart: MOSCOW_ANCHOR.currentStart,
      currentEnd: MOSCOW_ANCHOR.currentEnd,
      previousStart: MOSCOW_ANCHOR.comparableStart,
      previousEnd: MOSCOW_ANCHOR.comparableEnd,
      grain: "day",
    });
  });

  it("builds an owner-local ISO week and comparable elapsed week", () => {
    expect(buildAnalyticsPeriod("week", MOSCOW_ANCHOR)).toMatchObject({
      currentStart: "2026-08-16T21:00:00.000Z",
      currentEnd: "2026-08-20T09:00:00.000Z",
      previousStart: "2026-08-09T21:00:00.000Z",
      previousEnd: "2026-08-13T09:00:00.000Z",
      grain: "day",
    });
  });

  it("keeps local midnight correct across a daylight-saving transition", () => {
    const berlin: AnalyticsPeriodAnchor = {
      ...MOSCOW_ANCHOR,
      currentEnd: "2026-03-29T10:00:00.000Z",
      timeZone: "Europe/Berlin",
    };
    const period = buildAnalyticsPeriod("week", berlin);

    expect(period.currentStart).toBe("2026-03-22T23:00:00.000Z");
    expect(period.previousEnd).toBe("2026-03-22T11:00:00.000Z");
  });

  it("clamps the comparable year on leap day", () => {
    const leapYear: AnalyticsPeriodAnchor = {
      ...MOSCOW_ANCHOR,
      currentEnd: "2028-02-29T09:00:00.000Z",
    };
    const period = buildAnalyticsPeriod("year", leapYear);

    expect(period.currentStart).toBe("2027-12-31T21:00:00.000Z");
    expect(period.previousStart).toBe("2026-12-31T21:00:00.000Z");
    expect(period.previousEnd).toBe("2027-02-28T09:00:00.000Z");
    expect(period.grain).toBe("month");
  });
});
