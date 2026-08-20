import type { QueryClient } from "@tanstack/react-query";

import { emitClientEvent } from "../logging/client-events";
import { queryKeys } from "../queries/query-keys";

export async function restartTransactionPagination(queryClient: QueryClient): Promise<void> {
  await Promise.all([
    queryClient.cancelQueries({ queryKey: queryKeys.transactions.activeRoot }),
    queryClient.cancelQueries({ queryKey: queryKeys.transactions.trashRoot }),
  ]);
  await Promise.all([
    queryClient.resetQueries({ queryKey: queryKeys.transactions.activeRoot }),
    queryClient.resetQueries({ queryKey: queryKeys.transactions.trashRoot }),
  ]);
  emitClientEvent("pagination_restarted");
}

export async function refreshFinanceQueries(queryClient: QueryClient): Promise<void> {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: queryKeys.dashboard }),
    queryClient.invalidateQueries({ queryKey: queryKeys.reportsRoot }),
    queryClient.invalidateQueries({ queryKey: queryKeys.transactions.detailRoot }),
  ]);
}

export async function refreshDraftQueries(queryClient: QueryClient): Promise<void> {
  await queryClient.invalidateQueries({ queryKey: queryKeys.drafts.all });
}

export async function restartBudgetPagination(queryClient: QueryClient): Promise<void> {
  await Promise.all([
    queryClient.cancelQueries({ queryKey: queryKeys.budgets.activeRoot }),
    queryClient.cancelQueries({ queryKey: queryKeys.budgets.trashRoot }),
  ]);
  await Promise.all([
    queryClient.resetQueries({ queryKey: queryKeys.budgets.activeRoot }),
    queryClient.resetQueries({ queryKey: queryKeys.budgets.trashRoot }),
  ]);
  await queryClient.invalidateQueries({ queryKey: queryKeys.budgets.detailRoot });
}

export async function restartRecurringPagination(queryClient: QueryClient): Promise<void> {
  await Promise.all([
    queryClient.cancelQueries({ queryKey: queryKeys.recurring.activeRoot }),
    queryClient.cancelQueries({ queryKey: queryKeys.recurring.trashRoot }),
  ]);
  await Promise.all([
    queryClient.resetQueries({ queryKey: queryKeys.recurring.activeRoot }),
    queryClient.resetQueries({ queryKey: queryKeys.recurring.trashRoot }),
  ]);
  await queryClient.invalidateQueries({ queryKey: queryKeys.recurring.detailRoot });
}

export async function restartBankImportPagination(queryClient: QueryClient): Promise<void> {
  await queryClient.cancelQueries({ queryKey: queryKeys.bankImports.all });
  await queryClient.resetQueries({ queryKey: queryKeys.bankImports.all });
  emitClientEvent("pagination_restarted");
}
