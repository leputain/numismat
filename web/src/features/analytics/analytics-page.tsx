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
            Все операции
          </Link>
        }
        description="Сравнение с прошлым месяцем. Суммы в разных валютах показаны отдельно."
        eyebrow="Финансовая картина"
        title="Аналитика"
      />

      <section aria-labelledby="periods-title" className="analytics-overview">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Границы отчёта</p>
            <h2 className="section-title" id="periods-title">Периоды сравнения</h2>
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

      <section aria-labelledby="comparison-title" className="analytics-comparison">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Каждая валюта отдельно</p>
            <h2 className="section-title" id="comparison-title">Этот месяц и прошлый</h2>
          </div>
          <Link className="text-link" to="/transactions">Операции</Link>
        </div>
        <PeriodComparisonCards comparable={comparable} current={current} />
      </section>

      <CashflowTrend end={current.end} start={current.start} />

      <section
        aria-labelledby="analytics-categories-title"
        className="surface-panel surface-panel--roomy analytics-categories"
      >
        <div className="section-heading section-heading--inside">
          <div>
            <p className="eyebrow">Структура месяца</p>
            <h2 className="section-title" id="analytics-categories-title">Крупнейшие категории расходов</h2>
          </div>
        </div>
        <CategoryShareList categories={categories} totals={current.totals} />
      </section>
    </div>
  );
}
