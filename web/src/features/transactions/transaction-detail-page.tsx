import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router";

import { HttpApiError } from "../../api/errors";
import { apiClient } from "../../app/providers";
import { RouteErrorState } from "../../app/states/route-error-state";
import type { MutationResponse, Transaction } from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { ErrorState, PageSkeleton } from "../../shared/components/async-state";
import { MutationFeedback } from "../../shared/components/mutation-feedback";
import { PageHeading } from "../../shared/components/page-heading";
import { isOptimisticConflict } from "../../shared/errors/user-message";
import { formatTransactionDate } from "../../shared/format/date-time";
import { formatTransactionMoney } from "../../shared/finance/money";
import { emitClientEvent } from "../../shared/logging/client-events";
import { usePreparedMutation } from "../../shared/mutations/prepared-mutation";
import {
  refreshDraftQueries,
  refreshFinanceQueries,
  restartTransactionPagination,
} from "../../shared/mutations/query-recovery";
import { queryKeys } from "../../shared/queries/query-keys";

type TransactionAction = "delete" | "edit-draft" | "repeat" | "restore";

export function TransactionDetailPage({ transactionId }: { readonly transactionId: string }) {
  const { locale, timeZone } = useSessionFormat();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const transaction = useQuery({
    queryKey: queryKeys.transactions.detail(transactionId),
    queryFn: ({ signal }) =>
      apiClient.get<Transaction>(`/api/v1/transactions/${transactionId}`, { signal }),
    staleTime: 30_000,
  });
  const action = usePreparedMutation<MutationResponse, TransactionAction>({
    eventScope: "transaction",
    onSuccess: async (response) => {
      setConfirmDelete(false);
      await restartTransactionPagination(queryClient);
      await Promise.all([refreshFinanceQueries(queryClient), refreshDraftQueries(queryClient)]);
      if (response.result.kind === "draft") {
        navigate("/draft");
        return;
      }
      await queryClient.invalidateQueries({
        queryKey: queryKeys.transactions.detail(response.result.transaction_id),
      });
    },
    onRejected: async (error) => {
      await restartTransactionPagination(queryClient);
      if (isOptimisticConflict(error)) {
        emitClientEvent("mutation_conflict_detected");
      }
      await Promise.all([transaction.refetch(), refreshDraftQueries(queryClient)]);
      if (error instanceof HttpApiError && error.code === "active_draft_conflict") {
        navigate("/draft");
      }
    },
    onOutcomeUnknown: async () => {
      await restartTransactionPagination(queryClient);
      await Promise.all([refreshFinanceQueries(queryClient), refreshDraftQueries(queryClient)]);
    },
  });

  useEffect(() => emitClientEvent("transaction_detail_opened"), []);

  if (transaction.isPending) {
    return <PageSkeleton rows={4} />;
  }
  if (transaction.isError) {
    if (transaction.error instanceof HttpApiError && transaction.error.code === "not_found") {
      return (
        <RouteErrorState
          description="Запись удалена окончательно или адрес больше не актуален."
          title="Операция не найдена"
        />
      );
    }
    return <ErrorState onAction={() => void transaction.refetch()} />;
  }

  const item = transaction.data;
  const deleted = item.deleted_at !== null;
  const disabled = action.isPending || action.outcomeUnknown;
  const run = (kind: TransactionAction) => {
    action.run({
      path: `/api/v1/transactions/${item.id}/${kind}`,
      body: { version: item.version },
      context: kind,
    });
  };

  return (
    <div className="page-stack page-stack--narrow">
      <Link className="back-link" replace to="/transactions">← К операциям</Link>
      <PageHeading
        description={formatTransactionDate(item.occurred_at, locale, timeZone)}
        eyebrow={deleted ? "Корзина" : item.type === "income" ? "Доход" : "Расход"}
        title={item.category.name}
      />

      <article className="detail-card">
        <div className="detail-card__hero">
          <span aria-hidden="true" className="detail-card__emoji">{item.category.emoji}</span>
          <p className={`detail-card__amount detail-card__amount--${item.type}`}>
            {formatTransactionMoney(item.amount_minor, item.currency, item.type, locale)}
          </p>
          {deleted ? <span className="status-chip status-chip--danger">Удалена</span> : null}
        </div>
        <dl className="detail-list">
          <div><dt>Счёт</dt><dd>{item.account.name}</dd></div>
          <div><dt>Категория</dt><dd>{item.category.emoji} {item.category.name}</dd></div>
          <div><dt>Дата</dt><dd>{formatTransactionDate(item.occurred_at, locale, timeZone)}</dd></div>
          <div><dt>Описание</dt><dd>{item.description || "Без описания"}</dd></div>
          <div><dt>Источник</dt><dd>{item.source}</dd></div>
        </dl>
      </article>

      <MutationFeedback
        error={action.error}
        onRetryUnknown={action.retryUnknown}
        outcomeUnknown={action.outcomeUnknown}
        pending={action.isPending}
      />

      <section aria-label="Действия с операцией" className="action-grid">
        {deleted ? (
          <button
            className="button button--primary action-grid__wide"
            disabled={disabled}
            onClick={() => run("restore")}
            type="button"
          >
            {action.isPending ? "Восстанавливаем…" : "Восстановить"}
          </button>
        ) : (
          <>
            <button className="button button--primary" disabled={disabled} onClick={() => run("repeat")} type="button">
              Повторить
            </button>
            <button className="button button--secondary" disabled={disabled} onClick={() => run("edit-draft")} type="button">
              Изменить
            </button>
            <button className="button button--danger action-grid__wide" disabled={disabled} onClick={() => setConfirmDelete(true)} type="button">
              Удалить в корзину
            </button>
          </>
        )}
      </section>

      {confirmDelete ? (
        <div className="dialog-backdrop">
          <section
            aria-labelledby="delete-title"
            aria-modal="true"
            className="dialog"
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                setConfirmDelete(false);
              }
            }}
            role="dialog"
          >
            <p className="eyebrow">Подтверждение</p>
            <h2 className="section-title" id="delete-title">Переместить операцию в корзину?</h2>
            <p className="page-description">Её можно будет восстановить из раздела «Корзина».</p>
            <div className="dialog__actions">
              <button autoFocus className="button button--secondary" onClick={() => setConfirmDelete(false)} type="button">Оставить</button>
              <button className="button button--danger" onClick={() => run("delete")} type="button">Удалить</button>
            </div>
          </section>
        </div>
      ) : null}
    </div>
  );
}
