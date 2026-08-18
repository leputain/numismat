import ast
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User

from finbot.adapters.telegram.controllers.draft_completion import (
    DraftCompletionController,
    DraftCompletionKind,
)
from finbot.adapters.telegram.controllers.draft_conflicts import (
    DraftConflictController,
    DraftConflictReceiptSnapshot,
)
from finbot.adapters.telegram.controllers.draft_navigation import (
    DraftNavigationController,
    DraftNavigationReceiptSnapshot,
)
from finbot.adapters.telegram.controllers.draft_rules import (
    DraftRuleController,
    DraftRuleReceiptSnapshot,
)
from finbot.adapters.telegram.controllers.drafts import (
    DraftController,
    DraftSettingsReceiptSnapshot,
)
from finbot.adapters.telegram.controllers.ocr_queue import OcrQueueOperation
from finbot.adapters.telegram.controllers.transaction_draft_navigation import (
    TransactionDraftNavigationController,
    TransactionDraftNavigationReceiptSnapshot,
)
from finbot.adapters.telegram.controllers.transaction_draft_selection import (
    TransactionDraftSelectionController,
    TransactionDraftSelectionReceiptSnapshot,
)
from finbot.adapters.telegram.routers.draft_interactions import (
    DraftInteractionContexts,
    DraftInteractionControllers,
    DraftInteractionDelivery,
    DraftInteractionRenderers,
    DraftInteractionRouter,
)
from finbot.application.draft_navigation import (
    DraftCatalogChoice,
    DraftCatalogRef,
    DraftDateChoice,
    DraftNavigationAction,
)
from finbot.application.draft_rules import DraftRuleAction
from finbot.application.dto import DraftConflictResolution, DraftRef, OcrQueueStatus
from finbot.application.errors import (
    ApplicationValidationError,
    DraftRevisionConflictError,
)
from finbot.application.interactions import DraftAction, DraftInteraction
from finbot.application.transaction_draft_navigation import (
    TransactionDraftNavigationAction,
)
from finbot.application.transaction_draft_selection import (
    TransactionDraftSelectionAction,
    TransactionDraftSelectionStatus,
)
from finbot.domain.transactions import TransactionType

DRAFT_ID = UUID("00000000-0000-7000-8000-000000000401")
OBJECT_ID = UUID("00000000-0000-7000-8000-000000000402")
NEXT_DRAFT = DraftRef(DRAFT_ID, 4)


def _controller_result(receipts: dict[str, object | None], family: str) -> object | None:
    result = receipts[family]
    if isinstance(result, BaseException):
        raise result
    return result


def _message() -> Message:
    return Message(
        message_id=700,
        date=datetime(2026, 8, 13, tzinfo=UTC),
        chat=Chat(id=800, type="private"),
        text="safe",
    )


def _callback(interaction: DraftInteraction | str) -> CallbackQuery:
    data = interaction if isinstance(interaction, str) else interaction.encode()
    return CallbackQuery(
        id="opaque",
        from_user=User(id=900, is_bot=False, first_name="Owner"),
        chat_instance="private",
        message=_message(),
        data=data,
    )


def _interaction(
    action: DraftAction,
    *,
    page: int | None = None,
    object_id: UUID | None = None,
    object_version: int | None = None,
) -> DraftInteraction:
    return DraftInteraction(
        action,
        DRAFT_ID,
        3,
        page=page,
        object_id=object_id,
        object_version=object_version,
    )


class _Drafts:
    def __init__(self, events: list[object], receipts: dict[str, object | None]) -> None:
        self.events = events
        self.receipts = receipts

    async def discard(self, context: object, expected: DraftRef) -> object | None:
        self.events.append(("controller", "drafts", context, expected))
        return _controller_result(self.receipts, "drafts")


class _Rules:
    def __init__(self, events: list[object], receipts: dict[str, object | None]) -> None:
        self.events = events
        self.receipts = receipts

    async def execute(
        self,
        context: object,
        expected: DraftRef,
        action: DraftRuleAction,
    ) -> object | None:
        self.events.append(("controller", "rules", context, expected, action))
        return _controller_result(self.receipts, "rules")


