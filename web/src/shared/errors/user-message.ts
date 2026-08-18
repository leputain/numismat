import { HttpApiError, MutationResultUnknownError, NetworkError } from "../../api/errors";

export function isUnknownMutationOutcome(error: unknown): boolean {
  return (
    error instanceof MutationResultUnknownError ||
    (error instanceof HttpApiError && error.code === "idempotency_in_progress")
  );
}

export function isOptimisticConflict(error: unknown): boolean {
  return (
    error instanceof HttpApiError &&
    (error.code === "draft_revision_conflict" || error.code === "object_version_conflict")
  );
}

export function userErrorMessage(error: unknown): string {
  if (error instanceof NetworkError) {
    return "Сеть недоступна. Повторите запрос после восстановления соединения.";
  }
  if (!(error instanceof HttpApiError)) {
    return "Сервис вернул неожиданный ответ. Данные не были сохранены в журнал браузера.";
  }
  switch (error.code) {
    case "active_draft_conflict":
      return "Уже есть другой активный черновик. Откройте его и выберите, как продолжить.";
    case "catalog_unavailable":
      return "Справочник временно недоступен или превысил безопасный лимит. Обновите данные.";
    case "budget_overlap":
      return "Для этой валюты и категории уже есть пересекающийся бюджет. Измените период.";
    case "draft_revision_conflict":
    case "object_version_conflict":
      return "Данные изменились в другом окне. Мы обновили экран — проверьте значения перед повтором.";
    case "invalid_state":
    case "review_required":
      return "Состояние черновика изменилось. Экран будет обновлён.";
    case "not_found":
      return "Запись больше недоступна.";
    case "validation_failed":
      return "Проверьте введённое значение и попробуйте снова.";
    case "idempotency_key_conflict":
      return "Запрос нельзя безопасно повторить. Обновите экран и выполните действие заново.";
    case "forbidden":
    case "origin_forbidden":
      return "Действие отклонено настройками безопасности.";
    default:
      return "Действие не выполнено. Обновите экран и попробуйте снова.";
  }
}
