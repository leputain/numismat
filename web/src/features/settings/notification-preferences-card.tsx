import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { ErrorState, PageSkeleton } from "../../shared/components/async-state";
import { MutationFeedback } from "../../shared/components/mutation-feedback";
import { isOptimisticConflict } from "../../shared/errors/user-message";
import { usePreparedMutation } from "../../shared/mutations/prepared-mutation";
import { queryKeys } from "../../shared/queries/query-keys";
import {
  getNotificationPreferences,
  isLocalMinute,
  sameNotificationPreferences,
} from "./notification-preferences-contract";
import type {
  NotificationPreferencesRequest,
  NotificationPreferencesResponse,
} from "./notification-preferences-contract";

const WEEKDAYS = [
  "Понедельник",
  "Вторник",
  "Среда",
  "Четверг",
  "Пятница",
  "Суббота",
  "Воскресенье",
] as const;

interface NotificationForm {
  readonly budget80: boolean;
  readonly budget100: boolean;
  readonly recurringReady: boolean;
  readonly weeklyDigest: boolean;
  readonly quietEnabled: boolean;
  readonly quietStart: string;
  readonly quietEnd: string;
  readonly weeklyWeekday: number;
  readonly weeklyTime: string;
}

interface NotificationEditor {
  readonly base: NotificationPreferencesResponse;
  readonly form: NotificationForm;
}

function formFromSnapshot(snapshot: NotificationPreferencesResponse): NotificationForm {
  return {
    budget80: snapshot.budget_80_enabled,
    budget100: snapshot.budget_100_enabled,
    recurringReady: snapshot.recurring_ready_enabled,
    weeklyDigest: snapshot.weekly_digest_enabled,
    quietEnabled: snapshot.quiet_start !== null,
    quietStart: snapshot.quiet_start ?? "22:00",
    quietEnd: snapshot.quiet_end ?? "07:00",
    weeklyWeekday: snapshot.weekly_weekday,
    weeklyTime: snapshot.weekly_time,
  };
}

function editorFromSnapshot(snapshot: NotificationPreferencesResponse): NotificationEditor {
  return { base: snapshot, form: formFromSnapshot(snapshot) };
}

function requestFromForm(
  form: NotificationForm,
  version: number,
): NotificationPreferencesRequest | undefined {
  if (
    !Number.isInteger(version) ||
    version < 0 ||
    version >= 2 ** 31 - 1 ||
    !Number.isInteger(form.weeklyWeekday) ||
    form.weeklyWeekday < 0 ||
    form.weeklyWeekday > 6 ||
    !isLocalMinute(form.weeklyTime) ||
    (form.quietEnabled &&
      (!isLocalMinute(form.quietStart) ||
        !isLocalMinute(form.quietEnd) ||
        form.quietStart === form.quietEnd))
  ) {
    return undefined;
  }
  return {
    budget_80_enabled: form.budget80,
    budget_100_enabled: form.budget100,
    recurring_ready_enabled: form.recurringReady,
    weekly_digest_enabled: form.weeklyDigest,
    quiet_start: form.quietEnabled ? form.quietStart : null,
    quiet_end: form.quietEnabled ? form.quietEnd : null,
    weekly_weekday: form.weeklyWeekday,
    weekly_time: form.weeklyTime,
    version,
  };
}

function formMatchesSnapshot(
  form: NotificationForm,
  snapshot: NotificationPreferencesResponse,
): boolean {
  const request = requestFromForm(form, snapshot.version);
  return (
    request !== undefined &&
    sameNotificationPreferences({ ...snapshot, version: snapshot.version + 1 }, request)
  );
}

function SwitchField({
  checked,
  description,
  disabled,
  label,
  onChange,
}: {
  readonly checked: boolean;
  readonly description: string;
  readonly disabled: boolean;
  readonly label: string;
  readonly onChange: (checked: boolean) => void;
}) {
  return (
    <label className="settings-switch">
      <span className="settings-switch__copy">
        <span className="settings-switch__label">{label}</span>
        <span className="settings-switch__description">{description}</span>
      </span>
      <input
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.currentTarget.checked)}
        role="switch"
        type="checkbox"
      />
    </label>
  );
}

