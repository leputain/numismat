import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { apiClient } from "../../app/providers";
import type {
  BankImportBatch,
  BankImportBatchMutationResponse,
  BankImportDraftMutationResponse,
  BankImportRow,
  BankImportRowMutationResponse,
  BankImportRowPageResponse,
  ReconciliationCandidatesResponse,
} from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { EmptyState, ErrorState, PageSkeleton } from "../../shared/components/async-state";
import { MutationFeedback } from "../../shared/components/mutation-feedback";
import { PageHeading } from "../../shared/components/page-heading";
import { formatTransactionDate } from "../../shared/format/date-time";
import { formatTransactionMoney } from "../../shared/finance/money";
import {
  refreshDraftQueries,
  refreshFinanceQueries,
  restartBankImportPagination,
  restartTransactionPagination,
} from "../../shared/mutations/query-recovery";
import { usePreparedMutation } from "../../shared/mutations/prepared-mutation";
import { queryKeys } from "../../shared/queries/query-keys";

const PAGE_LIMIT = 20;

function rowsPath(batchId: string, cursor: string | null): string {
  const params = new URLSearchParams({ limit: String(PAGE_LIMIT) });
  if (cursor !== null) params.set("cursor", cursor);
  return `/api/v1/bank-imports/${batchId}/rows?${params.toString()}`;
}

function outcomeLabel(outcome: BankImportRow["outcome"]): string {
  const labels: Record<BankImportRow["outcome"], string> = {
    pending: "Ожидает решения",
    awaiting_review: "Черновик на проверке",
    dismissed: "Черновик закрыт",
    confirmed: "Подтверждена",
    linked: "Связана",
    skipped: "Пропущена",
    cancelled: "Отменена",
  };
  return labels[outcome];
}

function BankImportRowCard({ batch, row }: { readonly batch: BankImportBatch; readonly row: BankImportRow }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { locale, timeZone } = useSessionFormat();
  const [showCandidates, setShowCandidates] = useState(false);
  const candidates = useQuery({
    queryKey: queryKeys.bankImports.candidates(batch.id, row.id),
    queryFn: ({ signal }) => apiClient.get<ReconciliationCandidatesResponse>(`/api/v1/bank-imports/${batch.id}/rows/${row.id}/candidates`, { signal }),
    enabled: showCandidates && batch.state === "open" && (row.outcome === "pending" || row.outcome === "dismissed"),
    staleTime: 0,
  });
  type ActionContext = "draft" | "link" | "skip";
  type ActionResponse = BankImportDraftMutationResponse | BankImportRowMutationResponse;
  const mutation = usePreparedMutation<ActionResponse, ActionContext>({
    eventScope: "bank_import",
    onSuccess: async (response, context) => {
      await restartBankImportPagination(queryClient);
      if (context === "draft" && response.result.kind === "draft") {
        await refreshDraftQueries(queryClient);
        navigate("/draft");
      } else if (context === "link") {
        await restartTransactionPagination(queryClient);
        await refreshFinanceQueries(queryClient);
      }
    },
    onRejected: async () => {
      await restartBankImportPagination(queryClient);
    },
    onOutcomeUnknown: async () => {
      await Promise.all([
        restartBankImportPagination(queryClient),
        restartTransactionPagination(queryClient),
        refreshDraftQueries(queryClient),
      ]);
    },
  });
  const actionable = batch.state === "open" && (row.outcome === "pending" || row.outcome === "dismissed");
  const disabled = mutation.isPending || mutation.outcomeUnknown;
  const baseBody = { batch_version: batch.version, row_version: row.version };
  return (
    <article className="surface-panel space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="eyebrow">Строка {row.position}</p>
          <p className={`mt-2 text-xl font-semibold ${row.type === "expense" ? "text-[#e6a18b]" : "text-[#82c6ae]"}`}>
            {formatTransactionMoney(row.amount_minor, row.currency, row.type, locale)}
          </p>
        </div>
        <span className="revision-chip">{outcomeLabel(row.outcome)}</span>
      </div>
      <dl className="detail-list detail-list--compact">
        <div><dt>Дата</dt><dd>{formatTransactionDate(row.occurred_at, locale, timeZone)}</dd></div>
        <div><dt>Описание</dt><dd>{row.description || "Без описания"}</dd></div>
      </dl>
      {row.possible_duplicate ? <p className="notice notice--warning text-sm">В этом счёте уже есть строка с тем же защищённым reference или отпечатком. Решение остаётся за вами.</p> : null}
      {row.outcome === "awaiting_review" ? <Link className="button button--primary w-full" to="/draft">Открыть черновик</Link> : null}
      {actionable ? (
        <div className="space-y-3">
          <div className="grid gap-2 sm:grid-cols-3">
            <button className="button button--primary" disabled={disabled} onClick={() => mutation.run({ path: `/api/v1/bank-imports/${batch.id}/rows/${row.id}/draft`, body: baseBody, context: "draft" })} type="button">Создать черновик</button>
            <button className="button button--secondary" disabled={disabled} onClick={() => setShowCandidates((value) => !value)} type="button">Совпадения</button>
            <button className="button button--danger-ghost" disabled={disabled} onClick={() => mutation.run({ path: `/api/v1/bank-imports/${batch.id}/rows/${row.id}/skip`, body: baseBody, context: "skip" })} type="button">Пропустить</button>
          </div>
          {showCandidates ? (
            candidates.isPending ? <PageSkeleton rows={2} /> : candidates.isError ? (
              <ErrorState onAction={() => void candidates.refetch()} />
            ) : candidates.data.items.length === 0 ? (
              <p className="text-sm text-stone-500">Точных кандидатов в окне ±3 дня нет.</p>
            ) : (
              <div className="space-y-2">
                {candidates.data.items.map((candidate) => (
                  <div className="rounded-2xl border border-white/8 bg-black/10 p-4" key={candidate.transaction.id}>
                    <p className="font-medium text-stone-200">#{candidate.rank} · {candidate.transaction.category.name}</p>
                    <p className="mt-1 text-sm text-stone-500">{formatTransactionDate(candidate.transaction.occurred_at, locale, timeZone)}</p>
                    <button className="button button--secondary mt-3" disabled={disabled} onClick={() => mutation.run({ path: `/api/v1/bank-imports/${batch.id}/rows/${row.id}/link`, body: { ...baseBody, transaction_id: candidate.transaction.id, transaction_version: candidate.transaction.version }, context: "link" })} type="button">Связать с этой операцией</button>
                  </div>
                ))}
              </div>
            )
          ) : null}
        </div>
      ) : null}
      <MutationFeedback error={mutation.error} onRetryUnknown={mutation.retryUnknown} outcomeUnknown={mutation.outcomeUnknown} pending={mutation.isPending} />
    </article>
  );
}

