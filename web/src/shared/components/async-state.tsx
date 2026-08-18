import type { ReactNode } from "react";

interface ErrorStateProps {
  readonly title?: string;
  readonly description?: string;
  readonly actionLabel?: string;
  readonly onAction?: () => void;
}
export function ErrorState({
  title = "Не удалось загрузить данные",
  description = "Проверьте соединение и повторите запрос.",
  actionLabel = "Повторить",
  onAction,
}: ErrorStateProps) {
  return (
    <section className="state-panel" role="alert">
      <span aria-hidden="true" className="state-panel__mark">!</span>
      <div>
        <h2 className="state-panel__title">{title}</h2>
        <p className="state-panel__description">{description}</p>
        {onAction === undefined ? null : (
          <button className="button button--secondary mt-4" onClick={onAction} type="button">
            {actionLabel}
          </button>
        )}
      </div>
    </section>
  );
}

export function EmptyState({ title, children }: { readonly title: string; readonly children: ReactNode }) {
  return (
    <section className="empty-state">
      <span aria-hidden="true" className="empty-state__coin">○</span>
      <h2 className="empty-state__title">{title}</h2>
      <div className="empty-state__description">{children}</div>
    </section>
  );
}

export function PageSkeleton({ rows = 3 }: { readonly rows?: number }) {
  return (
    <div aria-busy="true" aria-label="Загрузка" className="space-y-4" role="status">
      <div className="skeleton h-8 w-48" />
      <div className="skeleton h-32 w-full" />
      {Array.from({ length: rows }, (_, index) => (
        <div className="skeleton h-20 w-full" key={index} />
      ))}
      <span className="sr-only">Загрузка данных…</span>
    </div>
  );
}
