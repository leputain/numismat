import type { RecurringInstance, RecurringSchedule } from "../../shared/api/types";

export function recurringStateLabel(state: RecurringSchedule["state"]): string {
  return {
    active: "Активно",
    paused: "На паузе",
    paused_error: "Нужно исправить",
    completed: "Завершено",
    deleted: "Удалено",
  }[state];
}

export function recurringCadenceLabel(schedule: RecurringSchedule): string {
  const unit = { daily: "дн.", weekly: "нед.", monthly: "мес." }[schedule.cadence];
  return `Каждые ${String(schedule.interval)} ${unit}`;
}

export function recurringOutcomeLabel(outcome: RecurringInstance["outcome"]): string {
  return {
    pending: "Ожидает генерации",
    blocked: "Нужно исправить",
    skipped: "Пропущен",
    awaiting_review: "Ждёт проверки",
    confirmed: "Подтверждён",
    dismissed: "Отклонён",
  }[outcome];
}

export function formatNominalLocal(value: string | null, locale = "ru-RU"): string {
  if (value === null) {
    return "—";
  }
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/u.exec(value);
  if (match === null) {
    return "—";
  }
  const [, year, month, day, hour, minute] = match;
  if ([year, month, day, hour, minute].some((part) => part === undefined)) {
    return "—";
  }
  const parsed = new Date(`${year}-${month}-${day}T${hour}:${minute}:00Z`);
  if (!Number.isFinite(parsed.getTime())) {
    return "—";
  }
  return new Intl.DateTimeFormat(locale, {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
  }).format(parsed);
}
