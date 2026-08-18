from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import TelegramResponseOutbox
from finbot.adapters.telegram.controllers.draft_ingress import DraftIngressReceiptSnapshot
from finbot.adapters.telegram.controllers.draft_navigation import (
    DraftNavigationReceiptSnapshot,
)
from finbot.adapters.telegram.controllers.plain_drafts import (
    PlainDraftOperation,
    PlainDraftReceiptSnapshot,
)
from finbot.adapters.telegram.controllers.transaction_draft_navigation import (
    TransactionDraftNavigationReceiptSnapshot,
)
from finbot.adapters.telegram.controllers.transaction_draft_selection import (
    TransactionDraftSelectionReceiptSnapshot,
)
from finbot.adapters.telegram.executor import TelegramMutationRequest
from finbot.application.draft_ingress import (
    DraftIngressOperation,
    DraftIngressResult,
    DraftIngressStatus,
)
from finbot.application.draft_navigation import (
    DraftNavigationAction,
    DraftNavigationChoices,
    DraftNavigationResult,
    DraftNavigationStatus,
)
from finbot.application.dto import (
    CategorySnapshot,
    DraftRef,
    DraftSnapshot,
    OwnerSnapshot,
    TransactionSnapshot,
)
from finbot.application.interactions import DraftAction, DraftInteraction
from finbot.application.transaction_draft_navigation import (
    TransactionDraftNavigationAction,
    TransactionDraftNavigationChoices,
    TransactionDraftNavigationResult,
)
from finbot.application.transaction_draft_selection import (
    TransactionDraftSelectionAction,
    TransactionDraftSelectionResult,
    TransactionDraftSelectionStatus,
)
from finbot.bootstrap import (
    _CallbackMutationReceipt,
    _draft_ingress_receipt,
    _draft_navigation_receipt,
    _enqueue_draft_ingress_receipt,
    _enqueue_draft_navigation_receipt,
    _enqueue_plain_draft_receipt,
    _enqueue_transaction_draft_navigation_receipt,
    _enqueue_transaction_draft_selection_receipt,
    _plain_draft_receipt,
    _transaction_draft_navigation_receipt,
    _transaction_draft_selection_receipt,
)
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000201")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000301")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000401")


def _owner() -> OwnerSnapshot:
    return OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", ACCOUNT_ID)


def _payload() -> dict[str, object]:
    return {
        "flow": "quick",
        "type": "expense",
        "amount_minor": 12_300,
        "account_id": str(ACCOUNT_ID),
        "account_name": "Счёт",
        "category_id": str(CATEGORY_ID),
        "category_name": "Категория",
        "category_emoji": "▫️",
        "occurred_at": datetime(2026, 8, 13, 10, tzinfo=UTC).isoformat(),
        "description": "",
    }


def _receipt(
    state: str,
    *,
    action: DraftNavigationAction = DraftNavigationAction.BACK,
    choices: DraftNavigationChoices | None = None,
) -> DraftNavigationReceiptSnapshot:
    draft = DraftSnapshot(DRAFT_ID, state, _payload(), revision=8)
    return DraftNavigationReceiptSnapshot(
        expected=DraftRef(DRAFT_ID, 7),
        result=DraftNavigationResult(
            action,
            DraftNavigationStatus.UPDATED,
            _owner(),
            draft,
            choices or DraftNavigationChoices(),
        ),
        message_id=77,
    )


@pytest.mark.parametrize(
    "state",
    [
        "wizard_type",
        "wizard_amount",
        "review_amount",
        "review_type",
        "wizard_date",
        "custom_date",
        "review_date",
        "review_date_input",
        "custom_category",
        "custom_account",
        "wizard_description",
        "wizard_confirm",
        "quick_confirm",
        "review",
    ],
)
def test_navigation_renderer_uses_the_resulting_exact_draft_revision(state: str) -> None:
    rendered, next_draft = _draft_navigation_receipt(_receipt(state))

    assert next_draft == DraftRef(DRAFT_ID, 8)
    if rendered.reply_markup is not None:
        for row in rendered.reply_markup.inline_keyboard:
            for button in row:
                if button.callback_data and button.callback_data.startswith("d"):
                    interaction = DraftInteraction.decode(button.callback_data)
                    assert interaction.draft_id == DRAFT_ID
                    assert interaction.revision == 8


