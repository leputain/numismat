import { lazy, Suspense } from "react";
import type { ComponentType } from "react";
import { Outlet, Route, Routes, useParams } from "react-router";

import { PageSkeleton } from "../shared/components/async-state";
import { RouteErrorState } from "./states/route-error-state";
import { AppShell } from "./shell/app-shell";

function lazyNamed<TProps extends object>(
  loader: () => Promise<ComponentType<TProps>>,
) {
  return lazy(async () => ({ default: await loader() }));
}

const DashboardPage = lazyNamed(async () =>
  (await import("../features/dashboard/dashboard-page")).DashboardPage
);
const AnalyticsPage = lazyNamed(async () =>
  (await import("../features/analytics/analytics-page")).AnalyticsPage
);
const DraftPage = lazyNamed(async () =>
  (await import("../features/drafts/draft-page")).DraftPage
);
const ComposeDraftPage = lazyNamed(async () =>
  (await import("../features/drafts/compose-draft-page")).ComposeDraftPage
);
const BankImportDetailPage = lazyNamed(async () =>
  (await import("../features/bank-imports/bank-import-detail-page")).BankImportDetailPage
);
const BankImportsPage = lazyNamed(async () =>
  (await import("../features/bank-imports/bank-imports-page")).BankImportsPage
);
const ExchangeRatePublishPage = lazyNamed(async () =>
  (await import("../features/exchange-rates/exchange-rate-publish-page"))
    .ExchangeRatePublishPage
);
const ExchangeRateSourcePage = lazyNamed(async () =>
  (await import("../features/exchange-rates/exchange-rate-source-page")).ExchangeRateSourcePage
);
const ExchangeRateVersionPage = lazyNamed(async () =>
  (await import("../features/exchange-rates/exchange-rate-version-page"))
    .ExchangeRateVersionPage
);
const ExchangeRatesPage = lazyNamed(async () =>
  (await import("../features/exchange-rates/exchange-rates-page")).ExchangeRatesPage
);
const BudgetDetailPage = lazyNamed(async () =>
  (await import("../features/budgets/budget-detail-page")).BudgetDetailPage
);
const BudgetEditorPage = lazyNamed(async () =>
  (await import("../features/budgets/budget-editor-page")).BudgetEditorPage
);
const BudgetsPage = lazyNamed(async () =>
  (await import("../features/budgets/budgets-page")).BudgetsPage
);
const RecurringDetailPage = lazyNamed(async () =>
  (await import("../features/recurring/recurring-detail-page")).RecurringDetailPage
);
const RecurringEditorPage = lazyNamed(async () =>
  (await import("../features/recurring/recurring-editor-page")).RecurringEditorPage
);
const RecurringPage = lazyNamed(async () =>
  (await import("../features/recurring/recurring-page")).RecurringPage
);
const TransactionDetailPage = lazyNamed(async () =>
  (await import("../features/transactions/transaction-detail-page")).TransactionDetailPage
);
const TransactionsPage = lazyNamed(async () =>
  (await import("../features/transactions/transactions-page")).TransactionsPage
);
const MorePage = lazyNamed(async () =>
  (await import("../features/more/more-page")).MorePage
);
const AccountsPage = lazyNamed(async () =>
  (await import("../features/catalogs/accounts-page")).AccountsPage
);
const CategoriesPage = lazyNamed(async () =>
  (await import("../features/catalogs/categories-page")).CategoriesPage
);
const SettingsPage = lazyNamed(async () =>
  (await import("../features/settings/settings-page")).SettingsPage
);

const CANONICAL_LOWERCASE_UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export function isCanonicalTransactionId(value: string | undefined): value is string {
  return value !== undefined && CANONICAL_LOWERCASE_UUID.test(value);
}

function TransactionDetailRoute() {
  const { transactionId } = useParams<{ transactionId: string }>();
  if (!isCanonicalTransactionId(transactionId)) {
    return (
      <RouteErrorState
        description="Идентификатор операции имеет недопустимый формат."
        title="Операция не найдена"
      />
    );
  }
  return <TransactionDetailPage transactionId={transactionId} />;
}

function BankImportDetailRoute() {
  const { batchId } = useParams<{ batchId: string }>();
  if (!isCanonicalTransactionId(batchId)) {
    return (
      <RouteErrorState
        description="Идентификатор пакета импорта имеет недопустимый формат."
        title="Импорт не найден"
      />
    );
  }
  return <BankImportDetailPage batchId={batchId} />;
}

