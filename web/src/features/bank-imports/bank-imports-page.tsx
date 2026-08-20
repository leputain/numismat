import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router";

import { apiClient } from "../../app/providers";
import type {
  AccountsResponse,
  BankImportBatchMutationResponse,
  BankImportBatchPageResponse,
} from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { EmptyState, ErrorState, PageSkeleton } from "../../shared/components/async-state";
import { MutationFeedback } from "../../shared/components/mutation-feedback";
import { PageHeading } from "../../shared/components/page-heading";
import { emitClientEvent } from "../../shared/logging/client-events";
import { restartBankImportPagination } from "../../shared/mutations/query-recovery";
import { usePreparedRawMutation } from "../../shared/mutations/prepared-raw-mutation";
import { queryKeys } from "../../shared/queries/query-keys";

const PAGE_LIMIT = 20;
const MAX_CSV_BYTES = 2 * 1024 * 1024;
type CsvEncoding = "utf-8" | "windows-1251";

function batchesPath(cursor: string | null): string {
  const params = new URLSearchParams({ limit: String(PAGE_LIMIT) });
  if (cursor !== null) params.set("cursor", cursor);
  return `/api/v1/bank-imports?${params.toString()}`;
}

function batchStateLabel(state: "cancelled" | "completed" | "open"): string {
  if (state === "completed") return "Завершён";
  if (state === "cancelled") return "Отменён";
  return "На сверке";
}

