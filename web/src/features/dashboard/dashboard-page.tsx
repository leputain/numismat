import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { Link } from "react-router";

import { apiClient } from "../../app/providers";
import type {
  ActiveDraftResponse,
  DashboardResponse,
  PeriodReportResponse,
} from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { EmptyState, ErrorState } from "../../shared/components/async-state";
import { PageHeading } from "../../shared/components/page-heading";
import { formatExclusivePeriod } from "../../shared/format/date-time";
import { emitClientEvent } from "../../shared/logging/client-events";
import { queryKeys } from "../../shared/queries/query-keys";
import {
  CategoryShareList,
  MonthlySummaryCards,
  PeriodTotalsCards,
} from "../analytics/analytics-insights";
import { TransactionCard } from "../transactions/transaction-card";

const QUICK_ACTIONS = [
  { to: "/draft", icon: "+", label: "Добавить", description: "Новая операция", primary: true },
  { to: "/transactions", icon: "≡", label: "История", description: "Все записи", primary: false },
  { to: "/analytics", icon: "⌁", label: "Аналитика", description: "Динамика", primary: false },
  { to: "/budgets", icon: "◎", label: "Бюджеты", description: "Лимиты", primary: false },
] as const;

function DashboardSkeleton() {
  return (
    <div aria-busy="true" aria-label="Загрузка обзора" className="page-stack" role="status">
      <div className="skeleton h-20 w-full" />
      <div className="quick-actions">
        {QUICK_ACTIONS.map((action) => (
          <div className="skeleton h-20 w-full" key={action.to} />
        ))}
      </div>
      <div className="summary-grid">
        <div className="skeleton h-48 w-full" />
        <div className="skeleton h-48 w-full" />
      </div>
      <div className="skeleton h-72 w-full" />
      <span className="sr-only">Собираем обзор…</span>
    </div>
  );
}

function TodaySection({ report }: { readonly report: ReturnType<typeof useQuery<PeriodReportResponse>> }) {
  const { locale, timeZone } = useSessionFormat();
  return (
    <section aria-labelledby="today-title" className="surface-panel today-panel">
      <div className="section-heading section-heading--inside">
        <div>
          <p className="eyebrow">Короткий срез</p>
          <h2 className="section-title" id="today-title">Сегодня</h2>
        </div>
        {report.data === undefined ? null : (
          <span className="period-caption">
            {formatExclusivePeriod(
              report.data.period.start,
              report.data.period.end,
              locale,
              timeZone,
            )}
          </span>
        )}
      </div>
      {report.isPending ? (
        <div aria-busy="true" aria-label="Загрузка данных за сегодня" className="today-grid" role="status">
          <div className="skeleton h-24 w-full" />
          <span className="sr-only">Загружаем данные за сегодня…</span>
        </div>
      ) : report.isError ? (
        <div className="inline-state" role="alert">
          <div>
            <strong>Срез за сегодня не загрузился</strong>
            <span>Остальной обзор доступен.</span>
          </div>
          <button className="button button--secondary" onClick={() => void report.refetch()} type="button">
            Повторить
          </button>
        </div>
      ) : (
        <PeriodTotalsCards totals={report.data.period.totals} />
      )}
    </section>
  );
}

export function DashboardPage() {
  const { locale, timeZone } = useSessionFormat();
  const dashboard = useQuery({
    queryKey: queryKeys.dashboard,
    queryFn: ({ signal }) => apiClient.get<DashboardResponse>("/api/v1/dashboard", { signal }),
    staleTime: 30_000,
  });
  const today = useQuery({
    queryKey: queryKeys.today,
    queryFn: ({ signal }) => apiClient.get<PeriodReportResponse>("/api/v1/reports/today", { signal }),
    staleTime: 30_000,
  });
  const activeDraft = useQuery({
    queryKey: queryKeys.drafts.active,
    queryFn: ({ signal }) => apiClient.get<ActiveDraftResponse>("/api/v1/drafts/active", { signal }),
    staleTime: 0,
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
  return (
    <div className="page-stack">
      <PageHeading
        action={
          <Link className="button button--primary" to="/draft">
            <span aria-hidden="true">＋</span> Записать
          </Link>
        }
        description={formatExclusivePeriod(current.start, current.end, locale, timeZone)}
        eyebrow="Личные финансы"
        title="Обзор"
      />

      {activeDraft.data?.draft == null ? null : (
        <Link className="draft-banner" to="/draft">
          <span aria-hidden="true" className="draft-banner__icon">✦</span>
          <span className="min-w-0 flex-1">
            <strong>Черновик ждёт проверки</strong>
            <span>Продолжить с сохранённого шага</span>
          </span>
          <span aria-hidden="true">›</span>
        </Link>
      )}

      <section aria-label="Быстрые действия">
        <div className="quick-actions">
          {QUICK_ACTIONS.map((action) => (
            <Link
              className={`quick-action${action.primary ? " quick-action--primary" : ""}`}
              key={action.to}
              to={action.to}
            >
              <span aria-hidden="true" className="quick-action__icon">{action.icon}</span>
              <span>
                <strong>{action.label}</strong>
                <small>{action.description}</small>
              </span>
            </Link>
          ))}
        </div>
      </section>

      <section aria-labelledby="month-title">
        <div className="section-heading">
          <div>
            <p className="eyebrow">По валютам отдельно</p>
            <h2 className="section-title" id="month-title">Итоги месяца</h2>
          </div>
          <Link className="text-link" to="/analytics">Подробнее</Link>
        </div>
        <MonthlySummaryCards comparable={comparable} current={current} />
      </section>

      <TodaySection report={today} />

      <div className="dashboard-columns">
        <section aria-labelledby="categories-title" className="surface-panel">
          <div className="section-heading section-heading--inside">
            <div>
              <p className="eyebrow">Главные траты</p>
              <h2 className="section-title" id="categories-title">Категории</h2>
            </div>
            <Link className="text-link" to="/analytics">Аналитика</Link>
          </div>
          <CategoryShareList categories={dashboard.data.top_categories} totals={current.totals} />
        </section>

        <section aria-labelledby="recent-title" className="surface-panel">
          <div className="section-heading section-heading--inside">
            <div>
              <p className="eyebrow">Последние записи</p>
              <h2 className="section-title" id="recent-title">Недавние операции</h2>
            </div>
            <Link className="text-link" to="/transactions">Все</Link>
          </div>
          {dashboard.data.recent_transactions.length === 0 ? (
            <EmptyState title="История пока пуста">
              <p>Первая подтверждённая операция появится здесь.</p>
              <Link className="button button--primary mt-5" to="/draft">Добавить запись</Link>
            </EmptyState>
          ) : (
            <div className="transaction-list transaction-list--panel">
              {dashboard.data.recent_transactions.map((transaction) => (
                <TransactionCard key={transaction.id} transaction={transaction} />
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