class _Navigation:
    def __init__(self, events: list[object], receipts: dict[str, object | None]) -> None:
        self.events = events
        self.receipts = receipts

    async def execute(
        self,
        context: object,
        expected: DraftRef,
        action: DraftNavigationAction,
        choice: object,
    ) -> object | None:
        self.events.append(("controller", "navigation", context, expected, action, choice))
        return _controller_result(self.receipts, "navigation")


class _TransactionNavigation:
    def __init__(self, events: list[object], receipts: dict[str, object | None]) -> None:
        self.events = events
        self.receipts = receipts

    async def execute(
        self,
        context: object,
        expected: DraftRef,
        action: TransactionDraftNavigationAction,
        history_page: int,
    ) -> object | None:
        self.events.append(
            (
                "controller",
                "transaction_navigation",
                context,
                expected,
                action,
                history_page,
            )
        )
        return _controller_result(self.receipts, "transaction_navigation")


class _TransactionSelection:
    def __init__(self, events: list[object], receipts: dict[str, object | None]) -> None:
        self.events = events
        self.receipts = receipts

    async def execute(
        self,
        context: object,
        expected: DraftRef,
        action: TransactionDraftSelectionAction,
        choice: object,
        history_page: int,
    ) -> object | None:
        self.events.append(
            (
                "controller",
                "transaction_selection",
                context,
                expected,
                action,
                choice,
                history_page,
            )
        )
        return _controller_result(self.receipts, "transaction_selection")


class _Completion:
    def __init__(self, events: list[object], receipts: dict[str, object | None]) -> None:
        self.events = events
        self.receipts = receipts

    async def _execute(
        self,
        name: str,
        context: object,
        expected: DraftRef,
    ) -> object | None:
        self.events.append(("controller", name, context, expected))
        return _controller_result(self.receipts, "completion")

    async def confirm(self, context: object, expected: DraftRef) -> object | None:
        return await self._execute("confirm", context, expected)

    async def cancel(self, context: object, expected: DraftRef) -> object | None:
        return await self._execute("cancel", context, expected)

    async def skip_ocr_item(self, context: object, expected: DraftRef) -> object | None:
        return await self._execute("skip_ocr_item", context, expected)


class _Conflicts:
    def __init__(self, events: list[object], receipts: dict[str, object | None]) -> None:
        self.events = events
        self.receipts = receipts

    async def resolve(
        self,
        context: object,
        expected: DraftRef,
        resolution: DraftConflictResolution,
    ) -> object | None:
        self.events.append(("controller", "conflicts", context, expected, resolution))
        return _controller_result(self.receipts, "conflicts")


class _Rendered:
    def __init__(self, text: str) -> None:
        self.text = text
        self.reply_markup = None


def _plain_completion() -> object:
    return SimpleNamespace(
        kind=DraftCompletionKind.PLAIN,
        plain=object(),
        ocr_queue=None,
    )


def _ocr_completion(
    operation: OcrQueueOperation = OcrQueueOperation.CONFIRMED,
    status: OcrQueueStatus = OcrQueueStatus.ADVANCED,
) -> object:
    return SimpleNamespace(
        kind=DraftCompletionKind.OCR_QUEUE,
        plain=None,
        ocr_queue=SimpleNamespace(
            operation=operation,
            result=SimpleNamespace(status=status),
        ),
    )


def _default_receipts() -> dict[str, object | None]:
    return {
        "drafts": object(),
        "rules": object(),
        "navigation": object(),
        "transaction_navigation": SimpleNamespace(history_page=5),
        "transaction_selection": SimpleNamespace(
            history_page=5,
            result=SimpleNamespace(status=TransactionDraftSelectionStatus.TRANSACTION_UPDATED),
        ),
        "completion": _plain_completion(),
        "conflicts": SimpleNamespace(history_page=5),
    }


def _context_factory(name: str, events: list[object]) -> Any:
    def context(update_id: int | None, message: Message) -> object:
        token = object()
        events.append(("context", name, update_id, message.message_id, token))
        return token

    return context


