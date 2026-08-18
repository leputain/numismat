from datetime import datetime
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from fakes.repositories import InMemoryDraftRepository

from finbot.application.draft_conflicts import (
    PendingEditIntent,
    PendingQuickIntent,
    PendingRepeatIntent,
    PendingWizardIntent,
    PreparedDraftConflictReplacement,
    ResolveDraftConflictCommand,
    decode_pending_draft_intent,
    encode_pending_draft_intent,
)
from finbot.application.draft_preparation import (
    DraftPreparationState,
    PreparedDraftResult,
    PrepareQuickDraftCommand,
)
from finbot.application.dto import (
    CreateDraftCommand,
    DraftConflictResolution,
    DraftRef,
    PreparedTransactionDraft,
    PrepareRepeatDraftCommand,
    ReviewedTransactionInput,
)
from finbot.application.errors import DraftRevisionConflictError, InvalidStateError
from finbot.application.use_cases.draft_conflicts import (
    PrepareDraftConflictReplacement,
    ResolveDraftConflict,
)
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000401")
TRANSACTION_ID = UUID("00000000-0000-7000-8000-000000000501")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000601")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000701")
OCCURRED_AT = datetime(2026, 8, 13, 12, tzinfo=ZoneInfo("Europe/Moscow"))


class _QuickDrafts:
    def __init__(self, result: PreparedDraftResult | None = None) -> None:
        self.result = result
        self.commands: list[PrepareQuickDraftCommand] = []

    async def execute(self, command: PrepareQuickDraftCommand) -> PreparedDraftResult:
        self.commands.append(command)
        if self.result is None:
            raise AssertionError("Quick preparation was not expected")
        return self.result


class _Targets:
    def __init__(self, repeated: PreparedTransactionDraft | None = None) -> None:
        self.repeated = repeated
        self.repeat_commands: list[PrepareRepeatDraftCommand] = []
        self.edit_calls: list[tuple[UUID, UUID, int]] = []

    async def prepare_repeat(
        self,
        command: PrepareRepeatDraftCommand,
    ) -> PreparedTransactionDraft:
        self.repeat_commands.append(command)
        if self.repeated is None:
            raise AssertionError("Repeat preparation was not expected")
        return self.repeated

    async def validate_edit(
        self,
        owner_id: UUID,
        transaction_id: UUID,
        expected_version: int,
    ) -> None:
        self.edit_calls.append((owner_id, transaction_id, expected_version))


def _reviewed() -> ReviewedTransactionInput:
    return ReviewedTransactionInput(
        TransactionType.EXPENSE,
        12_345,
        ACCOUNT_ID,
        CATEGORY_ID,
        OCCURRED_AT,
        "закрытое описание",
    )


def _prepared_repeat() -> PreparedTransactionDraft:
    return PreparedTransactionDraft(
        TRANSACTION_ID,
        7,
        _reviewed(),
        "RUB",
        "Закрытый счёт",
        "Закрытая категория",
        "▫️",
    )


def _legacy_repeat_intent() -> dict[str, object]:
    return {
        "kind": "repeat",
        "state": "quick_confirm",
        "payload": {
            "flow": "repeat",
            "type": "expense",
            "amount": 12_345,
            "account_id": str(ACCOUNT_ID),
            "account_name": "Закрытый счёт",
            "account_slug": "legacy-account",
            "category_id": str(CATEGORY_ID),
            "category_name": "Закрытая категория",
            "category_slug": "legacy-category",
            "currency": "RUB",
            "occurred_at": "2026-08-13T12:00:00+03:00",
            "description": "закрытое описание",
            "history_page": 9,
            "ui_message_id": 94_000_004,
        },
    }


