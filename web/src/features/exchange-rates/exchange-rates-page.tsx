import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { apiClient } from "../../app/providers";
import type { RateSourcesResponse } from "../../shared/api/types";
import { EmptyState, ErrorState, PageSkeleton } from "../../shared/components/async-state";
import { PageHeading } from "../../shared/components/page-heading";
import { queryKeys } from "../../shared/queries/query-keys";

export function ExchangeRatesPage() {
  const sources = useQuery({
    queryKey: queryKeys.exchangeRates.sources,
    queryFn: ({ signal }) => apiClient.get<RateSourcesResponse>("/api/v1/exchange-rate-sources", { signal }),
    staleTime: 30_000,
  });
  const content = (() => {
    if (sources.isPending) return <PageSkeleton rows={4} />;
    if (sources.isError) return <ErrorState onAction={() => void sources.refetch()} />;
    if (sources.data.items.length === 0) {
      return (
        <EmptyState title="Источников пока нет">
          <p>Опубликуйте первый набор прямых ручных курсов.</p>
          <Link className="button button--primary mt-5" to="/rates/new">Добавить источник</Link>
        </EmptyState>
      );
    }
    return (
      <div className="grid gap-3 sm:grid-cols-2">
        {sources.data.items.map((source) => (
          <Link className="surface-panel group transition-colors hover:border-amber-200/20" key={source.id} to={`/rates/${source.id}`}>
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="eyebrow">Ручной источник</p>
                <h2 className="mt-2 text-2xl font-semibold text-stone-100">В {source.target_currency}</h2>
                <p className="mt-2 text-sm text-stone-500">Опубликована версия {String(source.latest_version)}</p>
              </div>
              <span aria-hidden="true" className="text-xl text-stone-600 transition-colors group-hover:text-amber-200">›</span>
            </div>
          </Link>
        ))}
      </div>
    );
  })();
  return (
    <div className="page-stack">
      <PageHeading
        action={<Link className="button button--primary" to="/rates/new">Новый</Link>}
        description="Версионные ручные источники для воспроизводимых отчётов. Автоматических обратных и составных курсов нет."
        eyebrow="Immutable snapshots"
        title="Курсы валют"
      />
      <section aria-live="polite">{content}</section>
    </div>
  );
}
