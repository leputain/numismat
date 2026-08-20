import { Route, Routes, useParams } from "react-router";

import { DashboardPage } from "../features/dashboard/dashboard-page";
import { AnalyticsPage } from "../features/analytics/analytics-page";
import { DraftPage } from "../features/drafts/draft-page";
import { BankImportDetailPage } from "../features/bank-imports/bank-import-detail-page";
import { BankImportsPage } from "../features/bank-imports/bank-imports-page";
import { ExchangeRatePublishPage } from "../features/exchange-rates/exchange-rate-publish-page";
import { ExchangeRateSourcePage } from "../features/exchange-rates/exchange-rate-source-page";
import { ExchangeRateVersionPage } from "../features/exchange-rates/exchange-rate-version-page";
import { ExchangeRatesPage } from "../features/exchange-rates/exchange-rates-page";
import { BudgetDetailPage } from "../features/budgets/budget-detail-page";
import { BudgetEditorPage } from "../features/budgets/budget-editor-page";
import { BudgetsPage } from "../features/budgets/budgets-page";
import { RecurringDetailPage } from "../features/recurring/recurring-detail-page";
import { RecurringEditorPage } from "../features/recurring/recurring-editor-page";
import { RecurringPage } from "../features/recurring/recurring-page";
import { TransactionDetailPage } from "../features/transactions/transaction-detail-page";
import { TransactionsPage } from "../features/transactions/transactions-page";
import { MorePage } from "../features/more/more-page";
import { RouteErrorState } from "./states/route-error-state";
import { AppShell } from "./shell/app-shell";

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

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<AppShell />}>
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
        <Route element={<MorePage />} path="more" />
        <Route element={<RouteErrorState />} path="*" />
      </Route>
    </Routes>
  );
}
