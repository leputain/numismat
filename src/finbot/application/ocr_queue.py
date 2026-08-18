from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from finbot.application.dto import OcrQueueCandidate
from finbot.application.errors import OcrQueueInvalidError
from finbot.application.ocr import MAX_OCR_TRANSACTIONS
from finbot.domain.transactions import TransactionType

OCR_QUEUE_SCHEMA_VERSION = 1
_QUEUE_KEY = "ocr_batch"
_SAFE_ERROR = "Очередь OCR повреждена"


def _invalid() -> OcrQueueInvalidError:
    return OcrQueueInvalidError(_SAFE_ERROR)


def _bounded_int(
    value: object,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise _invalid()
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        parsed = int(value)
    else:
        raise _invalid()
    if not minimum <= parsed <= maximum:
        raise _invalid()
    return parsed


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid()
    clean = value.strip()
    return clean or None


def serialize_ocr_candidate(candidate: OcrQueueCandidate) -> dict[str, object]:
    return {
        "amount_minor": candidate.amount_minor,
        "type": candidate.kind.value,
        "occurred_at": (
            candidate.occurred_at.isoformat() if candidate.occurred_at is not None else None
        ),
        "description": candidate.description,
        "needs_confirmation": candidate.needs_confirmation,
        "category_hint": candidate.category_hint,
        "category_explicit": candidate.category_explicit,
        "account_hint": candidate.account_hint,
    }


def deserialize_ocr_candidate(value: object) -> OcrQueueCandidate:
    if not isinstance(value, Mapping):
        raise _invalid()
    amount = _bounded_int(
        value.get("amount_minor", value.get("amount")),
        minimum=1,
        maximum=2**63 - 1,
    )
    description = value.get("description", "")
    occurred_raw = value.get("occurred_at")
    needs_confirmation = value.get("needs_confirmation", False)
    category_explicit = value.get("category_explicit", False)
    if not isinstance(description, str) or len(description) > 500:
        raise _invalid()
    if not isinstance(needs_confirmation, bool) or not isinstance(category_explicit, bool):
        raise _invalid()
    try:
        occurred_at = (
            datetime.fromisoformat(occurred_raw) if isinstance(occurred_raw, str) else None
        )
        if occurred_raw is not None and not isinstance(occurred_raw, str):
            raise _invalid()
        return OcrQueueCandidate(
            kind=TransactionType(str(value.get("type", ""))),
            amount_minor=amount,
            occurred_at=occurred_at,
            description=description,
            needs_confirmation=needs_confirmation,
            category_hint=_optional_string(value.get("category_hint")),
            category_explicit=category_explicit,
            account_hint=_optional_string(value.get("account_hint")),
        )
    except OcrQueueInvalidError:
        raise
    except TypeError, ValueError:
        raise _invalid() from None


@dataclass(frozen=True, slots=True)
class OcrQueueState:
    position: int
    total: int
    saved: int
    skipped: int
    remaining: tuple[OcrQueueCandidate, ...] = field(repr=False)
    version: int = OCR_QUEUE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.version != OCR_QUEUE_SCHEMA_VERSION:
            raise _invalid()
        if not 1 <= self.position <= self.total <= MAX_OCR_TRANSACTIONS:
            raise _invalid()
        if self.saved < 0 or self.skipped < 0:
            raise _invalid()
        if self.position != self.saved + self.skipped + 1:
            raise _invalid()
        if len(self.remaining) != self.total - self.position:
            raise _invalid()

    @classmethod
    def initial(cls, remaining: tuple[OcrQueueCandidate, ...]) -> OcrQueueState:
        return cls(
            position=1,
            total=1 + len(remaining),
            saved=0,
            skipped=0,
            remaining=remaining,
        )

    def consume(
        self,
        *,
        saved: bool,
    ) -> tuple[OcrQueueCandidate | None, OcrQueueState | None, int, int]:
        saved_count = self.saved + int(saved)
        skipped_count = self.skipped + int(not saved)
        if not self.remaining:
            return None, None, saved_count, skipped_count
        next_candidate = self.remaining[0]
        next_state = OcrQueueState(
            position=self.position + 1,
            total=self.total,
            saved=saved_count,
            skipped=skipped_count,
            remaining=self.remaining[1:],
        )
        return next_candidate, next_state, saved_count, skipped_count


def decode_ocr_queue(payload: Mapping[str, Any]) -> OcrQueueState:
    raw = payload.get(_QUEUE_KEY)
    if not isinstance(raw, Mapping):
        raise _invalid()
    remaining_raw = raw.get("remaining")
    if not isinstance(remaining_raw, list):
        raise _invalid()
    try:
        remaining = tuple(deserialize_ocr_candidate(item) for item in remaining_raw)
        return OcrQueueState(
            version=_bounded_int(
                raw.get("version"),
                minimum=OCR_QUEUE_SCHEMA_VERSION,
                maximum=OCR_QUEUE_SCHEMA_VERSION,
            ),
            position=_bounded_int(
                raw.get("index", raw.get("position")),
                minimum=1,
                maximum=MAX_OCR_TRANSACTIONS,
            ),
            total=_bounded_int(
                raw.get("total"),
                minimum=1,
                maximum=MAX_OCR_TRANSACTIONS,
            ),
            saved=_bounded_int(
                raw.get("saved"),
                minimum=0,
                maximum=MAX_OCR_TRANSACTIONS,
            ),
            skipped=_bounded_int(
                raw.get("skipped"),
                minimum=0,
                maximum=MAX_OCR_TRANSACTIONS,
            ),
            remaining=remaining,
        )
    except OcrQueueInvalidError:
        raise
    except TypeError, ValueError:
        raise _invalid() from None


def encode_ocr_queue(queue: OcrQueueState) -> dict[str, object]:
    return {
        "version": queue.version,
        "index": queue.position,
        "total": queue.total,
        "saved": queue.saved,
        "skipped": queue.skipped,
        "remaining": [serialize_ocr_candidate(candidate) for candidate in queue.remaining],
    }