export function BankImportsPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { locale } = useSessionFormat();
  const inputRef = useRef<HTMLInputElement>(null);
  const [selectedAccountId, setSelectedAccountId] = useState("");
  const [encoding, setEncoding] = useState<CsvEncoding>("utf-8");
  const [selectedFile, setSelectedFile] = useState<File>();
  const [localError, setLocalError] = useState<string>();
  const accounts = useQuery({
    queryKey: queryKeys.catalogs.accounts,
    queryFn: ({ signal }) =>
      apiClient.get<AccountsResponse>("/api/v1/accounts?archived=false", { signal }),
    staleTime: 30_000,
  });
  const feed = useInfiniteQuery({
    queryKey: queryKeys.bankImports.batches(PAGE_LIMIT),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      apiClient.get<BankImportBatchPageResponse>(batchesPath(pageParam), { signal }),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    staleTime: 15_000,
  });

  useEffect(() => emitClientEvent("bank_import_opened"), []);
  useEffect(() => {
    if (selectedAccountId !== "" || accounts.data === undefined) return;
    const preferred =
      accounts.data.items.find((item) => item.id === accounts.data.default_account_id) ??
      accounts.data.items[0];
    if (preferred !== undefined) setSelectedAccountId(preferred.id);
  }, [accounts.data, selectedAccountId]);

  const upload = usePreparedRawMutation<BankImportBatchMutationResponse, undefined>({
    onSuccess: async (response) => {
      setSelectedFile(undefined);
      setLocalError(undefined);
      if (inputRef.current !== null) inputRef.current.value = "";
      await restartBankImportPagination(queryClient);
      navigate(`/imports/${response.result.batch_id}`);
    },
    onRejected: async () => {
      await restartBankImportPagination(queryClient);
    },
    onOutcomeUnknown: async () => {
      await restartBankImportPagination(queryClient);
    },
  });

  const submitUpload = async () => {
    const account = accounts.data?.items.find((item) => item.id === selectedAccountId);
    if (selectedFile === undefined || account === undefined) {
      setLocalError("Выберите активный счёт и CSV-файл.");
      return;
    }
    if (selectedFile.size < 1 || selectedFile.size > MAX_CSV_BYTES) {
      setLocalError("CSV должен занимать от 1 байта до 2 МиБ.");
      return;
    }
    let content: Uint8Array;
    try {
      content = new Uint8Array(await selectedFile.arrayBuffer());
    } catch {
      setLocalError("Не удалось прочитать файл в памяти браузера.");
      return;
    }
    if (content.byteLength < 1 || content.byteLength > MAX_CSV_BYTES) {
      setLocalError("CSV должен занимать от 1 байта до 2 МиБ.");
      return;
    }
    const params = new URLSearchParams({
      account_id: account.id,
      account_version: String(account.version),
      profile: "canonical_v1",
    });
    setLocalError(undefined);
    upload.run({
      path: `/api/v1/bank-imports/upload?${params.toString()}`,
      content,
      contentType: `text/csv; charset=${encoding}`,
      context: undefined,
    });
  };

  const batches = feed.data?.pages.flatMap((page) => page.items) ?? [];
  const accountNames = new Map(accounts.data?.items.map((item) => [item.id, item.name]));
  return (
    <div className="bank-imports-page page-stack">
      <PageHeading
        description="Файл обрабатывается локально и не хранится целиком. Каждая строка требует вашего решения."
        eyebrow="Проверка перед сохранением"
        title="Банковский импорт"
      />

      <section className="surface-panel space-y-5" aria-labelledby="upload-title">
        <div>
          <p className="eyebrow">Новый пакет</p>
          <h2 className="section-title mt-2" id="upload-title">Загрузить CSV</h2>
          <p className="page-description mt-2">До 2 МиБ, 2000 строк. Один файл — один банковский счёт.</p>
        </div>
        {accounts.isPending ? <PageSkeleton rows={2} /> : accounts.isError ? (
          <ErrorState onAction={() => void accounts.refetch()} />
        ) : accounts.data.items.length === 0 ? (
          <EmptyState title="Нет активного счёта">Создайте счёт в настройках перед импортом.</EmptyState>
        ) : (
          <div className="field-stack">
            <label className="field-label" htmlFor="bank-import-account">Счёт назначения</label>
            <select
              className="field-input"
              disabled={upload.isPending || upload.outcomeUnknown}
              id="bank-import-account"
              onChange={(event) => setSelectedAccountId(event.currentTarget.value)}
              value={selectedAccountId}
            >
              {accounts.data.items.map((account) => (
                <option key={account.id} value={account.id}>{account.name} · {account.currency}</option>
              ))}
            </select>
            <label className="field-label" htmlFor="bank-import-encoding">Кодировка файла</label>
            <select
              className="field-input"
              disabled={upload.isPending || upload.outcomeUnknown}
              id="bank-import-encoding"
              onChange={(event) => setEncoding(event.currentTarget.value as CsvEncoding)}
              value={encoding}
            >
              <option value="utf-8">UTF-8 / UTF-8 BOM</option>
              <option value="windows-1251">Windows-1251</option>
            </select>
            <label className="field-label" htmlFor="bank-import-file">CSV-файл</label>
            <input
              accept=".csv,text/csv"
              className="field-input"
              disabled={upload.isPending || upload.outcomeUnknown}
              id="bank-import-file"
              onChange={(event) => {
                setSelectedFile(event.currentTarget.files?.[0]);
                setLocalError(undefined);
              }}
              ref={inputRef}
              type="file"
            />
            <p className="text-xs text-[var(--nm-muted)]">Имя файла не отправляется отдельно и не сохраняется.</p>
            {localError === undefined ? null : <p className="text-sm text-[var(--nm-danger)]" role="alert">{localError}</p>}
            <button
              className="button button--primary"
              disabled={upload.isPending || upload.outcomeUnknown || selectedFile === undefined}
              onClick={() => void submitUpload()}
              type="button"
            >
              {upload.isPending ? "Проверяем…" : "Подготовить к сверке"}
            </button>
          </div>
        )}
        <MutationFeedback
          error={upload.error}
          onRetryUnknown={upload.retryUnknown}
          outcomeUnknown={upload.outcomeUnknown}
          pending={upload.isPending}
        />
      </section>

      <section aria-live="polite" className="space-y-3">
        <div>
          <p className="eyebrow">История пакетов</p>
          <h2 className="section-title mt-2">Импорты</h2>
        </div>
        {feed.isPending ? <PageSkeleton rows={4} /> : feed.isError ? (
          <ErrorState onAction={() => void restartBankImportPagination(queryClient)} />
        ) : batches.length === 0 ? (
          <EmptyState title="Импортов пока нет">Первый проверенный CSV появится здесь.</EmptyState>
        ) : (
          <>
            <div className="space-y-3">
              {batches.map((batch) => (
                <Link className="surface-panel block transition-colors hover:border-[var(--nm-accent)]" key={batch.id} to={`/imports/${batch.id}`}>
                  <div className="flex items-start justify-between gap-4">
                    <div>
                      <p className="font-semibold text-[var(--nm-text)]">{accountNames.get(batch.account_id) ?? "Счёт"}</p>
                      <p className="mt-1 text-sm text-[var(--nm-muted)]">{new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(new Date(batch.created_at))}</p>
                    </div>
                    <span className="revision-chip">{batchStateLabel(batch.state)}</span>
                  </div>
                  <p className="mt-4 text-sm text-[var(--nm-muted)]">Всего {batch.counts.total} · ждут решения {batch.counts.pending + batch.counts.staged}</p>
                </Link>
              ))}
            </div>
            {feed.hasNextPage ? (
              <div className="load-more">
                <button className="button button--secondary" disabled={feed.isFetchingNextPage} onClick={() => void feed.fetchNextPage()} type="button">
                  {feed.isFetchingNextPage ? "Загружаем…" : "Показать ещё"}
                </button>
              </div>
            ) : <p className="feed-end">Это весь список</p>}
          </>
        )}
      </section>
    </div>
  );
}
