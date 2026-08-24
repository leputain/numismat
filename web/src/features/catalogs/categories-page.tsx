import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import { apiClient } from "../../app/providers";
import type {
  CategoriesResponse,
  Category,
  CategoryMutationResponse,
  CreateCategoryRequest,
  RenameCatalogRequest,
  VersionedCatalogRequest,
} from "../../shared/api/types";
import { EmptyState, ErrorState, PageSkeleton } from "../../shared/components/async-state";
import { MutationFeedback } from "../../shared/components/mutation-feedback";
import { PageHeading } from "../../shared/components/page-heading";
import { emitClientEvent } from "../../shared/logging/client-events";
import { usePreparedMutation } from "../../shared/mutations/prepared-mutation";
import { queryKeys } from "../../shared/queries/query-keys";

type CategoryKind = "expense" | "income";
type CategoryMode = "active" | "archive";
type CategoryAction = "archive" | "create" | "rename" | "restore";

export function CategoriesPage() {
  const queryClient = useQueryClient();
  const [kind, setKind] = useState<CategoryKind>("expense");
  const [mode, setMode] = useState<CategoryMode>("active");
  const [name, setName] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState("");
  const [archiveId, setArchiveId] = useState<string | null>(null);
  const archived = mode === "archive";
  const categories = useQuery({
    queryKey: queryKeys.catalogs.categoriesByState(kind, archived),
    queryFn: ({ signal }) =>
      apiClient.get<CategoriesResponse>(
        `/api/v1/categories?kind=${kind}&archived=${String(archived)}`,
        { signal },
      ),
    staleTime: 30_000,
  });
  const mutation = usePreparedMutation<CategoryMutationResponse, CategoryAction>({
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
  const submitCreate = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const body: CreateCategoryRequest = { name: name.trim(), kind };
    mutation.run({ path: "/api/v1/categories", body, context: "create" });
  };
  const runVersioned = (category: Category, action: "archive" | "restore") => {
    const body: VersionedCatalogRequest = { version: category.version };
    mutation.run({ path: `/api/v1/categories/${category.id}/${action}`, body, context: action });
  };

  return (
    <div className="page-stack page-stack--narrow">
      <PageHeading
        description="Доходы и расходы разделены. Архив скрывает категорию из нового ввода, но не меняет историю."
        eyebrow="Справочник"
        title="Категории"
      />

      <div aria-label="Тип категорий" className="segment-control" role="tablist">
        <button aria-selected={kind === "expense"} className="segment-control__button" onClick={() => setKind("expense")} role="tab" type="button">Расходы</button>
        <button aria-selected={kind === "income"} className="segment-control__button" onClick={() => setKind("income")} role="tab" type="button">Доходы</button>
      </div>
      <div aria-label="Состояние категорий" className="segment-control" role="tablist">
        <button aria-selected={mode === "active"} className="segment-control__button" onClick={() => setMode("active")} role="tab" type="button">Активные</button>
        <button aria-selected={mode === "archive"} className="segment-control__button" onClick={() => setMode("archive")} role="tab" type="button">Архив</button>
      </div>

      {mode === "active" ? (
        <form className="surface-panel space-y-4" onSubmit={submitCreate}>
          <div><p className="eyebrow">Новая категория</p><h2 className="section-title">{kind === "expense" ? "Для расходов" : "Для доходов"}</h2></div>
          <label className="field-stack" htmlFor="new-category-name"><span className="field-label">Название</span><input className="field-input" disabled={disabled} id="new-category-name" maxLength={60} minLength={1} onChange={(event) => setName(event.currentTarget.value)} placeholder={kind === "expense" ? "Например, Обучение" : "Например, Подработка"} required value={name} /></label>
          <button className="button button--primary" disabled={disabled || name.trim().length === 0} type="submit">Добавить категорию</button>
        </form>
      ) : null}

      <MutationFeedback error={mutation.error} onRetryUnknown={mutation.retryUnknown} outcomeUnknown={mutation.outcomeUnknown} pending={mutation.isPending} />

      {categories.isPending ? <PageSkeleton rows={5} /> : categories.isError ? <ErrorState onAction={() => void categories.refetch()} /> : categories.data.items.length === 0 ? <EmptyState title={archived ? "Архив пуст" : "Категорий нет"}>Добавьте понятную категорию для быстрого выбора.</EmptyState> : (
        <section aria-live="polite" className="space-y-3">
          {categories.data.items.map((category) => (
            <article className="surface-panel space-y-4" key={category.id}>
              <div className="flex items-start justify-between gap-3">
                <div className="flex min-w-0 items-center gap-3"><span aria-hidden="true" className="text-2xl">{category.emoji || "▫️"}</span><div><h2 className="text-base font-semibold text-[var(--nm-text)]">{category.name}</h2><p className="mt-1 text-xs text-[var(--nm-muted)]">Версия {category.version}</p></div></div>
              </div>
              {editingId === category.id ? (
                <form className="flex flex-col gap-2 sm:flex-row" onSubmit={(event) => { event.preventDefault(); const body: RenameCatalogRequest = { name: editingName.trim(), version: category.version }; mutation.run({ path: `/api/v1/categories/${category.id}`, body, method: "PATCH", context: "rename" }); }}>
                  <label className="field-stack flex-1" htmlFor={`category-name-${category.id}`}><span className="field-label">Название</span><input autoFocus className="field-input" disabled={disabled} id={`category-name-${category.id}`} maxLength={60} minLength={1} onChange={(event) => setEditingName(event.currentTarget.value)} required value={editingName} /></label>
                  <div className="flex items-end gap-2"><button className="button button--primary" disabled={disabled || editingName.trim().length === 0} type="submit">Сохранить</button><button className="button button--ghost" disabled={disabled} onClick={() => setEditingId(null)} type="button">Отмена</button></div>
                </form>
              ) : archiveId === category.id ? (
                <div className="notice notice--warning"><div><p className="notice__title">Убрать категорию в архив?</p><p className="notice__text">История операций не изменится.</p></div><div className="flex flex-wrap gap-2"><button className="button button--danger" disabled={disabled} onClick={() => runVersioned(category, "archive")} type="button">Архивировать</button><button className="button button--ghost" disabled={disabled} onClick={() => setArchiveId(null)} type="button">Не сейчас</button></div></div>
              ) : (
                <div className="flex flex-wrap gap-2">
                  {category.archived ? <button className="button button--primary" disabled={disabled} onClick={() => runVersioned(category, "restore")} type="button">Восстановить</button> : <><button className="button button--secondary" disabled={disabled} onClick={() => { setArchiveId(null); setEditingId(category.id); setEditingName(category.name); }} type="button">Переименовать</button><button className="button button--danger-ghost" disabled={disabled} onClick={() => setArchiveId(category.id)} type="button">В архив</button></>}
                </div>
              )}
            </article>
          ))}
        </section>
      )}
    </div>
  );
}