def test_navigation_renderer_uses_typed_category_choices() -> None:
    category = CategorySnapshot(
        CATEGORY_ID,
        TransactionType.EXPENSE,
        "Категория",
        "▫️",
        None,
        4,
    )
    receipt = _receipt(
        "review_category",
        action=DraftNavigationAction.EDIT_CATEGORY,
        choices=DraftNavigationChoices(categories=(category,)),
    )

    rendered, _ = _draft_navigation_receipt(receipt)

    assert rendered.reply_markup is not None
    callbacks = [
        button.callback_data
        for row in rendered.reply_markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]
    selected = next(value for value in callbacks if DraftInteraction.decode(value).object_id)
    interaction = DraftInteraction.decode(selected)
    assert interaction.object_id == CATEGORY_ID
    assert interaction.object_version == 4


@pytest.mark.parametrize(
    ("state", "prompt"),
    [
        ("custom_category", "Введите короткое название"),
        ("custom_account", "Введите название"),
        ("custom_date", "ДД.ММ"),
        ("review_date_input", "ДД.ММ"),
    ],
)
def test_navigation_input_states_render_a_text_prompt_not_another_choice_menu(
    state: str,
    prompt: str,
) -> None:
    rendered, _ = _draft_navigation_receipt(_receipt(state))

    assert prompt in rendered.text
    assert rendered.reply_markup is not None
    callbacks = [
        button.callback_data
        for row in rendered.reply_markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]
    assert callbacks
    assert all(
        DraftInteraction.decode(value).action.name in {"BACK", "CANCEL"} for value in callbacks
    )


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
async def test_navigation_enqueuer_binds_only_the_resulting_revision() -> None:
    session = _CaptureSession()
    request = TelegramMutationRequest(91, 92, 93, "ru", "Europe/Moscow", "RUB")

    await _enqueue_draft_navigation_receipt(
        cast(AsyncSession, session),
        request,
        _receipt("quick_confirm"),
    )

    assert session.added is not None
    assert session.added.draft_id == DRAFT_ID
    assert session.added.draft_revision == 8


def test_closed_navigation_has_no_draft_binding_or_finance_data_in_repr() -> None:
    receipt = DraftNavigationReceiptSnapshot(
        expected=DraftRef(DRAFT_ID, 7),
        result=DraftNavigationResult(
            DraftNavigationAction.BACK,
            DraftNavigationStatus.CLOSED,
            _owner(),
        ),
        message_id=77,
    )

    rendered, next_draft = _draft_navigation_receipt(receipt)

    assert next_draft is None
    assert "Быстрый ввод" in rendered.text
    for sensitive in (str(OWNER_ID), str(DRAFT_ID), "RUB", "12300"):
        assert sensitive not in repr(receipt)


def test_rendered_callback_receipt_hides_text_and_keyboard_from_repr() -> None:
    receipt = _CallbackMutationReceipt("private financial receipt", None)

    assert "private financial receipt" not in repr(receipt)


def _draft_ingress_snapshot(
    operation: DraftIngressOperation,
    status: DraftIngressStatus,
    *,
    message_id: int | None,
) -> DraftIngressReceiptSnapshot:
    if status is DraftIngressStatus.CONFLICT:
        payload: dict[str, object] = {
            "flow": "wizard",
            "pending_intent": {"kind": operation.value},
        }
        state = "wizard_amount"
        revision = 8
    elif operation is DraftIngressOperation.WIZARD:
        payload = {"flow": "wizard"}
        state = "wizard_type"
        revision = 1
    else:
        payload = {
            **_payload(),
            "flow": "repeat",
            "currency": "RUB",
            "source_transaction_id": str(UUID("00000000-0000-7000-8000-000000000501")),
            "source_version": 4,
        }
        state = "review"
        revision = 1
    return DraftIngressReceiptSnapshot(
        DraftIngressResult(
            operation,
            status,
            _owner(),
            DraftSnapshot(DRAFT_ID, state, payload, revision=revision),
        ),
        message_id,
    )


@pytest.mark.parametrize(
    ("operation", "status", "expected_text"),
    [
        (DraftIngressOperation.WIZARD, DraftIngressStatus.STARTED, "Новая операция"),
        (DraftIngressOperation.REPEAT, DraftIngressStatus.STARTED, "проверьте"),
        (DraftIngressOperation.WIZARD, DraftIngressStatus.CONFLICT, "незавершённый"),
    ],
)
def test_draft_ingress_renderer_uses_only_the_resulting_exact_reference(
    operation: DraftIngressOperation,
    status: DraftIngressStatus,
    expected_text: str,
) -> None:
    receipt = _draft_ingress_snapshot(operation, status, message_id=77)

    rendered, draft = _draft_ingress_receipt(receipt)

    assert expected_text.casefold() in rendered.text.casefold()
    assert draft == receipt.draft_ref
    assert "ui_message_id" not in receipt.result.draft.payload


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id", [None, 77])
async def test_draft_ingress_enqueuer_sends_or_edits_and_binds_exact_revision(
    message_id: int | None,
) -> None:
    session = _CaptureSession()
    receipt = _draft_ingress_snapshot(
        DraftIngressOperation.WIZARD,
        DraftIngressStatus.STARTED,
        message_id=message_id,
    )

    await _enqueue_draft_ingress_receipt(
        cast(AsyncSession, session),
        TelegramMutationRequest(91, 92, 93, "ru", "Europe/Moscow", "RUB"),
        receipt,
    )

    assert session.added is not None
    assert session.added.method == ("send_message" if message_id is None else "edit_message_text")
    assert session.added.message_id == message_id
    assert session.added.draft_id == receipt.draft_ref.draft_id
    assert session.added.draft_revision == receipt.draft_ref.revision