@pytest.mark.parametrize(
    ("raw", "expected_type"),
    [
        ({"kind": "wizard"}, PendingWizardIntent),
        ({"kind": "quick", "text": "1450 закрытое описание"}, PendingQuickIntent),
        (
            {
                "kind": "edit",
                "transaction_id": str(TRANSACTION_ID),
                "version": "7",
                "history_page": 5,
                "ui_message_id": 94_000_004,
            },
            PendingEditIntent,
        ),
        (_legacy_repeat_intent(), PendingRepeatIntent),
    ],
)
def test_codec_accepts_only_real_intent_kinds_and_emits_channel_neutral_shapes(
    raw: dict[str, object],
    expected_type: type[object],
) -> None:
    intent = decode_pending_draft_intent(raw)

    assert isinstance(intent, expected_type)
    encoded = encode_pending_draft_intent(intent)
    rendered = repr(encoded)
    for forbidden in (
        "history_page",
        "ui_message_id",
        "presentation_ref",
        "telegram_chat_id",
        "telegram_message_id",
        "account_slug",
        "category_slug",
    ):
        assert forbidden not in rendered


def test_repeat_codec_normalizes_money_and_drops_legacy_metadata() -> None:
    intent = decode_pending_draft_intent(_legacy_repeat_intent())

    assert isinstance(intent, PendingRepeatIntent)
    payload = intent.to_draft_payload()
    assert payload["amount_minor"] == 12_345
    assert "amount" not in payload
    assert "history_page" not in payload
    assert "account_slug" not in payload
    assert "category_slug" not in payload


def test_repeat_producer_factory_preserves_authoritative_source_reference() -> None:
    intent = PendingRepeatIntent.from_prepared(_prepared_repeat())

    assert decode_pending_draft_intent(encode_pending_draft_intent(intent)) == intent


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"kind": "unknown"},
        {"kind": "quick", "text": ""},
        {"kind": "quick", "text": "1450", "ui_message_id": 1},
        {"kind": "repeat", "state": "arbitrary", "payload": {}},
        {
            "kind": "repeat",
            "payload": {
                **cast(dict[str, object], _legacy_repeat_intent()["payload"]),
                "source_transaction_id": 1,
            },
        },
        {"kind": "edit", "transaction_id": str(TRANSACTION_ID), "version": 0},
    ],
)
def test_codec_rejects_malformed_or_unbounded_intent(raw: object) -> None:
    with pytest.raises(InvalidStateError, match="Новое действие устарело"):
        decode_pending_draft_intent(raw)


@pytest.mark.asyncio
async def test_resume_discards_pending_intent_and_resumes_suspended_exact_draft() -> None:
    repository = InMemoryDraftRepository()
    drafts = DraftUseCases(repository)
    created = await drafts.create(
        CreateDraftCommand(
            OWNER_ID,
            "wizard_amount",
            {
                "flow": "wizard",
                "pending_intent": {"kind": "quick", "text": "1450 закрыто"},
            },
        )
    )
    suspended = await repository.set_suspended(OWNER_ID, created.ref, True)

    result = await ResolveDraftConflict(repository).execute(
        ResolveDraftConflictCommand(
            OWNER_ID,
            suspended.ref,
            DraftConflictResolution.RESUME,
        )
    )

    assert result.resolution is DraftConflictResolution.RESUME
    assert result.draft.draft_id == suspended.draft_id
    assert result.draft.revision == suspended.revision + 1
    assert result.draft.state == suspended.state
    assert result.draft.suspended is False
    assert dict(result.draft.payload) == {"flow": "wizard"}


@pytest.mark.asyncio
async def test_keep_discards_pending_intent_without_replacing_active_draft() -> None:
    repository = InMemoryDraftRepository()
    created = await DraftUseCases(repository).create(
        CreateDraftCommand(
            OWNER_ID,
            "review",
            {"flow": "quick", "pending_intent": {"kind": "wizard"}},
        )
    )

    result = await ResolveDraftConflict(repository).execute(
        ResolveDraftConflictCommand(
            OWNER_ID,
            created.ref,
            DraftConflictResolution.KEEP,
        )
    )

    assert result.resolution is DraftConflictResolution.KEEP
    assert result.draft.draft_id == created.draft_id
    assert result.draft.revision == created.revision + 1
    assert result.draft.suspended is False
    assert dict(result.draft.payload) == {"flow": "quick"}


