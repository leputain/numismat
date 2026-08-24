export type ClientEventCode =
  | "analytics_opened"
  | "auth_protocol_error"
  | "auth_reopen_required"
  | "bank_import_action_completed"
  | "bank_import_action_rejected"
  | "bank_import_action_started"
  | "bank_import_opened"
  | "budget_action_completed"
  | "budget_action_rejected"
  | "budget_action_started"
  | "budget_detail_opened"
  | "budgets_opened"
  | "catalog_action_completed"
  | "catalog_action_rejected"
  | "catalog_action_started"
  | "catalog_opened"
  | "dashboard_opened"
  | "draft_action_completed"
  | "draft_action_rejected"
  | "draft_action_started"
  | "draft_opened"
  | "exchange_rate_action_completed"
  | "exchange_rate_action_rejected"
  | "exchange_rate_action_started"
  | "mutation_conflict_detected"
  | "mutation_outcome_unknown"
  | "more_opened"
  | "pagination_restarted"
  | "recurring_action_completed"
  | "recurring_action_rejected"
  | "recurring_action_started"
  | "recurring_detail_opened"
  | "recurring_opened"
  | "settings_action_completed"
  | "settings_action_rejected"
  | "settings_action_started"
  | "settings_opened"
  | "telegram_sdk_lifecycle_failed"
  | "transaction_action_completed"
  | "transaction_action_rejected"
  | "transaction_action_started"
  | "transaction_detail_opened"
  | "transactions_opened"
  | "ui_unexpected_error";

/**
 * Privacy boundary for client diagnostics. Production deliberately emits nothing;
 * development output is restricted to one fixed event code without metadata.
 */
export function emitClientEvent(code: ClientEventCode): void {
  if (import.meta.env.DEV) {
    console.info(code);
  }
}
