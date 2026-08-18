export interface BudgetWindow {
  readonly startsOn: string;
  readonly endsOn: string;
}

const MONTH_VALUE = /^(\d{4})-(0[1-9]|1[0-2])$/u;

function daysInMonth(year: number, month: number): number {
  if (month === 2) {
    return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0) ? 29 : 28;
  }
  return [4, 6, 9, 11].includes(month) ? 30 : 31;
}

function localYearMonth(now: Date, timeZone: string): readonly [number, number] {
  try {
    const parts = new Intl.DateTimeFormat("en-CA", {
      calendar: "gregory",
      numberingSystem: "latn",
      timeZone,
      year: "numeric",
      month: "2-digit",
    }).formatToParts(now);
    const year = Number(parts.find((part) => part.type === "year")?.value);
    const month = Number(parts.find((part) => part.type === "month")?.value);
    if (Number.isInteger(year) && Number.isInteger(month) && month >= 1 && month <= 12) {
      return [year, month];
    }
  } catch {
    // Authentication normally guarantees a valid IANA zone; UTC is fail-safe UI fallback.
  }
  return [now.getUTCFullYear(), now.getUTCMonth() + 1];
}

export function ownerMonthWindow(timeZone: string, now = new Date()): BudgetWindow {
  const [year, month] = localYearMonth(now, timeZone);
  const monthText = String(month).padStart(2, "0");
  const lastDay = new Date(Date.UTC(year, month, 0)).getUTCDate();
  return {
    startsOn: `${String(year).padStart(4, "0")}-${monthText}-01`,
    endsOn: `${String(year).padStart(4, "0")}-${monthText}-${String(lastDay).padStart(2, "0")}`,
  };
}

export function budgetMonthWindow(value: string): BudgetWindow | undefined {
  const match = MONTH_VALUE.exec(value);
  if (match === null) {
    return undefined;
  }
  const yearText = match[1];
  const monthText = match[2];
  if (yearText === undefined || monthText === undefined) {
    return undefined;
  }
  const year = Number(yearText);
  const month = Number(monthText);
  if (!Number.isInteger(year) || year < 1 || year > 9999) {
    return undefined;
  }
  return {
    startsOn: `${yearText}-${monthText}-01`,
    endsOn: `${yearText}-${monthText}-${String(daysInMonth(year, month)).padStart(2, "0")}`,
  };
}

export function formatBudgetDate(value: string, locale = "ru-RU"): string {
  if (!/^\d{4}-\d{2}-\d{2}$/u.test(value)) {
    return "—";
  }
  const parsed = new Date(`${value}T12:00:00Z`);
  if (!Number.isFinite(parsed.getTime())) {
    return "—";
  }
  try {
    return new Intl.DateTimeFormat(locale, {
      day: "numeric",
      month: "short",
      year: "numeric",
      timeZone: "UTC",
    }).format(parsed);
  } catch {
    return value;
  }
}
