import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router";

import { apiClient } from "../../app/providers";
import type {
  AccountsResponse,
  CategoriesResponse,
  CreateRecurringScheduleRequest,
  RecurringSchedule,
  RecurringScheduleMutationResponse,
  ReplaceRecurringScheduleRequest,
} from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { ErrorState, PageSkeleton } from "../../shared/components/async-state";
import { MutationFeedback } from "../../shared/components/mutation-feedback";
import { PageHeading } from "../../shared/components/page-heading";
import { usePreparedMutation } from "../../shared/mutations/prepared-mutation";
import { restartRecurringPagination } from "../../shared/mutations/query-recovery";
import { queryKeys } from "../../shared/queries/query-keys";
import { majorToMinor, minorToMajor } from "../budgets/budget-money";

type Kind = "expense" | "income";
type Cadence = "daily" | "weekly" | "monthly";
const MAX_INTERVAL: Record<Cadence, number> = { daily: 365, weekly: 52, monthly: 24 };

function todayIn(timeZone: string): string {
  try {
    return new Intl.DateTimeFormat("en-CA", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      timeZone,
    }).format(new Date());
  } catch {
    return new Date().toISOString().slice(0, 10);
  }
}

function RecurringForm({ initial }: { readonly initial: RecurringSchedule | undefined }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { timeZone } = useSessionFormat();
  const [name, setName] = useState(initial?.name ?? "");
  const [kind, setKind] = useState<Kind>(initial?.kind ?? "expense");
  const [amount, setAmount] = useState(initial === undefined ? "" : minorToMajor(initial.amount_minor));
  const [accountId, setAccountId] = useState(initial?.account_id ?? "");
  const [categoryId, setCategoryId] = useState(initial?.category_id ?? "");
  const [cadence, setCadence] = useState<Cadence>(initial?.cadence ?? "monthly");
  const [interval, setInterval] = useState(String(initial?.interval ?? 1));
  const [anchorDate, setAnchorDate] = useState(initial?.anchor_date ?? todayIn(timeZone));
  const [localTime, setLocalTime] = useState(initial?.local_time ?? "09:00");
  const [endsOn, setEndsOn] = useState(initial?.ends_on ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [formError, setFormError] = useState<string>();
  const accounts = useQuery({
    queryKey: queryKeys.catalogs.accounts,
    queryFn: ({ signal }) => apiClient.get<AccountsResponse>("/api/v1/accounts?archived=false", { signal }),
    staleTime: 60_000,
  });
  const categories = useQuery({
    queryKey: queryKeys.catalogs.categories(kind),
    queryFn: ({ signal }) => apiClient.get<CategoriesResponse>(`/api/v1/categories?kind=${kind}&archived=false`, { signal }),
    staleTime: 60_000,
  });
  const selectedAccount = useMemo(
    () => accounts.data?.items.find((account) => account.id === accountId),
    [accountId, accounts.data],
  );
  const mutation = usePreparedMutation<RecurringScheduleMutationResponse, null>({
    eventScope: "recurring",
    async onSuccess(response) {
      await restartRecurringPagination(queryClient);
      await navigate(`/recurring/${response.result.schedule_id}`, { replace: true });
    },
    async onRejected() {
      if (initial !== undefined) {
        await queryClient.invalidateQueries({ queryKey: queryKeys.recurring.detail(initial.id) });
      }
    },
    onOutcomeUnknown() {},
  });

  const submit = () => {
    const amountMinor = majorToMinor(amount);
    const parsedInterval = Number(interval);
    const normalizedName = name.trim().replace(/\s+/gu, " ");
    if (normalizedName.length === 0 || normalizedName.length > 60) {
      setFormError("Название должно содержать от 1 до 60 символов.");
      return;
    }
    if (amountMinor === undefined) {
      setFormError("Укажите положительную сумму не более двух знаков после запятой.");
      return;
    }
    if (selectedAccount === undefined || categoryId.length === 0) {
      setFormError("Выберите доступные счёт и категорию.");
      return;
    }
    if (!Number.isInteger(parsedInterval) || parsedInterval < 1 || parsedInterval > MAX_INTERVAL[cadence]) {
      setFormError(`Интервал должен быть от 1 до ${String(MAX_INTERVAL[cadence])}.`);
      return;
    }
    if (anchorDate.length !== 10 || localTime.length !== 5 || (endsOn && endsOn < anchorDate)) {
      setFormError("Проверьте дату начала, время и дату окончания.");
      return;
    }
    if (description.length > 500 || /[\u0000-\u001f\u007f]/u.test(description)) {
      setFormError("Описание должно быть одной строкой длиной до 500 символов.");
      return;
    }
    setFormError(undefined);
    const common: CreateRecurringScheduleRequest = {
      name: normalizedName,
      kind,
      amount_minor: amountMinor,
      currency: selectedAccount.currency,
      account_id: accountId,
      category_id: categoryId,
      cadence,
      interval: parsedInterval,
      anchor_date: anchorDate,
      local_time: localTime,
      ends_on: endsOn || null,
      description,
    };
    if (initial === undefined) {
      mutation.run({ path: "/api/v1/recurring-schedules", body: common, context: null });
    } else {
      const body: ReplaceRecurringScheduleRequest = { ...common, version: initial.version };
      mutation.run({ path: `/api/v1/recurring-schedules/${initial.id}`, body, method: "PUT", context: null });
    }
  };
  const accountMissing = initial !== undefined && accounts.data?.items.every((item) => item.id !== initial.account_id);
  const categoryMissing = initial !== undefined && categories.data?.items.every((item) => item.id !== initial.category_id);

  return (
    <form className="surface-panel field-stack" onSubmit={(event) => { event.preventDefault(); submit(); }}>
      <div className="field-stack"><label className="field-label" htmlFor="recurring-name">Название</label><input autoComplete="off" className="field-input" disabled={mutation.isPending} id="recurring-name" maxLength={60} onChange={(event) => setName(event.currentTarget.value)} placeholder="Например, Аренда" required type="text" value={name} /></div>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="field-stack"><label className="field-label" htmlFor="recurring-kind">Тип</label><select className="field-input" disabled={mutation.isPending} id="recurring-kind" onChange={(event) => { setKind(event.currentTarget.value as Kind); setCategoryId(""); }} value={kind}><option value="expense">Расход</option><option value="income">Доход</option></select></div>
        <div className="field-stack"><label className="field-label" htmlFor="recurring-amount">Сумма</label><input autoComplete="off" className="field-input" disabled={mutation.isPending} id="recurring-amount" inputMode="decimal" onChange={(event) => setAmount(event.currentTarget.value)} placeholder="10 000,00" required type="text" value={amount} /></div>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="field-stack"><label className="field-label" htmlFor="recurring-account">Счёт</label><select className="field-input" disabled={accounts.isPending || mutation.isPending} id="recurring-account" onChange={(event) => setAccountId(event.currentTarget.value)} required value={accountId}><option value="">Выберите счёт</option>{accountMissing ? <option value={initial.account_id}>Недоступный счёт — выберите другой</option> : null}{accounts.data?.items.map((account) => <option key={account.id} value={account.id}>{account.name} · {account.currency}</option>)}</select></div>
        <div className="field-stack"><label className="field-label" htmlFor="recurring-category">Категория</label><select className="field-input" disabled={categories.isPending || mutation.isPending} id="recurring-category" onChange={(event) => setCategoryId(event.currentTarget.value)} required value={categoryId}><option value="">Выберите категорию</option>{categoryMissing ? <option value={initial.category_id}>Недоступная категория — выберите другую</option> : null}{categories.data?.items.map((category) => <option key={category.id} value={category.id}>{category.emoji} {category.name}</option>)}</select></div>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="field-stack"><label className="field-label" htmlFor="recurring-cadence">Периодичность</label><select className="field-input" disabled={mutation.isPending} id="recurring-cadence" onChange={(event) => { setCadence(event.currentTarget.value as Cadence); setInterval("1"); }} value={cadence}><option value="daily">Дни</option><option value="weekly">Недели</option><option value="monthly">Месяцы</option></select></div>
        <div className="field-stack"><label className="field-label" htmlFor="recurring-interval">Каждые</label><input className="field-input" disabled={mutation.isPending} id="recurring-interval" max={MAX_INTERVAL[cadence]} min={1} onChange={(event) => setInterval(event.currentTarget.value)} required type="number" value={interval} /></div>
      </div>
      <div className="grid gap-4 sm:grid-cols-3">
        <div className="field-stack"><label className="field-label" htmlFor="recurring-start">Начало</label><input className="field-input" disabled={mutation.isPending} id="recurring-start" onChange={(event) => setAnchorDate(event.currentTarget.value)} required type="date" value={anchorDate} /></div>
        <div className="field-stack"><label className="field-label" htmlFor="recurring-time">Время</label><input className="field-input" disabled={mutation.isPending} id="recurring-time" onChange={(event) => setLocalTime(event.currentTarget.value)} required step={60} type="time" value={localTime} /></div>
        <div className="field-stack"><label className="field-label" htmlFor="recurring-end">Окончание</label><input className="field-input" disabled={mutation.isPending} id="recurring-end" min={anchorDate} onChange={(event) => setEndsOn(event.currentTarget.value)} type="date" value={endsOn} /></div>
      </div>
      <div className="field-stack"><label className="field-label" htmlFor="recurring-description">Описание</label><input autoComplete="off" className="field-input" disabled={mutation.isPending} id="recurring-description" maxLength={500} onChange={(event) => setDescription(event.currentTarget.value)} type="text" value={description} /></div>
      {formError === undefined ? null : <p aria-live="polite" className="text-sm text-[var(--nm-danger)]" role="alert">{formError}</p>}
      <MutationFeedback error={mutation.error} onRetryUnknown={mutation.retryUnknown} outcomeUnknown={mutation.outcomeUnknown} pending={mutation.isPending} />
      <div className="flex flex-wrap gap-3 pt-2"><button className="button button--primary" disabled={mutation.isPending} type="submit">{mutation.isPending ? "Сохраняем…" : initial === undefined ? "Создать" : "Сохранить"}</button><Link className="button button--secondary" to={initial === undefined ? "/recurring" : `/recurring/${initial.id}`}>Отмена</Link></div>
      <p className="text-xs leading-relaxed text-[var(--nm-muted)]">Расписание использует часовой пояс {initial?.timezone ?? timeZone}. При переводе часов несуществующее время переносится вперёд.</p>
    </form>
  );
}

export function RecurringEditorPage({ scheduleId }: { readonly scheduleId?: string }) {
  const schedule = useQuery({
    queryKey: queryKeys.recurring.detail(scheduleId ?? "new"),
    queryFn: ({ signal }) => {
      if (scheduleId === undefined) throw new Error("Recurring schedule id is unavailable");
      return apiClient.get<RecurringSchedule>(`/api/v1/recurring-schedules/${scheduleId}`, { signal });
    },
    enabled: scheduleId !== undefined,
    staleTime: 0,
  });
  if (scheduleId !== undefined && schedule.isPending) return <PageSkeleton rows={6} />;
  if (scheduleId !== undefined && schedule.isError) return <ErrorState onAction={() => void schedule.refetch()} />;
  if (schedule.data?.state === "deleted") return <ErrorState actionLabel="Назад" description="Сначала восстановите расписание." onAction={() => window.history.back()} title="Редактирование недоступно" />;
  return <div className="recurring-editor-page page-stack"><PageHeading description="Операция не сохраняется автоматически: сначала появится черновик для проверки." eyebrow={scheduleId === undefined ? "Новое правило" : "Изменение расписания"} title={scheduleId === undefined ? "Создать расписание" : "Изменить расписание"} /><RecurringForm initial={schedule.data} key={schedule.data?.id ?? "new"} /></div>;
}