@pytest.mark.asyncio
async def test_stale_reference_rejects_without_mutating_current_draft() -> None:
    repository = InMemoryDraftRepository()
    created = await DraftUseCases(repository).create(
        CreateDraftCommand(OWNER_ID, "wizard_type", {"flow": "wizard"})
    )
    stale = DraftRef(DRAFT_ID, created.revision)

    with pytest.raises(DraftRevisionConflictError):
        await ResolveDraftConflict(repository).execute(
            ResolveDraftConflictCommand(
                OWNER_ID,
                stale,
                DraftConflictResolution.RESUME,
            )
        )

    assert await repository.get_active(OWNER_ID) == created


@pytest.mark.asyncio
async def test_invalid_pending_intent_fails_closed_without_discarding_it() -> None:
    repository = InMemoryDraftRepository()
    created = await DraftUseCases(repository).create(
        CreateDraftCommand(
            OWNER_ID,
            "wizard_type",
            {"pending_intent": {"kind": "quick", "text": "1450", "unexpected": True}},
        )
    )

    with pytest.raises(InvalidStateError, match="Новое действие устарело"):
        await ResolveDraftConflict(repository).execute(
            ResolveDraftConflictCommand(
                OWNER_ID,
                created.ref,
                DraftConflictResolution.KEEP,
            )
        )

    assert await repository.get_active(OWNER_ID) == created


@pytest.mark.asyncio
async def test_replace_remains_blocked_without_authoritative_preparer() -> None:
    repository = InMemoryDraftRepository()
    created = await DraftUseCases(repository).create(
        CreateDraftCommand(
            OWNER_ID,
            "wizard_type",
            {"pending_intent": {"kind": "wizard"}},
        )
    )

    with pytest.raises(InvalidStateError, match="Безопасная замена"):
        await ResolveDraftConflict(repository).execute(
            ResolveDraftConflictCommand(
                OWNER_ID,
                created.ref,
                DraftConflictResolution.REPLACE,
            )
        )

    assert await repository.get_active(OWNER_ID) == created


@pytest.mark.asyncio
async def test_wizard_replace_creates_a_new_revision_one_channel_neutral_draft() -> None:
    repository = InMemoryDraftRepository()
    created = await DraftUseCases(repository).create(
        CreateDraftCommand(
            OWNER_ID,
            "review",
            {
                "flow": "quick",
                "pending_intent": {"kind": "wizard"},
            },
        )
    )
    replacements = PrepareDraftConflictReplacement(_QuickDrafts(), _Targets())

    result = await ResolveDraftConflict(repository, replacements).execute(
        ResolveDraftConflictCommand(
            OWNER_ID,
            created.ref,
            DraftConflictResolution.REPLACE,
        )
    )

    assert result.resolution is DraftConflictResolution.REPLACE
    assert result.draft.draft_id != created.draft_id
    assert result.draft.revision == 1
    assert result.draft.state == "wizard_type"
    assert result.draft.suspended is False
    assert dict(result.draft.payload) == {"flow": "wizard"}


@pytest.mark.asyncio
async def test_quick_replace_reparses_with_authoritative_shared_preparation() -> None:
    quick = _QuickDrafts(
        PreparedDraftResult(
            DraftPreparationState.REVIEW,
            {
                "flow": "quick",
                "type": "expense",
                "amount_minor": 12_345,
                "account_id": str(ACCOUNT_ID),
                "category_id": str(CATEGORY_ID),
                "occurred_at": OCCURRED_AT.isoformat(),
                "description": "закрытое описание",
            },
        )
    )
    replacements = PrepareDraftConflictReplacement(quick, _Targets())

    prepared = await replacements.prepare(
        OWNER_ID,
        PendingQuickIntent("1450 закрытое описание"),
    )

    assert quick.commands == [PrepareQuickDraftCommand(OWNER_ID, "1450 закрытое описание")]
    assert prepared.state == "review"
    assert dict(prepared.payload) == dict(cast(PreparedDraftResult, quick.result).payload)


