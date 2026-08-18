"""Pure codec for versioned Telegram draft interactions.

The wire format is deliberately compact because Telegram limits callback data to
64 bytes::

    d<action>.<draft_uuid>.<revision>[.p<page>][.o<object_uuid>.<version>]

UUIDs are unpadded base64url values.  Integers use canonical lower-case base36.
With every optional field present and every counter at its documented maximum,
the encoded value is 63 ASCII bytes.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

MAX_CALLBACK_BYTES = 64
MAX_REVISION = 36**4 - 1
MAX_OBJECT_VERSION = 36**4 - 1
MAX_PAGE = 36**2 - 1

_UUID_TOKEN_LENGTH = 22
_UUID_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
_BASE36_RE = re.compile(r"^[0-9a-z]+$")
_BASE36_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


class InteractionCodecError(ValueError):
    """Base error for invalid interaction data."""


class MalformedInteractionError(InteractionCodecError):
    """Raised when callback data is malformed or outside codec limits."""


class StaleInteractionError(InteractionCodecError):
    """Raised when a button targets an obsolete draft revision."""


class DraftAction(StrEnum):
    """Allowed actions for a transaction-draft interaction."""

    CONFIRM = "confirm"
    CANCEL = "cancel"
    BACK = "back"
    EDIT_TYPE = "edit_type"
    EDIT_AMOUNT = "edit_amount"
    EDIT_CATEGORY = "edit_category"
    EDIT_ACCOUNT = "edit_account"
    EDIT_DATE = "edit_date"
    EDIT_DESCRIPTION = "edit_description"
    SELECT_TYPE = "select_type"
    SELECT_CATEGORY = "select_category"
    SELECT_ACCOUNT = "select_account"
    SELECT_DATE = "select_date"
    SKIP_DESCRIPTION = "skip_description"
    CATEGORY_PAGE = "category_page"
    ACCOUNT_PAGE = "account_page"
    RESUME = "resume"
    REPLACE = "replace"
    KEEP = "keep"
    RULE_GLOBAL = "rule_global"
    RULE_ACCOUNT = "rule_account"
    RULE_REMOVE = "rule_remove"
    DISCARD = "discard"
    TX_EDIT_AMOUNT = "tx_edit_amount"
    TX_EDIT_CATEGORY = "tx_edit_category"
    TX_EDIT_ACCOUNT = "tx_edit_account"
    TX_EDIT_DATE = "tx_edit_date"
    TX_EDIT_DESCRIPTION = "tx_edit_description"
    TX_SELECT_CATEGORY = "tx_select_category"
    TX_SELECT_ACCOUNT = "tx_select_account"
    TX_SELECT_DATE = "tx_select_date"
    TX_DATE_BACK = "tx_date_back"
    TX_BACK = "tx_back"
    SKIP_OCR_ITEM = "skip_ocr_item"


_ACTION_TO_CODE: dict[DraftAction, str] = {
    DraftAction.CONFIRM: "0",
    DraftAction.CANCEL: "1",
    DraftAction.BACK: "2",
    DraftAction.EDIT_TYPE: "3",
    DraftAction.EDIT_AMOUNT: "4",
    DraftAction.EDIT_CATEGORY: "5",
    DraftAction.EDIT_ACCOUNT: "6",
    DraftAction.EDIT_DATE: "7",
    DraftAction.EDIT_DESCRIPTION: "8",
    DraftAction.SELECT_TYPE: "9",
    DraftAction.SELECT_CATEGORY: "a",
    DraftAction.SELECT_ACCOUNT: "b",
    DraftAction.SELECT_DATE: "c",
    DraftAction.SKIP_DESCRIPTION: "d",
    DraftAction.CATEGORY_PAGE: "e",
    DraftAction.ACCOUNT_PAGE: "f",
    DraftAction.RESUME: "g",
    DraftAction.REPLACE: "h",
    DraftAction.KEEP: "i",
    DraftAction.RULE_GLOBAL: "j",
    DraftAction.RULE_ACCOUNT: "k",
    DraftAction.RULE_REMOVE: "l",
    DraftAction.DISCARD: "m",
    DraftAction.TX_EDIT_AMOUNT: "n",
    DraftAction.TX_EDIT_CATEGORY: "o",
    DraftAction.TX_EDIT_ACCOUNT: "p",
    DraftAction.TX_EDIT_DATE: "q",
    DraftAction.TX_EDIT_DESCRIPTION: "r",
    DraftAction.TX_SELECT_CATEGORY: "s",
    DraftAction.TX_SELECT_ACCOUNT: "t",
    DraftAction.TX_SELECT_DATE: "u",
    DraftAction.TX_DATE_BACK: "v",
    DraftAction.TX_BACK: "w",
    DraftAction.SKIP_OCR_ITEM: "x",
}
_CODE_TO_ACTION = {code: action for action, code in _ACTION_TO_CODE.items()}


def encode_uuid(value: UUID) -> str:
    """Encode a UUID as 22 canonical unpadded base64url characters."""

    if not isinstance(value, UUID):
        raise TypeError("value must be a UUID")
    return base64.urlsafe_b64encode(value.bytes).rstrip(b"=").decode("ascii")


def decode_uuid(value: str) -> UUID:
    """Decode a canonical 22-character unpadded base64url UUID."""

    if not isinstance(value, str) or _UUID_TOKEN_RE.fullmatch(value) is None:
        raise MalformedInteractionError("invalid UUID token")
    try:
        raw = base64.b64decode(value + "==", altchars=b"-_", validate=True)
        decoded = UUID(bytes=raw)
    except (binascii.Error, ValueError) as exc:
        raise MalformedInteractionError("invalid UUID token") from exc
    if encode_uuid(decoded) != value:
        raise MalformedInteractionError("non-canonical UUID token")
    return decoded


def encode_base36(value: int) -> str:
    """Encode a non-negative integer using canonical lower-case base36."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("value must be an integer")
    if value < 0:
        raise ValueError("value must be non-negative")
    if value == 0:
        return "0"

    result: list[str] = []
    while value:
        value, remainder = divmod(value, 36)
        result.append(_BASE36_ALPHABET[remainder])
    return "".join(reversed(result))