def _router(
    events: list[object],
    receipts: dict[str, object | None] | None = None,
) -> tuple[DraftInteractionRouter, dict[str, object | None]]:
    actual_receipts = receipts or _default_receipts()
    controllers = DraftInteractionControllers(
        cast(DraftController, _Drafts(events, actual_receipts)),
        cast(DraftRuleController, _Rules(events, actual_receipts)),
        cast(DraftNavigationController, _Navigation(events, actual_receipts)),
        cast(
            TransactionDraftNavigationController,
            _TransactionNavigation(events, actual_receipts),
        ),
        cast(
            TransactionDraftSelectionController,
            _TransactionSelection(events, actual_receipts),
        ),
        cast(DraftCompletionController, _Completion(events, actual_receipts)),
        cast(DraftConflictController, _Conflicts(events, actual_receipts)),
    )
    contexts = DraftInteractionContexts(
        cast(Any, _context_factory("drafts", events)),
        cast(Any, _context_factory("rules", events)),
        cast(Any, _context_factory("navigation", events)),
        cast(Any, _context_factory("transaction_navigation", events)),
        cast(Any, _context_factory("transaction_selection", events)),
        cast(Any, _context_factory("completion", events)),
        cast(Any, _context_factory("conflicts", events)),
    )

    def draft_renderer(receipt: DraftSettingsReceiptSnapshot) -> _Rendered:
        events.append(("render", "drafts", receipt))
        return _Rendered("drafts")

    def rule_renderer(
        receipt: DraftRuleReceiptSnapshot,
    ) -> tuple[_Rendered, DraftRef]:
        events.append(("render", "rules", receipt))
        return _Rendered("rules"), NEXT_DRAFT

    def navigation_renderer(
        receipt: DraftNavigationReceiptSnapshot,
    ) -> tuple[_Rendered, DraftRef]:
        events.append(("render", "navigation", receipt))
        return _Rendered("navigation"), NEXT_DRAFT

    def transaction_navigation_renderer(
        receipt: TransactionDraftNavigationReceiptSnapshot,
    ) -> tuple[_Rendered, DraftRef]:
        events.append(("render", "transaction_navigation", receipt))
        return _Rendered("transaction_navigation"), NEXT_DRAFT

    def transaction_selection_renderer(
        receipt: TransactionDraftSelectionReceiptSnapshot,
    ) -> tuple[_Rendered, DraftRef]:
        events.append(("render", "transaction_selection", receipt))
        return _Rendered("transaction_selection"), NEXT_DRAFT

    def plain_renderer(receipt: object) -> _Rendered:
        events.append(("render", "plain_completion", receipt))
        return _Rendered("plain_completion")

    def ocr_renderer(receipt: object) -> tuple[_Rendered, DraftRef]:
        events.append(("render", "ocr_completion", receipt))
        return _Rendered("ocr_completion"), NEXT_DRAFT

    def conflict_renderer(receipt: DraftConflictReceiptSnapshot) -> tuple[_Rendered, DraftRef]:
        events.append(("render", "conflicts", receipt))
        return _Rendered("conflicts"), NEXT_DRAFT

    renderers = DraftInteractionRenderers(
        draft_renderer,
        rule_renderer,
        navigation_renderer,
        transaction_navigation_renderer,
        transaction_selection_renderer,
        cast(Any, plain_renderer),
        cast(Any, ocr_renderer),
        conflict_renderer,
    )

    async def replace(message: Message, text: str, reply_markup: object) -> int:
        events.append(("replace", message.message_id, text, reply_markup))
        return 701

    async def bind(
        message: Message,
        draft: DraftRef,
        delivered_message_id: int,
        *,
        history_page: int | None = None,
        override_context: bool = False,
    ) -> None:
        events.append(
            (
                "bind",
                message.chat.id,
                draft,
                delivered_message_id,
                history_page,
                override_context,
            )
        )

    delivery = DraftInteractionDelivery(cast(Any, replace), bind)
    return DraftInteractionRouter(controllers, contexts, renderers, delivery), actual_receipts


