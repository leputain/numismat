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
import { EmptyState, ErrorState, PageSkeleton } from "../../shared/components/async-state";
import { PageHeading } from "../../shared/components/page-heading";
import { formatExclusivePeriod } from "../../shared/format/date-time";
import { formatMoney, moneyMagnitude } from "../../shared/finance/money";
import { emitClientEvent } from "../../shared/logging/client-events";
import { queryKeys } from "../../shared/queries/query-keys";
import { TransactionCard } from "../transactions/transaction-card";

type Totals = DashboardResponse["current_period"]["totals"];
type CategoryTotal = DashboardResponse["top_categories"][number];

function CurrencyTotals({ totals, compact = false }: { readonly totals: Totals; readonly compact?: boolean }) {
  const { locale } = useSessionFormat();
  if (totals.length === 0) {
    return <p className="text-sm text-stone-500">За период движений нет.</p>;
  }
  return (
    <div className={compact ? "summary-grid summary-grid--compact" : "summary-grid"}>
      {totals.map((total) => (
        <article className="summary-card" key={total.currency}>
          <div className="summary-card__heading">
            <span className="summary-card__currency">{total.currency}</span>
            <span className="summary-card__net-label">Баланс периода</span>
          </div>
          <p className="summary-card__net">{formatMoney(total.net_minor, total.currency, locale)}</p>
          <dl className="summary-card__split">
            <div>
              <dt>Доход</dt>
              <dd className="money-income">{formatMoney(total.income_minor, total.currency, locale)}</dd>
            </div>
            <div>
              <dt>Расход</dt>
              <dd className="money-expense">{formatMoney(total.expense_minor, total.currency, locale)}</dd>
            </div>
          </dl>
        </article>
      ))}
    </div>
  );
}

function categoryGroups(categories: CategoryTotal[]): Array<[string, CategoryTotal[]]> {
  const groups = new Map<string, CategoryTotal[]>();
  for (const category of categories) {
    const items = groups.get(category.currency) ?? [];
    items.push(category);
    groups.set(category.currency, items);
  }
  return [...groups.entries()];
}

function CategoryBars({ categories }: { readonly categories: CategoryTotal[] }) {
  const { locale } = useSessionFormat();
  if (categories.length === 0) {
    return <EmptyState title="Пока без лидеров">Категории появятся после первых операций периода.</EmptyState>;
  }
  return (
    <div className="space-y-6">
      {categoryGroups(categories).map(([currency, items]) => {
        const maximum = items.reduce(
          (current, item) => {
            const amount = moneyMagnitude(item.amount_minor);
            return amount > current ? amount : current;
          },
          0n,
        );
        return (
          <section aria-labelledby={`categories-${currency}`} key={currency}>
            <h3 className="section-kicker" id={`categories-${currency}`}>{currency}</h3>
            <div className="category-bars">
              {items.map((item) => {
                const width = maximum === 0n ? 0 : Number((moneyMagnitude(item.amount_minor) * 100n) / maximum);
                return (
                  <div className="category-bar" key={`${item.currency}:${item.category_id}`}>
                    <div className="category-bar__label">
                      <span><span aria-hidden="true">{item.emoji}</span> {item.name}</span>
                      <span>{formatMoney(item.amount_minor, item.currency, locale)}</span>
                    </div>
                    <div aria-hidden="true" className="category-bar__track">
                      <span className="category-bar__fill" style={{ width: `${String(width)}%` }} />
                    </div>
                  </div>
                );
              })}
            </div>
          </section>
        );
      })}
    </div>
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

  if (dashboard.isPending || today.isPending) {
    return <PageSkeleton rows={4} />;
  }
  if (dashboard.isError || today.isError) {
    return (
      <ErrorState
        onAction={() => {
          void dashboard.refetch();
          void today.refetch();
        }}
      />
    );
  }

  const current = dashboard.data.current_period;
  const comparable = dashboard.data.comparable_period;
  return (
    <div className="page-stack">
      <PageHeading
        description={formatExclusivePeriod(current.start, current.end, locale, timeZone)}
        eyebrow="Текущий месяц"
        title="Финансы без шума"
      />

      {activeDraft.data?.draft === null || activeDraft.data === undefined ? null : (
        <Link className="draft-banner" to="/draft">
          <span aria-hidden="true" className="draft-banner__icon">✦</span>
          <span className="min-w-0 flex-1">
            <strong>Есть незавершённый черновик</strong>
            <span>Продолжить с актуальной ревизии</span>
          </span>
          <span aria-hidden="true">›</span>
        </Link>
      )}

      <section aria-labelledby="today-title">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Живой срез</p>
            <h2 className="section-title" id="today-title">Сегодня</h2>
          </div>
          <span className="period-caption">
            {formatExclusivePeriod(today.data.period.start, today.data.period.end, locale, timeZone)}
          </span>
        </div>
        <CurrencyTotals compact totals={today.data.period.totals} />
      </section>

      <section aria-labelledby="month-title">
        <div className="section-heading">
          <div>
            <p className="eyebrow">По валютам отдельно</p>
            <h2 className="section-title" id="month-title">Итоги месяца</h2>
          </div>
          <span className="period-caption">
            Предыдущий: {formatExclusivePeriod(comparable.start, comparable.end, locale, timeZone)}
          </span>
        </div>
        <CurrencyTotals totals={current.totals} />
      </section>

      <div className="dashboard-columns">
        <section aria-labelledby="categories-title" className="surface-panel">
          <div className="section-heading section-heading--inside">
            <div>
              <p className="eyebrow">Структура</p>
              <h2 className="section-title" id="categories-title">Крупные категории</h2>
            </div>
          </div>
          <CategoryBars categories={dashboard.data.top_categories} />
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
            <EmptyState title="История чиста">Создайте первую операцию через проверяемый черновик.</EmptyState>
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
