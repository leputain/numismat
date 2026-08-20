import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { Link } from "react-router";

import { apiClient } from "../../app/providers";
import type { DashboardResponse } from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { ErrorState } from "../../shared/components/async-state";
import { PageHeading } from "../../shared/components/page-heading";
import { formatExclusivePeriod } from "../../shared/format/date-time";
import { emitClientEvent } from "../../shared/logging/client-events";
import { queryKeys } from "../../shared/queries/query-keys";
import { CategoryShareList, PeriodComparisonCards } from "./analytics-insights";
import { CashflowTrend } from "./cashflow-trend";

function AnalyticsSkeleton() {
  return (
    <div aria-busy="true" aria-label="Загрузка аналитики" className="page-stack" role="status">
      <div className="skeleton h-20 w-full" />
      <div className="analytics-period-grid">
        <div className="skeleton h-24 w-full" />
        <div className="skeleton h-24 w-full" />
      </div>
      <div className="skeleton h-80 w-full" />
      <div className="skeleton h-72 w-full" />
      <span className="sr-only">Собираем аналитику…</span>
    </div>
  );
}

export function AnalyticsPage() {
  const { locale, timeZone } = useSessionFormat();
  const dashboard = useQuery({
    queryKey: queryKeys.dashboard,
    queryFn: ({ signal }) => apiClient.get<DashboardResponse>("/api/v1/dashboard", { signal }),
    staleTime: 30_000,
  });

  useEffect(() => emitClientEvent("analytics_opened"), []);

  if (dashboard.isPending) {
    return <AnalyticsSkeleton />;
  }
  if (dashboard.isError) {
    return <ErrorState onAction={() => void dashboard.refetch()} />;
  }

  const { current_period: current, comparable_period: comparable, top_categories: categories } =
    dashboard.data;
  return (
    <div className="page-stack analytics-page">
      <PageHeading
        action={
          <Link className="button button--secondary analytics-page__action" to="/transactions">
            Операции
          </Link>
        }
        description="Неделя, месяц или год — суммы по каждой валюте отдельно."
        eyebrow="Финансовая картина"
        title="Аналитика"
      />

      <CashflowTrend comparable={comparable} current={current} timeZone={timeZone} />

      <section aria-labelledby="monthly-context-title" className="analytics-monthly-context">
        <div className="section-heading analytics-monthly-context__heading">
          <div>
            <p className="eyebrow">Месячный контекст</p>
            <h2 className="section-title" id="monthly-context-title">Месяц в деталях</h2>
            <p className="section-description">
              Отдельный срез текущего месяца: границы, сравнение и структура расходов.
            </p>
          </div>
        </div>

        <section aria-labelledby="periods-title" className="analytics-overview analytics-context-section">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Границы отчёта</p>
              <h3 className="section-title" id="periods-title">Текущий и прошлый месяц</h3>
            </div>
          </div>
          <div className="analytics-period-grid analytics-overview__periods">
            <article className="period-card period-card--current">
              <span>Этот месяц</span>
              <strong>{formatExclusivePeriod(current.start, current.end, locale, timeZone)}</strong>
            </article>
            <article className="period-card">
              <span>Прошлый месяц</span>
              <strong>{formatExclusivePeriod(comparable.start, comparable.end, locale, timeZone)}</strong>
            </article>
          </div>
        </section>

        <section
          aria-labelledby="comparison-title"
          className="analytics-comparison analytics-context-section"
        >
          <div className="section-heading">
            <div>
              <p className="eyebrow">Каждая валюта отдельно</p>
              <h3 className="section-title" id="comparison-title">Изменение расходов</h3>
            </div>
            <Link className="text-link" to="/transactions">Операции</Link>
          </div>
          <PeriodComparisonCards comparable={comparable} current={current} />
        </section>

        <section
          aria-labelledby="analytics-categories-title"
          className="analytics-categories analytics-context-section"
        >
          <div className="section-heading">
            <div>
              <p className="eyebrow">Структура месяца</p>
              <h3 className="section-title" id="analytics-categories-title">Куда ушли деньги</h3>
            </div>
          </div>
          <CategoryShareList categories={categories} totals={current.totals} />
        </section>
      </section>
    </div>
  );
}
