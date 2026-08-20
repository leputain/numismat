import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { apiClient } from "../../app/providers";
import type {
  Budget,
  BudgetMutationResponse,
  CategoriesResponse,
  CreateBudgetRequest,
  ReplaceBudgetRequest,
} from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { ErrorState, PageSkeleton } from "../../shared/components/async-state";
import { MutationFeedback } from "../../shared/components/mutation-feedback";
import { PageHeading } from "../../shared/components/page-heading";
import { usePreparedMutation } from "../../shared/mutations/prepared-mutation";
import { restartBudgetPagination } from "../../shared/mutations/query-recovery";
import { queryKeys } from "../../shared/queries/query-keys";
import { majorToMinor, minorToMajor, validBudgetPeriod } from "./budget-money";
import { ownerMonthWindow } from "./budget-window";

interface BudgetFormProps {
  readonly initial: Budget | undefined;
}

function BudgetForm({ initial }: BudgetFormProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { baseCurrency, timeZone } = useSessionFormat();
  const defaultWindow = ownerMonthWindow(timeZone);
  const [name, setName] = useState(initial?.name ?? "");
  const [limit, setLimit] = useState(
    initial === undefined ? "" : minorToMajor(initial.limit_minor),
  );
  const [currency, setCurrency] = useState(initial?.currency ?? baseCurrency);
  const [categoryId, setCategoryId] = useState(initial?.category_id ?? "");
  const [startsOn, setStartsOn] = useState(initial?.starts_on ?? defaultWindow.startsOn);
  const [endsOn, setEndsOn] = useState(initial?.ends_on ?? defaultWindow.endsOn);
  const [formError, setFormError] = useState<string>();
  const categories = useQuery({
    queryKey: queryKeys.catalogs.categories("expense"),
    queryFn: ({ signal }) =>
      apiClient.get<CategoriesResponse>(
        "/api/v1/categories?kind=expense&archived=false",
        { signal },
      ),
    staleTime: 60_000,
  });
  const mutation = usePreparedMutation<BudgetMutationResponse, null>({
    eventScope: "budget",
    async onSuccess(response) {
      await restartBudgetPagination(queryClient);
      await navigate(`/budgets/${response.result.budget_id}`, { replace: true });
    },
    async onRejected() {
      if (initial !== undefined) {
        await queryClient.invalidateQueries({ queryKey: queryKeys.budgets.detail(initial.id) });
      }
    },
    onOutcomeUnknown() {},
  });

  const submit = () => {
    const limitMinor = majorToMinor(limit);
    const canonicalCurrency = currency.trim().toUpperCase();
    if (name.trim().length === 0 || name.trim().length > 60) {
      setFormError("Название должно содержать от 1 до 60 символов.");
      return;
    }
    if (limitMinor === undefined) {
      setFormError("Укажите положительный лимит не более двух знаков после запятой.");
      return;
    }
    if (!/^[A-Z]{3}$/u.test(canonicalCurrency)) {
      setFormError("Валюта должна состоять из трёх заглавных латинских букв.");
      return;
    }
    if (!validBudgetPeriod(startsOn, endsOn)) {
      setFormError("Период должен содержать от 1 до 366 дней.");
      return;
    }
    setFormError(undefined);
    const common: CreateBudgetRequest = {
      name: name.trim(),
      limit_minor: limitMinor,
      currency: canonicalCurrency,
      category_id: categoryId || null,
      starts_on: startsOn,
      ends_on: endsOn,
    };
    if (initial === undefined) {
      mutation.run({ path: "/api/v1/budgets", body: common, context: null });
      return;
    }
    const body: ReplaceBudgetRequest = { ...common, version: initial.version };
    mutation.run({
      path: `/api/v1/budgets/${initial.id}`,
      body,
      method: "PUT",
      context: null,
    });
  };

  const categoryMissing =
    initial?.category_id !== null &&
    initial?.category_id !== undefined &&
    categories.data?.items.every((category) => category.id !== initial.category_id);

  return (
    <form
      className="surface-panel field-stack"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <div className="field-stack">
        <label className="field-label" htmlFor="budget-name">Название</label>
        <input
          autoComplete="off"
          className="field-input"
          disabled={mutation.isPending}
          id="budget-name"
          maxLength={60}
          onChange={(event) => setName(event.currentTarget.value)}
          placeholder="Например, Продукты"
          required
          type="text"
          value={name}
        />
      </div>

      <div className="grid gap-4 sm:grid-cols-[1fr_8rem]">
        <div className="field-stack">
          <label className="field-label" htmlFor="budget-limit">Лимит</label>
          <input
            autoComplete="off"
            className="field-input"
            disabled={mutation.isPending}
            id="budget-limit"
            inputMode="decimal"
            onChange={(event) => setLimit(event.currentTarget.value)}
            placeholder="50 000,00"
            required
            type="text"
            value={limit}
          />
        </div>
        <div className="field-stack">
          <label className="field-label" htmlFor="budget-currency">Валюта</label>
          <input
            autoCapitalize="characters"
            autoComplete="off"
            className="field-input uppercase"
            disabled={mutation.isPending}
            id="budget-currency"
            maxLength={3}
            onChange={(event) => setCurrency(event.currentTarget.value.toUpperCase())}
            pattern="[A-Z]{3}"
            required
            spellCheck={false}
            type="text"
            value={currency}
          />
        </div>
      </div>

      <div className="field-stack">
        <label className="field-label" htmlFor="budget-category">Категория расходов</label>
        <select
          className="field-input"
          disabled={categories.isPending || mutation.isPending}
          id="budget-category"
          onChange={(event) => setCategoryId(event.currentTarget.value)}
          value={categoryId}
        >
          <option value="">Все расходы в валюте</option>
          {categoryMissing ? (
            <option value={initial.category_id ?? ""}>Недоступная категория — выберите другую</option>
          ) : null}
          {categories.data?.items.map((category) => (
            <option key={category.id} value={category.id}>
              {category.emoji} {category.name}
            </option>
          ))}
        </select>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="field-stack">
          <label className="field-label" htmlFor="budget-start">Начало</label>
          <input
            className="field-input"
            disabled={mutation.isPending}
            id="budget-start"
            onChange={(event) => setStartsOn(event.currentTarget.value)}
            required
            type="date"
            value={startsOn}
          />
        </div>
        <div className="field-stack">
          <label className="field-label" htmlFor="budget-end">Окончание включительно</label>
          <input
            className="field-input"
            disabled={mutation.isPending}
            id="budget-end"
            onChange={(event) => setEndsOn(event.currentTarget.value)}
            required
            type="date"
            value={endsOn}
          />
        </div>
      </div>

      {formError === undefined ? null : (
        <p aria-live="polite" className="text-sm text-[var(--nm-danger)]" role="alert">{formError}</p>
      )}
      <MutationFeedback
        error={mutation.error}
        onRetryUnknown={mutation.retryUnknown}
        outcomeUnknown={mutation.outcomeUnknown}
        pending={mutation.isPending}
      />
      <div className="flex flex-wrap gap-3 pt-2">
        <button className="button button--primary" disabled={mutation.isPending} type="submit">
          {mutation.isPending ? "Сохраняем…" : initial === undefined ? "Создать" : "Сохранить"}
        </button>
        <Link
          className="button button--secondary"
          to={initial === undefined ? "/budgets" : `/budgets/${initial.id}`}
        >
          Отмена
        </Link>
      </div>
      <p className="text-xs leading-relaxed text-[var(--nm-muted)]">
        Даты считаются в часовом поясе, выбранном при создании бюджета. Суммы в разных валютах не объединяются.
      </p>
    </form>
  );
}