def _record_answers(monkeypatch: pytest.MonkeyPatch, events: list[object]) -> None:
    async def answer(
        _callback: CallbackQuery,
        text: str | None = None,
        *,
        show_alert: bool | None = None,
        **_kwargs: object,
    ) -> bool:
        events.append(("answer", text, show_alert))
        return True

    monkeypatch.setattr(CallbackQuery, "answer", answer)


def _controller_event(events: list[object]) -> tuple[Any, ...]:
    return cast(tuple[Any, ...], next(event for event in events if event[0] == "controller"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (DraftAction.RULE_GLOBAL, DraftRuleAction.GLOBAL),
        (DraftAction.RULE_ACCOUNT, DraftRuleAction.ACCOUNT),
        (DraftAction.RULE_REMOVE, DraftRuleAction.REMOVE),
    ],
)
async def test_every_rule_action_maps_to_the_exact_controller_action(
    monkeypatch: pytest.MonkeyPatch,
    action: DraftAction,
    expected: DraftRuleAction,
) -> None:
    events: list[object] = []
    _record_answers(monkeypatch, events)
    router, _receipts = _router(events)

    await router.versioned_draft(_callback(_interaction(action)), 91)

    assert _controller_event(events)[1:] == (
        "rules",
        events[0][4],
        DraftRef(DRAFT_ID, 3),
        expected,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (DraftAction.EDIT_TYPE, DraftNavigationAction.EDIT_TYPE),
        (DraftAction.EDIT_AMOUNT, DraftNavigationAction.EDIT_AMOUNT),
        (DraftAction.EDIT_CATEGORY, DraftNavigationAction.EDIT_CATEGORY),
        (DraftAction.EDIT_ACCOUNT, DraftNavigationAction.EDIT_ACCOUNT),
        (DraftAction.EDIT_DATE, DraftNavigationAction.EDIT_DATE),
        (DraftAction.EDIT_DESCRIPTION, DraftNavigationAction.EDIT_DESCRIPTION),
        (DraftAction.SKIP_DESCRIPTION, DraftNavigationAction.SKIP_DESCRIPTION),
        (DraftAction.BACK, DraftNavigationAction.BACK),
    ],
)
async def test_every_plain_navigation_action_maps_without_a_choice(
    monkeypatch: pytest.MonkeyPatch,
    action: DraftAction,
    expected: DraftNavigationAction,
) -> None:
    events: list[object] = []
    _record_answers(monkeypatch, events)
    router, _receipts = _router(events)

    await router.versioned_draft(_callback(_interaction(action)), 91)

    controller = _controller_event(events)
    assert controller[1] == "navigation"
    assert controller[4:] == (expected, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interaction", "expected_action", "expected_choice"),
    [
        (
            _interaction(DraftAction.SELECT_TYPE, page=0),
            DraftNavigationAction.SELECT_TYPE,
            TransactionType.EXPENSE,
        ),
        (
            _interaction(DraftAction.SELECT_TYPE, page=1),
            DraftNavigationAction.SELECT_TYPE,
            TransactionType.INCOME,
        ),
        (
            _interaction(DraftAction.SELECT_CATEGORY),
            DraftNavigationAction.SELECT_CATEGORY,
            DraftCatalogChoice.CUSTOM,
        ),
        (
            _interaction(
                DraftAction.SELECT_CATEGORY,
                object_id=OBJECT_ID,
                object_version=7,
            ),
            DraftNavigationAction.SELECT_CATEGORY,
            DraftCatalogRef(OBJECT_ID, 7),
        ),
        (
            _interaction(
                DraftAction.SELECT_ACCOUNT,
                object_id=OBJECT_ID,
                object_version=7,
            ),
            DraftNavigationAction.SELECT_ACCOUNT,
            DraftCatalogRef(OBJECT_ID, 7),
        ),
        (
            _interaction(DraftAction.SELECT_DATE, page=2),
            DraftNavigationAction.SELECT_DATE,
            DraftDateChoice.CUSTOM,
        ),
    ],
)
async def test_navigation_choices_are_typed_before_controller_invocation(
    monkeypatch: pytest.MonkeyPatch,
    interaction: DraftInteraction,
    expected_action: DraftNavigationAction,
    expected_choice: object,
) -> None:
    events: list[object] = []
    _record_answers(monkeypatch, events)
    router, _receipts = _router(events)

    await router.versioned_draft(_callback(interaction), 91)

    assert _controller_event(events)[4:] == (expected_action, expected_choice)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (DraftAction.TX_EDIT_AMOUNT, TransactionDraftNavigationAction.EDIT_AMOUNT),
        (DraftAction.TX_EDIT_CATEGORY, TransactionDraftNavigationAction.EDIT_CATEGORY),
        (DraftAction.TX_EDIT_ACCOUNT, TransactionDraftNavigationAction.EDIT_ACCOUNT),
        (DraftAction.TX_EDIT_DATE, TransactionDraftNavigationAction.EDIT_DATE),
        (
            DraftAction.TX_EDIT_DESCRIPTION,
            TransactionDraftNavigationAction.EDIT_DESCRIPTION,
        ),
        (DraftAction.TX_DATE_BACK, TransactionDraftNavigationAction.DATE_BACK),
        (DraftAction.TX_BACK, TransactionDraftNavigationAction.BACK),
    ],
)
async def test_every_transaction_navigation_action_preserves_history_page(
    monkeypatch: pytest.MonkeyPatch,
    action: DraftAction,
    expected: TransactionDraftNavigationAction,
) -> None:
    events: list[object] = []
    _record_answers(monkeypatch, events)
    router, _receipts = _router(events)

    await router.versioned_draft(_callback(_interaction(action, page=6)), 91)

    assert _controller_event(events)[4:] == (expected, 6)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interaction", "expected_action", "expected_choice", "expected_page"),
    [
        (
            _interaction(
                DraftAction.TX_SELECT_CATEGORY,
                page=5,
                object_id=OBJECT_ID,
                object_version=7,
            ),
            TransactionDraftSelectionAction.CATEGORY,
            DraftCatalogRef(OBJECT_ID, 7),
            5,
        ),
        (
            _interaction(
                DraftAction.TX_SELECT_ACCOUNT,
                object_id=OBJECT_ID,
                object_version=7,
            ),
            TransactionDraftSelectionAction.ACCOUNT,
            DraftCatalogRef(OBJECT_ID, 7),
            0,
        ),
        (
            _interaction(DraftAction.TX_SELECT_DATE, page=16),
            TransactionDraftSelectionAction.DATE,
            DraftDateChoice.YESTERDAY,
            5,
        ),
    ],
)
async def test_every_transaction_selection_family_decodes_choice_and_page(
    monkeypatch: pytest.MonkeyPatch,
    interaction: DraftInteraction,
    expected_action: TransactionDraftSelectionAction,
    expected_choice: object,
    expected_page: int,
) -> None:
    events: list[object] = []
    _record_answers(monkeypatch, events)
    router, _receipts = _router(events)

    await router.versioned_draft(_callback(interaction), 91)

    assert _controller_event(events)[4:] == (
        expected_action,
        expected_choice,
        expected_page,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "controller_name", "answer"),
    [
        (DraftAction.CONFIRM, "confirm", "Сохранено"),
        (DraftAction.CANCEL, "cancel", "Отменено"),
        (DraftAction.SKIP_OCR_ITEM, "skip_ocr_item", "Отменено"),
    ],
)
async def test_completion_actions_invoke_the_exact_controller_method(
    monkeypatch: pytest.MonkeyPatch,
    action: DraftAction,
    controller_name: str,
    answer: str,
) -> None:
    events: list[object] = []
    _record_answers(monkeypatch, events)
    router, _receipts = _router(events)

    await router.versioned_draft(_callback(_interaction(action)), 91)

    assert _controller_event(events)[1] == controller_name
    assert events[-1] == ("answer", answer, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "resolution", "answer"),
    [
        (DraftAction.RESUME, DraftConflictResolution.RESUME, "Черновик продолжен"),
        (DraftAction.REPLACE, DraftConflictResolution.REPLACE, "Начат новый ввод"),
        (DraftAction.KEEP, DraftConflictResolution.KEEP, "Текущий черновик сохранён"),
    ],
)
async def test_conflict_actions_map_to_exact_resolution_and_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
    action: DraftAction,
    resolution: DraftConflictResolution,
    answer: str,
) -> None:
    events: list[object] = []
    _record_answers(monkeypatch, events)
    router, _receipts = _router(events)

    await router.versioned_draft(_callback(_interaction(action)), 91)

    assert _controller_event(events)[4] is resolution
    assert events[-1] == ("answer", answer, None)


