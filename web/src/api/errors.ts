export type ApiErrorCode =
  | "active_draft_conflict"
  | "auth_session_invalid"
  | "budget_overlap"
  | "catalog_unavailable"
  | "csrf_failed"
  | "draft_revision_conflict"
  | "duplicate_operation"
  | "forbidden"
  | "idempotency_in_progress"
  | "idempotency_key_conflict"
  | "internal_error"
  | "invalid_cursor"
  | "invalid_state"
  | "method_not_allowed"
  | "not_found"
  | "object_version_conflict"
  | "ocr_processing_failed"
  | "ocr_queue_invalid"
  | "origin_forbidden"
  | "readiness_failed"
  | "rate_limited"
  | "request_too_large"
  | "review_required"
  | "telegram_auth_expired"
  | "telegram_auth_invalid"
  | "telegram_auth_replayed"
  | "telegram_owner_forbidden"
  | "unauthorized"
  | "unsupported_media_type"
  | "validation_failed"
  | "unknown_error";

const KNOWN_ERROR_CODES = new Set<ApiErrorCode>([
  "active_draft_conflict",
  "auth_session_invalid",
  "budget_overlap",
  "catalog_unavailable",
  "csrf_failed",
  "draft_revision_conflict",
  "duplicate_operation",
  "forbidden",
  "idempotency_in_progress",
  "idempotency_key_conflict",
  "internal_error",
  "invalid_cursor",
  "invalid_state",
  "method_not_allowed",
  "not_found",
  "object_version_conflict",
  "ocr_processing_failed",
  "ocr_queue_invalid",
  "origin_forbidden",
  "readiness_failed",
  "rate_limited",
  "request_too_large",
  "review_required",
  "telegram_auth_expired",
  "telegram_auth_invalid",
  "telegram_auth_replayed",
  "telegram_owner_forbidden",
  "unauthorized",
  "unsupported_media_type",
  "validation_failed",
]);

export function normalizeApiErrorCode(value: unknown): ApiErrorCode {
  return typeof value === "string" && KNOWN_ERROR_CODES.has(value as ApiErrorCode)
    ? (value as ApiErrorCode)
    : "unknown_error";
}

export class HttpApiError extends Error {
  public readonly status: number;
  public readonly code: ApiErrorCode;

  public constructor(status: number, code: ApiErrorCode) {
    super("API request was rejected");
    this.name = "HttpApiError";
    this.status = status;
    this.code = code;
  }
}

export class ProtocolError extends Error {
  public constructor() {
    super("API protocol violation");
    this.name = "ProtocolError";
  }
}

export class NetworkError extends Error {
  public constructor() {
    super("Network request failed");
    this.name = "NetworkError";
  }
}

export class MutationResultUnknownError extends Error {
  public constructor() {
    super("Mutation result is unknown");
    this.name = "MutationResultUnknownError";
  }
}

export class ReopenRequiredError extends Error {
  public constructor() {
    super("Mini App must be reopened");
    this.name = "ReopenRequiredError";
  }
}
