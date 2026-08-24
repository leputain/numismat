from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from finbot.application.draft_conflicts import PENDING_DRAFT_INTENT_KEY
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    DraftRef,
    DraftSnapshot,
    OwnerSnapshot,
    UpdateDraftCommand,
)
from finbot.application.errors import (
    ApplicationValidationError,
    CatalogUnavailableError,
    DraftRevisionConflictError,
    InvalidStateError,
)
from finbot.application.finance_draft_text_input import (
    FINANCE_DRAFT_TEXT_INPUT_STATES,
    FinanceDraftTextInputAccountQuery,
    FinanceDraftTextInputCatalogPort,
    FinanceDraftTextInputCategoryQuery,
    FinanceDraftTextInputChoices,
    FinanceDraftTextInputClock,
    FinanceDraftTextInputCommand,
    FinanceDraftTextInputError,
    FinanceDraftTextInputNotApplicableError,
    FinanceDraftTextInputOwnerQuery,
    FinanceDraftTextInputResult,
    FinanceDraftTextInputStatus,
)
from finbot.application.rules import learning_candidates
from finbot.application.services.catalogs import normalize_catalog_name
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.domain.dates import parse_local_datetime
from finbot.domain.money import MoneyError, parse_minor, validate_minor
from finbot.domain.transactions import TransactionType

_REVIEW_RETURN_STATES = frozenset({"wizard_confirm", "quick_confirm", "review"})
_CATEGORY_BACK_STATES = frozenset(
    {"", "wizard_category", "quick_category", "category_required", "review_category"}
)
_ACCOUNT_BACK_STATES = frozenset(
    {"", "wizard_account", "quick_account", "account_required", "review_account"}
)


class SystemFinanceDraftTextInputClock:
    def now(self, timezone: str) -> datetime:
        return datetime.now(ZoneInfo(timezone))


def _require_current(current: DraftSnapshot | None, expected: DraftRef) -> DraftSnapshot:
    if current is None or current.ref != expected:
        raise DraftRevisionConflictError(
            current_revision=current.revision if current is not None else None
        )
    if current.suspended:
        raise FinanceDraftTextInputNotApplicableError("Черновик приостановлен")
    if current.state not in FINANCE_DRAFT_TEXT_INPUT_STATES:
        raise FinanceDraftTextInputNotApplicableError("Этот экран не принимает текстовый ввод")
    return current


def _payload_kind(payload: Mapping[str, Any]) -> TransactionType:
    try:
        return TransactionType(str(payload["type"]))
    except KeyError, ValueError:
        raise ApplicationValidationError("В черновике не указан тип операции") from None


def _canonicalize_amount(payload: dict[str, object]) -> None:
    amount = payload.get("amount_minor", payload.get("amount"))
    if amount is None:
        return
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise InvalidStateError("Черновик содержит некорректную сумму")
    try:
        payload["amount_minor"] = validate_minor(amount)
    except MoneyError:
        raise InvalidStateError("Черновик содержит некорректную сумму") from None
    payload.pop("amount", None)


def _clear_rule_learning(payload: dict[str, object]) -> None:
    payload.pop("rule_offer_pattern", None)
    payload.pop("pending_rule", None)


def _uses_guided_capture(payload: Mapping[str, Any]) -> bool:
    return str(payload.get("flow")) == "wizard" or (
        str(payload.get("flow")) == "quick" and str(payload.get("input_mode")) == "amount_only"
    )


def _attach_category(
    payload: dict[str, object],
    category: CategorySnapshot,
    kind: TransactionType,
) -> None:
    if category.archived_at is not None or category.kind is not kind:
        raise CatalogUnavailableError("Категория больше недоступна")
    payload.update(
        {
            "category_id": str(category.category_id),
            "category_name": category.name,
            "category_emoji": category.emoji,
        }
    )
    payload.pop("category_slug", None)


def _attach_account(payload: dict[str, object], account: AccountSnapshot) -> None:
    if account.archived_at is not None:
        raise CatalogUnavailableError("Счёт больше недоступен")
    if payload.get("account_id") is not None and str(payload["account_id"]) != str(
        account.account_id
    ):
        _clear_rule_learning(payload)
    payload.update(
        {
            "account_id": str(account.account_id),
            "account_name": account.name,
            "currency": account.currency,
        }
    )
    payload.pop("account_slug", None)


def _stage_rule_offer(payload: dict[str, object], previous_category_id: object) -> None:
    if str(payload.get("flow")) != "quick" or bool(payload.get("category_explicit")):
        _clear_rule_learning(payload)
        return
    if str(previous_category_id) == str(payload.get("category_id")):
        return
    candidates = learning_candidates(str(payload.get("description", "")))
    if candidates:
        payload["rule_offer_pattern"] = candidates[0]
    else:
        payload.pop("rule_offer_pattern", None)
    payload.pop("pending_rule", None)


def _validated_return_state(payload: Mapping[str, Any], key: str, default: str) -> str:
    state = str(payload.get(key, default))
    if state not in _REVIEW_RETURN_STATES:
        raise InvalidStateError("Черновик не содержит безопасного экрана возврата")
    return state


