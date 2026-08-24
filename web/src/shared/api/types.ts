import type { components, operations } from "../../api/generated/schema";

type Schemas = components["schemas"];

export type Account = Schemas["AccountResponse"];
export type AccountsResponse = Schemas["AccountsResponse"];
export type ActiveDraftResponse = Schemas["ActiveDraftResponse"];
export type Category = Schemas["CategoryResponse"];
export type CategoriesResponse = Schemas["CategoriesResponse"];
export type AccountMutationResponse = Schemas["AccountMutationResponse"];
export type CategoryMutationResponse = Schemas["CategoryMutationResponse"];
export type CreateAccountRequest =
  operations["create_account_api_v1_accounts_post"]["requestBody"]["content"]["application/json"];
export type CreateCategoryRequest =
  operations["create_category_api_v1_categories_post"]["requestBody"]["content"]["application/json"];
export type RenameCatalogRequest =
  operations["update_account_api_v1_accounts__account_id__patch"]["requestBody"]["content"]["application/json"];
export type VersionedCatalogRequest =
  operations["archive_account_api_v1_accounts__account_id__archive_post"]["requestBody"]["content"]["application/json"];
export type Budget = Schemas["BudgetResponse"];
export type BudgetMutationResponse = Schemas["BudgetMutationResponse"];
export type BudgetPageResponse = Schemas["BudgetPageResponse"];
export type CreateBudgetRequest =
  operations["create_budget_api_v1_budgets_post"]["requestBody"]["content"]["application/json"];
export type ReplaceBudgetRequest =
  operations["replace_budget_api_v1_budgets__budget_id__put"]["requestBody"]["content"]["application/json"];
export type DashboardResponse = Schemas["DashboardResponse"];
export type Draft = Schemas["DraftResponse"];
export type DraftAction =
  operations["patch_draft_api_v1_drafts__draft_id__patch"]["requestBody"]["content"]["application/json"];
export type ComposeDraftRequest =
  operations["compose_draft_api_v1_drafts_compose_post"]["requestBody"]["content"]["application/json"];
export type QuickDraftRequest =
  operations["begin_quick_draft_api_v1_drafts_quick_post"]["requestBody"]["content"]["application/json"];
export type MutationResponse = Schemas["MutationResponse"];
export type NotificationPreferencesResponse = Schemas["NotificationPreferencesResponse"];
export type NotificationPreferencesRequest =
  operations["replace_notification_preferences_api_v1_settings_notifications_put"]["requestBody"]["content"]["application/json"];
export type PeriodReportResponse = Schemas["PeriodReportResponse"];
export type TimeSeriesResponse = Schemas["TimeSeriesResponse"];
export type TimeSeriesBucket = Schemas["TimeSeriesBucketResponse"];
export type TimeSeriesGrain = TimeSeriesResponse["grain"];
export type Transaction = Schemas["TransactionResponse"];
export type TransactionPageResponse = Schemas["TransactionPageResponse"];
export type VersionedBudgetRequest =
  operations["delete_budget_api_v1_budgets__budget_id__delete_post"]["requestBody"]["content"]["application/json"];
export type RecurringSchedule = Schemas["RecurringScheduleResponse"];
export type RecurringSchedulePageResponse = Schemas["RecurringSchedulePageResponse"];
export type RecurringScheduleMutationResponse = Schemas["RecurringScheduleMutationResponse"];
export type RecurringInstance = Schemas["RecurringInstanceResponse"];
export type RecurringInstancePageResponse = Schemas["RecurringInstancePageResponse"];
export type RecurringInstanceMutationResponse = Schemas["RecurringInstanceMutationResponse"];
export type CreateRecurringScheduleRequest =
  operations["create_recurring_schedule_api_v1_recurring_schedules_post"]["requestBody"]["content"]["application/json"];
export type ReplaceRecurringScheduleRequest =
  operations["replace_recurring_schedule_api_v1_recurring_schedules__schedule_id__put"]["requestBody"]["content"]["application/json"];
export type VersionedRecurringRequest =
  operations["recurring_schedule_pause_endpoint_api_v1_recurring_schedules__schedule_id__pause_post"]["requestBody"]["content"]["application/json"];
export type RateSource = Schemas["RateSourceResponse"];
export type RateSourcesResponse = Schemas["RateSourcesResponse"];
export type RateVersion = Schemas["RateVersionResponse"];
export type RateVersionSummary = Schemas["RateVersionSummaryResponse"];
export type RateVersionPageResponse = Schemas["RateVersionPageResponse"];
export type RateVersionMutationResponse = Schemas["RateVersionMutationResponse"];
export type ConvertedPeriodResponse = Schemas["ConvertedPeriodResponse"];
export type PublishManualRateVersionRequest =
  operations["publish_manual_exchange_rate_version_api_v1_exchange_rate_sources_manual_versions_post"]["requestBody"]["content"]["application/json"];
export type BankImportBatch = Schemas["BankImportBatchResponse"];
export type BankImportBatchPageResponse = Schemas["BankImportBatchPageResponse"];
export type BankImportBatchMutationResponse = Schemas["BankImportBatchMutationResponse"];
export type BankImportRow = Schemas["BankImportRowResponse"];
export type BankImportRowPageResponse = Schemas["BankImportRowPageResponse"];
export type BankImportRowMutationResponse = Schemas["BankImportRowMutationResponse"];
export type BankImportDraftMutationResponse = Schemas["BankImportDraftMutationResponse"];
export type ReconciliationCandidatesResponse = Schemas["ReconciliationCandidatesResponse"];
export type VersionedBankImportRowRequest =
  operations["stage_bank_import_draft_api_v1_bank_imports__batch_id__rows__row_id__draft_post"]["requestBody"]["content"]["application/json"];
export type LinkBankImportRowRequest =
  operations["link_bank_import_row_api_v1_bank_imports__batch_id__rows__row_id__link_post"]["requestBody"]["content"]["application/json"];
export type CancelBankImportRequest =
  operations["cancel_bank_import_api_v1_bank_imports__batch_id__cancel_post"]["requestBody"]["content"]["application/json"];