export function NotificationPreferencesCard() {
  const queryClient = useQueryClient();
  const preferences = useQuery({
    queryKey: queryKeys.settings.notificationPreferences,
    queryFn: ({ signal }) => getNotificationPreferences(signal),
  });
  const [editor, setEditor] = useState<NotificationEditor>();
  const [validationError, setValidationError] = useState<string>();
  const [saved, setSaved] = useState(false);
  const [confirmationPending, setConfirmationPending] =
    useState<NotificationPreferencesRequest>();

  const mutation = usePreparedMutation<void, NotificationPreferencesRequest>({
    eventScope: "settings",
    async onSuccess(_response, request) {
      const refreshed = await preferences.refetch();
      const confirmed =
        refreshed.data !== undefined && sameNotificationPreferences(refreshed.data, request);
      setSaved(confirmed);
      setConfirmationPending(confirmed ? undefined : request);
      if (refreshed.data !== undefined) {
        setEditor(editorFromSnapshot(refreshed.data));
      }
    },
    async onRejected(error) {
      setSaved(false);
      setConfirmationPending(undefined);
      if (isOptimisticConflict(error)) {
        const refreshed = await preferences.refetch();
        if (refreshed.data !== undefined) {
          setEditor(editorFromSnapshot(refreshed.data));
        }
      }
    },
    async onOutcomeUnknown() {
      setSaved(false);
      await queryClient.invalidateQueries({
        queryKey: queryKeys.settings.notificationPreferences,
      });
    },
  });

  useEffect(() => {
    if (
      preferences.data !== undefined &&
      (editor === undefined ||
        (!mutation.outcomeUnknown &&
          editor.base.version !== preferences.data.version &&
          formMatchesSnapshot(editor.form, editor.base)))
    ) {
      setEditor(editorFromSnapshot(preferences.data));
    }
  }, [editor, mutation.outcomeUnknown, preferences.data]);

  if (preferences.isError && preferences.data === undefined) {
    return (
      <section aria-label="Настройки уведомлений" className="surface-panel">
        <ErrorState onAction={() => void preferences.refetch()} />
      </section>
    );
  }
  if (preferences.isPending || preferences.data === undefined || editor === undefined) {
    return (
      <section aria-label="Настройки уведомлений" className="surface-panel">
        <PageSkeleton rows={2} />
      </section>
    );
  }

  const form = editor.form;
  const unchanged = formMatchesSnapshot(form, editor.base);
  const update = (patch: Partial<NotificationForm>) => {
    setEditor((current) =>
      current === undefined
        ? current
        : { ...current, form: { ...current.form, ...patch } },
    );
    setValidationError(undefined);
    setSaved(false);
    setConfirmationPending(undefined);
  };

  return (
    <form
      className="surface-panel field-stack"
      onSubmit={(event) => {
        event.preventDefault();
        const candidate = requestFromForm(form, editor.base.version);
        if (candidate === undefined) {
          setValidationError(
            "Проверьте время: используйте формат ЧЧ:ММ, а начало и конец тихих часов должны различаться.",
          );
          setSaved(false);
          return;
        }
        setValidationError(undefined);
        setSaved(false);
        setConfirmationPending(undefined);
        mutation.run({
          path: "/api/v1/settings/notifications",
          method: "PUT",
          body: candidate,
          context: candidate,
        });
      }}
    >
      <div>
        <p className="eyebrow">Уведомления</p>
        <h2 className="section-title">Только полезные сигналы</h2>
        <p className="page-description">
          Всё выключено по умолчанию. В сообщениях нет сумм и описаний операций.
        </p>
      </div>

      <div className="settings-switch-list">
        <SwitchField
          checked={form.budget80}
          description="Предупредить, когда расходы достигли 80% лимита."
          disabled={mutation.isPending}
          label="Бюджет: раннее предупреждение"
          onChange={(checked) => update({ budget80: checked })}
        />
        <SwitchField
          checked={form.budget100}
          description="Сообщить о достижении лимита бюджета."
          disabled={mutation.isPending}
          label="Бюджет: лимит достигнут"
          onChange={(checked) => update({ budget100: checked })}
        />
        <SwitchField
          checked={form.recurringReady}
          description="Напомнить проверить подготовленную регулярную операцию."
          disabled={mutation.isPending}
          label="Регулярная операция готова"
          onChange={(checked) => update({ recurringReady: checked })}
        />
        <SwitchField
          checked={form.weeklyDigest}
          description="Один короткий итог в выбранный день и время."
          disabled={mutation.isPending}
          label="Еженедельная сводка"
          onChange={(checked) => update({ weeklyDigest: checked })}
        />
      </div>

      <fieldset className="settings-subpanel">
        <legend className="sr-only">Расписание еженедельной сводки</legend>
        <div className="settings-field-grid">
          <div className="field-stack">
            <label className="field-label" htmlFor="notification-weekday">День сводки</label>
            <select
              className="field-input"
              disabled={!form.weeklyDigest || mutation.isPending}
              id="notification-weekday"
              onChange={(event) => update({ weeklyWeekday: Number(event.currentTarget.value) })}
              value={form.weeklyWeekday}
            >
              {WEEKDAYS.map((weekday, index) => (
                <option key={weekday} value={index}>{weekday}</option>
              ))}
            </select>
          </div>
          <div className="field-stack">
            <label className="field-label" htmlFor="notification-weekly-time">Время</label>
            <input
              className="field-input"
              disabled={!form.weeklyDigest || mutation.isPending}
              id="notification-weekly-time"
              onChange={(event) => update({ weeklyTime: event.currentTarget.value })}
              type="time"
              value={form.weeklyTime}
            />
          </div>
        </div>
      </fieldset>

      <div className="settings-subpanel field-stack">
        <SwitchField
          checked={form.quietEnabled}
          description="Сигналы дождутся окончания локального тихого периода."
          disabled={mutation.isPending}
          label="Тихие часы"
          onChange={(checked) => update({ quietEnabled: checked })}
        />
        <div className="settings-field-grid">
          <div className="field-stack">
            <label className="field-label" htmlFor="notification-quiet-start">С</label>
            <input
              className="field-input"
              disabled={!form.quietEnabled || mutation.isPending}
              id="notification-quiet-start"
              onChange={(event) => update({ quietStart: event.currentTarget.value })}
              type="time"
              value={form.quietStart}
            />
          </div>
          <div className="field-stack">
            <label className="field-label" htmlFor="notification-quiet-end">До</label>
            <input
              className="field-input"
              disabled={!form.quietEnabled || mutation.isPending}
              id="notification-quiet-end"
              onChange={(event) => update({ quietEnd: event.currentTarget.value })}
              type="time"
              value={form.quietEnd}
            />
          </div>
        </div>
      </div>

      {validationError === undefined ? null : (
        <p aria-live="polite" className="text-sm text-[var(--nm-danger)]" role="alert">
          {validationError}
        </p>
      )}
      {saved ? (
        <p aria-live="polite" className="notice notice--success" role="status">
          Настройки уведомлений сохранены и перечитаны с сервера.
        </p>
      ) : null}
      {!saved && confirmationPending !== undefined ? (
        <div aria-live="polite" className="notice notice--warning" role="status">
          <div>
            <p className="notice__title">Нужно перечитать настройки</p>
            <p className="notice__text">
              Запись подтверждена, но каноническая версия пока не получена.
            </p>
          </div>
          <button
            className="button button--secondary shrink-0"
            onClick={() => {
              void (async () => {
                const refreshed = await preferences.refetch();
                const confirmed =
                  refreshed.data !== undefined &&
                  sameNotificationPreferences(refreshed.data, confirmationPending);
                  setSaved(confirmed);
                  if (confirmed) {
                    setConfirmationPending(undefined);
                    setEditor(editorFromSnapshot(refreshed.data));
                  }
              })();
            }}
            type="button"
          >
            Перечитать
          </button>
        </div>
      ) : null}
      <MutationFeedback
        error={mutation.error}
        onRetryUnknown={mutation.retryUnknown}
        outcomeUnknown={mutation.outcomeUnknown}
        pending={mutation.isPending}
      />
      <div className="flex flex-wrap gap-3 pt-2">
        <button
          className="button button--primary"
          disabled={mutation.isPending || mutation.outcomeUnknown || unchanged}
          type="submit"
        >
          {mutation.isPending ? "Сохраняем…" : unchanged ? "Без изменений" : "Сохранить уведомления"}
        </button>
      </div>
    </form>
  );
}