@pytest.mark.asyncio
async def test_discard_routes_exact_ref_and_acknowledges_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    _record_answers(monkeypatch, events)
    router, _receipts = _router(events)

    await router.versioned_draft(_callback(_interaction(DraftAction.DISCARD)), 91)

    assert _controller_event(events)[1] == "drafts"
    assert _controller_event(events)[3] == DraftRef(DRAFT_ID, 3)
    assert events[-1] == ("answer", "Незавершённый ввод сброшен", None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interaction", "message"),
    [
        (_interaction(DraftAction.SELECT_TYPE), "Кнопка повреждена"),
        (
            _interaction(
                DraftAction.SELECT_CATEGORY,
                page=1,
                object_id=OBJECT_ID,
                object_version=1,
            ),
            "Кнопка повреждена",
        ),
        (_interaction(DraftAction.SELECT_DATE, page=3), "Кнопка повреждена"),
        (
            _interaction(DraftAction.TX_SELECT_CATEGORY),
            "Кнопка выбора повреждена",
        ),
        (_interaction(DraftAction.TX_SELECT_DATE), "Кнопка даты повреждена"),
    ],
)
async def test_invalid_action_payload_fails_closed_before_context_or_controller(
    monkeypatch: pytest.MonkeyPatch,
    interaction: DraftInteraction,
    message: str,
) -> None:
    events: list[object] = []
    _record_answers(monkeypatch, events)
    router, _receipts = _router(events)

    await router.versioned_draft(_callback(interaction), 91)

    assert events == [("answer", message, True)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        "d0.broken.1",
        "d?.AAAAAAAAAAAAAAAAAAAAAA.1",
        "d0.AAAAAAAAAAAAAAAAAAAAAA.01",
        "d0.AAAAAAAAAAAAAAAAAAAAAA.1.extra",
    ],
)
async def test_malformed_compact_callback_fails_closed_before_any_controller(
    monkeypatch: pytest.MonkeyPatch,
    data: str,
) -> None:
    events: list[object] = []
    _record_answers(monkeypatch, events)
    router, _receipts = _router(events)

    await router.versioned_draft(_callback(data), 91)

    assert events == [("answer", "Кнопка повреждена или устарела", True)]


