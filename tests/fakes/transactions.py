from finbot.application.dto import (
    ConfirmTransactionDraftCommand,
    EditTransactionCommand,
    PreparedTransactionDraft,
    PrepareRepeatDraftCommand,
    TransactionMutationResult,
    VersionedTransactionCommand,
)


class InMemoryTransactionCommandRepository:
    """Deterministic command-port fake; tests provide every result explicitly."""

    def __init__(
        self,
        *,
        mutation_result: TransactionMutationResult,
        prepared_repeat: PreparedTransactionDraft,
    ) -> None:
        self.mutation_result = mutation_result
        self.prepared_repeat = prepared_repeat
        self.calls: list[
            ConfirmTransactionDraftCommand
            | EditTransactionCommand
            | VersionedTransactionCommand
            | PrepareRepeatDraftCommand
        ] = []

    async def confirm_reviewed_draft(
        self,
        command: ConfirmTransactionDraftCommand,
    ) -> TransactionMutationResult:
        self.calls.append(command)
        return self.mutation_result

    async def edit(self, command: EditTransactionCommand) -> TransactionMutationResult:
        self.calls.append(command)
        return self.mutation_result

    async def delete(
        self,
        command: VersionedTransactionCommand,
    ) -> TransactionMutationResult:
        self.calls.append(command)
        return self.mutation_result

    async def restore(
        self,
        command: VersionedTransactionCommand,
    ) -> TransactionMutationResult:
        self.calls.append(command)
        return self.mutation_result

    async def prepare_repeat(
        self,
        command: PrepareRepeatDraftCommand,
    ) -> PreparedTransactionDraft:
        self.calls.append(command)
        return self.prepared_repeat
