import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Link, useNavigate } from "react-router";

import { apiClient } from "../../app/providers";
import type {
  PublishManualRateVersionRequest,
  RateSource,
  RateSourcesResponse,
  RateVersionMutationResponse,
} from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { ErrorState, PageSkeleton } from "../../shared/components/async-state";
import { MutationFeedback } from "../../shared/components/mutation-feedback";
import { PageHeading } from "../../shared/components/page-heading";
import { usePreparedMutation } from "../../shared/mutations/prepared-mutation";
import { queryKeys } from "../../shared/queries/query-keys";

const CURRENCY_PATTERN = /^[A-Z]{3}$/u;
const RATE_PATTERN = /^(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,12})?$/u;
const ZERO_RATE_PATTERN = /^0(?:\.0+)?$/u;
const MAX_ENTRIES = 32;

interface DraftRateEntry {
  readonly id: number;
  readonly sourceCurrency: string;
  readonly rate: string;
}

function localDateTimeValue(date = new Date()): string {
  const shifted = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return shifted.toISOString().slice(0, 16);
}

function toUtcTimestamp(value: string): string | undefined {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/u.test(value)) {
    return undefined;
  }
  const timestamp = new Date(value);
  if (!Number.isFinite(timestamp.getTime())) {
    return undefined;
  }
  return timestamp.toISOString().replace(".000Z", "Z");
}

function defaultSourceCurrency(target: string): string {
  return target === "USD" ? "EUR" : "USD";
}

function normalizedCurrency(value: string): string {
  return value.trim().toUpperCase();
}