def _plain_receipt(operation: PlainDraftOperation) -> PlainDraftReceiptSnapshot:
    transaction = (
        TransactionSnapshot(
            UUID("00000000-0000-7000-8000-000000000501"),
            TransactionType.EXPENSE,
            12_300,
            "RUB",
            ACCOUNT_ID,
            "Счёт",
            CATEGORY_ID,
            "Категория",
            "▫️",
            datetime(2026, 8, 13, 10, tzinfo=UTC),
            "",
        )
        if operation is PlainDraftOperation.CONFIRMED
        else None
    )
    return PlainDraftReceiptSnapshot(
        operation,
        DraftRef(DRAFT_ID, 7),
        _owner(),
        77,
        transaction,
    )


def _transaction() -> TransactionSnapshot:
    return TransactionSnapshot(
        UUID("00000000-0000-7000-8000-000000000501"),
        TransactionType.EXPENSE,
        12_300,
        "RUB",
        ACCOUNT_ID,
        "Счёт",
        CATEGORY_ID,
        "Категория",
        "▫️",
        datetime(2026, 8, 13, 10, tzinfo=UTC),
        "",
        version=4,
    )


def _transaction_navigation_receipt(
    action: TransactionDraftNavigationAction,
) -> TransactionDraftNavigationReceiptSnapshot:
    state = {
        TransactionDraftNavigationAction.EDIT_TYPE: "edit_type",
        TransactionDraftNavigationAction.EDIT_AMOUNT: "edit_amount",
        TransactionDraftNavigationAction.EDIT_CATEGORY: "edit_category",
        TransactionDraftNavigationAction.EDIT_ACCOUNT: "edit_account",
        TransactionDraftNavigationAction.EDIT_DATE: "edit_date_menu",
        TransactionDraftNavigationAction.EDIT_DESCRIPTION: "edit_description",
        TransactionDraftNavigationAction.DATE_BACK: "edit_date_menu",
        TransactionDraftNavigationAction.BACK: "edit_menu",
    }[action]
    choices = TransactionDraftNavigationChoices()
    if action is TransactionDraftNavigationAction.EDIT_CATEGORY:
        choices = TransactionDraftNavigationChoices(
            categories=(
                CategorySnapshot(
                    CATEGORY_ID,
                    TransactionType.EXPENSE,
                    "Категория",
                    "▫️",
                    None,
                    3,
                ),
            )
        )
    return TransactionDraftNavigationReceiptSnapshot(
        DraftRef(DRAFT_ID, 7),
        TransactionDraftNavigationResult(
            action,
            _owner(),
            DraftSnapshot(DRAFT_ID, state, _payload(), revision=8),
            _transaction(),
            choices,
        ),
        77,
        2,
    )


@pytest.mark.parametrize(
    ("operation", "expected_text"),
    [
        (PlainDraftOperation.CONFIRMED, "Операция сохранена"),
        (PlainDraftOperation.CANCELLED, "Ввод отменён"),
    ],
)
def test_plain_draft_renderer_is_terminal_and_has_no_draft_callback(
    operation: PlainDraftOperation,
    expected_text: str,
) -> None:
    rendered = _plain_draft_receipt(_plain_receipt(operation))

    assert expected_text in rendered.text
    if rendered.reply_markup is not None:
        callbacks = [
            button.callback_data
            for row in rendered.reply_markup.inline_keyboard
            for button in row
            if button.callback_data is not None
        ]
        assert all(not value.startswith("d") for value in callbacks)


@pytest.mark.asyncio
async def test_plain_draft_enqueuer_never_binds_a_deleted_review_draft() -> None:
    session = _CaptureSession()

    await _enqueue_plain_draft_receipt(
        cast(AsyncSession, session),
        TelegramMutationRequest(91, 92, 93, "ru", "Europe/Moscow", "RUB"),
        _plain_receipt(PlainDraftOperation.CONFIRMED),
    )

    assert session.added is not None
    assert session.added.draft_id is None
    assert session.added.draft_revision is None


