import type { TransactionQueryKeyFilters } from "../../shared/queries/query-keys";

export type TransactionKindFilter = "expense" | "income";
export type TransactionPeriodId = "all" | "week" | "month" | "year" | "custom";
export type TransactionFeedMode = "active" | "trash";

export type TransactionQueryFilters = TransactionQueryKeyFilters;

export interface TransactionFilterState {
  readonly period: TransactionPeriodId;
  readonly customStart: string;
  readonly customEnd: string;
  readonly type: TransactionKindFilter | null;
  readonly accountId: string | null;
  readonly categoryId: string | null;
  readonly currency: string | null;
}

interface LocalDate {
  readonly year: number;
  readonly month: number;
  readonly day: number;
}

export const TRANSACTION_PERIOD_OPTIONS: ReadonlyArray<{
  readonly id: TransactionPeriodId;
  readonly label: string;
}> = [
  { id: "all", label: "Всё" },
  { id: "week", label: "7 дней" },
  { id: "month", label: "30 дней" },
  { id: "year", label: "Год" },
  { id: "custom", label: "Свой период" },
];

export const EMPTY_TRANSACTION_FILTERS: TransactionQueryFilters = Object.freeze({
  start: null,
  end: null,
  type: null,
  accountId: null,
  categoryId: null,
  currency: null,
});

const DATE_VALUE = /^(\d{4})-(\d{2})-(\d{2})$/u;
const MILLISECONDS_PER_DAY = 86_400_000;
const MAX_CUSTOM_DATE_DISTANCE = 365;
const BUDGET_FILTER_PARAMETERS = new Set([
  "period",
  "start",
  "end",
  "type",
  "currency",
  "category_id",
]);
const CANONICAL_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const CURRENCY = /^[A-Z]{3}$/u;

function utcDate(value: LocalDate): Date {
  const result = new Date(0);
  result.setUTCHours(0, 0, 0, 0);
  result.setUTCFullYear(value.year, value.month - 1, value.day);
  return result;
}

function formatLocalDate(value: LocalDate): string {
  return [
    String(value.year).padStart(4, "0"),
    String(value.month).padStart(2, "0"),
    String(value.day).padStart(2, "0"),
  ].join("-");
}

