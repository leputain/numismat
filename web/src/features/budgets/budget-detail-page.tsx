import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { Link } from "react-router";

import { apiClient } from "../../app/providers";
import type {
  Budget,
  BudgetMutationResponse,
  VersionedBudgetRequest,
} from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { ErrorState, PageSkeleton } from "../../shared/components/async-state";
import { MutationFeedback } from "../../shared/components/mutation-feedback";
import { PageHeading } from "../../shared/components/page-heading";
import { formatMoney } from "../../shared/finance/money";
import { emitClientEvent } from "../../shared/logging/client-events";
import { usePreparedMutation } from "../../shared/mutations/prepared-mutation";
import { restartBudgetPagination } from "../../shared/mutations/query-recovery";
import { queryKeys } from "../../shared/queries/query-keys";
import { formatBudgetDate } from "./budget-window";

type LifecycleAction = "delete" | "restore";

export function BudgetDetailPage({ budgetId }: { readonly budgetId: string }) {
  const queryClient = useQueryClient();
  const { locale } = useSessionFormat();
  const budget = useQuery({
    queryKey: queryKeys.budgets.detail(budgetId),
    queryFn: ({ signal }) => apiClient.get<Budget>(`/api/v1/budgets/${budgetId}`, { signal }),
    staleTime: 15_000,
  });
  const lifecycle = usePreparedMutation<BudgetMutationResponse, LifecycleAction>({
    eventScope: "budget",
    async onSuccess() {
      await restartBudgetPagination(queryClient);
      await queryClient.invalidateQueries({ queryKey: queryKeys.budgets.detail(budgetId) });
    },
    async onRejected() {
      await queryClient.invalidateQueries({ queryKey: queryKeys.budgets.detail(budgetId) });
    },
    onOutcomeUnknown() {},
  });

  useEffect(() => emitClientEvent("budget_detail_opened"), []);

  if (budget.isPending) {
    return <PageSkeleton rows={4} />;
  }
  if (budget.isError) {
    return <ErrorState onAction={() => void budget.refetch()} />;
  }

  const value = budget.data;
  const deleted = value.deleted_at !== null;
  const progress = value.progress;
  const overspent = progress.overspent_minor !== "0";

  const runLifecycle = (action: LifecycleAction) => {
    const body: VersionedBudgetRequest = { version: value.version };
    lifecycle.run({
      path: `/api/v1/budgets/${value.id}/${action}`,
      body,
      context: action,
    });
  };

  return (
    <div className="budget-detail-page page-stack">
      <PageHeading
        action={
          deleted ? undefined : (
            <Link className="button button--secondary" to={`/budgets/${value.id}/edit`}>
              Изменить
            </Link>
          )
        }
        description={`${formatBudgetDate(value.starts_on, locale)} — ${formatBudgetDate(value.ends_on, locale)}`}
        eyebrow={deleted ? "Удалённый бюджет" : "Прогресс бюджета"}
        title={value.name}
      />

      <section className="surface-panel">
        <div className="grid gap-5 sm:grid-cols-2">
          <div>
            <p className="eyebrow">Лимит</p>
            <p className="mt-2 text-2xl font-semibold text-[var(--nm-text)]">
              {formatMoney(value.limit_minor, value.currency, locale)}
            </p>
          </div>
          <div>
            <p className="eyebrow">Потрачено</p>
            <p className="mt-2 text-2xl font-semibold text-[var(--nm-text)]">
              {formatMoney(progress.spent_minor, value.currency, locale)}
            </p>
          </div>
        </div>
        <div className="mt-6 h-3 overflow-hidden rounded-full bg-[var(--nm-line)]">
          <span
            className={`block h-full rounded-full ${overspent ? "bg-[var(--nm-danger)]" : "bg-[var(--nm-accent)]"}`}
            style={{ width: `${String(Math.floor(progress.progress_bps / 100))}%` }}
          />
        </div>
        <dl className="mt-6 grid gap-4 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-[var(--nm-muted)]">{overspent ? "Перерасход" : "Осталось"}</dt>
            <dd className={overspent ? "mt-1 font-medium text-[var(--nm-danger)]" : "mt-1 font-medium text-[var(--nm-text)]"}>
              {formatMoney(
                overspent ? progress.overspent_minor : progress.remaining_minor,
                value.currency,
                locale,
              )}
            </dd>
          </div>
          <div>
            <dt className="text-[var(--nm-muted)]">Охват</dt>
            <dd className="mt-1 font-medium text-[var(--nm-text)]">
              {value.category_id === null ? "Все расходы" : "Одна категория расходов"}
            </dd>
          </div>
          <div>
            <dt className="text-[var(--nm-muted)]">Валюта</dt>
            <dd className="mt-1 font-medium text-[var(--nm-text)]">{value.currency}</dd>
          </div>
          <div>
            <dt className="text-[var(--nm-muted)]">Часовой пояс периода</dt>
            <dd className="mt-1 font-medium text-[var(--nm-text)]">{value.timezone}</dd>
          </div>
        </dl>
      </section>

      <MutationFeedback
        error={lifecycle.error}
        onRetryUnknown={lifecycle.retryUnknown}
        outcomeUnknown={lifecycle.outcomeUnknown}
        pending={lifecycle.isPending}
      />

      <div className="flex flex-wrap gap-3">
        <Link className="button button--secondary" to="/budgets">
          К списку
        </Link>
        <button
          className={deleted ? "button button--primary" : "button button--danger"}
          disabled={lifecycle.isPending}
          onClick={() => runLifecycle(deleted ? "restore" : "delete")}
          type="button"
        >
          {lifecycle.isPending ? "Сохраняем…" : deleted ? "Восстановить" : "Удалить"}
        </button>
      </div>
    </div>
  );
}
