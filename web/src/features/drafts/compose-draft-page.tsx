import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router";

import { apiClient } from "../../app/providers";
import type {
  AccountsResponse,
  CategoriesResponse,
  ComposeDraftRequest,
  MutationResponse,
} from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { ErrorState, PageSkeleton } from "../../shared/components/async-state";
import { MutationFeedback } from "../../shared/components/mutation-feedback";
import { PageHeading } from "../../shared/components/page-heading";
import { isOptimisticConflict } from "../../shared/errors/user-message";
import { emitClientEvent } from "../../shared/logging/client-events";
import { usePreparedMutation } from "../../shared/mutations/prepared-mutation";
import { refreshDraftQueries } from "../../shared/mutations/query-recovery";
import { queryKeys } from "../../shared/queries/query-keys";

type TransactionType = "expense" | "income";

const AMOUNT_PATTERN = /^(?:0|[1-9][0-9]{0,16})(?:[.,][0-9]{1,2})?$/u;

export function ownerToday(timeZone: string, now = new Date()): string {
  const values: Partial<Record<"day" | "month" | "year", string>> = {};
  for (const part of new Intl.DateTimeFormat("en-US", {
    day: "2-digit",
    month: "2-digit",
    timeZone,
    year: "numeric",
  }).formatToParts(now)) {
    if (part.type === "day" || part.type === "month" || part.type === "year") {
      values[part.type] = part.value;
    }
  }
  if (values.year === undefined || values.month === undefined || values.day === undefined) {
    throw new Error("Owner-local date formatter returned incomplete parts");
  }
  return `${values.year}-${values.month}-${values.day}`;
}

export function isComposeAmount(value: string): boolean {
  if (!AMOUNT_PATTERN.test(value)) {
    return false;
  }
  const normalized = value.replace(",", ".");
  return /[1-9]/u.test(normalized);
}