function PublishForm({ source, knownSources }: {
  readonly source: RateSource | undefined;
  readonly knownSources: readonly RateSource[];
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { baseCurrency } = useSessionFormat();
  const initialTarget = source?.target_currency ?? baseCurrency;
  const nextEntryId = useRef(1);
  const [targetCurrency, setTargetCurrency] = useState(initialTarget);
  const [effectiveAt, setEffectiveAt] = useState(localDateTimeValue);
  const [entries, setEntries] = useState<readonly DraftRateEntry[]>([
    { id: 0, sourceCurrency: defaultSourceCurrency(initialTarget), rate: "" },
  ]);
  const [formError, setFormError] = useState<string>();
  const mutation = usePreparedMutation<RateVersionMutationResponse, null>({
    eventScope: "exchange_rate",
    async onSuccess(response) {
      await queryClient.invalidateQueries({ queryKey: queryKeys.exchangeRates.all });
      await navigate(`/rates/versions/${response.result.rate_version_id}`, { replace: true });
    },
    async onRejected() {
      await queryClient.invalidateQueries({ queryKey: queryKeys.exchangeRates.sources });
    },
    onOutcomeUnknown() {},
  });

  const updateEntry = (id: number, values: Partial<Omit<DraftRateEntry, "id">>) => {
    setEntries((current) => current.map((entry) => entry.id === id ? { ...entry, ...values } : entry));
  };

  const addEntry = () => {
    if (entries.length >= MAX_ENTRIES) return;
    const id = nextEntryId.current;
    nextEntryId.current += 1;
    setEntries((current) => [...current, { id, sourceCurrency: "", rate: "" }]);
  };

  const removeEntry = (id: number) => {
    if (entries.length === 1) return;
    setEntries((current) => current.filter((entry) => entry.id !== id));
  };

  const submit = () => {
    const target = normalizedCurrency(targetCurrency);
    const wireEffectiveAt = toUtcTimestamp(effectiveAt);
    const normalizedEntries = entries.map((entry) => ({
      source_currency: normalizedCurrency(entry.sourceCurrency),
      rate: entry.rate.trim(),
    }));
    if (!CURRENCY_PATTERN.test(target)) {
      setFormError("Целевая валюта должна состоять из трёх латинских букв.");
      return;
    }
    const existing = knownSources.find((item) => item.target_currency === target);
    if (source === undefined && existing !== undefined) {
      setFormError(`Источник для ${target} уже существует. Откройте его и опубликуйте следующую версию.`);
      return;
    }
    if (wireEffectiveAt === undefined) {
      setFormError("Укажите корректные дату и время начала действия версии.");
      return;
    }
    if (normalizedEntries.some((entry) => !CURRENCY_PATTERN.test(entry.source_currency))) {
      setFormError("Каждая исходная валюта должна состоять из трёх латинских букв.");
      return;
    }
    if (normalizedEntries.some((entry) => entry.source_currency === target)) {
      setFormError("Целевую валюту не нужно добавлять как отдельный курс 1:1.");
      return;
    }
    if (new Set(normalizedEntries.map((entry) => entry.source_currency)).size !== normalizedEntries.length) {
      setFormError("Исходные валюты не должны повторяться.");
      return;
    }
    if (normalizedEntries.some((entry) => !RATE_PATTERN.test(entry.rate) || ZERO_RATE_PATTERN.test(entry.rate))) {
      setFormError("Курс должен быть положительной ASCII-десятичной строкой: до 6 знаков до точки и до 12 после.");
      return;
    }
    setFormError(undefined);
    const body: PublishManualRateVersionRequest = {
      target_currency: target,
      expected_source_version: source?.latest_version ?? 0,
      effective_at: wireEffectiveAt,
      entries: normalizedEntries,
    };
    mutation.run({
      path: "/api/v1/exchange-rate-sources/manual/versions",
      body,
      context: null,
    });
  };

  return (
    <form className="surface-panel field-stack" onSubmit={(event) => { event.preventDefault(); submit(); }}>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="field-stack">
          <label className="field-label" htmlFor="rate-target">Целевая валюта</label>
          <input
            autoCapitalize="characters"
            autoComplete="off"
            className="field-input uppercase"
            disabled={source !== undefined || mutation.isPending}
            id="rate-target"
            maxLength={3}
            onChange={(event) => setTargetCurrency(event.currentTarget.value.toUpperCase())}
            required
            type="text"
            value={targetCurrency}
          />
        </div>
        <div className="field-stack">
          <label className="field-label" htmlFor="rate-effective-at">Действует с</label>
          <input
            className="field-input"
            disabled={mutation.isPending}
            id="rate-effective-at"
            onChange={(event) => setEffectiveAt(event.currentTarget.value)}
            required
            step={60}
            type="datetime-local"
            value={effectiveAt}
          />
        </div>
      </div>

      <div className="space-y-3">
        <div className="flex items-end justify-between gap-3">
          <div>
            <p className="field-label">Прямые курсы</p>
            <p className="mt-1 text-xs leading-relaxed text-stone-500">Сколько единиц целевой валюты приходится на одну единицу исходной.</p>
          </div>
          <button className="button button--ghost" disabled={entries.length >= MAX_ENTRIES || mutation.isPending} onClick={addEntry} type="button">Добавить</button>
        </div>
        {entries.map((entry, index) => (
          <div className="grid gap-3 rounded-2xl border border-white/8 bg-white/[0.025] p-4 sm:grid-cols-[8rem_1fr_auto]" key={entry.id}>
            <div className="field-stack">
              <label className="field-label" htmlFor={`rate-source-${String(entry.id)}`}>Валюта {String(index + 1)}</label>
              <input autoCapitalize="characters" autoComplete="off" className="field-input uppercase" disabled={mutation.isPending} id={`rate-source-${String(entry.id)}`} maxLength={3} onChange={(event) => updateEntry(entry.id, { sourceCurrency: event.currentTarget.value.toUpperCase() })} required type="text" value={entry.sourceCurrency} />
            </div>
            <div className="field-stack">
              <label className="field-label" htmlFor={`rate-value-${String(entry.id)}`}>Курс к {normalizedCurrency(targetCurrency) || "целевой"}</label>
              <input autoComplete="off" className="field-input" disabled={mutation.isPending} id={`rate-value-${String(entry.id)}`} inputMode="decimal" maxLength={19} onChange={(event) => updateEntry(entry.id, { rate: event.currentTarget.value.replace(",", ".") })} placeholder="92.35" required type="text" value={entry.rate} />
            </div>
            <button aria-label={`Удалить курс ${String(index + 1)}`} className="button button--ghost self-end" disabled={entries.length === 1 || mutation.isPending} onClick={() => removeEntry(entry.id)} type="button">×</button>
          </div>
        ))}
      </div>

      {formError === undefined ? null : <p aria-live="polite" className="text-sm text-[#df9a94]" role="alert">{formError}</p>}
      <MutationFeedback error={mutation.error} onRetryUnknown={mutation.retryUnknown} outcomeUnknown={mutation.outcomeUnknown} pending={mutation.isPending} />
      <div className="flex flex-wrap gap-3 pt-2">
        <button className="button button--primary" disabled={mutation.isPending} type="submit">{mutation.isPending ? "Публикуем…" : "Опубликовать версию"}</button>
        <Link className="button button--secondary" to={source === undefined ? "/rates" : `/rates/${source.id}`}>Отмена</Link>
      </div>
      <p className="text-xs leading-relaxed text-stone-500">После публикации версия неизменяема. Обратные и составные курсы не вычисляются: для каждой валюты нужен прямой курс.</p>
    </form>
  );
}

export function ExchangeRatePublishPage({ sourceId }: { readonly sourceId?: string }) {
  const sources = useQuery({
    queryKey: queryKeys.exchangeRates.sources,
    queryFn: ({ signal }) => apiClient.get<RateSourcesResponse>("/api/v1/exchange-rate-sources", { signal }),
    staleTime: 15_000,
  });
  if (sourceId !== undefined && sources.isPending) return <PageSkeleton rows={5} />;
  if (sources.isError) return <ErrorState onAction={() => void sources.refetch()} />;
  const source = sourceId === undefined ? undefined : sources.data?.items.find((item) => item.id === sourceId);
  if (sourceId !== undefined && source === undefined) {
    return <ErrorState actionLabel="К источникам" description="Источник недоступен или был удалён." onAction={() => window.location.assign("/rates")} title="Источник не найден" />;
  }
  return (
    <div className="page-stack">
      <PageHeading
        description="Публикация создаёт новый immutable snapshot; прежние отчёты остаются воспроизводимыми."
        eyebrow={source === undefined ? "Новый ручной источник" : `Следующая версия после v${String(source.latest_version)}`}
        title={source === undefined ? "Добавить курсы" : `Курсы в ${source.target_currency}`}
      />
      <PublishForm key={source?.id ?? "new"} knownSources={sources.data?.items ?? []} source={source} />
    </div>
  );
}