@pytest.mark.parametrize("action", list(TransactionDraftNavigationAction))
def test_transaction_navigation_renderer_uses_exact_resulting_draft(
    action: TransactionDraftNavigationAction,
) -> None:
    rendered, next_draft = _transaction_draft_navigation_receipt(
        _transaction_navigation_receipt(action)
    )

    assert next_draft == DraftRef(DRAFT_ID, 8)
    assert rendered.reply_markup is not None
    callbacks = [
        button.callback_data
        for row in rendered.reply_markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("d")
    ]
    assert callbacks
    for callback_data in callbacks:
        interaction = DraftInteraction.decode(callback_data)
        assert interaction.draft_id == DRAFT_ID
        assert interaction.revision == 8


@pytest.mark.asyncio
async def test_transaction_navigation_enqueuer_binds_resulting_revision() -> None:
    session = _CaptureSession()

    await _enqueue_transaction_draft_navigation_receipt(
        cast(AsyncSession, session),
        TelegramMutationRequest(91, 92, 93, "ru", "Europe/Moscow", "RUB"),
        _transaction_navigation_receipt(TransactionDraftNavigationAction.EDIT_DATE),
    )

    assert session.added is not None
    assert session.added.draft_id == DRAFT_ID
    assert session.added.draft_revision == 8


def test_transaction_edit_callbacks_keep_history_page_outside_the_draft_payload() -> None:
    amount_receipt = _transaction_navigation_receipt(TransactionDraftNavigationAction.EDIT_AMOUNT)
    rendered, _ = _transaction_draft_navigation_receipt(amount_receipt)

    assert "history_page" not in amount_receipt.result.draft.payload
    assert rendered.reply_markup is not None
    interaction = DraftInteraction.decode(
        next(
            button.callback_data
            for row in rendered.reply_markup.inline_keyboard
            for button in row
            if button.callback_data is not None
        )
    )
    assert interaction.action is DraftAction.TX_BACK
    assert interaction.page == 2

    date_rendered, _ = _transaction_draft_navigation_receipt(
        _transaction_navigation_receipt(TransactionDraftNavigationAction.EDIT_DATE)
    )
    assert date_rendered.reply_markup is not None
    decoded = [
        DraftInteraction.decode(button.callback_data)
        for row in date_rendered.reply_markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]
    choices = {item.page for item in decoded if item.action is DraftAction.TX_SELECT_DATE}
    assert choices == {6, 7, 8}
    assert next(item for item in decoded if item.action is DraftAction.TX_BACK).page == 2


def _transaction_selection_receipt(
    status: TransactionDraftSelectionStatus,
) -> TransactionDraftSelectionReceiptSnapshot:
    draft = (
        DraftSnapshot(DRAFT_ID, "edit_date", _payload(), revision=8)
        if status is TransactionDraftSelectionStatus.DATE_INPUT_REQUIRED
        else None
    )
    transaction = (
        _transaction() if status is TransactionDraftSelectionStatus.TRANSACTION_UPDATED else None
    )
    return TransactionDraftSelectionReceiptSnapshot(
        DraftRef(DRAFT_ID, 7),
        TransactionDraftSelectionResult(
            TransactionDraftSelectionAction.DATE,
            status,
            _owner(),
            draft,
            transaction,
        ),
        77,
        2,
    )


def test_transaction_custom_date_renderer_binds_input_revision() -> None:
    rendered, next_draft = _transaction_draft_selection_receipt(
        _transaction_selection_receipt(TransactionDraftSelectionStatus.DATE_INPUT_REQUIRED)
    )

    assert "ДД.ММ" in rendered.text
    assert next_draft == DraftRef(DRAFT_ID, 8)
    assert rendered.reply_markup is not None
    callbacks = [
        button.callback_data
        for row in rendered.reply_markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("d")
    ]
    assert callbacks
    assert all(DraftInteraction.decode(value).revision == 8 for value in callbacks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_draft"),
    [
        (TransactionDraftSelectionStatus.DATE_INPUT_REQUIRED, DRAFT_ID),
        (TransactionDraftSelectionStatus.TRANSACTION_UPDATED, None),
    ],
)
async def test_transaction_selection_enqueuer_binds_only_continuing_draft(
    status: TransactionDraftSelectionStatus,
    expected_draft: UUID | None,
) -> None:
    session = _CaptureSession()

    await _enqueue_transaction_draft_selection_receipt(
        cast(AsyncSession, session),
        TelegramMutationRequest(91, 92, 93, "ru", "Europe/Moscow", "RUB"),
        _transaction_selection_receipt(status),
    )

    assert session.added is not None
    assert session.added.draft_id == expected_draft
    assert session.added.draft_revision == (8 if expected_draft is not None else None)
