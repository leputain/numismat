import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { apiClient } from "../../app/providers";
import type { RateSourcesResponse, RateVersionPageResponse } from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { EmptyState, ErrorState, PageSkeleton } from "../../shared/components/async-state";
import { PageHeading } from "../../shared/components/page-heading";
import { formatTransactionDate } from "../../shared/format/date-time";
import { queryKeys } from "../../shared/queries/query-keys";

const PAGE_LIMIT = 20;

function versionsPath(sourceId: string, cursor: string | null): string {
  const params = new URLSearchParams({ limit: String(PAGE_LIMIT) });
  if (cursor !== null) params.set("cursor", cursor);
  return `/api/v1/exchange-rate-sources/${sourceId}/versions?${params.toString()}`;
}

export function ExchangeRateSourcePage({ sourceId }: { readonly sourceId: string }) {
  const { locale, timeZone } = useSessionFormat();
  const sources = useQuery({
    queryKey: queryKeys.exchangeRates.sources,
    queryFn: ({ signal }) => apiClient.get<RateSourcesResponse>("/api/v1/exchange-rate-sources", { signal }),
    staleTime: 15_000,
  });
  const source = sources.data?.items.find((item) => item.id === sourceId);
  const versions = useInfiniteQuery({
    queryKey: queryKeys.exchangeRates.versions(sourceId, PAGE_LIMIT),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => apiClient.get<RateVersionPageResponse>(versionsPath(sourceId, pageParam), { signal }),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: source !== undefined,
    staleTime: 15_000,
  });

  if (sources.isPending) return <PageSkeleton rows={4} />;
  if (sources.isError) return <ErrorState onAction={() => void sources.refetch()} />;
  if (source === undefined) {
    return <ErrorState actionLabel="К источникам" description="Источник недоступен или был удалён." onAction={() => window.location.assign("/rates")} title="Источник не найден" />;
  }
  const items = versions.data?.pages.flatMap((page) => page.items) ?? [];
  return (
    <div className="exchange-rate-source-page page-stack">
      <PageHeading
        action={<Link className="button button--primary" to={`/rates/${source.id}/publish`}>Новая версия</Link>}
        description="Каждая публикация сохраняется отдельно; существующие версии не редактируются."
        eyebrow={`Ручной источник · версия ${String(source.latest_version)}`}
        title={`Курсы в ${source.target_currency}`}
      />
      <section aria-live="polite" className="space-y-3">
        {versions.isPending ? <PageSkeleton rows={4} /> : versions.isError ? <ErrorState onAction={() => void versions.refetch()} /> : items.length === 0 ? <EmptyState title="Версий пока нет">Опубликуйте первый набор курсов.</EmptyState> : items.map((version) => (
          <Link className="surface-panel block transition-colors hover:border-[var(--nm-accent)]" key={version.id} to={`/rates/versions/${version.id}`}>
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="font-semibold text-[var(--nm-text)]">Версия {String(version.version)}</p>
                <p className="mt-1 text-sm text-[var(--nm-muted)]">Действует с {formatTransactionDate(version.effective_at, locale, timeZone)}</p>
                <p className="mt-1 text-xs text-[var(--nm-muted)]">Опубликована {formatTransactionDate(version.created_at, locale, timeZone)}</p>
              </div>
              <span aria-hidden="true" className="text-[var(--nm-muted)]">›</span>
            </div>
          </Link>
        ))}
        {versions.hasNextPage ? <button className="button button--secondary" disabled={versions.isFetchingNextPage} onClick={() => void versions.fetchNextPage()} type="button">{versions.isFetchingNextPage ? "Загружаем…" : "Показать ещё"}</button> : null}
      </section>
      <Link className="text-link self-start" to="/rates">← Все источники</Link>
    </div>
  );
}
