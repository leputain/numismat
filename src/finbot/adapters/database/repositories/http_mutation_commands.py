from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.repositories.draft_conflicts import (
    SqlAlchemyDraftConflictReplacementTargets,
)
from finbot.adapters.database.repositories.draft_navigation import (
    SqlAlchemyDraftNavigationCatalogRepository,
)
from finbot.adapters.database.repositories.draft_preparation import (
    SqlAlchemyDraftPreparationRepository,
)
from finbot.adapters.database.repositories.drafts import SqlAlchemyDraftRepository
from finbot.adapters.database.repositories.finance_draft_text_input import (
    SqlAlchemyFinanceDraftTextInputCatalogRepository,
)
from finbot.adapters.database.repositories.http_idempotency import (
    IdempotencyResult,
    IdempotencyResultKind,
)
from finbot.adapters.database.repositories.queries import SqlAlchemyQueryRepository
from finbot.adapters.database.repositories.transaction_draft_selection import (
    SqlAlchemyTransactionDraftSelectionRepository,
)
from finbot.adapters.database.repositories.transaction_edit_ingress import (
    SqlAlchemyTransactionEditTargetReader,
)
from finbot.adapters.database.repositories.transaction_edit_text_input import (
    SqlAlchemyTransactionEditTextTargetRepository,
)
from finbot.adapters.database.repositories.transactions import (
    SqlAlchemyTransactionCommandRepository,
)
from finbot.adapters.deterministic_draft_parser import DeterministicQuickDraftParser
from finbot.application.draft_composition import BeginComposeDraftCommand
from finbot.application.draft_conflicts import (
    PENDING_DRAFT_INTENT_KEY,
    ResolveDraftConflictCommand,
)
from finbot.application.draft_ingress import (
    BeginQuickDraftCommand,
    BeginRepeatDraftCommand,
    BeginWizardDraftCommand,
    DraftIngressStatus,
)
from finbot.application.draft_navigation import (
    DraftCatalogChoice,
    DraftCatalogRef,
    DraftDateChoice,
    DraftNavigationAction,
    DraftNavigationChoice,
    DraftNavigationCommand,
    DraftNavigationStatus,
)
from finbot.application.draft_rules import DraftRuleAction, StageDraftRuleCommand
from finbot.application.draft_views import SUPPORTED_DRAFT_SCHEMA_VERSION
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    ConfirmTransactionDraftCommand,
    DraftConflictResolution,
    DraftRef,
    DraftSnapshot,
    TransactionMutationResult,
    VersionedTransactionCommand,
)
from finbot.application.errors import (
    ApplicationValidationError,
    DraftRevisionConflictError,
    EntityNotFoundError,
    InvalidStateError,
)
from finbot.application.finance_draft_text_input import (
    FINANCE_DRAFT_TEXT_INPUT_STATES,
    FinanceDraftTextInputCommand,
    FinanceDraftTextInputStatus,
)
from finbot.application.revision_mutations import (
    DraftExistingSelection,
    DraftPatchAction,
    DraftPatchCommand,
    DraftPatchResult,
)
from finbot.application.transaction_draft_navigation import (
    TRANSACTION_DRAFT_NAVIGATION_ALLOWED_STATES,
    TransactionDraftNavigationAction,
    TransactionDraftNavigationCommand,
)
from finbot.application.transaction_draft_selection import (
    TransactionDraftSelectionAction,
    TransactionDraftSelectionChoice,
    TransactionDraftSelectionCommand,
    TransactionDraftSelectionStatus,
    TransactionTypeSelection,
)
from finbot.application.transaction_edit_ingress import (
    BeginTransactionEditCommand,
    TransactionEditIngressStatus,
)
from finbot.application.transaction_edit_text_input import (
    TRANSACTION_EDIT_TEXT_STATES,
    TransactionEditTextInputCommand,
    TransactionEditTextInputStatus,
)
from finbot.application.use_cases.draft_composition import PrepareComposedDraft
from finbot.application.use_cases.draft_conflicts import (
    PrepareDraftConflictReplacement as PrepareConflictReplacement,
)
from finbot.application.use_cases.draft_conflicts import ResolveDraftConflict
from finbot.application.use_cases.draft_ingress import DraftIngressUseCases
from finbot.application.use_cases.draft_navigation import DraftNavigationUseCases
from finbot.application.use_cases.draft_preparation import (
    PrepareParsedDraft,
    PrepareQuickDraft,
    SystemDraftPreparationClock,
)
from finbot.application.use_cases.draft_rules import DraftRuleUseCases
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.application.use_cases.finance_draft_text_input import (
    FinanceDraftTextInputUseCase,
)
from finbot.application.use_cases.queries import GetOwnerSettings, GetTransaction
from finbot.application.use_cases.transaction_draft_navigation import (
    TransactionDraftNavigationUseCases,
)
from finbot.application.use_cases.transaction_draft_selection import (
    TransactionDraftSelectionUseCases,
)
from finbot.application.use_cases.transaction_edit_ingress import BeginTransactionEdit
from finbot.application.use_cases.transaction_edit_text_input import (
    TransactionEditTextInputUseCase,
)
from finbot.application.use_cases.transactions import TransactionUseCases
from finbot.domain.transactions import TransactionType


