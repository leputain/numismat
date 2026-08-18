import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import type { ReactNode } from "react";

import { apiClient } from "../../app/providers";
import type { Account, AccountsResponse, CategoriesResponse, Category } from "../../shared/api/types";
import { ErrorState } from "../../shared/components/async-state";
import { queryKeys } from "../../shared/queries/query-keys";

const CATALOG_CAP = 200;

interface SelectorShellProps<TItem> {
  readonly label: string;
  readonly items: TItem[];
  readonly disabled: boolean;
  readonly allowCustom: boolean;
  readonly emptyText: string;
  itemKey(item: TItem): string;
  itemText(item: TItem): string;
  renderItem(item: TItem): ReactNode;
  onSelect(item: TItem): void;
  onCustom(): void;
}

function SelectorShell<TItem>({
  label,
  items,
  disabled,
  allowCustom,
  emptyText,
  itemKey,
  itemText,
  renderItem,
  onSelect,
  onCustom,
}: SelectorShellProps<TItem>) {
  const [search, setSearch] = useState("");
  const visibleItems = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase();
    if (needle.length === 0) {
      return items;
    }
    return items.filter((item) => itemText(item).toLocaleLowerCase().includes(needle));
  }, [itemText, items, search]);

  return (
    <div className="catalog-selector">
      <label className="field-label" htmlFor={`catalog-search-${label}`}>{label}</label>
      <input
        autoComplete="off"
        className="field-input"
        disabled={disabled}
        id={`catalog-search-${label}`}
        onChange={(event) => setSearch(event.currentTarget.value)}
        placeholder="Найти в справочнике"
        spellCheck={false}
        type="search"
        value={search}
      />
      <div aria-label={label} className="catalog-options" role="listbox">
        {visibleItems.length === 0 ? <p className="catalog-options__empty">{emptyText}</p> : null}
        {visibleItems.map((item) => (
          <button
            aria-selected={false}
            className="catalog-option"
            disabled={disabled}
            key={itemKey(item)}
            onClick={() => onSelect(item)}
            role="option"
            type="button"
          >
            {renderItem(item)}
          </button>
        ))}
      </div>
      {allowCustom ? (
        <button className="button button--ghost w-full" disabled={disabled} onClick={onCustom} type="button">
          + Создать новое значение
        </button>
      ) : null}
    </div>
  );
}

interface CategorySelectorProps {
  readonly kind: "expense" | "income";
  readonly disabled?: boolean;
  readonly allowCustom?: boolean;
  onSelect(category: Category): void;
  onCustom(): void;
}

export function CategorySelector({
  kind,
  disabled = false,
  allowCustom = true,
  onSelect,
  onCustom,
}: CategorySelectorProps) {
  const categories = useQuery({
    queryKey: queryKeys.catalogs.categories(kind),
    queryFn: ({ signal }) =>
      apiClient.get<CategoriesResponse>(
        `/api/v1/categories?kind=${kind}&archived=false`,
        { signal },
      ),
    staleTime: 60_000,
  });
  if (categories.isPending) {
    return <div aria-label="Загрузка категорий" className="skeleton h-48 w-full" role="status" />;
  }
  if (categories.isError || categories.data.items.length > CATALOG_CAP) {
    return (
      <ErrorState
        description="Категории не показываются частично: обновите полный bounded-справочник перед выбором."
        onAction={() => void categories.refetch()}
        title="Категории недоступны"
      />
    );
  }
  return (
    <SelectorShell
      allowCustom={allowCustom}
      disabled={disabled}
      emptyText="Совпадений не найдено"
      itemKey={(item) => item.id}
      itemText={(item) => item.name}
      items={categories.data.items}
      label={kind === "income" ? "Категория дохода" : "Категория расхода"}
      onCustom={onCustom}
      onSelect={onSelect}
      renderItem={(item) => (
        <><span aria-hidden="true" className="catalog-option__emoji">{item.emoji}</span><span>{item.name}</span></>
      )}
    />
  );
}

interface AccountSelectorProps {
  readonly disabled?: boolean;
  readonly allowCustom?: boolean;
  onSelect(account: Account): void;
  onCustom(): void;
}

export function AccountSelector({
  disabled = false,
  allowCustom = true,
  onSelect,
  onCustom,
}: AccountSelectorProps) {
  const accounts = useQuery({
    queryKey: queryKeys.catalogs.accounts,
    queryFn: ({ signal }) =>
      apiClient.get<AccountsResponse>("/api/v1/accounts?archived=false", { signal }),
    staleTime: 60_000,
  });
  if (accounts.isPending) {
    return <div aria-label="Загрузка счетов" className="skeleton h-48 w-full" role="status" />;
  }
  if (accounts.isError || accounts.data.items.length > CATALOG_CAP) {
    return (
      <ErrorState
        description="Счета не показываются частично: обновите полный bounded-справочник перед выбором."
        onAction={() => void accounts.refetch()}
        title="Счета недоступны"
      />
    );
  }
  return (
    <SelectorShell
      allowCustom={allowCustom}
      disabled={disabled}
      emptyText="Совпадений не найдено"
      itemKey={(item) => item.id}
      itemText={(item) => `${item.name} ${item.currency}`}
      items={accounts.data.items}
      label="Счёт"
      onCustom={onCustom}
      onSelect={onSelect}
      renderItem={(item) => (
        <><span className="catalog-option__name">{item.name}</span><span className="catalog-option__meta">{item.currency}</span></>
      )}
    />
  );
}