@pytest.mark.asyncio
async def test_quick_replace_persists_only_the_reparsed_payload() -> None:
    repository = InMemoryDraftRepository()
    created = await DraftUseCases(repository).create(
        CreateDraftCommand(
            OWNER_ID,
            "wizard_type",
            {"pending_intent": {"kind": "quick", "text": "1450 закрыто"}},
        )
    )
    quick = _QuickDrafts(
        PreparedDraftResult(
            DraftPreparationState.CATEGORY_REQUIRED,
            {
                "flow": "quick",
                "type": "expense",
                "amount_minor": 145_000,
                "occurred_at": OCCURRED_AT.isoformat(),
                "description": "закрыто",
            },
        )
    )

    result = await ResolveDraftConflict(
        repository,
        PrepareDraftConflictReplacement(quick, _Targets()),
    ).execute(
        ResolveDraftConflictCommand(
            OWNER_ID,
            created.ref,
            DraftConflictResolution.REPLACE,
        )
    )

    assert result.draft.draft_id != created.draft_id
    assert result.draft.revision == 1
    assert result.draft.state == "category_required"
    assert "pending_intent" not in result.draft.payload
    assert "text" not in result.draft.payload


@pytest.mark.asyncio
async def test_repeat_replace_rebuilds_and_compares_the_authoritative_source() -> None:
    authoritative = _prepared_repeat()
    targets = _Targets(authoritative)
    replacements = PrepareDraftConflictReplacement(_QuickDrafts(), targets)
    intent = PendingRepeatIntent(
        _reviewed(),
        authoritative.currency,
        authoritative.account_name,
        authoritative.category_name,
        authoritative.category_emoji,
        authoritative.source_transaction_id,
        authoritative.source_version,
    )

    prepared = await replacements.prepare(OWNER_ID, intent)

    assert targets.repeat_commands == [
        PrepareRepeatDraftCommand(OWNER_ID, TRANSACTION_ID, 7, OCCURRED_AT)
    ]
    assert prepared.state == "review"
    assert dict(prepared.payload) == dict(authoritative.to_payload())


@pytest.mark.asyncio
async def test_typed_repeat_replace_persists_the_authoritative_snapshot() -> None:
    authoritative = _prepared_repeat()
    intent = PendingRepeatIntent.from_prepared(authoritative)
    repository = InMemoryDraftRepository()
    created = await DraftUseCases(repository).create(
        CreateDraftCommand(
            OWNER_ID,
            "review",
            {"pending_intent": encode_pending_draft_intent(intent)},
        )
    )

    result = await ResolveDraftConflict(
        repository,
        PrepareDraftConflictReplacement(_QuickDrafts(), _Targets(authoritative)),
    ).execute(
        ResolveDraftConflictCommand(
            OWNER_ID,
            created.ref,
            DraftConflictResolution.REPLACE,
        )
    )

    assert result.draft.draft_id != created.draft_id
    assert result.draft.revision == 1
    assert result.draft.state == "review"
    assert dict(result.draft.payload) == dict(authoritative.to_payload())


@pytest.mark.asyncio
async def test_legacy_repeat_without_source_reference_remains_fail_closed() -> None:
    repository = InMemoryDraftRepository()
    created = await DraftUseCases(repository).create(
        CreateDraftCommand(
            OWNER_ID,
            "wizard_type",
            {"pending_intent": _legacy_repeat_intent()},
        )
    )
    targets = _Targets(_prepared_repeat())

    with pytest.raises(InvalidStateError, match="Новое действие устарело"):
        await ResolveDraftConflict(
            repository,
            PrepareDraftConflictReplacement(_QuickDrafts(), targets),
        ).execute(
            ResolveDraftConflictCommand(
                OWNER_ID,
                created.ref,
                DraftConflictResolution.REPLACE,
            )
        )

    assert targets.repeat_commands == []
    assert await repository.get_active(OWNER_ID) == created