def _return_state(payload: dict[str, object], key: str, default: str) -> str:
    state = _validated_return_state(payload, key, default)
    payload.pop(key, None)
    return state


def _custom_back_state(
    payload: Mapping[str, Any],
    allowed: frozenset[str],
) -> str:
    state = str(payload.get("custom_back_state", ""))
    if state not in allowed:
        raise InvalidStateError("Черновик не содержит безопасного экрана возврата")
    return state


class FinanceDraftTextInputUseCase:
    """Advance one exact finance draft from bounded text without owning commit."""

    __slots__ = ("_accounts", "_catalogs", "_categories", "_clock", "_drafts", "_owners")

    def __init__(
        self,
        drafts: DraftUseCases,
        owners: FinanceDraftTextInputOwnerQuery,
        accounts: FinanceDraftTextInputAccountQuery,
        categories: FinanceDraftTextInputCategoryQuery,
        catalogs: FinanceDraftTextInputCatalogPort,
        clock: FinanceDraftTextInputClock | None = None,
    ) -> None:
        self._drafts = drafts
        self._owners = owners
        self._accounts = accounts
        self._categories = categories
        self._catalogs = catalogs
        self._clock = clock or SystemFinanceDraftTextInputClock()

    async def execute(
        self,
        command: FinanceDraftTextInputCommand,
    ) -> FinanceDraftTextInputResult:
        current = _require_current(
            await self._drafts.get_active(command.owner_id),
            command.expected,
        )
        if PENDING_DRAFT_INTENT_KEY in current.payload:
            raise InvalidStateError("Сначала выберите действие для незавершённого ввода")
        owner = await self._owners(command.owner_id)
        if owner.owner_id != command.owner_id:
            raise ApplicationValidationError("Контекст владельца повреждён")
        payload = dict(current.payload)
        _canonicalize_amount(payload)

        if current.state == "wizard_amount":
            return await self._amount(command, owner, current, payload, review=False)
        if current.state == "review_amount":
            return await self._amount(command, owner, current, payload, review=True)
        if current.state == "custom_category":
            return await self._custom_category(command, owner, current, payload)
        if current.state == "custom_account":
            return await self._custom_account(command, owner, current, payload)
        if current.state == "custom_date":
            return await self._date(command, owner, current, payload, review=False)
        if current.state == "review_date_input":
            return await self._date(command, owner, current, payload, review=True)
        return await self._description(command, owner, current, payload)

    async def _update(
        self,
        command: FinanceDraftTextInputCommand,
        owner: OwnerSnapshot,
        current: DraftSnapshot,
        state: str,
        payload: dict[str, object],
        *,
        choices: FinanceDraftTextInputChoices | None = None,
        retry_error: FinanceDraftTextInputError | None = None,
    ) -> FinanceDraftTextInputResult:
        updated = await self._drafts.update(
            UpdateDraftCommand(command.owner_id, current.ref, state, payload)
        )
        return FinanceDraftTextInputResult(
            status=(
                FinanceDraftTextInputStatus.RETRY
                if retry_error is not None
                else FinanceDraftTextInputStatus.UPDATED
            ),
            owner=owner,
            draft=updated,
            choices=choices or FinanceDraftTextInputChoices(),
            retry_error=retry_error,
        )

    async def _retry(
        self,
        command: FinanceDraftTextInputCommand,
        owner: OwnerSnapshot,
        current: DraftSnapshot,
        payload: dict[str, object],
        error: FinanceDraftTextInputError,
    ) -> FinanceDraftTextInputResult:
        return await self._update(
            command,
            owner,
            current,
            current.state,
            payload,
            retry_error=error,
        )

    async def _amount(
        self,
        command: FinanceDraftTextInputCommand,
        owner: OwnerSnapshot,
        current: DraftSnapshot,
        payload: dict[str, object],
        *,
        review: bool,
    ) -> FinanceDraftTextInputResult:
        try:
            payload["amount_minor"] = parse_minor(command.text)
        except MoneyError:
            return await self._retry(
                command,
                owner,
                current,
                payload,
                FinanceDraftTextInputError.INVALID_AMOUNT,
            )
        payload.pop("amount", None)
        if review:
            target = _return_state(payload, "review_return_state", "quick_confirm")
            return await self._update(command, owner, current, target, payload)
        kind = _payload_kind(payload)
        categories = tuple(await self._categories(command.owner_id, kind=kind))
        return await self._update(
            command,
            owner,
            current,
            "wizard_category",
            payload,
            choices=FinanceDraftTextInputChoices(categories=categories),
        )

    async def _custom_category(
        self,
        command: FinanceDraftTextInputCommand,
        owner: OwnerSnapshot,
        current: DraftSnapshot,
        payload: dict[str, object],
    ) -> FinanceDraftTextInputResult:
        try:
            name = normalize_catalog_name(command.text)
        except ValueError:
            return await self._retry(
                command,
                owner,
                current,
                payload,
                FinanceDraftTextInputError.INVALID_CATEGORY_NAME,
            )
        kind = _payload_kind(payload)
        back_state = _custom_back_state(payload, _CATEGORY_BACK_STATES)
        review_target = (
            _validated_return_state(payload, "review_return_state", "quick_confirm")
            if back_state == "review_category"
            else None
        )
        try:
            category = await self._catalogs.create_or_get_category(
                command.owner_id,
                name,
                kind,
            )
        except CatalogUnavailableError:
            return await self._retry(
                command,
                owner,
                current,
                payload,
                FinanceDraftTextInputError.CATEGORY_UNAVAILABLE,
            )
        previous_category_id = payload.get("category_id")
        payload.pop("custom_back_state", None)
        _attach_category(payload, category, kind)

        if back_state in {"review_category", "category_required"}:
            _stage_rule_offer(payload, previous_category_id)
            if back_state == "review_category":
                payload.pop("review_return_state", None)
                if review_target is None:  # pragma: no cover - guarded above
                    raise InvalidStateError("Черновик не содержит безопасного экрана возврата")
                return await self._update(command, owner, current, review_target, payload)

            account = await self._catalogs.resolve_account(
                command.owner_id,
                str(payload.get("account_hint", "")).strip() or None,
                owner.default_account_id,
            )
            if account is not None:
                _attach_account(payload, account)
                return await self._update(command, owner, current, "review", payload)
            accounts = tuple(await self._accounts(command.owner_id))
            return await self._update(
                command,
                owner,
                current,
                "account_required",
                payload,
                choices=FinanceDraftTextInputChoices(accounts=accounts),
            )

        accounts = tuple(await self._accounts(command.owner_id))
        target = "wizard_account" if _uses_guided_capture(payload) else "quick_account"
        return await self._update(
            command,
            owner,
            current,
            target,
            payload,
            choices=FinanceDraftTextInputChoices(accounts=accounts),
        )

    async def _custom_account(
        self,
        command: FinanceDraftTextInputCommand,
        owner: OwnerSnapshot,
        current: DraftSnapshot,
        payload: dict[str, object],
    ) -> FinanceDraftTextInputResult:
        try:
            name = normalize_catalog_name(command.text)
        except ValueError:
            return await self._retry(
                command,
                owner,
                current,
                payload,
                FinanceDraftTextInputError.INVALID_ACCOUNT_NAME,
            )
        back_state = _custom_back_state(payload, _ACCOUNT_BACK_STATES)
        review_target = (
            _validated_return_state(payload, "review_return_state", "quick_confirm")
            if back_state == "review_account"
            else None
        )
        try:
            account = await self._catalogs.create_or_get_account(
                command.owner_id,
                name,
                owner.base_currency,
            )
        except CatalogUnavailableError:
            return await self._retry(
                command,
                owner,
                current,
                payload,
                FinanceDraftTextInputError.ACCOUNT_UNAVAILABLE,
            )
        payload.pop("custom_back_state", None)
        _attach_account(payload, account)
        if back_state == "account_required":
            target = "review"
        elif back_state == "review_account":
            payload.pop("review_return_state", None)
            if review_target is None:  # pragma: no cover - guarded above
                raise InvalidStateError("Черновик не содержит безопасного экрана возврата")
            target = review_target
        elif _uses_guided_capture(payload):
            target = "wizard_date"
        else:
            target = "quick_confirm"
        return await self._update(command, owner, current, target, payload)

    async def _date(
        self,
        command: FinanceDraftTextInputCommand,
        owner: OwnerSnapshot,
        current: DraftSnapshot,
        payload: dict[str, object],
        *,
        review: bool,
    ) -> FinanceDraftTextInputResult:
        try:
            now = self._clock.now(owner.timezone)
            if now.utcoffset() is None:
                raise ApplicationValidationError("Текущее время должно содержать часовой пояс")
            occurred_at = parse_local_datetime(
                command.text,
                owner.timezone,
                now=now,
            )
        except ZoneInfoNotFoundError:
            raise ApplicationValidationError("Часовой пояс владельца не поддерживается") from None
        except ValueError:
            return await self._retry(
                command,
                owner,
                current,
                payload,
                FinanceDraftTextInputError.INVALID_DATE,
            )
        payload["occurred_at"] = occurred_at.isoformat()
        if review:
            target = _return_state(payload, "review_return_state", "quick_confirm")
            return await self._update(command, owner, current, target, payload)
        payload["return_state"] = "wizard_confirm"
        payload["description_back_state"] = "wizard_date"
        return await self._update(command, owner, current, "wizard_description", payload)

    async def _description(
        self,
        command: FinanceDraftTextInputCommand,
        owner: OwnerSnapshot,
        current: DraftSnapshot,
        payload: dict[str, object],
    ) -> FinanceDraftTextInputResult:
        description = command.text.strip()
        if len(description) > 500:
            return await self._retry(
                command,
                owner,
                current,
                payload,
                FinanceDraftTextInputError.INVALID_DESCRIPTION,
            )
        payload["description"] = "" if description == "-" else description
        _clear_rule_learning(payload)
        target = _return_state(payload, "return_state", "wizard_confirm")
        payload.pop("description_back_state", None)
        return await self._update(command, owner, current, target, payload)
