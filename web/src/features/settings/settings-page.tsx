import { useEffect, useState } from "react";

import { useAuth } from "../auth/auth-context";
import type { AuthState } from "../auth/auth-coordinator";
import { MutationFeedback } from "../../shared/components/mutation-feedback";
import { PageHeading } from "../../shared/components/page-heading";
import { isOptimisticConflict } from "../../shared/errors/user-message";
import { emitClientEvent } from "../../shared/logging/client-events";
import { usePreparedMutation } from "../../shared/mutations/prepared-mutation";
import { NotificationPreferencesCard } from "./notification-preferences-card";

const TIMEZONE_PRESETS = [
  "Europe/Kaliningrad",
  "Europe/Moscow",
  "Europe/Samara",
  "Asia/Yekaterinburg",
  "Asia/Omsk",
  "Asia/Krasnoyarsk",
  "Asia/Irkutsk",
  "Asia/Yakutsk",
  "Asia/Vladivostok",
  "UTC",
] as const;

interface TimezoneSettingsRequest {
  readonly timezone: string;
  readonly version: number;
}

function isConfirmedTimezone(
  state: AuthState,
  request: TimezoneSettingsRequest,
): boolean {
  return (
    state.status === "authenticated" &&
    state.session.timezone === request.timezone &&
    state.session.settingsVersion === request.version + 1
  );
}

export function canonicalIanaTimezone(value: string): string | undefined {
  const candidate = value.trim();
  if (candidate.length < 1 || candidate.length > 64) {
    return undefined;
  }
  try {
    new Intl.DateTimeFormat("en", { timeZone: candidate }).format(0);
    return candidate;
  } catch {
    return undefined;
  }
}

export function SettingsPage() {
  const { refreshSession, state } = useAuth();
  const authenticatedTimezone =
    state.status === "authenticated" ? state.session.timezone : undefined;
  const [timezone, setTimezone] = useState(
    authenticatedTimezone ?? "UTC",
  );
  const [validationError, setValidationError] = useState<string>();
  const [saved, setSaved] = useState(false);
  const [confirmationPending, setConfirmationPending] = useState<TimezoneSettingsRequest>();

  useEffect(() => emitClientEvent("settings_opened"), []);
  useEffect(() => {
    if (authenticatedTimezone !== undefined) {
      setTimezone(authenticatedTimezone);
    }
  }, [authenticatedTimezone]);

  const mutation = usePreparedMutation<void, TimezoneSettingsRequest>({
    eventScope: "settings",
    async onSuccess(_response, request) {
      setConfirmationPending(request);
      const refreshed = await refreshSession();
      const confirmed = isConfirmedTimezone(refreshed, request);
      setSaved(confirmed);
      if (confirmed) {
        setConfirmationPending(undefined);
      }
    },
    async onRejected(error) {
      setSaved(false);
      setConfirmationPending(undefined);
      if (isOptimisticConflict(error)) {
        await refreshSession();
      }
    },
    async onOutcomeUnknown() {
      setSaved(false);
      await refreshSession();
    },
  });

  if (state.status !== "authenticated") {
    return null;
  }
  const session = state.session;
  const canonical = canonicalIanaTimezone(timezone);
  const unchanged = canonical === session.timezone;

  return (
    <div className="page-stack">
      <PageHeading
        description="Часовой пояс управляет границами дня и отчётов. Базовая валюта уже используется историей и меняется отдельно."
        eyebrow="Профиль учёта"
        title="Настройки"
      />

      <section aria-labelledby="settings-current" className="surface-panel">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Текущие значения</p>
            <h2 className="section-title" id="settings-current">Контекст отчётов</h2>
          </div>
        </div>
        <dl className="grid gap-4 sm:grid-cols-2" data-mobile-stack="true">
          <div>
            <dt className="field-label">Часовой пояс</dt>
            <dd className="mt-1 font-medium text-[var(--nm-text)]">{session.timezone}</dd>
          </div>
          <div>
            <dt className="field-label">Базовая валюта</dt>
            <dd className="mt-1 font-medium text-[var(--nm-text)]">{session.baseCurrency}</dd>
            <p className="mt-1 text-xs leading-relaxed text-[var(--nm-muted)]">
              Только просмотр: автоматическая смена могла бы переосмыслить старые отчёты.
            </p>
          </div>
        </dl>
      </section>

      <form
        className="surface-panel field-stack"
        onSubmit={(event) => {
          event.preventDefault();
          const selected = canonicalIanaTimezone(timezone);
          if (selected === undefined) {
            setValidationError("Введите существующий IANA timezone, например Europe/Moscow.");
            setSaved(false);
            return;
          }
          setValidationError(undefined);
          setSaved(false);
          setConfirmationPending(undefined);
          mutation.run({
            path: "/api/v1/settings/timezone",
            method: "PUT",
            body: { timezone: selected, version: session.settingsVersion },
            context: { timezone: selected, version: session.settingsVersion },
          });
        }}
      >
        <div>
          <p className="eyebrow">Локальное время</p>
          <h2 className="section-title">Изменить часовой пояс</h2>
          <p className="page-description">
            Выберите подсказку или введите точное имя из базы IANA.
          </p>
        </div>
        <div className="field-stack">
          <label className="field-label" htmlFor="settings-timezone">IANA timezone</label>
          <input
            autoCapitalize="none"
            autoComplete="off"
            className="field-input"
            disabled={mutation.isPending}
            id="settings-timezone"
            list="settings-timezone-presets"
            maxLength={64}
            onChange={(event) => {
              setTimezone(event.currentTarget.value);
              setValidationError(undefined);
              setSaved(false);
              setConfirmationPending(undefined);
            }}
            placeholder="Europe/Moscow"
            required
            spellCheck={false}
            type="text"
            value={timezone}
          />
          <datalist id="settings-timezone-presets">
            {TIMEZONE_PRESETS.map((item) => <option key={item} value={item} />)}
          </datalist>
          <p className="text-xs leading-relaxed text-[var(--nm-muted)]">
            Примеры: Europe/Moscow, Asia/Yekaterinburg, UTC.
          </p>
        </div>

        {validationError === undefined ? null : (
          <p aria-live="polite" className="text-sm text-[var(--nm-danger)]" role="alert">
            {validationError}
          </p>
        )}
        {saved ? (
          <p aria-live="polite" className="notice notice--success" role="status">
            Часовой пояс сохранён, данные сессии обновлены.
          </p>
        ) : null}
        {!saved && confirmationPending !== undefined ? (
          <div aria-live="polite" className="notice notice--warning" role="status">
            <div>
              <p className="notice__title">Сохранение подтверждено, чтение настроек — нет</p>
              <p className="notice__text">
                Не показываем устаревшее значение как сохранённое. Перечитайте профиль после
                восстановления связи.
              </p>
            </div>
            <button
              className="button button--secondary shrink-0"
              onClick={() => {
                void (async () => {
                  const refreshed = await refreshSession();
                  const confirmed = isConfirmedTimezone(refreshed, confirmationPending);
                  setSaved(confirmed);
                  if (confirmed) {
                    setConfirmationPending(undefined);
                  }
                })();
              }}
              type="button"
            >
              Перечитать профиль
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
            {mutation.isPending ? "Сохраняем…" : unchanged ? "Уже выбрано" : "Сохранить"}
          </button>
        </div>
      </form>

      <NotificationPreferencesCard />
    </div>
  );
}