@pytest.mark.asyncio
async def test_retired_but_valid_compact_action_is_not_mutated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    _record_answers(monkeypatch, events)
    router, _receipts = _router(events)

    await router.versioned_draft(
        _callback(_interaction(DraftAction.CATEGORY_PAGE, page=1)),
        91,
    )

    assert events == [("answer", "Действие больше не поддерживается", True)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("family", "interaction"),
    [
        ("drafts", _interaction(DraftAction.DISCARD)),
        ("rules", _interaction(DraftAction.RULE_GLOBAL)),
        ("navigation", _interaction(DraftAction.EDIT_AMOUNT)),
        ("transaction_navigation", _interaction(DraftAction.TX_EDIT_AMOUNT)),
        (
            "transaction_selection",
            _interaction(
                DraftAction.TX_SELECT_ACCOUNT,
                object_id=OBJECT_ID,
                object_version=1,
            ),
        ),
        ("completion", _interaction(DraftAction.CONFIRM)),
        ("conflicts", _interaction(DraftAction.RESUME)),
    ],
)
async def test_duplicate_receipt_never_renders_delivers_or_binds(
    monkeypatch: pytest.MonkeyPatch,
    family: str,
    interaction: DraftInteraction,
) -> None:
    events: list[object] = []
    _record_answers(monkeypatch, events)
    receipts = _default_receipts()
    receipts[family] = None
    router, _receipts = _router(events, receipts)

    await router.versioned_draft(_callback(interaction))

    assert events[-1] == ("answer", "Уже обработано", None)
    assert not [event for event in events if event[0] in {"render", "replace", "bind"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (
            DraftRevisionConflictError(current_revision=4),
            "Форма уже изменилась. Откройте актуальный черновик",
        ),
        (ApplicationValidationError("Безопасная ошибка"), "Безопасная ошибка"),
    ],
)
async def test_controller_failures_are_acknowledged_without_postcommit_delivery(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    expected: str,
) -> None:
    events: list[object] = []
    _record_answers(monkeypatch, events)
    receipts = _default_receipts()
    receipts["rules"] = failure
    router, _receipts = _router(events, receipts)

    await router.versioned_draft(_callback(_interaction(DraftAction.RULE_GLOBAL)), 91)

    assert events[-1] == ("answer", expected, True)
    assert not [event for event in events if event[0] in {"render", "replace", "bind"}]


