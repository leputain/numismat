import { userErrorMessage } from "../errors/user-message";

interface MutationFeedbackProps {
  readonly error: unknown;
  readonly outcomeUnknown: boolean;
  readonly pending: boolean;
  readonly onRetryUnknown: () => void;
}
export function MutationFeedback({
  error,
  outcomeUnknown,
  pending,
  onRetryUnknown,
}: MutationFeedbackProps) {
  if (outcomeUnknown) {
    return (
      <div aria-live="assertive" className="notice notice--warning" role="alert">
        <div>
          <p className="notice__title">Результат пока не подтверждён</p>
          <p className="notice__text">
            Не создавайте действие заново. Безопасный повтор использует тот же ключ и то же тело запроса.
          </p>
        </div>
        <button
          className="button button--secondary shrink-0"
          disabled={pending}
          onClick={onRetryUnknown}
          type="button"
        >
          {pending ? "Проверяем…" : "Повторить безопасно"}
        </button>
      </div>
    );
  }
  if (error === null || error === undefined) {
    return null;
  }
  return (
    <div aria-live="assertive" className="notice notice--danger" role="alert">
      <div>
        <p className="notice__title">Действие не выполнено</p>
        <p className="notice__text">{userErrorMessage(error)}</p>
      </div>
    </div>
  );
}
