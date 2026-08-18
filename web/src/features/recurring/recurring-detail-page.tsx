import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { Link } from "react-router";

import { apiClient } from "../../app/providers";
import type {
  RecurringInstance,
  RecurringInstanceMutationResponse,
  RecurringInstancePageResponse,
  RecurringSchedule,
  RecurringScheduleMutationResponse,
  VersionedRecurringRequest,
} from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { ErrorState, PageSkeleton } from "../../shared/components/async-state";
import { MutationFeedback } from "../../shared/components/mutation-feedback";
import { PageHeading } from "../../shared/components/page-heading";
import { formatMoney } from "../../shared/finance/money";
import { emitClientEvent } from "../../shared/logging/client-events";
import { usePreparedMutation } from "../../shared/mutations/prepared-mutation";
import { restartRecurringPagination } from "../../shared/mutations/query-recovery";
import { queryKeys } from "../../shared/queries/query-keys";
import {
  formatNominalLocal,
  recurringCadenceLabel,
  recurringOutcomeLabel,
  recurringStateLabel,
} from "./recurring-format";

const INSTANCE_LIMIT = 20;
type ScheduleAction = "delete" | "pause" | "restore" | "resume";
interface InstanceAction {
  readonly action: "retry" | "skip";
  readonly instance: RecurringInstance;
}

function instancesPath(scheduleId: string, cursor: string | null): string {
  const params = new URLSearchParams({ limit: String(INSTANCE_LIMIT) });
  if (cursor !== null) {
    params.set("cursor", cursor);
  }
  return `/api/v1/recurring-schedules/${scheduleId}/instances?${params.toString()}`;
}

function InstanceRow({ instance, run, pending }: {
  readonly instance: RecurringInstance;
  readonly run: (action: InstanceAction) => void;
  readonly pending: boolean;
}) {
  return (
    <article className="rounded-2xl border border-white/8 bg-white/[0.025] p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="font-medium text-stone-200">{formatNominalLocal(instance.nominal_local)}</p>
          <p className="mt-1 text-xs text-stone-500">
            {recurringOutcomeLabel(instance.outcome)}
            {instance.dst_adjusted ? " · время сдвинуто из DST-разрыва" : ""}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {instance.transaction_id === null ? null : (
            <Link className="button button--ghost" to={`/transactions/${instance.transaction_id}`}>Операция</Link>
          )}
          {instance.draft_id === null ? null : (
            <Link className="button button--ghost" to="/draft">Проверить</Link>
          )}
          {instance.status === "blocked" ? (
            <button className="button button--secondary" disabled={pending} onClick={() => run({ action: "retry", instance })} type="button">Повторить</button>
          ) : null}
          {instance.status === "pending" || instance.status === "blocked" ? (
            <button className="button button--ghost" disabled={pending} onClick={() => run({ action: "skip", instance })} type="button">Пропустить</button>
          ) : null}
        </div>
      </div>
    </article>
  );
}