def decode_base36(value: str) -> int:
    """Decode a canonical lower-case base36 non-negative integer."""

    if not isinstance(value, str) or _BASE36_RE.fullmatch(value) is None:
        raise MalformedInteractionError("invalid base36 token")
    if len(value) > 1 and value.startswith("0"):
        raise MalformedInteractionError("non-canonical base36 token")
    return int(value, 36)


@dataclass(frozen=True, slots=True)
class DraftInteraction:
    """Version-bound action produced by a transaction-draft screen."""

    action: DraftAction = field(repr=False)
    draft_id: UUID = field(repr=False)
    revision: int = field(repr=False)
    page: int | None = field(default=None, repr=False)
    object_id: UUID | None = field(default=None, repr=False)
    object_version: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.action, DraftAction):
            raise TypeError("action must be a DraftAction")
        if not isinstance(self.draft_id, UUID):
            raise TypeError("draft_id must be a UUID")
        _validate_bounded_int("revision", self.revision, minimum=1, maximum=MAX_REVISION)
        if self.page is not None:
            _validate_bounded_int("page", self.page, minimum=0, maximum=MAX_PAGE)
        if (self.object_id is None) != (self.object_version is None):
            raise ValueError("object_id and object_version must be provided together")
        if self.object_id is not None and not isinstance(self.object_id, UUID):
            raise TypeError("object_id must be a UUID")
        if self.object_version is not None:
            _validate_bounded_int(
                "object_version", self.object_version, minimum=1, maximum=MAX_OBJECT_VERSION
            )

    def encode(self) -> str:
        """Encode this interaction into callback data."""

        return encode_draft_interaction(self)

    def is_current(self, expected_draft_id: UUID, expected_revision: int) -> bool:
        """Return whether this interaction targets the expected draft revision."""

        return self.draft_id == expected_draft_id and self.revision == expected_revision

    def validate(self, expected_draft_id: UUID, expected_revision: int) -> bool:
        """Return true for the current draft, otherwise raise ``StaleInteractionError``."""

        if not self.is_current(expected_draft_id, expected_revision):
            raise StaleInteractionError("interaction targets a stale draft revision")
        return True

    @classmethod
    def decode(cls, value: str) -> DraftInteraction:
        """Decode callback data into a validated interaction."""

        return decode_draft_interaction(value)


