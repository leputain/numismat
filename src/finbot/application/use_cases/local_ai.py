from finbot.application.draft_ingress import (
    DraftIngressOperation,
    DraftIngressOwnerQuery,
    DraftIngressResult,
    DraftIngressStatus,
)
from finbot.application.draft_preparation import PreparedDraftResult
from finbot.application.dto import CreateDraftCommand
from finbot.application.errors import ActiveDraftConflictError, ApplicationValidationError
from finbot.application.local_ai import (
    CreateLocalAiDraftCommand,
    LocalAiInvalidSuggestionError,
    LocalAiSuggestion,
    LocalAiSuggestionProvider,
    SuggestLocalTransactionCommand,
)
from finbot.application.use_cases.draft_preparation import PrepareParsedDraft
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.domain.money import MoneyError, parse_minor
from finbot.domain.transactions import TransactionDraft


class SuggestLocalTransaction:
    """Convert one explicit local-model suggestion into a validated domain draft."""

    __slots__ = ("_provider",)

    def __init__(self, provider: LocalAiSuggestionProvider) -> None:
        self._provider = provider

    async def execute(self, command: SuggestLocalTransactionCommand) -> TransactionDraft:
        suggestion = await self._provider.suggest(command.text)
        if not isinstance(suggestion, LocalAiSuggestion):
            raise LocalAiInvalidSuggestionError("Локальный AI вернул некорректный ответ")
        try:
            amount_minor = parse_minor(suggestion.amount_decimal)
            return TransactionDraft(
                amount_minor=amount_minor,
                type=suggestion.transaction_type,
                category_hint=suggestion.category_hint,
                category_explicit=False,
                account_hint=suggestion.account_hint,
                description=suggestion.description,
            )
        except MoneyError, TypeError, ValueError:
            raise LocalAiInvalidSuggestionError("Локальный AI вернул некорректный ответ") from None


class CreateLocalAiDraft:
    """Create a review-first draft and never replace or stage against active work."""

    __slots__ = ("_drafts", "_owners", "_parsed_drafts")

    def __init__(
        self,
        drafts: DraftUseCases,
        parsed_drafts: PrepareParsedDraft,
        owners: DraftIngressOwnerQuery,
    ) -> None:
        self._drafts = drafts
        self._parsed_drafts = parsed_drafts
        self._owners = owners

    async def execute(self, command: CreateLocalAiDraftCommand) -> DraftIngressResult:
        owner = await self._owners(command.owner_id)
        if owner.owner_id != command.owner_id:
            raise ApplicationValidationError("Контекст владельца повреждён")

        current = await self._drafts.get_active(command.owner_id)
        if current is not None:
            return DraftIngressResult(
                DraftIngressOperation.LOCAL_AI,
                DraftIngressStatus.CONFLICT,
                owner,
                current,
            )

        prepared = await self._parsed_drafts.prepare_for_owner(
            owner,
            command.draft,
            flow="local_ai",
        )
        self._validate_prepared(prepared)
        try:
            created = await self._drafts.create(
                CreateDraftCommand(
                    command.owner_id,
                    prepared.state.value,
                    prepared.payload,
                )
            )
        except ActiveDraftConflictError:
            # The repository owner lock is the authoritative absent-row race guard.
            current = await self._drafts.get_active(command.owner_id)
            if current is None:
                raise
            return DraftIngressResult(
                DraftIngressOperation.LOCAL_AI,
                DraftIngressStatus.CONFLICT,
                owner,
                current,
            )
        return DraftIngressResult(
            DraftIngressOperation.LOCAL_AI,
            DraftIngressStatus.STARTED,
            owner,
            created,
        )

    @staticmethod
    def _validate_prepared(prepared: PreparedDraftResult) -> None:
        if (
            not isinstance(prepared, PreparedDraftResult)
            or prepared.payload.get("flow") != "local_ai"
        ):
            raise ApplicationValidationError("Предложенный черновик повреждён")


__all__ = ["CreateLocalAiDraft", "SuggestLocalTransaction"]