@pytest.mark.asyncio
async def test_repeat_rejects_tampered_pending_values_after_authoritative_reload() -> None:
    authoritative = _prepared_repeat()
    tampered = PendingRepeatIntent(
        ReviewedTransactionInput(
            TransactionType.EXPENSE,
            99_999,
            ACCOUNT_ID,
            CATEGORY_ID,
            OCCURRED_AT,
            "закрытое описание",
        ),
        authoritative.currency,
        authoritative.account_name,
        authoritative.category_name,
        authoritative.category_emoji,
        authoritative.source_transaction_id,
        authoritative.source_version,
    )

    with pytest.raises(InvalidStateError, match="Новое действие устарело"):
        await PrepareDraftConflictReplacement(
            _QuickDrafts(),
            _Targets(authoritative),
        ).prepare(OWNER_ID, tampered)


@pytest.mark.asyncio
async def test_edit_replace_validates_target_and_drops_navigation_metadata() -> None:
    targets = _Targets()
    replacements = PrepareDraftConflictReplacement(_QuickDrafts(), targets)
    intent = decode_pending_draft_intent(
        {
            "kind": "edit",
            "transaction_id": str(TRANSACTION_ID),
            "version": 7,
            "history_page": 12,
            "ui_message_id": 94_000_004,
        }
    )

    prepared = await replacements.prepare(OWNER_ID, intent)

    assert targets.edit_calls == [(OWNER_ID, TRANSACTION_ID, 7)]
    assert prepared.state == "edit_menu"
    assert dict(prepared.payload) == {
        "transaction_id": str(TRANSACTION_ID),
        "version": 7,
    }


@pytest.mark.asyncio
async def test_edit_replace_persists_only_authoritative_identity_and_version() -> None:
    repository = InMemoryDraftRepository()
    created = await DraftUseCases(repository).create(
        CreateDraftCommand(
            OWNER_ID,
            "review",
            {
                "pending_intent": {
                    "kind": "edit",
                    "transaction_id": str(TRANSACTION_ID),
                    "version": 7,
                    "history_page": 12,
                    "ui_message_id": 94_000_004,
                }
            },
        )
    )
    targets = _Targets()

    result = await ResolveDraftConflict(
        repository,
        PrepareDraftConflictReplacement(_QuickDrafts(), targets),
    ).execute(
        ResolveDraftConflictCommand(
            OWNER_ID,
            created.ref,
            DraftConflictResolution.REPLACE,
        )
    )

    assert targets.edit_calls == [(OWNER_ID, TRANSACTION_ID, 7)]
    assert result.draft.draft_id != created.draft_id
    assert result.draft.revision == 1
    assert result.draft.state == "edit_menu"
    assert dict(result.draft.payload) == {
        "transaction_id": str(TRANSACTION_ID),
        "version": 7,
    }


def test_replacement_contract_rejects_adapter_or_navigation_state() -> None:
    with pytest.raises(ValueError, match="adapter state"):
        PreparedDraftConflictReplacement(
            "review",
            {"flow": "quick", "history_page": 4},
        )


def test_intent_command_and_result_contracts_hide_private_values_from_repr() -> None:
    quick = decode_pending_draft_intent({"kind": "quick", "text": "1450 закрытое описание"})
    repeated = decode_pending_draft_intent(_legacy_repeat_intent())
    edited = decode_pending_draft_intent(
        {
            "kind": "edit",
            "transaction_id": str(TRANSACTION_ID),
            "version": 7,
        }
    )
    command = ResolveDraftConflictCommand(
        OWNER_ID,
        DraftRef(DRAFT_ID, 7),
        DraftConflictResolution.RESUME,
    )

    rendered = repr((quick, repeated, edited, command))
    for private in (
        str(OWNER_ID),
        str(DRAFT_ID),
        str(TRANSACTION_ID),
        str(ACCOUNT_ID),
        str(CATEGORY_ID),
        "12345",
        "закрыт",
        "RUB",
    ):
        assert private not in rendered