function parseLocalDate(value: string): LocalDate | null {
  const match = DATE_VALUE.exec(value);
  if (match === null) {
    return null;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (year < 1 || year > 9999 || month < 1 || month > 12 || day < 1 || day > 31) {
    return null;
  }
  const parsed = { year, month, day };
  return formatLocalDate({
    year: utcDate(parsed).getUTCFullYear(),
    month: utcDate(parsed).getUTCMonth() + 1,
    day: utcDate(parsed).getUTCDate(),
  }) === value
    ? parsed
    : null;
}

function shiftLocalDate(value: LocalDate, days: number): LocalDate {
  const shifted = utcDate(value);
  shifted.setUTCDate(shifted.getUTCDate() + days);
  return {
    year: shifted.getUTCFullYear(),
    month: shifted.getUTCMonth() + 1,
    day: shifted.getUTCDate(),
  };
}

function dayDistance(start: LocalDate, end: LocalDate): number {
  return Math.round((utcDate(end).getTime() - utcDate(start).getTime()) / MILLISECONDS_PER_DAY);
}

function usableTimeZone(value: string): string {
  try {
    new Intl.DateTimeFormat("en", { timeZone: value }).format(0);
    return value;
  } catch {
    return "UTC";
  }
}

function ownerLocalDate(now: Date, timeZone: string): LocalDate {
  const parts = new Intl.DateTimeFormat("en-CA-u-ca-iso8601-nu-latn", {
    day: "2-digit",
    month: "2-digit",
    timeZone: usableTimeZone(timeZone),
    year: "numeric",
  }).formatToParts(now);
  const values = new Map(parts.map((part) => [part.type, part.value]));
  return {
    year: Number(values.get("year")),
    month: Number(values.get("month")),
    day: Number(values.get("day")),
  };
}

function ownerLocalDateTime(instant: Date, timeZone: string): Required<LocalDate> & {
  readonly hour: number;
  readonly minute: number;
  readonly second: number;
} {
  const parts = new Intl.DateTimeFormat("en-GB-u-ca-iso8601-nu-latn", {
    day: "2-digit",
    hour: "2-digit",
    hourCycle: "h23",
    minute: "2-digit",
    month: "2-digit",
    second: "2-digit",
    timeZone,
    year: "numeric",
  }).formatToParts(instant);
  const values = new Map(parts.map((part) => [part.type, part.value]));
  return {
    year: Number(values.get("year")),
    month: Number(values.get("month")),
    day: Number(values.get("day")),
    hour: Number(values.get("hour")),
    minute: Number(values.get("minute")),
    second: Number(values.get("second")),
  };
}

function naiveUtcMilliseconds(value: LocalDate & {
  readonly hour?: number;
  readonly minute?: number;
  readonly second?: number;
}): number {
  const result = utcDate(value);
  result.setUTCHours(value.hour ?? 0, value.minute ?? 0, value.second ?? 0, 0);
  return result.getTime();
}

function ownerLocalMidnightToUtc(value: LocalDate, rawTimeZone: string): string {
  const timeZone = usableTimeZone(rawTimeZone);
  const desired = naiveUtcMilliseconds(value);
  let candidate = desired;
  for (let attempt = 0; attempt < 4; attempt += 1) {
    const observed = ownerLocalDateTime(new Date(candidate), timeZone);
    const correction = desired - naiveUtcMilliseconds(observed);
    candidate += correction;
    if (correction === 0) {
      break;
    }
  }
  return new Date(candidate).toISOString();
}

function requiredLocalDate(value: string): LocalDate {
  const parsed = parseLocalDate(value);
  if (parsed === null) {
    throw new RangeError("Transaction filter date is invalid");
  }
  return parsed;
}

export function createTransactionFilterState(
  timeZone: string,
  anchor = new Date(),
  search = "",
): TransactionFilterState {
  const today = ownerLocalDate(anchor, timeZone);
  const initial: TransactionFilterState = {
    period: "all",
    customStart: formatLocalDate(shiftLocalDate(today, -29)),
    customEnd: formatLocalDate(today),
    type: null,
    accountId: null,
    categoryId: null,
    currency: null,
  };
  if (search === "" || search === "?") {
    return initial;
  }
  if (search.length > 512) {
    return initial;
  }
  const parameters = new URLSearchParams(search);
  const entries = [...parameters.entries()];
  if (
    entries.some(([key]) => !BUDGET_FILTER_PARAMETERS.has(key)) ||
    new Set(entries.map(([key]) => key)).size !== entries.length ||
    parameters.get("period") !== "custom" ||
    parameters.get("type") !== "expense"
  ) {
    return initial;
  }
  const start = parameters.get("start");
  const end = parameters.get("end");
  const currency = parameters.get("currency");
  const categoryId = parameters.get("category_id");
  const parsedStart = start === null ? null : parseLocalDate(start);
  const parsedEnd = end === null ? null : parseLocalDate(end);
  if (
    parsedStart === null ||
    parsedEnd === null ||
    currency === null ||
    !CURRENCY.test(currency) ||
    (categoryId !== null && !CANONICAL_UUID.test(categoryId)) ||
    dayDistance(parsedStart, parsedEnd) < 0 ||
    dayDistance(parsedStart, parsedEnd) > MAX_CUSTOM_DATE_DISTANCE
  ) {
    return initial;
  }
  return {
    ...initial,
    period: "custom",
    customStart: formatLocalDate(parsedStart),
    customEnd: formatLocalDate(parsedEnd),
    type: "expense",
    categoryId,
    currency,
  };
}

export function budgetTransactionsPath(budget: {
  readonly starts_on: string;
  readonly ends_on: string;
  readonly currency: string;
  readonly category_id: string | null;
}): string {
  const parameters = new URLSearchParams({
    period: "custom",
    start: budget.starts_on,
    end: budget.ends_on,
    type: "expense",
    currency: budget.currency,
  });
  if (budget.category_id !== null) {
    parameters.set("category_id", budget.category_id);
  }
  return `/transactions?${parameters.toString()}`;
}

export function updateCustomTransactionPeriod(
  state: TransactionFilterState,
  boundary: "start" | "end",
  value: string,
): TransactionFilterState {
  const changed = parseLocalDate(value);
  if (changed === null) {
    return state;
  }
  let start = boundary === "start" ? changed : requiredLocalDate(state.customStart);
  let end = boundary === "end" ? changed : requiredLocalDate(state.customEnd);
  if (dayDistance(start, end) < 0) {
    if (boundary === "start") {
      end = start;
    } else {
      start = end;
    }
  } else if (dayDistance(start, end) > MAX_CUSTOM_DATE_DISTANCE) {
    if (boundary === "start") {
      end = shiftLocalDate(start, MAX_CUSTOM_DATE_DISTANCE);
    } else {
      start = shiftLocalDate(end, -MAX_CUSTOM_DATE_DISTANCE);
    }
  }
  return {
    ...state,
    period: "custom",
    customStart: formatLocalDate(start),
    customEnd: formatLocalDate(end),
  };
}

export function buildTransactionQueryFilters(
  state: TransactionFilterState,
  timeZone: string,
  anchor: Date,
): TransactionQueryFilters {
  let period: Pick<TransactionQueryFilters, "start" | "end"> = {
    start: null,
    end: null,
  };
  if (state.period !== "all") {
    const today = ownerLocalDate(anchor, timeZone);
    const endDate =
      state.period === "custom" ? requiredLocalDate(state.customEnd) : today;
    const startDate =
      state.period === "custom"
        ? requiredLocalDate(state.customStart)
        : shiftLocalDate(
            today,
            state.period === "week" ? -6 : state.period === "month" ? -29 : -364,
          );
    period = {
      start: ownerLocalMidnightToUtc(startDate, timeZone),
      end: ownerLocalMidnightToUtc(shiftLocalDate(endDate, 1), timeZone),
    };
  }
  return Object.freeze({
    start: period.start,
    end: period.end,
    type: state.type,
    accountId: state.accountId,
    categoryId: state.categoryId,
    currency: state.currency,
  });
}

export function activeTransactionFilterCount(filters: TransactionQueryFilters): number {
  return (
    (filters.start === null ? 0 : 1) +
    (filters.type === null ? 0 : 1) +
    (filters.accountId === null ? 0 : 1) +
    (filters.categoryId === null ? 0 : 1) +
    (filters.currency === null ? 0 : 1)
  );
}

export function transactionPagePath(
  mode: TransactionFeedMode,
  cursor: string | null,
  filters: TransactionQueryFilters,
  limit: number,
): string {
  if (!Number.isInteger(limit) || limit < 1 || limit > 100) {
    throw new RangeError("Transaction page limit is invalid");
  }
  if ((filters.start === null) !== (filters.end === null)) {
    throw new RangeError("Transaction filter period is incomplete");
  }
  const params = new URLSearchParams({ limit: String(limit) });
  if (cursor !== null) {
    params.set("cursor", cursor);
  }
  if (mode === "active") {
    if (filters.start !== null && filters.end !== null) {
      params.set("start", filters.start);
      params.set("end", filters.end);
    }
    if (filters.type !== null) {
      params.set("type", filters.type);
    }
    if (filters.accountId !== null) {
      params.set("account_id", filters.accountId);
    }
    if (filters.categoryId !== null) {
      params.set("category_id", filters.categoryId);
    }
    if (filters.currency !== null) {
      params.set("currency", filters.currency);
    }
  }
  const collection = mode === "active" ? "transactions" : "transactions/trash";
  return `/api/v1/${collection}?${params.toString()}`;
}