export function ComposeDraftPage() {
  const { timeZone } = useSessionFormat();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [type, setType] = useState<TransactionType>("expense");
  const [amount, setAmount] = useState("");
  const [accountId, setAccountId] = useState("");
  const [categoryId, setCategoryId] = useState("");
  const [occurredOn, setOccurredOn] = useState(() => ownerToday(timeZone));
  const [description, setDescription] = useState("");
  const accounts = useQuery({
    queryKey: queryKeys.catalogs.accounts,
    queryFn: ({ signal }) =>
      apiClient.get<AccountsResponse>("/api/v1/accounts?archived=false", { signal }),
    staleTime: 30_000,
  });
  const categories = useQuery({
    queryKey: queryKeys.catalogs.categories(type),
    queryFn: ({ signal }) =>
      apiClient.get<CategoriesResponse>(
        `/api/v1/categories?kind=${type}&archived=false`,
        { signal },
      ),
    staleTime: 30_000,
  });
  const mutation = usePreparedMutation<MutationResponse, "compose">({
    eventScope: "draft",
    onSuccess: async () => {
      await refreshDraftQueries(queryClient);
      navigate("/draft");
    },
    onRejected: async (error) => {
      if (isOptimisticConflict(error)) {
        emitClientEvent("mutation_conflict_detected");
      }
      await refreshDraftQueries(queryClient);
    },
    onOutcomeUnknown: async () => {
      await refreshDraftQueries(queryClient);
    },
  });

  useEffect(() => {
    setCategoryId("");
  }, [type]);

  const selectedAccount = useMemo(
    () => accounts.data?.items.find((item) => item.id === accountId),
    [accountId, accounts.data],
  );
  const selectedCategory = useMemo(
    () => categories.data?.items.find((item) => item.id === categoryId),
    [categories.data, categoryId],
  );
  const ready =
    isComposeAmount(amount) &&
    selectedAccount !== undefined &&
    selectedCategory !== undefined &&
    /^\d{4}-\d{2}-\d{2}$/u.test(occurredOn) &&
    description.length <= 500;
  const disabled = mutation.isPending || mutation.outcomeUnknown;

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!ready || selectedAccount === undefined || selectedCategory === undefined) {
      return;
    }
    const body: ComposeDraftRequest = {
      type,
      amount: amount.replace(",", "."),
      account: {
        kind: "existing",
        id: selectedAccount.id,
        version: selectedAccount.version,
      },
      category: {
        kind: "existing",
        id: selectedCategory.id,
        version: selectedCategory.version,
      },
      occurred_on: occurredOn,
      description: description.trim(),
    };
    mutation.run({
      path: "/api/v1/drafts/compose",
      body,
      context: "compose",
    });
  };

  if (accounts.isPending || categories.isPending) {
    return <PageSkeleton rows={5} />;
  }
  if (accounts.isError || categories.isError) {
    return (
      <ErrorState
        description="Счета и категории нужны для обязательной проверки операции."
        onAction={() => {
          void accounts.refetch();
          void categories.refetch();
        }}
      />
    );
  }

  return (
    <div className="page-stack page-stack--narrow">
      <PageHeading
        description="Все поля на одном экране. После продолжения откроется отдельная проверка — запись ещё не будет сохранена."
        eyebrow="Ручной ввод"
        title="Собрать операцию"
      />
      <form className="surface-panel space-y-5" onSubmit={submit}>
        <fieldset className="field-stack" disabled={disabled}>
          <legend className="field-label">Тип операции</legend>
          <div className="segment-control">
            <button
              aria-pressed={type === "expense"}
              className="segment-control__button"
              onClick={() => setType("expense")}
              type="button"
            >
              Расход
            </button>
            <button
              aria-pressed={type === "income"}
              className="segment-control__button"
              onClick={() => setType("income")}
              type="button"
            >
              Доход
            </button>
          </div>
        </fieldset>

        <label className="field-stack" htmlFor="compose-amount">
          <span className="field-label">Сумма</span>
          <input
            autoComplete="off"
            className="field-input"
            disabled={disabled}
            enterKeyHint="next"
            id="compose-amount"
            inputMode="decimal"
            maxLength={20}
            onChange={(event) => setAmount(event.currentTarget.value.trim())}
            placeholder="500,50"
            required
            value={amount}
          />
          {amount.length > 0 && !isComposeAmount(amount) ? (
            <span className="field-error">Положительная сумма без знака и разделителей тысяч.</span>
          ) : null}
        </label>

        <label className="field-stack" htmlFor="compose-category">
          <span className="field-label">Категория</span>
          <select
            className="field-input"
            disabled={disabled || categories.data.items.length === 0}
            id="compose-category"
            onChange={(event) => setCategoryId(event.currentTarget.value)}
            required
            value={categoryId}
          >
            <option value="">Выберите категорию</option>
            {categories.data.items.map((category) => (
              <option key={category.id} value={category.id}>
                {category.emoji} {category.name}
              </option>
            ))}
          </select>
        </label>

        <label className="field-stack" htmlFor="compose-account">
          <span className="field-label">Счёт</span>
          <select
            className="field-input"
            disabled={disabled || accounts.data.items.length === 0}
            id="compose-account"
            onChange={(event) => setAccountId(event.currentTarget.value)}
            required
            value={accountId}
          >
            <option value="">Выберите счёт</option>
            {accounts.data.items.map((account) => (
              <option key={account.id} value={account.id}>
                {account.name} · {account.currency}
              </option>
            ))}
          </select>
        </label>

        <label className="field-stack" htmlFor="compose-date">
          <span className="field-label">Дата</span>
          <input
            className="field-input"
            disabled={disabled}
            id="compose-date"
            max="9998-12-31"
            min="0001-01-01"
            onChange={(event) => setOccurredOn(event.currentTarget.value)}
            required
            type="date"
            value={occurredOn}
          />
        </label>

        <label className="field-stack" htmlFor="compose-description">
          <span className="field-label">Комментарий <span className="text-muted">необязательно</span></span>
          <textarea
            className="field-input min-h-24"
            disabled={disabled}
            id="compose-description"
            maxLength={500}
            onChange={(event) => setDescription(event.currentTarget.value)}
            placeholder="Например, продукты на неделю"
            value={description}
          />
        </label>

        <div className="dialog__actions">
          <button className="button button--primary" disabled={disabled || !ready} type="submit">
            {mutation.isPending ? "Готовим проверку…" : "Перейти к проверке"}
          </button>
          <Link aria-disabled={disabled} className="button button--ghost" to="/draft">Назад</Link>
        </div>
      </form>
      <MutationFeedback
        error={mutation.error}
        onRetryUnknown={mutation.retryUnknown}
        outcomeUnknown={mutation.outcomeUnknown}
        pending={mutation.isPending}
      />
    </div>
  );
}
