from collections.abc import Mapping
from enum import StrEnum

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHttpException

from finbot.adapters.http.schemas.common import ApiError, ApiErrorResponse
from finbot.application.errors import ApplicationError, ApplicationErrorCode


class HttpErrorCode(StrEnum):
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    VALIDATION_FAILED = "validation_failed"
    READINESS_FAILED = "readiness_failed"
    INTERNAL_ERROR = "internal_error"
    TELEGRAM_AUTH_INVALID = "telegram_auth_invalid"
    TELEGRAM_AUTH_EXPIRED = "telegram_auth_expired"
    TELEGRAM_OWNER_FORBIDDEN = "telegram_owner_forbidden"
    TELEGRAM_AUTH_REPLAYED = "telegram_auth_replayed"
    AUTH_SESSION_INVALID = "auth_session_invalid"
    CSRF_FAILED = "csrf_failed"
    ORIGIN_FORBIDDEN = "origin_forbidden"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    REQUEST_TOO_LARGE = "request_too_large"
    INVALID_CURSOR = "invalid_cursor"
    IDEMPOTENCY_KEY_CONFLICT = "idempotency_key_conflict"
    IDEMPOTENCY_IN_PROGRESS = "idempotency_in_progress"


_APPLICATION_STATUS = {
    ApplicationErrorCode.NOT_FOUND: 404,
    ApplicationErrorCode.ACTIVE_DRAFT_CONFLICT: 409,
    ApplicationErrorCode.DRAFT_REVISION_CONFLICT: 409,
    ApplicationErrorCode.OBJECT_VERSION_CONFLICT: 409,
    ApplicationErrorCode.INVALID_STATE: 409,
    ApplicationErrorCode.REVIEW_REQUIRED: 409,
    ApplicationErrorCode.CATALOG_UNAVAILABLE: 409,
    ApplicationErrorCode.OCR_QUEUE_INVALID: 409,
    ApplicationErrorCode.OCR_PROCESSING_FAILED: 422,
    ApplicationErrorCode.VALIDATION_FAILED: 422,
    ApplicationErrorCode.DUPLICATE_OPERATION: 409,
    ApplicationErrorCode.BUDGET_OVERLAP: 409,
}
_APPLICATION_SAFE_MESSAGES: Mapping[ApplicationErrorCode, str] = {
    ApplicationErrorCode.NOT_FOUND: "Объект не найден",
    ApplicationErrorCode.ACTIVE_DRAFT_CONFLICT: "Уже есть активный черновик",
    ApplicationErrorCode.DRAFT_REVISION_CONFLICT: "Черновик был изменён",
    ApplicationErrorCode.OBJECT_VERSION_CONFLICT: "Объект был изменён",
    ApplicationErrorCode.INVALID_STATE: "Операция недоступна в текущем состоянии",
    ApplicationErrorCode.REVIEW_REQUIRED: "Требуется подтверждение черновика",
    ApplicationErrorCode.CATALOG_UNAVAILABLE: "Элемент справочника недоступен",
    ApplicationErrorCode.OCR_QUEUE_INVALID: "Пакет OCR недоступен",
    ApplicationErrorCode.OCR_PROCESSING_FAILED: "Изображение не удалось обработать",
    ApplicationErrorCode.VALIDATION_FAILED: "Запрос не прошёл проверку",
    ApplicationErrorCode.DUPLICATE_OPERATION: "Операция уже выполнена",
    ApplicationErrorCode.BUDGET_OVERLAP: "Период бюджета пересекается с существующим",
}
_HTTP_SAFE_MESSAGES: Mapping[HttpErrorCode, str] = {
    HttpErrorCode.UNAUTHORIZED: "Требуется аутентификация",
    HttpErrorCode.FORBIDDEN: "Доступ запрещён",
    HttpErrorCode.NOT_FOUND: "Маршрут не найден",
    HttpErrorCode.METHOD_NOT_ALLOWED: "Метод не поддерживается",
    HttpErrorCode.VALIDATION_FAILED: "Запрос не прошёл проверку",
    HttpErrorCode.READINESS_FAILED: "Сервис временно не готов",
    HttpErrorCode.INTERNAL_ERROR: "Внутренняя ошибка сервиса",
    HttpErrorCode.TELEGRAM_AUTH_INVALID: "Данные Telegram не прошли проверку",
    HttpErrorCode.TELEGRAM_AUTH_EXPIRED: "Данные Telegram устарели",
    HttpErrorCode.TELEGRAM_OWNER_FORBIDDEN: "Владелец Mini App не подтверждён",
    HttpErrorCode.TELEGRAM_AUTH_REPLAYED: "Данные Telegram уже были использованы",
    HttpErrorCode.AUTH_SESSION_INVALID: "Сессия недействительна",
    HttpErrorCode.CSRF_FAILED: "Проверка CSRF не пройдена",
    HttpErrorCode.ORIGIN_FORBIDDEN: "Источник запроса не разрешён",
    HttpErrorCode.UNSUPPORTED_MEDIA_TYPE: "Тип содержимого не поддерживается",
    HttpErrorCode.REQUEST_TOO_LARGE: "Запрос слишком большой",
    HttpErrorCode.INVALID_CURSOR: "Курсор недействителен",
    HttpErrorCode.IDEMPOTENCY_KEY_CONFLICT: "Ключ идемпотентности уже использован",
    HttpErrorCode.IDEMPOTENCY_IN_PROGRESS: "Операция с этим ключом ещё выполняется",
}
_SAFE_DETAIL_FIELDS = frozenset({"current_revision", "current_version"})