export function BankImportDetailPage({ batchId }: { readonly batchId: string }) {
  const queryClient = useQueryClient();
  const batch = useQuery({
    queryKey: queryKeys.bankImports.detail(batchId),
    queryFn: ({ signal }) => apiClient.get<BankImportBatch>(`/api/v1/bank-imports/${batchId}`, { signal }),
    staleTime: 0,
  });
  const rows = useInfiniteQuery({
    queryKey: queryKeys.bankImports.rows(batchId, PAGE_LIMIT),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => apiClient.get<BankImportRowPageResponse>(rowsPath(batchId, pageParam), { signal }),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    staleTime: 0,
  });
  const cancel = usePreparedMutation<BankImportBatchMutationResponse, undefined>({
    eventScope: "bank_import",
    onSuccess: async () => restartBankImportPagination(queryClient),
    onRejected: async () => restartBankImportPagination(queryClient),
    onOutcomeUnknown: async () => restartBankImportPagination(queryClient),
  });
  if (batch.isPending || rows.isPending) return <PageSkeleton rows={5} />;
  if (batch.isError || rows.isError) return <ErrorState onAction={() => void restartBankImportPagination(queryClient)} />;
  const items = rows.data.pages.flatMap((page) => page.items);
  return (
    <div className="page-stack">
      <PageHeading
        action={<span className="revision-chip">Ревизия {batch.data.version}</span>}
        description="Каждая строка требует явного решения: черновик, связь с точным кандидатом или пропуск. Автоматической сверки нет."
        eyebrow="Банковский пакет"
        title={`Сверка · ${batch.data.counts.total} строк`}
      />
      <section className="surface-panel">
        <div className="grid grid-cols-2 gap-4 text-sm sm:grid-cols-4">
          <div><p className="text-stone-500">Ожидают</p><p className="mt-1 text-xl font-semibold">{batch.data.counts.pending + batch.data.counts.staged}</p></div>
          <div><p className="text-stone-500">Подтверждены</p><p className="mt-1 text-xl font-semibold">{batch.data.counts.confirmed}</p></div>
          <div><p className="text-stone-500">Связаны</p><p className="mt-1 text-xl font-semibold">{batch.data.counts.linked}</p></div>
          <div><p className="text-stone-500">Пропущены</p><p className="mt-1 text-xl font-semibold">{batch.data.counts.skipped}</p></div>
        </div>
        {batch.data.state === "open" ? (
          <button className="button button--danger-ghost mt-5" disabled={cancel.isPending || cancel.outcomeUnknown} onClick={() => cancel.run({ path: `/api/v1/bank-imports/${batch.data.id}/cancel`, body: { version: batch.data.version }, context: undefined })} type="button">Отменить неразобранные строки</button>
        ) : null}
        <MutationFeedback error={cancel.error} onRetryUnknown={cancel.retryUnknown} outcomeUnknown={cancel.outcomeUnknown} pending={cancel.isPending} />
      </section>
      {items.length === 0 ? <EmptyState title="Строк нет">Пакет не содержит доступных строк.</EmptyState> : (
        <section className="space-y-3" aria-live="polite">
          {items.map((row) => <BankImportRowCard batch={batch.data} key={row.id} row={row} />)}
          {rows.hasNextPage ? <div className="load-more"><button className="button button--secondary" disabled={rows.isFetchingNextPage} onClick={() => void rows.fetchNextPage()} type="button">{rows.isFetchingNextPage ? "Загружаем…" : "Показать ещё"}</button></div> : <p className="feed-end">Это все строки</p>}
        </section>
      )}
    </div>
  );
}
