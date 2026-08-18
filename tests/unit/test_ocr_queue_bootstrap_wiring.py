from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import Draft, TelegramResponseOutbox
from finbot.adapters.database.repositories.draft_presentations import (
    lock_telegram_draft_presentation_message,
)
from finbot.adapters.telegram.controllers.ocr_queue import (
    OcrQueueOperation,
    OcrQueueReceiptSnapshot,
)
from finbot.adapters.telegram.executor import TelegramMutationRequest
from finbot.adapters.telegram.presenters import money, wizard_summary
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    DraftSnapshot,
    OcrQueueMutationResult,
    OcrQueueStatus,
    OwnerSnapshot,
    TransactionSnapshot,
)
from finbot.application.interactions import DraftInteraction
from finbot.bootstrap import (
    _enqueue_ocr_queue_receipt,
    _ocr_queue_receipt,
)
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000201")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000301")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000401")
TRANSACTION_ID = UUID("00000000-0000-7000-8000-000000000501")
OCCURRED_AT = datetime(2026, 8, 13, 10, tzinfo=UTC)


def _owner() -> OwnerSnapshot:
    return OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", ACCOUNT_ID)


def _payload(*, amount_minor: int = 22_200) -> dict[str, object]:
    return {
        "flow": "ocr",
        "type": "expense",
        "amount_minor": amount_minor,
        "amount": 99_900,
        "account_id": str(ACCOUNT_ID),
        "account_name": "Счёт",
        "account_slug": "legacy-account",
        "category_id": str(CATEGORY_ID),
        "category_name": "Категория",
        "category_slug": "legacy-category",
        "category_emoji": "▫️",
        "occurred_at": OCCURRED_AT.isoformat(),
        "description": "",
        "ocr_batch": {
            "version": 1,
            "index": 2,
            "total": 3,
            "saved": 1,
            "skipped": 0,
            "remaining": [],
        },
    }


def _advanced(state: str, payload: dict[str, object]) -> OcrQueueMutationResult:
    return OcrQueueMutationResult(
        OcrQueueStatus.ADVANCED,
        saved=1,
        skipped=0,
        draft=DraftSnapshot(DRAFT_ID, state, payload, revision=4),
    )


def _receipt(
    operation: OcrQueueOperation,
    result: OcrQueueMutationResult,
    *,
    accounts: tuple[AccountSnapshot, ...] = (),
    categories: tuple[CategorySnapshot, ...] = (),
    message_id: int | None = 77,
) -> OcrQueueReceiptSnapshot:
    return OcrQueueReceiptSnapshot(
        operation=operation,
        expected=DraftSnapshot(DRAFT_ID, "review", _payload(), revision=3).ref,
        result=result,
        owner=_owner(),
        active_accounts=accounts,
        active_categories=categories,
        message_id=message_id,
    )


def test_canonical_amount_wins_for_rendering() -> None:
    payload = _payload(amount_minor=22_200)

    rendered = wizard_summary(payload, "RUB", "Europe/Moscow")

    assert money(22_200, "RUB") in rendered
    assert money(99_900, "RUB") not in rendered


@pytest.mark.parametrize(
    ("state", "accounts", "categories"),
    [
        ("review", (), ()),
        (
            "account_required",
            (AccountSnapshot(ACCOUNT_ID, "Счёт", "cash", "RUB", None, 2),),
            (),
        ),
        (
            "category_required",
            (),
            (
                CategorySnapshot(
                    CATEGORY_ID,
                    TransactionType.EXPENSE,
                    "Категория",
                    "▫️",
                    None,
                    3,
                ),
            ),
        ),
    ],
)
def test_advanced_receipt_builds_only_exact_versioned_callbacks(
    state: str,
    accounts: tuple[AccountSnapshot, ...],
    categories: tuple[CategorySnapshot, ...],
) -> None:
    receipt = _receipt(
        OcrQueueOperation.CONFIRMED,
        _advanced(state, _payload()),
        accounts=accounts,
        categories=categories,
    )

    rendered, next_draft = _ocr_queue_receipt(receipt)

    assert next_draft == receipt.result.draft.ref  # type: ignore[union-attr]
    assert rendered.reply_markup is not None
    callbacks = [
        button.callback_data
        for row in rendered.reply_markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]
    assert callbacks
    for callback_data in callbacks:
        interaction = DraftInteraction.decode(callback_data)
        assert interaction.draft_id == DRAFT_ID
        assert interaction.revision == 4


