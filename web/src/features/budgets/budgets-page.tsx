import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router";

import { apiClient } from "../../app/providers";
import type { BudgetPageResponse } from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { EmptyState, ErrorState, PageSkeleton } from "../../shared/components/async-state";
import { PageHeading } from "../../shared/components/page-heading";
import { emitClientEvent } from "../../shared/logging/client-events";
import { restartBudgetPagination } from "../../shared/mutations/query-recovery";
import { queryKeys } from "../../shared/queries/query-keys";
import { BudgetCard } from "./budget-card";
import { BudgetStateLegend } from "./budget-progress";
import { budgetMonthWindow, ownerMonthWindow } from "./budget-window";

const PAGE_LIMIT = 20;
type BudgetMode = "active" | "trash";

function pagePath(
  mode: BudgetMode,
  startsOn: string,
  endsOn: string,
  cursor: string | null,
): string {
  const params = new URLSearchParams({
    starts_on: startsOn,
    ends_on: endsOn,
    deleted: mode === "trash" ? "true" : "false",
    limit: String(PAGE_LIMIT),
  });
  if (cursor !== null) {
    params.set("cursor", cursor);
  }
  return `/api/v1/budgets?${params.toString()}`;
}

export function BudgetsPage() {
  const [mode, setMode] = useState<BudgetMode>("active");
  const queryClient = useQueryClient();
  const { timeZone } = useSessionFormat();
  const defaultWindow = useMemo(() => ownerMonthWindow(timeZone), [timeZone]);
  const [month, setMonth] = useState(defaultWindow.startsOn.slice(0, 7));
  const window = useMemo(
    () => budgetMonthWindow(month) ?? defaultWindow,
    [defaultWindow, month],
  );
  const queryKey =
    mode === "active"
      ? queryKeys.budgets.active(window.startsOn, window.endsOn, PAGE_LIMIT)
      : queryKeys.budgets.trash(window.startsOn, window.endsOn, PAGE_LIMIT);
  const feed = useInfiniteQuery({
    queryKey,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      apiClient.get<BudgetPageResponse>(
        pagePath(mode, window.startsOn, window.endsOn, pageParam),
        { signal },
      ),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    staleTime: 30_000,
  });

  useEffect(() => emitClientEvent("budgets_opened"), []);

  const content = (() => {
    if (feed.isPending) {
      return <PageSkeleton rows={4} />;
    }
    if (feed.isError) {
      return (
        <ErrorState
          description="Список будет загружен заново с начала."
          onAction={() => {
            void restartBudgetPagination(queryClient);
          }}
        />
      );
    }
    const budgets = feed.data.pages.flatMap((page) => page.items);
    if (budgets.length === 0) {
      return mode === "active" ? (
        <EmptyState title="Бюджетов пока нет">
          <p>Задайте лимит в конкретной валюте — пересчёта между валютами здесь нет.</p>
          <Link className="button button--primary mt-5" to="/budgets/new">
            Создать бюджет
          </Link>
        </EmptyState>
      ) : (
        <EmptyState title="Корзина пуста">
          Удалённые бюджеты останутся здесь, пока вы не восстановите их.
        </EmptyState>
      );
    }
    return (
      <div>
        <div className="space-y-3">
          {budgets.map((budget) => (
            <BudgetCard budget={budget} key={`${mode}:${budget.id}`} />
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
          <p className="feed-end">Это весь список за выбранный месяц</p>
        )}
      </div>
    );
  })();

  return (
    <div className="budgets-page page-stack">
      <PageHeading
        action={
          mode === "active" ? (
            <Link className="button button--primary" to="/budgets/new">
              Новый
            </Link>
          ) : undefined
        }
        description="Расходы считаются только в валюте бюджета и только в его локальном периоде."
        eyebrow="Контроль расходов"
        title="Бюджеты"
      />
      <BudgetStateLegend />
      <div aria-label="Раздел бюджетов" className="segment-control" role="tablist">
        <button
          aria-selected={mode === "active"}
          className="segment-control__button"
          onClick={() => setMode("active")}
          role="tab"
          type="button"
        >
          Активные
        </button>
        <button
          aria-selected={mode === "trash"}
          className="segment-control__button"
          onClick={() => setMode("trash")}
          role="tab"
          type="button"
        >
          Корзина
        </button>
      </div>
      <label className="field-stack max-w-xs" htmlFor="budget-month">
        <span className="field-label">Показывать месяц</span>
        <input
          className="field-input"
          id="budget-month"
          max="9998-12"
          min="0001-01"
          onChange={(event) => setMonth(event.currentTarget.value)}
          type="month"
          value={month}
        />
      </label>
      <section aria-live="polite">{content}</section>
    </div>
  );
}
