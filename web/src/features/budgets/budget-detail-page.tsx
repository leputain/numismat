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
import { BudgetProgressOverview } from "./budget-progress";
import { formatBudgetDate } from "./budget-window";
import { budgetTransactionsPath } from "../transactions/transaction-filter-model";

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

      <section className="budget-detail-summary surface-panel" data-state={value.progress.state}>
        <div className="budget-detail-summary__header">
          <div>
            <p className="eyebrow">Лимит · {value.currency}</p>
            <p className="budget-detail-summary__limit">
              {formatMoney(value.limit_minor, value.currency, locale)}
            </p>
          </div>
          <p className="budget-detail-summary__scope">Без пересчёта между валютами</p>
        </div>
        <BudgetProgressOverview budget={value} locale={locale} />
        <dl className="budget-detail-metadata">
          <div>
            <dt>Охват</dt>
            <dd>{value.category_id === null ? "Все расходы" : "Одна категория расходов"}</dd>
          </div>
          <div>
            <dt>Валюта</dt>
            <dd>{value.currency} · отдельно от других валют</dd>
          </div>
          <div>
            <dt>Часовой пояс периода</dt>
            <dd>{value.timezone}</dd>
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
        {!deleted ? (
          <Link className="button button--primary" to={budgetTransactionsPath(value)}>
            Операции этого бюджета
          </Link>
        ) : null}
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