def test_terminal_receipts_have_no_draft_binding() -> None:
    transaction = TransactionSnapshot(
        TRANSACTION_ID,
        TransactionType.EXPENSE,
        22_200,
        "RUB",
        ACCOUNT_ID,
        "Счёт",
        CATEGORY_ID,
        "Категория",
        "▫️",
        OCCURRED_AT,
        "",
    )
    result = OcrQueueMutationResult(
        OcrQueueStatus.COMPLETED,
        saved=2,
        skipped=1,
        transaction=transaction,
    )

    rendered, next_draft = _ocr_queue_receipt(_receipt(OcrQueueOperation.CONFIRMED, result))

    assert next_draft is None
    assert "сохранено 2, пропущено 1" in rendered.text


class _CaptureSession:
    def __init__(self) -> None:
        self.added: TelegramResponseOutbox | None = None

    def add(self, value: object) -> None:
        self.added = cast(TelegramResponseOutbox, value)

    async def execute(self, _statement: object) -> object:
        class _EmptyResult:
            @staticmethod
            def one_or_none() -> None:
                return None

        return _EmptyResult()


@pytest.mark.asyncio
async def test_enqueuer_binds_only_advanced_snapshot() -> None:
    request = TelegramMutationRequest(91, 92, 93, "ru", "Europe/Moscow", "RUB")
    advanced_session = _CaptureSession()
    advanced = _receipt(OcrQueueOperation.SKIPPED, _advanced("review", _payload()))

    await _enqueue_ocr_queue_receipt(cast(AsyncSession, advanced_session), request, advanced)

    assert advanced_session.added is not None
    assert advanced_session.added.draft_id == DRAFT_ID
    assert advanced_session.added.draft_revision == 4

    terminal_session = _CaptureSession()
    terminal = _receipt(
        OcrQueueOperation.CANCELLED,
        OcrQueueMutationResult(OcrQueueStatus.CANCELLED, saved=1, skipped=1),
    )
    await _enqueue_ocr_queue_receipt(cast(AsyncSession, terminal_session), request, terminal)

    assert terminal_session.added is not None
    assert terminal_session.added.draft_id is None
    assert terminal_session.added.draft_revision is None


@pytest.mark.asyncio
async def test_ocr_guard_locks_owner_then_exact_draft_before_presentation_check() -> None:
    session = AsyncMock(spec=AsyncSession)
    draft = Draft(
        id=DRAFT_ID,
        user_id=OWNER_ID,
        state="review",
        payload={"ui_message_id": 66},
        revision=4,
        presentation_ref="55",
        suspended=False,
    )
    session.scalar.side_effect = [OWNER_ID, draft, 77]

    assert await lock_telegram_draft_presentation_message(
        session,
        OWNER_ID,
        DraftSnapshot(DRAFT_ID, "review", _payload(), revision=4).ref,
        77,
    )

    owner_statement, draft_statement, projection_statement = (
        call.args[0] for call in session.scalar.await_args_list
    )
    assert owner_statement._for_update_arg is not None
    assert draft_statement._for_update_arg is not None
    assert projection_statement._for_update_arg is None
    assert DRAFT_ID in projection_statement.compile().params.values()
    assert 4 in projection_statement.compile().params.values()


def test_wiring_dtos_do_not_expose_financial_or_catalog_values_in_repr() -> None:
    account = AccountSnapshot(ACCOUNT_ID, "Секретный счёт", "cash", "RUB", None, 1)
    receipt = _receipt(
        OcrQueueOperation.CONFIRMED,
        _advanced("account_required", _payload()),
        accounts=(account,),
    )

    rendered = repr(receipt)
    for secret in ("Секретный", "RUB", str(ACCOUNT_ID), "22200", "99900"):
        assert secret not in rendered
