function safeDate(value: string): Date | undefined {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date : undefined;
}

function safeTimeZone(timeZone: string): string {
  try {
    new Intl.DateTimeFormat("en", { timeZone }).format(0);
    return timeZone;
  } catch {
    return "UTC";
  }
}

function safeLocale(locale: string): string {
  try {
    return Intl.getCanonicalLocales(locale)[0] ?? "ru-RU";
  } catch {
    return "ru-RU";
  }
}

export function formatTransactionDate(
  value: string,
  locale = "ru-RU",
  timeZone = "UTC",
): string {
  const date = safeDate(value);
  if (date === undefined) {
    return "Дата недоступна";
  }
  return new Intl.DateTimeFormat(safeLocale(locale), {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: safeTimeZone(timeZone),
  }).format(date);
}

export function formatPeriod(value: string, locale = "ru-RU", timeZone = "UTC"): string {
  const date = safeDate(value);
  if (date === undefined) {
    return "—";
  }
  return new Intl.DateTimeFormat(safeLocale(locale), {
    day: "numeric",
    month: "short",
    timeZone: safeTimeZone(timeZone),
  }).format(date);
}

export function formatExclusivePeriod(
  start: string,
  end: string,
  locale = "ru-RU",
  timeZone = "UTC",
): string {
  const startDate = safeDate(start);
  const exclusiveEnd = safeDate(end);
  if (startDate === undefined || exclusiveEnd === undefined || exclusiveEnd <= startDate) {
    return "—";
  }
  const inclusiveEnd = new Date(exclusiveEnd.getTime() - 1);
  const first = formatPeriod(startDate.toISOString(), locale, timeZone);
  const last = formatPeriod(inclusiveEnd.toISOString(), locale, timeZone);
  return first === last ? first : `${first} — ${last}`;
}

export function dateInputToWire(value: string): string | undefined {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/u.exec(value);
  if (match === null) {
    return undefined;
  }
  const [, year, month, day] = match;
  if (year === undefined || month === undefined || day === undefined) {
    return undefined;
  }
  return `${day}.${month}.${year}`;
}
