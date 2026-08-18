from enum import StrEnum
from typing import Any


class ApplicationErrorCode(StrEnum):
    """Stable adapter-facing error identifiers.

    Messages remain presentation concerns.  Metadata is deliberately limited to
    non-sensitive concurrency state that an adapter may need for recovery.
    """

    NOT_FOUND = "not_found"
    ACTIVE_DRAFT_CONFLICT = "active_draft_conflict"
    DRAFT_REVISION_CONFLICT = "draft_revision_conflict"
    OBJECT_VERSION_CONFLICT = "object_version_conflict"
    INVALID_STATE = "invalid_state"
    REVIEW_REQUIRED = "review_required"
    CATALOG_UNAVAILABLE = "catalog_unavailable"
    OCR_QUEUE_INVALID = "ocr_queue_invalid"
    OCR_PROCESSING_FAILED = "ocr_processing_failed"
    VALIDATION_FAILED = "validation_failed"
    DUPLICATE_OPERATION = "duplicate_operation"
    BUDGET_OVERLAP = "budget_overlap"


class ApplicationError(Exception):
    code = ApplicationErrorCode.VALIDATION_FAILED

    def __init__(self, message: str = "Операция не выполнена") -> None:
        super().__init__(message)

    @property
    def details(self) -> dict[str, Any]:
        return {}


class EntityNotFoundError(ApplicationError):
    code = ApplicationErrorCode.NOT_FOUND


class ApplicationValidationError(ApplicationError):
    code = ApplicationErrorCode.VALIDATION_FAILED


class ActiveDraftConflictError(ApplicationError):
    code = ApplicationErrorCode.ACTIVE_DRAFT_CONFLICT

    def __init__(self, *, current_revision: int) -> None:
        super().__init__("Уже есть активный черновик")
        self.current_revision = current_revision

    @property
    def details(self) -> dict[str, Any]:
        return {"current_revision": self.current_revision}


class DraftRevisionConflictError(ApplicationError):
    code = ApplicationErrorCode.DRAFT_REVISION_CONFLICT

    def __init__(self, *, current_revision: int | None = None) -> None:
        super().__init__("Черновик был изменён")
        self.current_revision = current_revision

    @property
    def details(self) -> dict[str, Any]:
        return (
            {"current_revision": self.current_revision} if self.current_revision is not None else {}
        )


class ObjectVersionConflictError(ApplicationError):
    code = ApplicationErrorCode.OBJECT_VERSION_CONFLICT

    def __init__(self, *, current_version: int | None = None) -> None:
        super().__init__("Объект был изменён")
        self.current_version = current_version

    @property
    def details(self) -> dict[str, Any]:
        return {"current_version": self.current_version} if self.current_version is not None else {}


class InvalidStateError(ApplicationError):
    code = ApplicationErrorCode.INVALID_STATE


class ReviewRequiredError(ApplicationError):
    code = ApplicationErrorCode.REVIEW_REQUIRED


class CatalogUnavailableError(ApplicationError):
    code = ApplicationErrorCode.CATALOG_UNAVAILABLE


class OcrQueueInvalidError(ApplicationError):
    code = ApplicationErrorCode.OCR_QUEUE_INVALID


class OcrProcessingError(ApplicationError):
    code = ApplicationErrorCode.OCR_PROCESSING_FAILED


class DuplicateOperationError(ApplicationError):
    code = ApplicationErrorCode.DUPLICATE_OPERATION


class BudgetOverlapError(ApplicationError):
    code = ApplicationErrorCode.BUDGET_OVERLAP