@pytest.mark.asyncio
async def test_conflict_value_error_is_fail_closed_and_not_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    _record_answers(monkeypatch, events)
    receipts = _default_receipts()
    receipts["conflicts"] = ValueError("Конфликт недействителен")
    router, _receipts = _router(events, receipts)

    await router.versioned_draft(_callback(_interaction(DraftAction.RESUME)), 91)

    assert events[-1] == ("answer", "Конфликт недействителен", True)
    assert not [event for event in events if event[0] in {"render", "replace", "bind"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("family", "interaction", "renderer", "binds"),
    [
        ("drafts", _interaction(DraftAction.DISCARD), "drafts", False),
        ("rules", _interaction(DraftAction.RULE_GLOBAL), "rules", True),
        ("navigation", _interaction(DraftAction.EDIT_AMOUNT), "navigation", True),
        (
            "transaction_navigation",
            _interaction(DraftAction.TX_EDIT_AMOUNT, page=5),
            "transaction_navigation",
            True,
        ),
        (
            "transaction_selection",
            _interaction(
                DraftAction.TX_SELECT_ACCOUNT,
                page=5,
                object_id=OBJECT_ID,
                object_version=1,
            ),
            "transaction_selection",
            True,
        ),
        ("completion", _interaction(DraftAction.CONFIRM), "ocr_completion", True),
        ("conflicts", _interaction(DraftAction.RESUME), "conflicts", True),
    ],
)
async def test_untracked_delivery_occurs_only_after_controller_and_binds_after_network(
    monkeypatch: pytest.MonkeyPatch,
    family: str,
    interaction: DraftInteraction,
    renderer: str,
    binds: bool,
) -> None:
    events: list[object] = []
    _record_answers(monkeypatch, events)
    receipts = _default_receipts()
    if family == "completion":
        receipts[family] = _ocr_completion()
    router, _receipts = _router(events, receipts)

    await router.versioned_draft(_callback(interaction))

    labels = [event[0] for event in events]
    assert labels.index("controller") < labels.index("render") < labels.index("replace")
    assert cast(tuple[Any, ...], next(event for event in events if event[0] == "render"))[1]
    assert renderer in cast(
        tuple[Any, ...], next(event for event in events if event[0] == "render")
    )
    if binds:
        assert labels.index("replace") < labels.index("bind") < labels.index("answer")
    else:
        assert "bind" not in labels
        assert labels.index("replace") < labels.index("answer")


@pytest.mark.asyncio
async def test_transaction_untracked_binding_overrides_exact_history_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    _record_answers(monkeypatch, events)
    router, _receipts = _router(events)

    await router.versioned_draft(_callback(_interaction(DraftAction.TX_EDIT_ACCOUNT, page=5)))

    bind = cast(tuple[Any, ...], next(event for event in events if event[0] == "bind"))
    assert bind[2:] == (NEXT_DRAFT, 701, 5, True)


@pytest.mark.asyncio
async def test_tracked_callback_never_performs_direct_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    _record_answers(monkeypatch, events)
    router, _receipts = _router(events)

    await router.versioned_draft(
        _callback(_interaction(DraftAction.RULE_ACCOUNT)),
        91,
    )

    assert not [event for event in events if event[0] in {"render", "replace", "bind"}]
    assert [event[0] for event in events] == ["context", "controller", "answer"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "status", "expected"),
    [
        (OcrQueueOperation.CONFIRMED, OcrQueueStatus.ADVANCED, "Сохранено, проверьте следующую"),
        (OcrQueueOperation.CONFIRMED, OcrQueueStatus.COMPLETED, "Сохранено"),
        (OcrQueueOperation.SKIPPED, OcrQueueStatus.ADVANCED, "Пропущено"),
        (OcrQueueOperation.CANCELLED, OcrQueueStatus.CANCELLED, "Отменено"),
    ],
)
async def test_ocr_completion_acknowledgement_preserves_queue_semantics(
    monkeypatch: pytest.MonkeyPatch,
    operation: OcrQueueOperation,
    status: OcrQueueStatus,
    expected: str,
) -> None:
    events: list[object] = []
    _record_answers(monkeypatch, events)
    receipts = _default_receipts()
    receipts["completion"] = _ocr_completion(operation, status)
    router, _receipts = _router(events, receipts)
    action = {
        OcrQueueOperation.CONFIRMED: DraftAction.CONFIRM,
        OcrQueueOperation.SKIPPED: DraftAction.SKIP_OCR_ITEM,
        OcrQueueOperation.CANCELLED: DraftAction.CANCEL,
    }[operation]

    await router.versioned_draft(_callback(_interaction(action)), 91)

    assert events[-1] == ("answer", expected, None)