class HttpApiError(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        code: HttpErrorCode,
        clear_auth_cookies: bool = False,
    ) -> None:
        super().__init__(code.value)
        self.status_code = status_code
        self.code = code
        self.clear_auth_cookies = clear_auth_cookies


def _safe_details(error: ApplicationError) -> dict[str, int]:
    details: dict[str, int] = {}
    for key, value in error.details.items():
        if key in _SAFE_DETAIL_FIELDS and type(value) is int and 0 < value <= 2**31 - 1:
            details[key] = value
    return details


def _response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, int] | None = None,
) -> JSONResponse:
    request.state.http_error_code = code
    payload = ApiErrorResponse(
        error=ApiError(code=code, message=message, details=details or {})
    ).model_dump(mode="json")
    return JSONResponse(status_code=status_code, content=payload)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HttpApiError)
    async def handle_http_api_error(request: Request, error: HttpApiError) -> JSONResponse:
        response = _response(
            request,
            status_code=error.status_code,
            code=error.code.value,
            message=_HTTP_SAFE_MESSAGES[error.code],
        )
        if error.clear_auth_cookies:
            from finbot.adapters.http.auth.cookies import clear_auth_cookies

            clear_auth_cookies(response)
        return response

    @app.exception_handler(ApplicationError)
    async def handle_application_error(request: Request, error: ApplicationError) -> JSONResponse:
        return _response(
            request,
            status_code=_APPLICATION_STATUS[error.code],
            code=error.code.value,
            message=_APPLICATION_SAFE_MESSAGES[error.code],
            details=_safe_details(error),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        return _response(
            request,
            status_code=422,
            code=HttpErrorCode.VALIDATION_FAILED.value,
            message=_HTTP_SAFE_MESSAGES[HttpErrorCode.VALIDATION_FAILED],
        )

    @app.exception_handler(StarletteHttpException)
    async def handle_starlette_error(
        request: Request, error: StarletteHttpException
    ) -> JSONResponse:
        if error.status_code == 401:
            code = HttpErrorCode.UNAUTHORIZED
        elif error.status_code == 403:
            code = HttpErrorCode.FORBIDDEN
        elif error.status_code == 404:
            code = HttpErrorCode.NOT_FOUND
        elif error.status_code == 405:
            code = HttpErrorCode.METHOD_NOT_ALLOWED
        elif error.status_code >= 500:
            code = HttpErrorCode.INTERNAL_ERROR
        else:
            code = HttpErrorCode.VALIDATION_FAILED
        return _response(
            request,
            status_code=error.status_code,
            code=code.value,
            message=_HTTP_SAFE_MESSAGES[code],
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, _error: Exception) -> JSONResponse:
        return _response(
            request,
            status_code=500,
            code=HttpErrorCode.INTERNAL_ERROR.value,
            message=_HTTP_SAFE_MESSAGES[HttpErrorCode.INTERNAL_ERROR],
        )