export function BudgetEditorPage({ budgetId }: { readonly budgetId?: string }) {
  const budget = useQuery({
    queryKey: queryKeys.budgets.detail(budgetId ?? "new"),
    queryFn: ({ signal }) => {
      if (budgetId === undefined) {
        throw new Error("Budget id is unavailable");
      }
      return apiClient.get<Budget>(`/api/v1/budgets/${budgetId}`, { signal });
    },
    enabled: budgetId !== undefined,
    staleTime: 0,
  });

  if (budgetId !== undefined && budget.isPending) {
    return <PageSkeleton rows={5} />;
  }
  if (budgetId !== undefined && budget.isError) {
    return <ErrorState onAction={() => void budget.refetch()} />;
  }
  if (budget.data?.deleted_at !== null && budget.data !== undefined) {
    return (
      <ErrorState
        actionLabel="Открыть бюджет"
        description="Удалённый бюджет сначала нужно восстановить."
        onAction={() => window.history.back()}
        title="Редактирование недоступно"
      />
    );
  }

  return (
    <div className="budget-editor-page page-stack">
      <PageHeading
        description="Период задаётся локальными календарными датами, окончание включено."
        eyebrow={budgetId === undefined ? "Новый лимит" : "Изменение бюджета"}
        title={budgetId === undefined ? "Создать бюджет" : "Изменить бюджет"}
      />
      <BudgetForm initial={budget.data} key={budget.data?.id ?? "new"} />
    </div>
  );
}