def test_dependency_bundles_are_repr_safe() -> None:
    router, _receipts = _router([])

    rendered = repr(router)

    assert rendered == "DraftInteractionRouter()"
    assert "controller" not in rendered.casefold()
    assert "render" not in rendered.casefold()
    assert "bind" not in rendered.casefold()


def test_router_module_has_no_database_sqlalchemy_or_bootstrap_dependency() -> None:
    path = Path(__file__).parents[2] / (
        "src/finbot/adapters/telegram/routers/draft_interactions.py"
    )
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    forbidden = ("sqlalchemy", "finbot.adapters.database", "finbot.bootstrap")
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)

    assert not [module for module in modules if module.startswith(forbidden)]
    assert "AsyncSession" not in source
    assert "session.begin" not in source


def test_bootstrap_only_composes_and_registers_the_draft_interaction_router() -> None:
    path = Path(__file__).parents[2] / "src/finbot/bootstrap.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    function_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert "draft_interaction_router = DraftInteractionRouter(" in source
    assert "controllers=DraftInteractionControllers(" in source
    assert "contexts=DraftInteractionContexts(" in source
    assert "renderers=DraftInteractionRenderers(" in source
    assert "delivery=DraftInteractionDelivery(" in source
    assert "partial(_bind_untracked_draft_interaction_presentation, sessions)" in source
    assert "versioned_draft=draft_interaction_router.versioned_draft" in source
    assert "versioned_draft_button" not in function_names
    assert "DraftInteraction.decode(" not in source


def test_bootstrap_discard_guard_accepts_only_the_exact_suspended_presentation() -> None:
    path = Path(__file__).parents[2] / "src/finbot/bootstrap.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    draft_controller_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "DraftController"
    ]

    assert len(draft_controller_calls) == 1
    call = draft_controller_calls[0]
    assert len(call.args) == 4
    guard = call.args[2]
    assert isinstance(guard, ast.Call)
    assert isinstance(guard.func, ast.Name)
    assert guard.func.id == "SqlAlchemyDraftConflictPresentationGuard"
