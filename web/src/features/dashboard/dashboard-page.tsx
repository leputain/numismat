import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo } from "react";
import { Link, useNavigate } from "react-router";

import { apiClient } from "../../app/providers";
import type {
  ActiveDraftResponse,
  BudgetPageResponse,
  DashboardResponse,
} from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { EmptyState, ErrorState } from "../../shared/components/async-state";
import { MutationFeedback } from "../../shared/components/mutation-feedback";
import { PageHeading } from "../../shared/components/page-heading";
import { formatExclusivePeriod } from "../../shared/format/date-time";
import { emitClientEvent } from "../../shared/logging/client-events";
import { queryKeys } from "../../shared/queries/query-keys";
import { MonthlySummaryCards } from "../analytics/analytics-insights";
import { ownerMonthWindow } from "../budgets/budget-window";
import { QuickCaptureForm } from "../drafts/quick-capture-form";
import { useQuickDraftMutation } from "../drafts/use-quick-draft-mutation";
import { TransactionCard } from "../transactions/transaction-card";
import { DashboardBudgetSignal } from "./budget-signal";

const DASHBOARD_BUDGET_LIMIT = 50;

function DashboardSkeleton() {
  return (
    <div aria-busy="true" aria-label="Загрузка обзора" className="page-stack" role="status">
      <div className="skeleton h-20 w-full" />
      <div className="skeleton h-48 w-full" />
      <div className="summary-grid monthly-results">
        <div className="skeleton h-48 w-full" />
        <div className="skeleton h-48 w-full" />
      </div>
      <div className="skeleton h-28 w-full" />
      <div className="skeleton h-64 w-full" />
      <span className="sr-only">Собираем обзор…</span>
    </div>
  );
}

function BudgetSignal({
  query,
  locale,
}: {
  readonly query: ReturnType<typeof useQuery<BudgetPageResponse>>;
  readonly locale: string;
}) {
  if (query.isPending) {
    return <div aria-label="Проверяем бюджеты" className="skeleton h-24 w-full" role="status" />;
  }
  if (query.isError) {
    return (
      <section className="notice notice--warning">
        <div>
          <p className="notice__title">Статус бюджета не загрузился</p>
          <p className="notice__text">Операции доступны; контроль лимита можно открыть отдельно.</p>
        </div>
        <Link className="button button--secondary" to="/budgets">Открыть бюджеты</Link>
      </section>
    );
  }
  return (
    <DashboardBudgetSignal
      budgets={query.data.items}
      complete={query.data.next_cursor === null}
      locale={locale}
    />
  );
}

export function DashboardPage() {
  const { locale, timeZone } = useSessionFormat();
  const navigate = useNavigate();
  const monthWindow = useMemo(() => ownerMonthWindow(timeZone), [timeZone]);
  const dashboard = useQuery({
    queryKey: queryKeys.dashboard,
    queryFn: ({ signal }) => apiClient.get<DashboardResponse>("/api/v1/dashboard", { signal }),
    staleTime: 30_000,
  });
  const activeDraft = useQuery({
    queryKey: queryKeys.drafts.active,
    queryFn: ({ signal }) => apiClient.get<ActiveDraftResponse>("/api/v1/drafts/active", { signal }),
    staleTime: 0,
  });
  const budgets = useQuery({
    queryKey: queryKeys.budgets.active(
      monthWindow.startsOn,
      monthWindow.endsOn,
      DASHBOARD_BUDGET_LIMIT,
    ),
    queryFn: ({ signal }) => {
      const params = new URLSearchParams({
        starts_on: monthWindow.startsOn,
        ends_on: monthWindow.endsOn,
        deleted: "false",
        limit: String(DASHBOARD_BUDGET_LIMIT),
      });
      return apiClient.get<BudgetPageResponse>(`/api/v1/budgets?${params.toString()}`, { signal });
    },
    staleTime: 30_000,
  });
  const quickDraft = useQuickDraftMutation({
    onDraftReady: () => navigate("/draft"),
  });

  useEffect(() => emitClientEvent("dashboard_opened"), []);

  if (dashboard.isPending) {
    return <DashboardSkeleton />;
  }
  if (dashboard.isError) {
    return <ErrorState onAction={() => void dashboard.refetch()} />;
  }

  const current = dashboard.data.current_period;
  const comparable = dashboard.data.comparable_period;
  const hasDraft = activeDraft.data?.draft != null;
  const recent = dashboard.data.recent_transactions.slice(0, 3);
  return (
    <div className="page-stack dashboard-page">
      <PageHeading
        description={formatExclusivePeriod(current.start, current.end, locale, timeZone)}
        eyebrow="Личные финансы"
        title="Обзор"
      />

      {hasDraft ? (
        <Link className="draft-banner" to="/draft">
          <span aria-hidden="true" className="draft-banner__icon">✦</span>
          <span className="min-w-0 flex-1">
            <strong>Черновик ждёт проверки</strong>
            <span>Продолжить с последнего шага</span>
          </span>
          <span aria-hidden="true">›</span>
        </Link>
      ) : (
        <section aria-labelledby="quick-capture-title" className="surface-panel dashboard-capture">
          <div className="section-heading section-heading--inside">
            <div>
              <p className="eyebrow">Без лишних кнопок</p>
              <h2 className="section-title" id="quick-capture-title">Записать операцию</h2>
            </div>
            <Link className="text-link" to="/draft">Полная форма</Link>
          </div>
          <QuickCaptureForm
            busy={quickDraft.isPending}
            disabled={quickDraft.disabled || activeDraft.isPending}
            onSubmit={quickDraft.submit}
          />
          <MutationFeedback
            error={quickDraft.error}
            onRetryUnknown={quickDraft.retryUnknown}
            outcomeUnknown={quickDraft.outcomeUnknown}
            pending={quickDraft.isPending}
          />
        </section>
      )}

      <section aria-labelledby="month-title" className="dashboard-hero">
        <div className="section-heading dashboard-hero__heading">
          <div>
            <p className="eyebrow">Каждая валюта отдельно</p>
            <h2 className="section-title" id="month-title">Результат месяца</h2>
          </div>
          <Link className="text-link" to="/analytics">Вся аналитика</Link>
        </div>
        <MonthlySummaryCards comparable={comparable} current={current} />
      </section>

      <BudgetSignal locale={locale} query={budgets} />

      <section aria-labelledby="recent-title" className="surface-panel">
        <div className="section-heading section-heading--inside">
          <div>
            <p className="eyebrow">Последние записи</p>
            <h2 className="section-title" id="recent-title">Три последние операции</h2>
          </div>
          <Link className="text-link" to="/transactions">Все</Link>
        </div>
        {recent.length === 0 ? (
          <EmptyState title="История пока пуста">
            <p>Первая подтверждённая операция появится здесь.</p>
          </EmptyState>
        ) : (
          <div className="transaction-list transaction-list--panel">
            {recent.map((transaction) => (
              <TransactionCard key={transaction.id} transaction={transaction} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
