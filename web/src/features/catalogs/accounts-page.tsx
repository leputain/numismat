import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import { apiClient } from "../../app/providers";
import type {
  Account,
  AccountMutationResponse,
  AccountsResponse,
  CreateAccountRequest,
  RenameCatalogRequest,
  VersionedCatalogRequest,
} from "../../shared/api/types";
import { EmptyState, ErrorState, PageSkeleton } from "../../shared/components/async-state";
import { MutationFeedback } from "../../shared/components/mutation-feedback";
import { PageHeading } from "../../shared/components/page-heading";
import { emitClientEvent } from "../../shared/logging/client-events";
import { usePreparedMutation } from "../../shared/mutations/prepared-mutation";
import { queryKeys } from "../../shared/queries/query-keys";

type AccountMode = "active" | "archive";
type AccountAction = "archive" | "create" | "default" | "rename" | "restore";

function AccountRow({
  account,
  defaultAccountId,
  disabled,
  editing,
  confirmingArchive,
  onAction,
  onCancelEdit,
  onConfirmArchive,
  onEdit,
  onRename,
}: {
  readonly account: Account;
  readonly defaultAccountId: string | null;
  readonly disabled: boolean;
  readonly editing: boolean;
  readonly confirmingArchive: boolean;
  onAction(action: Exclude<AccountAction, "create" | "rename">): void;
  onCancelEdit(): void;
  onConfirmArchive(): void;
  onEdit(): void;
  onRename(name: string): void;
}) {
  const [name, setName] = useState(account.name);
  const isDefault = defaultAccountId === account.id;

  useEffect(() => setName(account.name), [account.name, editing]);

  return (
    <article className="surface-panel space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-base font-semibold text-[var(--nm-text)]">{account.name}</h2>
            {isDefault ? <span className="revision-chip">Основной</span> : null}
          </div>
          <p className="mt-1 text-sm text-[var(--nm-muted)]">{account.currency} · {account.type}</p>
        </div>
        <span className="text-xs text-[var(--nm-muted)]">Версия {account.version}</span>
      </div>

      {editing ? (
        <form
          className="flex flex-col gap-2 sm:flex-row"
          onSubmit={(event) => {
            event.preventDefault();
            onRename(name.trim());
          }}
        >
          <label className="field-stack flex-1" htmlFor={`account-name-${account.id}`}>
            <span className="field-label">Название</span>
            <input
              autoFocus
              className="field-input"
              disabled={disabled}
              id={`account-name-${account.id}`}
              maxLength={60}
              minLength={1}
              onChange={(event) => setName(event.currentTarget.value)}
              required
              value={name}
            />
          </label>
          <div className="flex items-end gap-2">
            <button className="button button--primary" disabled={disabled || name.trim().length === 0} type="submit">Сохранить</button>
            <button className="button button--ghost" disabled={disabled} onClick={onCancelEdit} type="button">Отмена</button>
          </div>
        </form>
      ) : confirmingArchive ? (
        <div className="notice notice--warning">
          <div>
            <p className="notice__title">Убрать счёт в архив?</p>
            <p className="notice__text">Сохранённые операции останутся на месте.</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <button className="button button--danger" disabled={disabled} onClick={() => onAction("archive")} type="button">Архивировать</button>
            <button className="button button--ghost" disabled={disabled} onClick={onConfirmArchive} type="button">Не сейчас</button>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap gap-2">
          {account.archived ? (
            <button className="button button--primary" disabled={disabled} onClick={() => onAction("restore")} type="button">Восстановить</button>
          ) : (
            <>
              <button className="button button--secondary" disabled={disabled} onClick={onEdit} type="button">Переименовать</button>
              {!isDefault ? <button className="button button--ghost" disabled={disabled} onClick={() => onAction("default")} type="button">Сделать основным</button> : null}
              <button className="button button--danger-ghost" disabled={disabled || isDefault} onClick={onConfirmArchive} type="button">В архив</button>
            </>
          )}
        </div>
      )}
    </article>
  );
}

