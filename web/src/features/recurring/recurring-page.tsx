import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router";

import { apiClient } from "../../app/providers";
import type { RecurringSchedulePageResponse } from "../../shared/api/types";
import { EmptyState, ErrorState, PageSkeleton } from "../../shared/components/async-state";
import { PageHeading } from "../../shared/components/page-heading";
import { emitClientEvent } from "../../shared/logging/client-events";
import { restartRecurringPagination } from "../../shared/mutations/query-recovery";
import { queryKeys } from "../../shared/queries/query-keys";
import { RecurringCard } from "./recurring-card";

const PAGE_LIMIT = 20;
type RecurringMode = "active" | "trash";

function pagePath(mode: RecurringMode, cursor: string | null): string {
  const params = new URLSearchParams({
    deleted: mode === "trash" ? "true" : "false",
    limit: String(PAGE_LIMIT),
  });
  if (cursor !== null) {
    params.set("cursor", cursor);
  }
  return `/api/v1/recurring-schedules?${params.toString()}`;
}

export function RecurringPage() {
  const [mode, setMode] = useState<RecurringMode>("active");
  const queryClient = useQueryClient();
  const queryKey =
    mode === "active"
      ? queryKeys.recurring.active(PAGE_LIMIT)
      : queryKeys.recurring.trash(PAGE_LIMIT);
  const feed = useInfiniteQuery({
    queryKey,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      apiClient.get<RecurringSchedulePageResponse>(pagePath(mode, pageParam), { signal }),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    staleTime: 30_000,
  });

  useEffect(() => emitClientEvent("recurring_opened"), []);

  const content = (() => {
    if (feed.isPending) {
      return <PageSkeleton rows={4} />;
    }
    if (feed.isError) {
      return (
        <ErrorState
          description="Список будет загружен заново с начала."
          onAction={() => void restartRecurringPagination(queryClient)}
        />
      );
    }
    const schedules = feed.data.pages.flatMap((page) => page.items);
    if (schedules.length === 0) {
      return mode === "active" ? (
        <EmptyState title="Расписаний пока нет">
          <p>Создайте правило, а бот подготовит операцию как обычный черновик для проверки.</p>
          <Link className="button button--primary mt-5" to="/recurring/new">Создать</Link>
        </EmptyState>
      ) : (
        <EmptyState title="Корзина пуста">Удалённые расписания можно восстановить здесь.</EmptyState>
      );
    }
    return (
      <div>
        <div className="space-y-3">
          {schedules.map((schedule) => (
            <RecurringCard key={`${mode}:${schedule.id}`} schedule={schedule} />
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
          </div>
        ) : (
          <p className="feed-end">Это весь список</p>
        )}
      </div>
    );
  })();

  return (
    <div className="recurring-page page-stack">
      <PageHeading
        action={mode === "active" ? <Link className="button button--primary" to="/recurring/new">Новое</Link> : undefined}
        description="По расписанию создаётся черновик. Операция появится только после вашего подтверждения."
        eyebrow="Под вашим контролем"
        title="Регулярные"
      />
      <div aria-label="Раздел расписаний" className="segment-control" role="tablist">
        <button aria-selected={mode === "active"} className="segment-control__button" onClick={() => setMode("active")} role="tab" type="button">Активные</button>
        <button aria-selected={mode === "trash"} className="segment-control__button" onClick={() => setMode("trash")} role="tab" type="button">Корзина</button>
      </div>
      <section aria-live="polite">{content}</section>
    </div>
  );
}