export function RecurringDetailPage({ scheduleId }: { readonly scheduleId: string }) {
  const queryClient = useQueryClient();
  const { locale } = useSessionFormat();
  const schedule = useQuery({
    queryKey: queryKeys.recurring.detail(scheduleId),
    queryFn: ({ signal }) => apiClient.get<RecurringSchedule>(`/api/v1/recurring-schedules/${scheduleId}`, { signal }),
    staleTime: 15_000,
  });
  const instances = useInfiniteQuery({
    queryKey: queryKeys.recurring.instances(scheduleId, INSTANCE_LIMIT),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      apiClient.get<RecurringInstancePageResponse>(instancesPath(scheduleId, pageParam), { signal }),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    staleTime: 15_000,
  });
  const lifecycle = usePreparedMutation<RecurringScheduleMutationResponse, ScheduleAction>({
    eventScope: "recurring",
    async onSuccess() {
      await restartRecurringPagination(queryClient);
      await queryClient.invalidateQueries({ queryKey: queryKeys.recurring.detail(scheduleId) });
    },
    async onRejected() {
      await queryClient.invalidateQueries({ queryKey: queryKeys.recurring.detail(scheduleId) });
    },
    onOutcomeUnknown() {},
  });
  const instanceMutation = usePreparedMutation<RecurringInstanceMutationResponse, InstanceAction>({
    eventScope: "recurring",
    async onSuccess() {
      await Promise.all([
        restartRecurringPagination(queryClient),
        queryClient.invalidateQueries({ queryKey: queryKeys.recurring.instances(scheduleId, INSTANCE_LIMIT) }),
      ]);
    },
    async onRejected() {
      await queryClient.invalidateQueries({ queryKey: queryKeys.recurring.instances(scheduleId, INSTANCE_LIMIT) });
    },
    onOutcomeUnknown() {},
  });

  useEffect(() => emitClientEvent("recurring_detail_opened"), []);

  if (schedule.isPending) {
    return <PageSkeleton rows={5} />;
  }
  if (schedule.isError) {
    return <ErrorState onAction={() => void schedule.refetch()} />;
  }
  const value = schedule.data;
  const runLifecycle = (action: ScheduleAction) => {
    const body: VersionedRecurringRequest = { version: value.version };
    lifecycle.run({ path: `/api/v1/recurring-schedules/${value.id}/${action}`, body, context: action });
  };
  const runInstance = ({ action, instance }: InstanceAction) => {
    const body: VersionedRecurringRequest = { version: instance.version };
    instanceMutation.run({
      path: `/api/v1/recurring-instances/${instance.id}/${action}`,
      body,
      context: { action, instance },
    });
  };
  const instanceItems = instances.data?.pages.flatMap((page) => page.items) ?? [];

  return (
    <div className="page-stack">
      <PageHeading
        action={value.state === "deleted" ? undefined : <Link className="button button--secondary" to={`/recurring/${value.id}/edit`}>Изменить</Link>}
        description="Часовой пояс зафиксирован при создании и не меняется вместе с настройками владельца."
        eyebrow={recurringStateLabel(value.state)}
        title={value.name}
      />
      <section className="surface-panel">
        <div className="grid gap-5 sm:grid-cols-2">
          <div><p className="eyebrow">Сумма</p><p className="mt-2 text-2xl font-semibold text-stone-100">{formatMoney(value.amount_minor, value.currency, locale)}</p></div>
          <div><p className="eyebrow">Следующий черновик</p><p className="mt-2 text-lg font-semibold text-stone-100">{formatNominalLocal(value.next_due_local, locale)}</p></div>
        </div>
        <dl className="mt-6 grid gap-4 text-sm sm:grid-cols-2">
          <div><dt className="text-stone-500">Правило</dt><dd className="mt-1 text-stone-200">{recurringCadenceLabel(value)}</dd></div>
          <div><dt className="text-stone-500">Локальное время</dt><dd className="mt-1 text-stone-200">{value.local_time} · {value.timezone}</dd></div>
          <div><dt className="text-stone-500">Тип</dt><dd className="mt-1 text-stone-200">{value.kind === "expense" ? "Расход" : "Доход"}</dd></div>
          <div><dt className="text-stone-500">До даты</dt><dd className="mt-1 text-stone-200">{value.ends_on ?? "Без ограничения"}</dd></div>
        </dl>
        {value.description.length === 0 ? null : <p className="mt-6 text-sm leading-relaxed text-stone-400">{value.description}</p>}
      </section>

      <MutationFeedback error={lifecycle.error} onRetryUnknown={lifecycle.retryUnknown} outcomeUnknown={lifecycle.outcomeUnknown} pending={lifecycle.isPending} />
      <div className="flex flex-wrap gap-3">
        <Link className="button button--secondary" to="/recurring">К списку</Link>
        {value.state === "active" ? <button className="button button--secondary" disabled={lifecycle.isPending} onClick={() => runLifecycle("pause")} type="button">Пауза</button> : null}
        {value.state === "paused" || value.state === "paused_error" ? <button className="button button--primary" disabled={lifecycle.isPending} onClick={() => runLifecycle("resume")} type="button">Возобновить</button> : null}
        <button className={value.state === "deleted" ? "button button--primary" : "button button--danger"} disabled={lifecycle.isPending} onClick={() => runLifecycle(value.state === "deleted" ? "restore" : "delete")} type="button">{value.state === "deleted" ? "Восстановить" : "Удалить"}</button>
      </div>

      <section className="space-y-3" aria-live="polite">
        <h2 className="text-lg font-semibold text-stone-100">Экземпляры</h2>
        <MutationFeedback error={instanceMutation.error} onRetryUnknown={instanceMutation.retryUnknown} outcomeUnknown={instanceMutation.outcomeUnknown} pending={instanceMutation.isPending} />
        {instances.isPending ? <PageSkeleton rows={3} /> : instances.isError ? <ErrorState onAction={() => void instances.refetch()} /> : instanceItems.length === 0 ? <p className="text-sm text-stone-500">Runner ещё не сформировал ни одного due-экземпляра.</p> : instanceItems.map((instance) => <InstanceRow instance={instance} key={instance.id} pending={instanceMutation.isPending} run={runInstance} />)}
        {instances.hasNextPage ? <button className="button button--secondary" disabled={instances.isFetchingNextPage} onClick={() => void instances.fetchNextPage()} type="button">{instances.isFetchingNextPage ? "Загружаем…" : "Показать ещё"}</button> : null}
      </section>
    </div>
  );
}
