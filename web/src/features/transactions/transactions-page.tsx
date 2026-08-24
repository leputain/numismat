import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link, useLocation } from "react-router";

import { apiClient } from "../../app/providers";
import type { TransactionPageResponse } from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { EmptyState, ErrorState, PageSkeleton } from "../../shared/components/async-state";
import { PageHeading } from "../../shared/components/page-heading";
import { emitClientEvent } from "../../shared/logging/client-events";
import { restartTransactionPagination } from "../../shared/mutations/query-recovery";
import { queryKeys } from "../../shared/queries/query-keys";
import { TransactionCard } from "./transaction-card";
import { TransactionFilterPanel } from "./transaction-filter-panel";
import {
  activeTransactionFilterCount,
  buildTransactionQueryFilters,
  createTransactionFilterState,
  transactionPagePath,
} from "./transaction-filter-model";
import type {
  TransactionFeedMode,
  TransactionFilterState,
  TransactionQueryFilters,
} from "./transaction-filter-model";

const PAGE_LIMIT = 30;

function TransactionFeed({
  mode,
  filters,
}: {
  readonly mode: TransactionFeedMode;
  readonly filters: TransactionQueryFilters;
}) {
  const queryClient = useQueryClient();
  const queryKey =
    mode === "active"
      ? queryKeys.transactions.active(PAGE_LIMIT, filters)
      : queryKeys.transactions.trash(PAGE_LIMIT);
  const feed = useInfiniteQuery({
    queryKey,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      apiClient.get<TransactionPageResponse>(
        transactionPagePath(mode, pageParam, filters, PAGE_LIMIT),
        { signal },
      ),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    staleTime: 30_000,
  });

  if (feed.isPending) {
    return <PageSkeleton rows={5} />;
  }
  if (feed.isError) {
    return (
      <ErrorState
        description="Повторная загрузка начнётся с первой записи, чтобы в истории не появились дубликаты."
        onAction={() => {
          void restartTransactionPagination(queryClient);
        }}
      />
    );
  }

  const transactions = feed.data.pages.flatMap((page) => page.items);
  if (transactions.length === 0) {
    return mode === "active" ? (
      activeTransactionFilterCount(filters) > 0 ? (
        <EmptyState title="По фильтрам ничего нет">
          <p>Измените период или снимите часть условий — исходная история останется на месте.</p>
        </EmptyState>
      ) : (
        <EmptyState title="Операций пока нет">
          <p>Перед сохранением вы сможете проверить сумму, категорию и счёт.</p>
          <Link className="button button--primary mt-5" to="/draft">Создать черновик</Link>
        </EmptyState>
      )
    ) : (
      <EmptyState title="Корзина пуста">Удалённые операции появятся здесь и останутся доступными для восстановления.</EmptyState>
    );
  }

  return (
    <div>
      <div className="transaction-list">
        {transactions.map((transaction) => (
          <TransactionCard key={`${mode}:${transaction.id}`} transaction={transaction} />
        ))}
      </div>
      {feed.hasNextPage ? (
        <div className="load-more">
          <button
            className="button button--secondary"
            disabled={feed.isFetchingNextPage}
            onClick={() => void feed.fetchNextPage()}
            type="button"
          >
            {feed.isFetchingNextPage ? "Загружаем…" : "Показать ещё"}
          </button>
          {feed.isFetchNextPageError ? (
            <p aria-live="polite" className="feed-error" role="alert">
              Не удалось показать более ранние операции. Уже загруженные записи останутся на экране.
            </p>
          ) : null}
        </div>
      ) : (
        <p className="feed-end">Это вся доступная история</p>
      )}
    </div>
  );
}

export function TransactionsPage() {
  const queryClient = useQueryClient();
  const location = useLocation();
  const { baseCurrency, timeZone } = useSessionFormat();
  const [mode, setMode] = useState<TransactionFeedMode>("active");
  const [periodAnchor] = useState(() => new Date());
  const [filterState, setFilterState] = useState<TransactionFilterState>(() =>
    createTransactionFilterState(timeZone, periodAnchor, location.search),
  );
  const filters = useMemo(
    () => buildTransactionQueryFilters(filterState, timeZone, periodAnchor),
    [filterState, periodAnchor, timeZone],
  );

  useEffect(() => emitClientEvent("transactions_opened"), []);

  const replaceFilters = (nextState: TransactionFilterState) => {
    if (nextState !== filterState) {
      void queryClient.cancelQueries({ queryKey: queryKeys.transactions.activeRoot });
      queryClient.removeQueries({ queryKey: queryKeys.transactions.activeRoot });
      setFilterState(nextState);
    }
  };

  return (
    <div className="page-stack transactions-page">
      <PageHeading
        action={<Link className="button button--primary" to="/draft">Новая запись</Link>}
        description="Найдите нужные операции по периоду, типу, счёту, категории или валюте."
        eyebrow="История"
        title="Операции"
      />
      <div aria-label="Раздел операций" className="segment-control" role="tablist">
        <button
          aria-controls="transaction-feed"
          aria-selected={mode === "active"}
          className="segment-control__button"
          id="transactions-active-tab"
          onClick={() => setMode("active")}
          role="tab"
          type="button"
        >
          История
        </button>
        <button
          aria-controls="transaction-feed"
          aria-selected={mode === "trash"}
          className="segment-control__button"
          id="transactions-trash-tab"
          onClick={() => setMode("trash")}
          role="tab"
          type="button"
        >
          Корзина
        </button>
      </div>
      <section
        aria-labelledby={mode === "active" ? "transactions-active-tab" : "transactions-trash-tab"}
        aria-live="polite"
        className="transaction-feed-stack"
        id="transaction-feed"
        role="tabpanel"
      >
        {mode === "active" ? (
          <TransactionFilterPanel
            baseCurrency={baseCurrency}
            filters={filters}
            onChange={replaceFilters}
            onReset={() => {
              replaceFilters(createTransactionFilterState(timeZone, periodAnchor));
            }}
            state={filterState}
          />
        ) : null}
        <TransactionFeed filters={filters} key={mode} mode={mode} />
      </section>
    </div>
  );
}
