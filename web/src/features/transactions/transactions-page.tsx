import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router";

import { apiClient } from "../../app/providers";
import type { TransactionPageResponse } from "../../shared/api/types";
import { EmptyState, ErrorState, PageSkeleton } from "../../shared/components/async-state";
import { PageHeading } from "../../shared/components/page-heading";
import { emitClientEvent } from "../../shared/logging/client-events";
import { restartTransactionPagination } from "../../shared/mutations/query-recovery";
import { queryKeys } from "../../shared/queries/query-keys";
import { TransactionCard } from "./transaction-card";

const PAGE_LIMIT = 30;
type FeedMode = "active" | "trash";

function transactionPagePath(mode: FeedMode, cursor: string | null): string {
  const params = new URLSearchParams({ limit: String(PAGE_LIMIT) });
  if (cursor !== null) {
    params.set("cursor", cursor);
  }
  const collection = mode === "active" ? "transactions" : "transactions/trash";
  return `/api/v1/${collection}?${params.toString()}`;
}

function TransactionFeed({ mode }: { readonly mode: FeedMode }) {
  const queryClient = useQueryClient();
  const queryKey = mode === "active" ? queryKeys.transactions.active(PAGE_LIMIT) : queryKeys.transactions.trash(PAGE_LIMIT);
  const feed = useInfiniteQuery({
    queryKey,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      apiClient.get<TransactionPageResponse>(transactionPagePath(mode, pageParam), { signal }),
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
      <EmptyState title="Операций пока нет">
        <p>Перед сохранением вы сможете проверить сумму, категорию и счёт.</p>
        <Link className="button button--primary mt-5" to="/draft">Создать черновик</Link>
      </EmptyState>
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
  const [mode, setMode] = useState<FeedMode>("active");

  useEffect(() => emitClientEvent("transactions_opened"), []);

  return (
    <div className="page-stack transactions-page">
      <PageHeading
        action={<Link className="button button--primary" to="/draft">Новая запись</Link>}
        description="Свежие операции и удалённые записи. После изменений список обновляется автоматически."
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
        id="transaction-feed"
        role="tabpanel"
      >
        <TransactionFeed key={mode} mode={mode} />
      </section>
    </div>
  );
}