async def _empty_accounts(
    owner_id: UUID,
    *,
    archived: bool = False,
) -> tuple[AccountSnapshot, ...]:
    del owner_id, archived
    return ()


async def _empty_categories(
    owner_id: UUID,
    *,
    kind: TransactionType | str | None = None,
    archived: bool = False,
) -> tuple[CategorySnapshot, ...]:
    del owner_id, kind, archived
    return ()


def _transaction_result(snapshot: object) -> TransactionMutationResult:
    from finbot.application.dto import TransactionSnapshot

    if not isinstance(snapshot, TransactionSnapshot):
        raise TypeError("transaction snapshot is invalid")
    return TransactionMutationResult(
        entity_id=snapshot.transaction_id,
        version=snapshot.version,
        resulting_state="updated",
        transaction=snapshot,
    )


class SqlAlchemyRevisionMutationCommands:
    """Typed Task 14 commands over one caller-owned SQLAlchemy session."""

    __slots__ = (
        "_conflicts",
        "_draft_navigation",
        "_draft_rules",
        "_drafts",
        "_finance_text",
        "_ingress",
        "_transaction_edit",
        "_transaction_navigation",
        "_transaction_selection",
        "_transaction_text",
        "_transactions",
    )

    def __init__(self, session: AsyncSession) -> None:
        repository = SqlAlchemyDraftRepository(session)
        drafts = DraftUseCases(repository)
        reader = SqlAlchemyQueryRepository(session)
        owners = GetOwnerSettings(reader)
        transaction_commands = SqlAlchemyTransactionCommandRepository(session)
        transactions = TransactionUseCases(transaction_commands, repository)
        quick_drafts = PrepareQuickDraft(
            reader,
            DeterministicQuickDraftParser(),
            PrepareParsedDraft(
                reader,
                SqlAlchemyDraftPreparationRepository(session),
                SqlAlchemyDraftPreparationRepository(session),
                SystemDraftPreparationClock(),
            ),
        )
        navigation_catalogs = SqlAlchemyDraftNavigationCatalogRepository(session)
        compose_drafts = PrepareComposedDraft(reader, navigation_catalogs)

        self._drafts = drafts
        self._transactions = transactions
        self._ingress = DraftIngressUseCases(
            drafts,
            transaction_commands,
            owners,
            quick_drafts=quick_drafts,
            compose_drafts=compose_drafts,
        )
        self._conflicts = ResolveDraftConflict(
            repository,
            PrepareConflictReplacement(
                quick_drafts,
                SqlAlchemyDraftConflictReplacementTargets(session),
                compose_drafts=compose_drafts,
            ),
        )
        self._draft_navigation = DraftNavigationUseCases(
            drafts,
            owners,
            _empty_accounts,
            _empty_categories,
            navigation_catalogs,
        )
        self._draft_rules = DraftRuleUseCases(drafts)
        self._finance_text = FinanceDraftTextInputUseCase(
            drafts,
            owners,
            _empty_accounts,
            _empty_categories,
            SqlAlchemyFinanceDraftTextInputCatalogRepository(session),
        )
        self._transaction_edit = BeginTransactionEdit(
            drafts,
            owners,
            SqlAlchemyTransactionEditTargetReader(session),
        )
        self._transaction_navigation = TransactionDraftNavigationUseCases(
            drafts,
            owners,
            GetTransaction(reader),
            _empty_accounts,
            _empty_categories,
        )
        self._transaction_selection = TransactionDraftSelectionUseCases(
            drafts,
            owners,
            SqlAlchemyTransactionDraftSelectionRepository(session),
        )
        self._transaction_text = TransactionEditTextInputUseCase(
            SqlAlchemyTransactionEditTextTargetRepository(session),
            transactions,
            drafts,
        )

    @staticmethod
    def _require_supported_slot(current: DraftSnapshot | None) -> None:
        if current is not None and (
            type(current.schema_version) is not int
            or current.schema_version != SUPPORTED_DRAFT_SCHEMA_VERSION
        ):
            raise InvalidStateError("Версия черновика не поддерживается")

    @staticmethod
    def _require_current(
        current: DraftSnapshot | None,
        expected: DraftRef,
        *,
        allow_pending: bool = False,
        allow_suspended: bool = False,
        allow_specialized: bool = False,
    ) -> DraftSnapshot:
        if current is None or current.draft_id != expected.draft_id:
            raise EntityNotFoundError("Черновик не найден")
        if current.revision != expected.revision:
            raise DraftRevisionConflictError(current_revision=current.revision)
        SqlAlchemyRevisionMutationCommands._require_supported_slot(current)
        if current.suspended and not allow_suspended:
            raise InvalidStateError("Черновик приостановлен")
        if PENDING_DRAFT_INTENT_KEY in current.payload and not allow_pending:
            raise InvalidStateError("Сначала выберите действие для незавершённого ввода")
        if not allow_specialized and (
            "ocr_batch" in current.payload or current.state.startswith("settings_")
        ):
            raise InvalidStateError("Черновик требует специализированной обработки")
        return current

    async def create_draft(self, owner_id: UUID) -> tuple[int, DraftSnapshot]:
        self._require_supported_slot(await self._drafts.get_active(owner_id))
        result = await self._ingress.begin_wizard(BeginWizardDraftCommand(owner_id))
        status = 201 if result.status is DraftIngressStatus.STARTED else 200
        return status, result.draft

    async def begin_quick_draft(
        self,
        owner_id: UUID,
        text: str,
    ) -> tuple[int, DraftSnapshot]:
        self._require_supported_slot(await self._drafts.get_active(owner_id))
        result = await self._ingress.begin_quick(BeginQuickDraftCommand(owner_id, text))
        status = 201 if result.status is DraftIngressStatus.STARTED else 200
        return status, result.draft

    async def compose_draft(
        self,
        command: BeginComposeDraftCommand,
    ) -> tuple[int, DraftSnapshot]:
        self._require_supported_slot(await self._drafts.get_active(command.owner_id))
        result = await self._ingress.begin_compose(command)
        status = 201 if result.status is DraftIngressStatus.STARTED else 200
        return status, result.draft

    async def patch_draft(self, command: DraftPatchCommand) -> DraftPatchResult:
        current = self._require_current(
            await self._drafts.get_active(command.owner_id),
            command.expected,
        )
        if command.action is DraftPatchAction.INPUT_TEXT:
            return await self._text(command, current)
        if command.action is DraftPatchAction.SET_RULE:
            if not isinstance(command.value, DraftRuleAction):  # pragma: no cover
                raise TypeError("draft rule action is missing")
            rule_result = await self._draft_rules.execute(
                StageDraftRuleCommand(command.owner_id, command.expected, command.value)
            )
            return DraftPatchResult(draft=rule_result.draft)
        if current.state.startswith("edit_"):
            return await self._transaction_patch(command, current)
        return await self._finance_patch(command)

    async def _text(
        self,
        command: DraftPatchCommand,
        current: DraftSnapshot,
    ) -> DraftPatchResult:
        text = command.text
        if text is None:  # pragma: no cover - DTO invariant
            raise TypeError("draft text is missing")
        if current.state in TRANSACTION_EDIT_TEXT_STATES:
            transaction_result = await self._transaction_text.execute(
                TransactionEditTextInputCommand(command.owner_id, command.expected, text)
            )
            if transaction_result.status is TransactionEditTextInputStatus.RETRY:
                raise ApplicationValidationError("Запрос не прошёл проверку")
            return DraftPatchResult(transaction=_transaction_result(transaction_result.transaction))
        if current.state not in FINANCE_DRAFT_TEXT_INPUT_STATES:
            raise InvalidStateError("Текст недоступен в текущем состоянии")
        finance_result = await self._finance_text.execute(
            FinanceDraftTextInputCommand(command.owner_id, command.expected, text)
        )
        if finance_result.status is FinanceDraftTextInputStatus.RETRY:
            raise ApplicationValidationError("Запрос не прошёл проверку")
        return DraftPatchResult(draft=finance_result.draft)

    async def _finance_patch(self, command: DraftPatchCommand) -> DraftPatchResult:
        choice: DraftNavigationChoice | None = None
        if isinstance(command.selection, DraftExistingSelection):
            choice = DraftCatalogRef(command.selection.entity_id, command.selection.version)
        elif isinstance(command.selection, DraftCatalogChoice):
            choice = command.selection
        elif isinstance(command.value, (TransactionType, DraftDateChoice)):
            choice = command.value
        elif command.action in {
            DraftPatchAction.SELECT_TYPE,
            DraftPatchAction.SELECT_CATEGORY,
            DraftPatchAction.SELECT_ACCOUNT,
            DraftPatchAction.SELECT_DATE,
        }:  # pragma: no cover - DraftPatchCommand invariant
            raise TypeError("draft navigation choice is invalid")
        navigation_result = await self._draft_navigation.execute(
            DraftNavigationCommand(
                command.owner_id,
                command.expected,
                DraftNavigationAction(command.action.value),
                choice,
            )
        )
        if navigation_result.status is DraftNavigationStatus.CLOSED:
            return DraftPatchResult(closed=True)
        if navigation_result.draft is None:  # pragma: no cover - result DTO invariant
            raise RuntimeError("draft navigation result is incomplete")
        return DraftPatchResult(draft=navigation_result.draft)

    async def _transaction_patch(
        self,
        command: DraftPatchCommand,
        current: DraftSnapshot,
    ) -> DraftPatchResult:
        selection_actions = {
            DraftPatchAction.SELECT_EDIT_TYPE: TransactionDraftSelectionAction.TYPE,
            DraftPatchAction.SELECT_CATEGORY: TransactionDraftSelectionAction.CATEGORY,
            DraftPatchAction.SELECT_ACCOUNT: TransactionDraftSelectionAction.ACCOUNT,
            DraftPatchAction.SELECT_DATE: TransactionDraftSelectionAction.DATE,
        }
        selection_action = selection_actions.get(command.action)
        if selection_action is not None:
            selection_choice: TransactionDraftSelectionChoice
            if (
                selection_action is TransactionDraftSelectionAction.TYPE
                and isinstance(command.value, TransactionType)
                and isinstance(command.selection, DraftExistingSelection)
            ):
                selection_choice = TransactionTypeSelection(
                    command.value,
                    DraftCatalogRef(
                        command.selection.entity_id,
                        command.selection.version,
                    ),
                )
            elif selection_action in {
                TransactionDraftSelectionAction.CATEGORY,
                TransactionDraftSelectionAction.ACCOUNT,
            } and isinstance(command.selection, DraftExistingSelection):
                selection_choice = DraftCatalogRef(
                    command.selection.entity_id,
                    command.selection.version,
                )
            elif selection_action is TransactionDraftSelectionAction.DATE and isinstance(
                command.value, DraftDateChoice
            ):
                selection_choice = command.value
            else:
                raise InvalidStateError("Выбор недоступен для операции редактирования")
            selection_result = await self._transaction_selection.execute(
                TransactionDraftSelectionCommand(
                    command.owner_id,
                    command.expected,
                    selection_action,
                    selection_choice,
                )
            )
            if selection_result.status is TransactionDraftSelectionStatus.DATE_INPUT_REQUIRED:
                if selection_result.draft is None:  # pragma: no cover
                    raise RuntimeError("transaction edit draft result is incomplete")
                return DraftPatchResult(draft=selection_result.draft)
            if selection_result.transaction is None:  # pragma: no cover
                raise RuntimeError("transaction edit result is incomplete")
            return DraftPatchResult(transaction=_transaction_result(selection_result.transaction))

        try:
            action = (
                TransactionDraftNavigationAction.DATE_BACK
                if command.action is DraftPatchAction.BACK and current.state == "edit_date"
                else TransactionDraftNavigationAction(command.action.value)
            )
        except ValueError:
            raise InvalidStateError("Действие недоступно при редактировании") from None
        if current.state not in TRANSACTION_DRAFT_NAVIGATION_ALLOWED_STATES[action]:
            raise InvalidStateError("Экран редактирования уже неактуален")
        navigation_result = await self._transaction_navigation.execute(
            TransactionDraftNavigationCommand(command.owner_id, command.expected, action)
        )
        return DraftPatchResult(draft=navigation_result.draft)

    async def confirm_draft(
        self,
        owner_id: UUID,
        draft_id: UUID,
        revision: int,
    ) -> IdempotencyResult:
        expected = DraftRef(draft_id, revision)
        self._require_current(
            await self._drafts.get_active(owner_id),
            expected,
        )
        result = await self._transactions.confirm(
            ConfirmTransactionDraftCommand(owner_id, expected)
        )
        return IdempotencyResult(
            201,
            IdempotencyResultKind.TRANSACTION,
            result.entity_id,
            result.version,
        )

    async def cancel_draft(
        self,
        owner_id: UUID,
        draft_id: UUID,
        revision: int,
    ) -> None:
        expected = DraftRef(draft_id, revision)
        self._require_current(
            await self._drafts.get_active(owner_id),
            expected,
            allow_pending=True,
            allow_suspended=True,
        )
        await self._drafts.cancel(owner_id, expected)

    async def resolve_draft(
        self,
        owner_id: UUID,
        draft_id: UUID,
        revision: int,
        *,
        replace: bool,
    ) -> DraftSnapshot:
        expected = DraftRef(draft_id, revision)
        current = self._require_current(
            await self._drafts.get_active(owner_id),
            expected,
            allow_pending=True,
            allow_suspended=True,
            allow_specialized=True,
        )
        has_pending = PENDING_DRAFT_INTENT_KEY in current.payload
        if (
            "ocr_batch" in current.payload or current.state.startswith("settings_")
        ) and not has_pending:
            raise InvalidStateError("Черновик требует специализированной обработки")
        if replace and not has_pending:
            raise InvalidStateError("Новое действие уже отменено")
        if not replace and not (has_pending or current.suspended):
            raise InvalidStateError("Черновик уже активен")
        result = await self._conflicts.execute(
            ResolveDraftConflictCommand(
                owner_id,
                expected,
                (DraftConflictResolution.REPLACE if replace else DraftConflictResolution.RESUME),
            )
        )
        return result.draft

    async def repeat_transaction(
        self,
        owner_id: UUID,
        transaction_id: UUID,
        version: int,
    ) -> tuple[int, DraftSnapshot]:
        self._require_supported_slot(await self._drafts.get_active(owner_id))
        result = await self._ingress.begin_repeat(
            BeginRepeatDraftCommand(owner_id, transaction_id, version)
        )
        status = 201 if result.status is DraftIngressStatus.STARTED else 200
        return status, result.draft

    async def begin_transaction_edit(
        self,
        owner_id: UUID,
        transaction_id: UUID,
        version: int,
    ) -> tuple[int, DraftSnapshot]:
        self._require_supported_slot(await self._drafts.get_active(owner_id))
        result = await self._transaction_edit.execute(
            BeginTransactionEditCommand(owner_id, transaction_id, version)
        )
        status = 201 if result.status is TransactionEditIngressStatus.DRAFT_CREATED else 200
        return status, result.draft

    async def delete_transaction(
        self,
        owner_id: UUID,
        transaction_id: UUID,
        version: int,
    ) -> IdempotencyResult:
        result = await self._transactions.delete(
            VersionedTransactionCommand(owner_id, transaction_id, version)
        )
        return IdempotencyResult(
            200,
            IdempotencyResultKind.TRANSACTION,
            result.entity_id,
            result.version,
        )

    async def restore_transaction(
        self,
        owner_id: UUID,
        transaction_id: UUID,
        version: int,
    ) -> IdempotencyResult:
        result = await self._transactions.restore(
            VersionedTransactionCommand(owner_id, transaction_id, version)
        )
        return IdempotencyResult(
            200,
            IdempotencyResultKind.TRANSACTION,
            result.entity_id,
            result.version,
        )
