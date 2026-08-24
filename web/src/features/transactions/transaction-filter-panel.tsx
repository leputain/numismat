import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { apiClient } from "../../app/providers";
import type {
  Account,
  AccountsResponse,
  CategoriesResponse,
  Category,
} from "../../shared/api/types";
import { queryKeys } from "../../shared/queries/query-keys";
import {
  TRANSACTION_PERIOD_OPTIONS,
  activeTransactionFilterCount,
  updateCustomTransactionPeriod,
} from "./transaction-filter-model";
import type {
  TransactionFilterState,
  TransactionKindFilter,
  TransactionQueryFilters,
} from "./transaction-filter-model";

const CATALOG_CAP = 200;

interface TransactionFilterPanelProps {
  readonly state: TransactionFilterState;
  readonly filters: TransactionQueryFilters;
  readonly baseCurrency: string;
  onChange(value: TransactionFilterState): void;
  onReset(): void;
}

function archivedSuffix(archived: boolean): string {
  return archived ? " · архив" : "";
}

function orderedAccounts(active: readonly Account[], archived: readonly Account[]): Account[] {
  return [...active, ...archived];
}

function orderedCategories(
  active: readonly Category[],
  archived: readonly Category[],
): Category[] {
  return [...active, ...archived];
}

export function TransactionFilterPanel({
  state,
  filters,
  baseCurrency,
  onChange,
  onReset,
}: TransactionFilterPanelProps) {
  const activeAccounts = useQuery({
    queryKey: queryKeys.catalogs.accountsByArchived(false),
    queryFn: ({ signal }) =>
      apiClient.get<AccountsResponse>("/api/v1/accounts?archived=false", { signal }),
    staleTime: 60_000,
  });
  const archivedAccounts = useQuery({
    queryKey: queryKeys.catalogs.accountsByArchived(true),
    queryFn: ({ signal }) =>
      apiClient.get<AccountsResponse>("/api/v1/accounts?archived=true", { signal }),
    staleTime: 60_000,
  });
  const categoryKind = state.type ?? "expense";
  const activeCategories = useQuery({
    enabled: state.type !== null,
    queryKey: queryKeys.catalogs.categoriesByState(categoryKind, false),
    queryFn: ({ signal }) =>
      apiClient.get<CategoriesResponse>(
        `/api/v1/categories?kind=${categoryKind}&archived=false`,
        { signal },
      ),
    staleTime: 60_000,
  });
  const archivedCategories = useQuery({
    enabled: state.type !== null,
    queryKey: queryKeys.catalogs.categoriesByState(categoryKind, true),
    queryFn: ({ signal }) =>
      apiClient.get<CategoriesResponse>(
        `/api/v1/categories?kind=${categoryKind}&archived=true`,
        { signal },
      ),
    staleTime: 60_000,
  });

  const accountOverflow =
    (activeAccounts.data?.items.length ?? 0) > CATALOG_CAP ||
    (archivedAccounts.data?.items.length ?? 0) > CATALOG_CAP;
  const accountsUnavailable = activeAccounts.isError || archivedAccounts.isError || accountOverflow;
  const accountsPending = activeAccounts.isPending || archivedAccounts.isPending;
  const accounts =
    !accountsPending &&
    !accountsUnavailable &&
    activeAccounts.data !== undefined &&
    archivedAccounts.data !== undefined
      ? orderedAccounts(activeAccounts.data.items, archivedAccounts.data.items)
      : [];

  const categoryOverflow =
    (activeCategories.data?.items.length ?? 0) > CATALOG_CAP ||
    (archivedCategories.data?.items.length ?? 0) > CATALOG_CAP;
  const categoriesUnavailable =
    state.type !== null &&
    (activeCategories.isError || archivedCategories.isError || categoryOverflow);
  const categoriesPending =
    state.type !== null && (activeCategories.isPending || archivedCategories.isPending);
  const categories =
    state.type !== null &&
    !categoriesPending &&
    !categoriesUnavailable &&
    activeCategories.data !== undefined &&
    archivedCategories.data !== undefined
      ? orderedCategories(activeCategories.data.items, archivedCategories.data.items)
      : [];
  const currencies = useMemo(
    () =>
      [...new Set([baseCurrency, ...accounts.map((account) => account.currency)])]
        .filter((currency) => /^[A-Z]{3}$/u.test(currency))
        .sort((left, right) => left.localeCompare(right)),
    [accounts, baseCurrency],
  );
  const activeCount = activeTransactionFilterCount(filters);

  const changeType = (type: TransactionKindFilter | null) => {
    onChange({ ...state, type, categoryId: null });
  };

  return (
    <details className="transaction-filters surface-panel">
      <summary className="transaction-filters__summary">
        <span>
          <span aria-hidden="true" className="transaction-filters__icon">⌁</span>
          Фильтры
        </span>
        <span aria-live="polite" className="transaction-filters__status">
          {activeCount === 0 ? "Все операции" : `Выбрано: ${String(activeCount)}`}
        </span>
      </summary>

      <div className="transaction-filters__body" data-mobile-stack="true">
        <fieldset className="transaction-filter-period">
          <legend className="field-label">Период</legend>
          <div
            aria-label="Быстрый выбор периода"
            className="transaction-filter-presets"
            data-mobile-scroll="true"
            role="group"
          >
            {TRANSACTION_PERIOD_OPTIONS.map((option) => (
              <button
                aria-pressed={state.period === option.id}
                className="transaction-filter-preset"
                key={option.id}
                onClick={() => {
                  if (state.period !== option.id) {
                    onChange({ ...state, period: option.id });
                  }
                }}
                type="button"
              >
                {option.label}
              </button>
            ))}
          </div>
        </fieldset>

        {state.period === "custom" ? (
          <div className="transaction-filter-custom-period">
            <label className="field-stack" htmlFor="transaction-filter-start">
              <span className="field-label">С даты</span>
              <input
                className="field-input transaction-filter-control"
                id="transaction-filter-start"
                max="9998-12-31"
                min="0001-01-01"
                onChange={(event) => {
                  onChange(updateCustomTransactionPeriod(state, "start", event.currentTarget.value));
                }}
                type="date"
                value={state.customStart}
              />
            </label>
            <label className="field-stack" htmlFor="transaction-filter-end">
              <span className="field-label">По дату</span>
              <input
                className="field-input transaction-filter-control"
                id="transaction-filter-end"
                max="9998-12-31"
                min="0001-01-01"
                onChange={(event) => {
                  onChange(updateCustomTransactionPeriod(state, "end", event.currentTarget.value));
                }}
                type="date"
                value={state.customEnd}
              />
            </label>
          </div>
        ) : null}

        <div className="transaction-filter-grid" data-layout="responsive">
          <label className="field-stack" htmlFor="transaction-filter-type">
            <span className="field-label">Тип операции</span>
            <select
              className="field-input transaction-filter-control"
              id="transaction-filter-type"
              onChange={(event) => {
                const next = event.currentTarget.value;
                changeType(next === "" ? null : (next as TransactionKindFilter));
              }}
              value={state.type ?? ""}
            >
              <option value="">Расходы и доходы</option>
              <option value="expense">Расход</option>
              <option value="income">Доход</option>
            </select>
          </label>

          <label className="field-stack" htmlFor="transaction-filter-account">
            <span className="field-label">Счёт</span>
            <select
              className="field-input transaction-filter-control"
              disabled={accountsPending || accountsUnavailable}
              id="transaction-filter-account"
              onChange={(event) => {
                onChange({ ...state, accountId: event.currentTarget.value || null });
              }}
              value={state.accountId ?? ""}
            >
              <option value="">{accountsPending ? "Загружаем счета…" : "Все счета"}</option>
              {accounts.map((account) => (
                <option key={account.id} value={account.id}>
                  {account.name} · {account.currency}{archivedSuffix(account.archived)}
                </option>
              ))}
            </select>
          </label>

          <label className="field-stack" htmlFor="transaction-filter-category">
            <span className="field-label">Категория</span>
            <select
              className="field-input transaction-filter-control"
              disabled={state.type === null || categoriesPending || categoriesUnavailable}
              id="transaction-filter-category"
              onChange={(event) => {
                onChange({ ...state, categoryId: event.currentTarget.value || null });
              }}
              value={state.categoryId ?? ""}
            >
              <option value="">
                {state.type === null
                  ? "Сначала выберите тип"
                  : categoriesPending
                    ? "Загружаем категории…"
                    : "Все категории"}
              </option>
              {categories.map((category) => (
                <option key={category.id} value={category.id}>
                  {category.emoji} {category.name}{archivedSuffix(category.archived)}
                </option>
              ))}
            </select>
          </label>

          <label className="field-stack" htmlFor="transaction-filter-currency">
            <span className="field-label">Валюта</span>
            <select
              className="field-input transaction-filter-control"
              disabled={accountsPending || accountsUnavailable}
              id="transaction-filter-currency"
              onChange={(event) => {
                onChange({ ...state, currency: event.currentTarget.value || null });
              }}
              value={state.currency ?? ""}
            >
              <option value="">Все валюты</option>
              {currencies.map((currency) => (
                <option key={currency} value={currency}>{currency}</option>
              ))}
            </select>
          </label>
        </div>

        {accountsUnavailable || categoriesUnavailable ? (
          <div className="transaction-filter-catalog-error" role="alert">
            <span>Не удалось загрузить полный справочник. Частичный список не показан.</span>
            <button
              className="text-link"
              onClick={() => {
                if (accountsUnavailable) {
                  void activeAccounts.refetch();
                  void archivedAccounts.refetch();
                }
                if (categoriesUnavailable) {
                  void activeCategories.refetch();
                  void archivedCategories.refetch();
                }
              }}
              type="button"
            >
              Повторить
            </button>
          </div>
        ) : null}

        <div className="transaction-filters__footer">
          <p>Фильтры применяются сразу. Категории зависят от выбранного типа.</p>
          <button
            className="button button--ghost transaction-filters__reset"
            disabled={activeCount === 0}
            onClick={onReset}
            type="button"
          >
            Сбросить
          </button>
        </div>
      </div>
    </details>
  );
}
