import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { apiClient } from "../../app/providers";
import type { ConvertedPeriodResponse, RateVersion } from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { ErrorState, PageSkeleton } from "../../shared/components/async-state";
import { PageHeading } from "../../shared/components/page-heading";
import { formatTransactionDate } from "../../shared/format/date-time";
import { formatMoney } from "../../shared/finance/money";
import { queryKeys } from "../../shared/queries/query-keys";

interface PeriodSelection {
  readonly start: string;
  readonly end: string;
}

function localDateTimeValue(date: Date): string {
  const shifted = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return shifted.toISOString().slice(0, 16);
}

function toUtcTimestamp(value: string): string | undefined {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/u.test(value)) return undefined;
  const timestamp = new Date(value);
  if (!Number.isFinite(timestamp.getTime())) return undefined;
  return timestamp.toISOString().replace(".000Z", "Z");
}

function convertedPath(versionId: string, period: PeriodSelection): string {
  const params = new URLSearchParams({ version_id: versionId, start: period.start, end: period.end });
  return `/api/v1/reports/period/converted?${params.toString()}`;
}

function ConvertedTotals({ value }: { readonly value: ConvertedPeriodResponse }) {
  const { locale } = useSessionFormat();
  return (
    <div className="space-y-5">
      <div className="summary-grid summary-grid--compact">
        <article className="summary-card">
          <div className="summary-card__heading"><span className="summary-card__currency">{value.target_currency}</span><span className="summary-card__net-label">Итог по версии {String(value.version.version)}</span></div>
          <p className="summary-card__net">{formatMoney(value.net_minor, value.target_currency, locale)}</p>
          <dl className="summary-card__split">
            <div><dt>Доход</dt><dd className="money-income">{formatMoney(value.income_minor, value.target_currency, locale)}</dd></div>
            <div><dt>Расход</dt><dd className="money-expense">{formatMoney(value.expense_minor, value.target_currency, locale)}</dd></div>
          </dl>
        </article>
      </div>
      <div>
        <p className="eyebrow">Исходные итоги</p>
        {value.original_totals.length === 0 ? <p className="mt-2 text-sm text-[var(--nm-muted)]">За период движений нет.</p> : (
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            {value.original_totals.map((total) => (
              <dl className="rounded-2xl border border-[var(--nm-line)] bg-[var(--nm-surface)] p-4 text-sm" key={total.currency}>
                <div className="flex items-center justify-between gap-3"><dt className="text-[var(--nm-muted)]">Доход · {total.currency}</dt><dd className="text-[var(--nm-text)]">{formatMoney(total.income_minor, total.currency, locale)}</dd></div>
                <div className="mt-2 flex items-center justify-between gap-3"><dt className="text-[var(--nm-muted)]">Расход · {total.currency}</dt><dd className="text-[var(--nm-text)]">{formatMoney(total.expense_minor, total.currency, locale)}</dd></div>
              </dl>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export function ExchangeRateVersionPage({ versionId }: { readonly versionId: string }) {
  const { locale, timeZone } = useSessionFormat();
  const now = new Date();
  const monthStart = new Date(now.getFullYear(), now.getMonth(), 1, 0, 0, 0, 0);
  const [startInput, setStartInput] = useState(() => localDateTimeValue(monthStart));
  const [endInput, setEndInput] = useState(() => localDateTimeValue(now));
  const [period, setPeriod] = useState<PeriodSelection>();
  const [formError, setFormError] = useState<string>();
  const version = useQuery({
    queryKey: queryKeys.exchangeRates.detail(versionId),
    queryFn: ({ signal }) => apiClient.get<RateVersion>(`/api/v1/exchange-rate-versions/${versionId}`, { signal }),
    staleTime: Number.POSITIVE_INFINITY,
  });
  const converted = useQuery({
    queryKey: queryKeys.exchangeRates.converted(versionId, period?.start ?? "", period?.end ?? ""),
    queryFn: ({ signal }) => {
      if (period === undefined) throw new Error("Converted period is unavailable");
      return apiClient.get<ConvertedPeriodResponse>(convertedPath(versionId, period), { signal });
    },
    enabled: period !== undefined,
    staleTime: 30_000,
  });

  const submitPeriod = () => {
    const start = toUtcTimestamp(startInput);
    const end = toUtcTimestamp(endInput);
    if (start === undefined || end === undefined || start >= end) {
      setFormError("Конец периода должен быть позже начала.");
      return;
    }
    setFormError(undefined);
    if (period?.start === start && period.end === end) {
      void converted.refetch();
      return;
    }
    setPeriod({ start, end });
  };

  if (version.isPending) return <PageSkeleton rows={5} />;
  if (version.isError) return <ErrorState onAction={() => void version.refetch()} />;
  const value = version.data;
  return (
    <div className="exchange-rate-version-page page-stack">
      <PageHeading
        description={`Действует с ${formatTransactionDate(value.effective_at, locale, timeZone)}. Создана ${formatTransactionDate(value.created_at, locale, timeZone)}.`}
        eyebrow={`Сохранённая версия ${String(value.version)}`}
        title={`Курсы в ${value.target_currency}`}
      />
      <section className="surface-panel">
        <div className="section-heading section-heading--inside">
          <div><p className="eyebrow">Прямые курсы</p><h2 className="section-title">Состав версии</h2></div>
        </div>
        <div className="divide-y divide-[var(--nm-line)]">
          {value.entries.map((entry) => (
            <div className="flex items-center justify-between gap-4 py-3 text-sm" key={entry.source_currency}>
              <span className="text-[var(--nm-muted)]">1 {entry.source_currency}</span>
              <span className="font-medium text-[var(--nm-text)]">{entry.rate} {entry.target_currency}</span>
            </div>
          ))}
        </div>
        <p className="mt-4 text-xs leading-relaxed text-[var(--nm-muted)]">Курсы сохранены без округления. Обратные и составные курсы не рассчитываются.</p>
      </section>

      <section className="surface-panel space-y-5">
        <div>
          <p className="eyebrow">Расчёт по выбранной версии</p>
          <h2 className="section-title">Пересчитать период</h2>
          <p className="mt-2 text-sm leading-relaxed text-[var(--nm-muted)]">Начальная дата входит в период, конечная — нет. Исходные операции не изменяются.</p>
        </div>
        <form className="grid gap-4 sm:grid-cols-2" onSubmit={(event) => { event.preventDefault(); submitPeriod(); }}>
          <div className="field-stack"><label className="field-label" htmlFor="converted-start">Начало</label><input className="field-input" id="converted-start" onChange={(event) => setStartInput(event.currentTarget.value)} required step={60} type="datetime-local" value={startInput} /></div>
          <div className="field-stack"><label className="field-label" htmlFor="converted-end">Конец, не включая</label><input className="field-input" id="converted-end" onChange={(event) => setEndInput(event.currentTarget.value)} required step={60} type="datetime-local" value={endInput} /></div>
          <div className="sm:col-span-2">
            {formError === undefined ? null : <p aria-live="polite" className="mb-3 text-sm text-[var(--nm-danger)]" role="alert">{formError}</p>}
            <button className="button button--primary" disabled={converted.isFetching} type="submit">{converted.isFetching ? "Считаем…" : "Рассчитать"}</button>
          </div>
        </form>
        {converted.isError ? <ErrorState actionLabel="Повторить" description="Проверьте, что в версии есть прямой курс для каждой валюты периода." onAction={() => void converted.refetch()} title="Пересчёт не выполнен" /> : converted.data === undefined ? null : <ConvertedTotals value={converted.data} />}
      </section>
      <div className="flex flex-wrap gap-3"><Link className="button button--secondary" to={`/rates/${value.source_id}`}>К версиям</Link><Link className="button button--primary" to={`/rates/${value.source_id}/publish`}>Новая версия</Link></div>
    </div>
  );
}
