import type { TimeSeriesGrain } from "../../shared/api/types";

export type AnalyticsPeriodId = "week" | "month" | "year";

export interface AnalyticsPeriodAnchor {
  readonly currentStart: string;
  readonly currentEnd: string;
  readonly comparableStart: string;
  readonly comparableEnd: string;
  readonly timeZone: string;
}

export interface AnalyticsPeriodSpec {
  readonly id: AnalyticsPeriodId;
  readonly label: string;
  readonly shortDescription: string;
  readonly currentStart: string;
  readonly currentEnd: string;
  readonly previousStart: string;
  readonly previousEnd: string;
  readonly grain: TimeSeriesGrain;
}

export const ANALYTICS_PERIOD_OPTIONS: ReadonlyArray<
  Pick<AnalyticsPeriodSpec, "id" | "label" | "shortDescription">
> = [
  { id: "week", label: "Неделя", shortDescription: "С понедельника" },
  { id: "month", label: "Месяц", shortDescription: "С первого числа" },
  { id: "year", label: "Год", shortDescription: "С января" },
];

interface LocalDateTime {
  readonly year: number;
  readonly month: number;
  readonly day: number;
  readonly hour: number;
  readonly minute: number;
  readonly second: number;
  readonly millisecond: number;
}

const LOCAL_PARTS_FORMATTERS = new Map<string, Intl.DateTimeFormat>();

function localPartsFormatter(timeZone: string): Intl.DateTimeFormat {
  const cached = LOCAL_PARTS_FORMATTERS.get(timeZone);
  if (cached !== undefined) {
    return cached;
  }
  const formatter = new Intl.DateTimeFormat("en-GB-u-ca-iso8601-nu-latn", {
    day: "2-digit",
    hour: "2-digit",
    hourCycle: "h23",
    minute: "2-digit",
    month: "2-digit",
    second: "2-digit",
    timeZone,
    year: "numeric",
  });
  LOCAL_PARTS_FORMATTERS.set(timeZone, formatter);
  return formatter;
}

function requiredPart(parts: ReadonlyMap<string, string>, name: string): number {
  const value = parts.get(name);
  if (value === undefined || !/^\d+$/u.test(value)) {
    throw new RangeError(`Missing ${name} in owner-local timestamp`);
  }
  return Number(value);
}

function localDateTime(instant: Date, timeZone: string): LocalDateTime {
  const values = new Map<string, string>();
  for (const part of localPartsFormatter(timeZone).formatToParts(instant)) {
    if (part.type !== "literal") {
      values.set(part.type, part.value);
    }
  }
  return {
    year: requiredPart(values, "year"),
    month: requiredPart(values, "month"),
    day: requiredPart(values, "day"),
    hour: requiredPart(values, "hour"),
    minute: requiredPart(values, "minute"),
    second: requiredPart(values, "second"),
    millisecond: instant.getUTCMilliseconds(),
  };
}

function naiveUtcMilliseconds(value: LocalDateTime): number {
  return Date.UTC(
    value.year,
    value.month - 1,
    value.day,
    value.hour,
    value.minute,
    value.second,
    value.millisecond,
  );
}

function ownerLocalToUtc(value: LocalDateTime, timeZone: string): string {
  const desired = naiveUtcMilliseconds(value);
  let candidate = desired;
  for (let attempt = 0; attempt < 4; attempt += 1) {
    const observed = localDateTime(new Date(candidate), timeZone);
    const correction = desired - naiveUtcMilliseconds(observed);
    candidate += correction;
    if (correction === 0) {
      return new Date(candidate).toISOString();
    }
  }
  return new Date(candidate).toISOString();
}

function shiftLocalDays(value: LocalDateTime, days: number): LocalDateTime {
  const shifted = new Date(Date.UTC(value.year, value.month - 1, value.day + days));
  return {
    ...value,
    year: shifted.getUTCFullYear(),
    month: shifted.getUTCMonth() + 1,
    day: shifted.getUTCDate(),
  };
}

function startOfLocalDay(value: LocalDateTime): LocalDateTime {
  return { ...value, hour: 0, minute: 0, second: 0, millisecond: 0 };
}

function daysInMonth(year: number, month: number): number {
  return new Date(Date.UTC(year, month, 0)).getUTCDate();
}

function previousYear(value: LocalDateTime): LocalDateTime {
  const year = value.year - 1;
  return { ...value, year, day: Math.min(value.day, daysInMonth(year, value.month)) };
}

function instant(value: string): Date {
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) {
    throw new RangeError("Analytics period anchor must be a valid timestamp");
  }
  return parsed;
}

export function buildAnalyticsPeriod(
  id: AnalyticsPeriodId,
  anchor: AnalyticsPeriodAnchor,
): AnalyticsPeriodSpec {
  const end = instant(anchor.currentEnd);
  const localEnd = localDateTime(end, anchor.timeZone);

  if (id === "month") {
    return {
      id,
      label: "Месяц",
      shortDescription: "С первого числа",
      currentStart: anchor.currentStart,
      currentEnd: anchor.currentEnd,
      previousStart: anchor.comparableStart,
      previousEnd: anchor.comparableEnd,
      grain: "day",
    };
  }

  if (id === "week") {
    const localWeekday = new Date(
      Date.UTC(localEnd.year, localEnd.month - 1, localEnd.day),
    ).getUTCDay();
    const daysSinceMonday = (localWeekday + 6) % 7;
    const weekStart = startOfLocalDay(shiftLocalDays(localEnd, -daysSinceMonday));
    return {
      id,
      label: "Неделя",
      shortDescription: "С понедельника",
      currentStart: ownerLocalToUtc(weekStart, anchor.timeZone),
      currentEnd: anchor.currentEnd,
      previousStart: ownerLocalToUtc(shiftLocalDays(weekStart, -7), anchor.timeZone),
      previousEnd: ownerLocalToUtc(shiftLocalDays(localEnd, -7), anchor.timeZone),
      grain: "day",
    };
  }

  const yearStart: LocalDateTime = {
    year: localEnd.year,
    month: 1,
    day: 1,
    hour: 0,
    minute: 0,
    second: 0,
    millisecond: 0,
  };
  const priorEnd = previousYear(localEnd);
  return {
    id,
    label: "Год",
    shortDescription: "С января",
    currentStart: ownerLocalToUtc(yearStart, anchor.timeZone),
    currentEnd: anchor.currentEnd,
    previousStart: ownerLocalToUtc({ ...yearStart, year: yearStart.year - 1 }, anchor.timeZone),
    previousEnd: ownerLocalToUtc(priorEnd, anchor.timeZone),
    grain: "month",
  };
}