function BudgetDetailRoute() {
  const { budgetId } = useParams<{ budgetId: string }>();
  if (!isCanonicalTransactionId(budgetId)) {
    return (
      <RouteErrorState
        description="Идентификатор бюджета имеет недопустимый формат."
        title="Бюджет не найден"
      />
    );
  }
  return <BudgetDetailPage budgetId={budgetId} />;
}

function BudgetEditRoute() {
  const { budgetId } = useParams<{ budgetId: string }>();
  if (!isCanonicalTransactionId(budgetId)) {
    return (
      <RouteErrorState
        description="Идентификатор бюджета имеет недопустимый формат."
        title="Бюджет не найден"
      />
    );
  }
  return <BudgetEditorPage budgetId={budgetId} />;
}

function RecurringDetailRoute() {
  const { scheduleId } = useParams<{ scheduleId: string }>();
  if (!isCanonicalTransactionId(scheduleId)) {
    return <RouteErrorState description="Идентификатор расписания имеет недопустимый формат." title="Расписание не найдено" />;
  }
  return <RecurringDetailPage scheduleId={scheduleId} />;
}

function RecurringEditRoute() {
  const { scheduleId } = useParams<{ scheduleId: string }>();
  if (!isCanonicalTransactionId(scheduleId)) {
    return <RouteErrorState description="Идентификатор расписания имеет недопустимый формат." title="Расписание не найдено" />;
  }
  return <RecurringEditorPage scheduleId={scheduleId} />;
}

function ExchangeRateSourceRoute() {
  const { sourceId } = useParams<{ sourceId: string }>();
  if (!isCanonicalTransactionId(sourceId)) {
    return <RouteErrorState description="Идентификатор источника имеет недопустимый формат." title="Источник не найден" />;
  }
  return <ExchangeRateSourcePage sourceId={sourceId} />;
}

function ExchangeRatePublishRoute() {
  const { sourceId } = useParams<{ sourceId: string }>();
  if (!isCanonicalTransactionId(sourceId)) {
    return <RouteErrorState description="Идентификатор источника имеет недопустимый формат." title="Источник не найден" />;
  }
  return <ExchangeRatePublishPage sourceId={sourceId} />;
}

function ExchangeRateVersionRoute() {
  const { versionId } = useParams<{ versionId: string }>();
  if (!isCanonicalTransactionId(versionId)) {
    return <RouteErrorState description="Идентификатор версии имеет недопустимый формат." title="Версия не найдена" />;
  }
  return <ExchangeRateVersionPage versionId={versionId} />;
}

export function RouteLoadingBoundary() {
  return (
    <Suspense fallback={<PageSkeleton rows={4} />}>
      <Outlet />
    </Suspense>
  );
}

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route element={<RouteLoadingBoundary />}>
          <Route element={<DashboardPage />} index />
          <Route element={<AnalyticsPage />} path="analytics" />
          <Route element={<TransactionsPage />} path="transactions" />
          <Route element={<TransactionDetailRoute />} path="transactions/:transactionId" />
          <Route element={<BankImportsPage />} path="imports" />
          <Route element={<BankImportDetailRoute />} path="imports/:batchId" />
          <Route element={<BudgetsPage />} path="budgets" />
          <Route element={<BudgetEditorPage />} path="budgets/new" />
          <Route element={<BudgetEditRoute />} path="budgets/:budgetId/edit" />
          <Route element={<BudgetDetailRoute />} path="budgets/:budgetId" />
          <Route element={<RecurringPage />} path="recurring" />
          <Route element={<RecurringEditorPage />} path="recurring/new" />
          <Route element={<RecurringEditRoute />} path="recurring/:scheduleId/edit" />
          <Route element={<RecurringDetailRoute />} path="recurring/:scheduleId" />
          <Route element={<ExchangeRatesPage />} path="rates" />
          <Route element={<ExchangeRatePublishPage />} path="rates/new" />
          <Route element={<ExchangeRatePublishRoute />} path="rates/:sourceId/publish" />
          <Route element={<ExchangeRateSourceRoute />} path="rates/:sourceId" />
          <Route element={<ExchangeRateVersionRoute />} path="rates/versions/:versionId" />
          <Route element={<DraftPage />} path="draft" />
          <Route element={<ComposeDraftPage />} path="draft/compose" />
          <Route element={<MorePage />} path="more" />
          <Route element={<AccountsPage />} path="accounts" />
          <Route element={<CategoriesPage />} path="categories" />
          <Route element={<SettingsPage />} path="settings" />
          <Route element={<RouteErrorState />} path="*" />
        </Route>
      </Route>
    </Routes>
  );
}