export function AccountsPage() {
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<AccountMode>("active");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [archiveId, setArchiveId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [currency, setCurrency] = useState("RUB");
  const archived = mode === "archive";
  const accounts = useQuery({
    queryKey: queryKeys.catalogs.accountsByArchived(archived),
    queryFn: ({ signal }) =>
      apiClient.get<AccountsResponse>(`/api/v1/accounts?archived=${String(archived)}`, { signal }),
    staleTime: 30_000,
  });
  const mutation = usePreparedMutation<AccountMutationResponse, AccountAction>({
    eventScope: "catalog",
    async onSuccess(_response, action) {
      setEditingId(null);
      setArchiveId(null);
      if (action === "create") setName("");
      await queryClient.invalidateQueries({ queryKey: queryKeys.catalogs.all });
    },
    async onRejected() {
      await queryClient.invalidateQueries({ queryKey: queryKeys.catalogs.all });
    },
    async onOutcomeUnknown() {
      await queryClient.invalidateQueries({ queryKey: queryKeys.catalogs.all });
    },
  });

  useEffect(() => emitClientEvent("catalog_opened"), []);

  const disabled = mutation.isPending || mutation.outcomeUnknown;
  const runVersioned = (account: Account, action: "archive" | "default" | "restore") => {
    const body: VersionedCatalogRequest = { version: account.version };
    mutation.run({ path: `/api/v1/accounts/${account.id}/${action}`, body, context: action });
  };
  const submitCreate = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const body: CreateAccountRequest = { name: name.trim(), currency };
    mutation.run({ path: "/api/v1/accounts", body, context: "create" });
  };

  return (
    <div className="page-stack page-stack--narrow">
      <PageHeading
        description="Каждый счёт хранит собственную валюту. Основной счёт подставляется первым, но всегда остаётся проверяемым."
        eyebrow="Справочник"
        title="Счета"
      />

      <div aria-label="Состояние счетов" className="segment-control" role="tablist">
        <button aria-selected={mode === "active"} className="segment-control__button" onClick={() => setMode("active")} role="tab" type="button">Активные</button>
        <button aria-selected={mode === "archive"} className="segment-control__button" onClick={() => setMode("archive")} role="tab" type="button">Архив</button>
      </div>

      {mode === "active" ? (
        <form className="surface-panel space-y-4" onSubmit={submitCreate}>
          <div>
            <p className="eyebrow">Новый счёт</p>
            <h2 className="section-title">Добавить без перехода в мастер</h2>
          </div>
          <div className="grid gap-3 sm:grid-cols-[1fr_8rem]">
            <label className="field-stack" htmlFor="new-account-name"><span className="field-label">Название</span><input className="field-input" disabled={disabled} id="new-account-name" maxLength={60} minLength={1} onChange={(event) => setName(event.currentTarget.value)} placeholder="Например, Основной" required value={name} /></label>
            <label className="field-stack" htmlFor="new-account-currency"><span className="field-label">Валюта</span><input autoCapitalize="characters" className="field-input" disabled={disabled} id="new-account-currency" maxLength={3} minLength={3} onChange={(event) => setCurrency(event.currentTarget.value.toUpperCase().replace(/[^A-Z]/g, "").slice(0, 3))} pattern="[A-Z]{3}" required value={currency} /></label>
          </div>
          <button className="button button--primary" disabled={disabled || name.trim().length === 0 || currency.length !== 3} type="submit">Добавить счёт</button>
        </form>
      ) : null}

      <MutationFeedback error={mutation.error} onRetryUnknown={mutation.retryUnknown} outcomeUnknown={mutation.outcomeUnknown} pending={mutation.isPending} />

      {accounts.isPending ? <PageSkeleton rows={4} /> : accounts.isError ? <ErrorState onAction={() => void accounts.refetch()} /> : accounts.data.items.length === 0 ? <EmptyState title={archived ? "Архив пуст" : "Счетов нет"}>Создайте первый счёт, чтобы добавлять операции.</EmptyState> : (
        <section aria-live="polite" className="space-y-3">
          {accounts.data.items.map((account) => (
            <AccountRow
              account={account}
              confirmingArchive={archiveId === account.id}
              defaultAccountId={accounts.data.default_account_id}
              disabled={disabled}
              editing={editingId === account.id}
              key={account.id}
              onAction={(action) => runVersioned(account, action)}
              onCancelEdit={() => setEditingId(null)}
              onConfirmArchive={() => setArchiveId((current) => current === account.id ? null : account.id)}
              onEdit={() => { setArchiveId(null); setEditingId(account.id); }}
              onRename={(nextName) => {
                const body: RenameCatalogRequest = { name: nextName, version: account.version };
                mutation.run({ path: `/api/v1/accounts/${account.id}`, body, method: "PATCH", context: "rename" });
              }}
            />
          ))}
        </section>
      )}
    </div>
  );
}