def encode_draft_interaction(interaction: DraftInteraction) -> str:
    """Encode a validated draft interaction into at most 64 ASCII bytes."""

    if not isinstance(interaction, DraftInteraction):
        raise TypeError("interaction must be a DraftInteraction")

    parts = [
        f"d{_ACTION_TO_CODE[interaction.action]}",
        encode_uuid(interaction.draft_id),
        encode_base36(interaction.revision),
    ]
    if interaction.page is not None:
        parts.append(f"p{encode_base36(interaction.page)}")
    if interaction.object_id is not None and interaction.object_version is not None:
        parts.extend(
            (
                f"o{encode_uuid(interaction.object_id)}",
                encode_base36(interaction.object_version),
            )
        )

    encoded = ".".join(parts)
    try:
        encoded_length = len(encoded.encode("ascii"))
    except UnicodeEncodeError as exc:  # pragma: no cover - all components are constrained
        raise MalformedInteractionError("interaction must be ASCII") from exc
    if encoded_length > MAX_CALLBACK_BYTES:
        raise MalformedInteractionError("interaction exceeds callback size limit")
    return encoded


def decode_draft_interaction(value: str) -> DraftInteraction:
    """Strictly decode callback data, rejecting unknown or non-canonical forms."""

    if not isinstance(value, str):
        raise MalformedInteractionError("interaction must be text")
    try:
        encoded_length = len(value.encode("ascii"))
    except UnicodeEncodeError as exc:
        raise MalformedInteractionError("interaction must be ASCII") from exc
    if encoded_length > MAX_CALLBACK_BYTES:
        raise MalformedInteractionError("interaction exceeds callback size limit")

    parts = value.split(".")
    if len(parts) < 3 or len(parts[0]) != 2 or not parts[0].startswith("d"):
        raise MalformedInteractionError("invalid interaction prefix")
    action = _CODE_TO_ACTION.get(parts[0][1])
    if action is None:
        raise MalformedInteractionError("unknown interaction action")

    draft_id = decode_uuid(parts[1])
    revision = _decode_bounded_int(
        "revision", parts[2], minimum=1, maximum=MAX_REVISION, max_digits=4
    )

    page: int | None = None
    object_id: UUID | None = None
    object_version: int | None = None
    index = 3
    if index < len(parts) and parts[index].startswith("p"):
        page = _decode_bounded_int(
            "page", parts[index][1:], minimum=0, maximum=MAX_PAGE, max_digits=2
        )
        index += 1
    if index < len(parts) and parts[index].startswith("o"):
        object_id = decode_uuid(parts[index][1:])
        index += 1
        if index >= len(parts):
            raise MalformedInteractionError("missing object version")
        object_version = _decode_bounded_int(
            "object_version",
            parts[index],
            minimum=1,
            maximum=MAX_OBJECT_VERSION,
            max_digits=4,
        )
        index += 1
    if index != len(parts):
        raise MalformedInteractionError("unexpected interaction fields")

    interaction = DraftInteraction(
        action=action,
        draft_id=draft_id,
        revision=revision,
        page=page,
        object_id=object_id,
        object_version=object_version,
    )
    if encode_draft_interaction(interaction) != value:
        raise MalformedInteractionError("non-canonical interaction")
    return interaction


def _validate_bounded_int(name: str, value: int, *, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


def _decode_bounded_int(
    name: str,
    value: str,
    *,
    minimum: int,
    maximum: int,
    max_digits: int,
) -> int:
    if len(value) > max_digits:
        raise MalformedInteractionError(f"{name} token is too long")
    decoded = decode_base36(value)
    if not minimum <= decoded <= maximum:
        raise MalformedInteractionError(f"{name} is outside the allowed range")
    return decoded
