import { ProtocolError } from "../../api/errors";
import { apiClient } from "../../app/providers";
import type {
  NotificationPreferencesRequest,
  NotificationPreferencesResponse,
} from "../../shared/api/types";

export type { NotificationPreferencesRequest, NotificationPreferencesResponse };

const RESPONSE_KEYS = [
  "budget_100_enabled",
  "budget_80_enabled",
  "quiet_end",
  "quiet_start",
  "recurring_ready_enabled",
  "version",
  "weekly_digest_enabled",
  "weekly_time",
  "weekly_weekday",
] as const;

const LOCAL_MINUTE = /^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/u;

export function isLocalMinute(value: string): boolean {
  return LOCAL_MINUTE.test(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseNotificationPreferences(payload: unknown): NotificationPreferencesResponse {
  if (!isRecord(payload) || Object.keys(payload).sort().join("\u0000") !== RESPONSE_KEYS.join("\u0000")) {
    throw new ProtocolError();
  }
  const quietStart = payload.quiet_start;
  const quietEnd = payload.quiet_end;
  if (
    typeof payload.budget_80_enabled !== "boolean" ||
    typeof payload.budget_100_enabled !== "boolean" ||
    typeof payload.recurring_ready_enabled !== "boolean" ||
    typeof payload.weekly_digest_enabled !== "boolean" ||
    (quietStart !== null && (typeof quietStart !== "string" || !isLocalMinute(quietStart))) ||
    (quietEnd !== null && (typeof quietEnd !== "string" || !isLocalMinute(quietEnd))) ||
    (quietStart === null) !== (quietEnd === null) ||
    (quietStart !== null && quietStart === quietEnd) ||
    typeof payload.weekly_weekday !== "number" ||
    !Number.isInteger(payload.weekly_weekday) ||
    payload.weekly_weekday < 0 ||
    payload.weekly_weekday > 6 ||
    typeof payload.weekly_time !== "string" ||
    !isLocalMinute(payload.weekly_time) ||
    typeof payload.version !== "number" ||
    !Number.isInteger(payload.version) ||
    payload.version < 0 ||
    payload.version > 2 ** 31 - 1
  ) {
    throw new ProtocolError();
  }
  return {
    budget_80_enabled: payload.budget_80_enabled,
    budget_100_enabled: payload.budget_100_enabled,
    recurring_ready_enabled: payload.recurring_ready_enabled,
    weekly_digest_enabled: payload.weekly_digest_enabled,
    quiet_start: quietStart,
    quiet_end: quietEnd,
    weekly_weekday: payload.weekly_weekday,
    weekly_time: payload.weekly_time,
    version: payload.version,
  };
}

export async function getNotificationPreferences(
  signal?: AbortSignal,
): Promise<NotificationPreferencesResponse> {
  const payload = await apiClient.get<unknown>(
    "/api/v1/settings/notifications",
    signal === undefined ? {} : { signal },
  );
  return parseNotificationPreferences(payload);
}

export function sameNotificationPreferences(
  response: NotificationPreferencesResponse,
  request: NotificationPreferencesRequest,
): boolean {
  return (
    response.version === request.version + 1 &&
    response.budget_80_enabled === request.budget_80_enabled &&
    response.budget_100_enabled === request.budget_100_enabled &&
    response.recurring_ready_enabled === request.recurring_ready_enabled &&
    response.weekly_digest_enabled === request.weekly_digest_enabled &&
    response.quiet_start === request.quiet_start &&
    response.quiet_end === request.quiet_end &&
    response.weekly_weekday === request.weekly_weekday &&
    response.weekly_time === request.weekly_time
  );
}
