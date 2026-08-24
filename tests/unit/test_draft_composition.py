from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from fakes.repositories import InMemoryDraftRepository

from finbot.application.draft_composition import (
    BeginComposeDraftCommand,
    ComposedDraftInput,
    ComposeDraftSpec,
    PrepareComposedDraftCommand,
)
from finbot.application.draft_conflicts import (
    PendingComposeIntent,
    decode_pending_draft_intent,
    encode_pending_draft_intent,
)
from finbot.application.draft_ingress import (
    BankImportDraftIngressBlockedError,
    DraftIngressOperation,
    DraftIngressStatus,
)
from finbot.application.draft_navigation import DraftCatalogRef
from finbot.application.draft_preparation import DraftPreparationState, PreparedDraftResult
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    CreateDraftCommand,
    OwnerSnapshot,
)
from finbot.application.errors import CatalogUnavailableError, InvalidStateError
from finbot.application.use_cases.draft_composition import PrepareComposedDraft
from finbot.application.use_cases.draft_conflicts import PrepareDraftConflictReplacement
from finbot.application.use_cases.draft_ingress import DraftIngressUseCases
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.domain.money import MoneyError
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000201")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000301")
NOW = datetime(2026, 8, 24, 15, 16, 17, tzinfo=ZoneInfo("Europe/Moscow"))


def _owner() -> OwnerSnapshot:
    return OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", ACCOUNT_ID)


def _account(*, version: int = 3) -> AccountSnapshot:
    return AccountSnapshot(ACCOUNT_ID, "Основной", "cash", "RUB", None, version)


def _category(*, version: int = 4) -> CategorySnapshot:
    return CategorySnapshot(
        CATEGORY_ID,
        TransactionType.EXPENSE,
        "Другое",
        "▫️",
        None,
        version,
    )


class _Owners:
    def __init__(self, owner: OwnerSnapshot | None = None) -> None:
        self.owner = owner

    async def get_owner(self, owner_id: UUID) -> OwnerSnapshot | None:
        assert owner_id == OWNER_ID
        return self.owner


class _OwnerQuery:
    async def __call__(self, owner_id: UUID) -> OwnerSnapshot:
        assert owner_id == OWNER_ID
        return _owner()


class _Catalogs:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.account = _account()
        self.category = _category()

    async def select_account(self, owner_id: UUID, reference: DraftCatalogRef) -> AccountSnapshot:
        self.calls.append(("select_account", reference))
        assert owner_id == OWNER_ID
        return self.account

    async def select_category(
        self,
        owner_id: UUID,
        kind: TransactionType,
        reference: DraftCatalogRef,
    ) -> CategorySnapshot:
        self.calls.append(("select_category", reference))
        assert owner_id == OWNER_ID
        assert kind is TransactionType.EXPENSE
        return self.category

    async def resolve_account(
        self,
        owner_id: UUID,
        hint: str | None,
        default_account_id: UUID | None,
    ) -> AccountSnapshot | None:
        self.calls.append(("resolve_account", (hint, default_account_id)))
        assert owner_id == OWNER_ID
        return self.account

    async def resolve_fallback_category(
        self,
        owner_id: UUID,
        kind: TransactionType,
    ) -> CategorySnapshot:
        self.calls.append(("resolve_category", kind))
        assert owner_id == OWNER_ID
        return self.category


class _Clock:
    def now(self, timezone: str) -> datetime:
        assert timezone == "Europe/Moscow"
        return NOW


class _Repeats:
    async def prepare_repeat(self, *_args: object) -> object:
        raise AssertionError("repeat preparation was not expected")


class _Composer:
    def __init__(self, result: PreparedDraftResult) -> None:
        self.result = result
        self.commands: list[PrepareComposedDraftCommand] = []

    async def execute(self, command: PrepareComposedDraftCommand) -> PreparedDraftResult:
        self.commands.append(command)
        return self.result


class _UnexpectedQuick:
    async def execute(self, *_args: object) -> PreparedDraftResult:
        raise AssertionError("quick preparation was not expected")


class _UnexpectedTargets:
    async def prepare_repeat(self, *_args: object) -> object:
        raise AssertionError("repeat preparation was not expected")

    async def validate_edit(self, *_args: object) -> None:
        raise AssertionError("edit validation was not expected")


def _values(
    *,
    account: DraftCatalogRef | None = None,
    category: DraftCatalogRef | None = None,
) -> ComposedDraftInput:
    return ComposedDraftInput(
        TransactionType.EXPENSE,
        50_050,
        NOW,
        account,
        category,
        "обед",
    )


def _review_result(values: ComposedDraftInput | None = None) -> PreparedDraftResult:
    chosen = values or _values()
    return PreparedDraftResult(
        DraftPreparationState.REVIEW,
        {
            "flow": "quick",
            "type": chosen.kind.value,
            "amount_minor": chosen.amount_minor,
            "account_id": str(ACCOUNT_ID),
            "account_name": "Основной",
            "category_id": str(CATEGORY_ID),
            "category_name": "Другое",
            "category_emoji": "▫️",
            "currency": "RUB",
            "occurred_at": chosen.occurred_at.isoformat(),
            "description": chosen.description,
        },
    )


def test_compose_contract_rejects_float_money_and_hides_values_from_repr() -> None:
    with pytest.raises(MoneyError):
        ComposeDraftSpec(TransactionType.EXPENSE, 500.5)  # type: ignore[arg-type]

    spec = ComposeDraftSpec(TransactionType.EXPENSE, 50_050, description="секрет")
    command = spec.bind(OWNER_ID)
    values = _values()

    assert "50050" not in repr(spec)
    assert "секрет" not in repr(spec)
    assert str(OWNER_ID) not in repr(command)
    assert "обед" not in repr(values)


@pytest.mark.asyncio
async def test_composer_resolves_defaults_and_returns_review_without_transaction_write() -> None:
    catalogs = _Catalogs()
    values = _values()

    result = await PrepareComposedDraft(_Owners(_owner()), catalogs).execute(
        PrepareComposedDraftCommand(OWNER_ID, values)
    )

    assert result.state is DraftPreparationState.REVIEW
    assert dict(result.payload) == {
        "flow": "quick",
        "type": "expense",
        "amount_minor": 50_050,
        "account_id": str(ACCOUNT_ID),
        "account_name": "Основной",
        "category_id": str(CATEGORY_ID),
        "category_name": "Другое",
        "category_emoji": "▫️",
        "currency": "RUB",
        "occurred_at": NOW.isoformat(),
        "description": "обед",
        "category_explicit": False,
        "needs_confirmation": False,
    }
    assert catalogs.calls == [
        ("resolve_account", (None, ACCOUNT_ID)),
        ("resolve_category", TransactionType.EXPENSE),
    ]


@pytest.mark.asyncio
async def test_composer_uses_exact_versioned_references_and_fails_closed_on_mismatch() -> None:
    catalogs = _Catalogs()
    account = DraftCatalogRef(ACCOUNT_ID, 3)
    category = DraftCatalogRef(CATEGORY_ID, 4)
    command = PrepareComposedDraftCommand(OWNER_ID, _values(account=account, category=category))

    result = await PrepareComposedDraft(_Owners(_owner()), catalogs).execute(command)

    assert result.payload["category_explicit"] is True
    assert catalogs.calls == [("select_account", account), ("select_category", category)]

    catalogs.account = _account(version=5)
    with pytest.raises(CatalogUnavailableError, match="Счёт"):
        await PrepareComposedDraft(_Owners(_owner()), catalogs).execute(command)


def test_compose_pending_codec_is_exact_normalized_and_has_no_raw_body() -> None:
    account = DraftCatalogRef(ACCOUNT_ID, 3)
    category = DraftCatalogRef(CATEGORY_ID, 4)
    intent = PendingComposeIntent(_values(account=account, category=category))

    encoded = encode_pending_draft_intent(intent)

    assert encoded == {
        "kind": "compose",
        "type": "expense",
        "amount_minor": 50_050,
        "account_id": str(ACCOUNT_ID),
        "account_version": 3,
        "category_id": str(CATEGORY_ID),
        "category_version": 4,
        "occurred_at": NOW.isoformat(),
        "description": "обед",
    }
    assert "amount" not in encoded
    assert "body" not in encoded
    assert decode_pending_draft_intent(encoded) == intent

    with pytest.raises(InvalidStateError, match="Новое действие устарело"):
        decode_pending_draft_intent({**encoded, "raw_body": "forbidden"})
    with pytest.raises(InvalidStateError, match="Новое действие устарело"):
        decode_pending_draft_intent({**encoded, "account_id": None, "account_version": None})


@pytest.mark.asyncio
async def test_compose_conflict_replace_revalidates_normalized_intent() -> None:
    values = _values(
        account=DraftCatalogRef(ACCOUNT_ID, 3),
        category=DraftCatalogRef(CATEGORY_ID, 4),
    )
    composer = _Composer(_review_result(values))
    replacements = PrepareDraftConflictReplacement(
        _UnexpectedQuick(),  # type: ignore[arg-type]
        _UnexpectedTargets(),  # type: ignore[arg-type]
        compose_drafts=composer,
    )

    prepared = await replacements.prepare(OWNER_ID, PendingComposeIntent(values))

    assert composer.commands == [PrepareComposedDraftCommand(OWNER_ID, values)]
    assert prepared.state == "review"
    assert dict(prepared.payload) == dict(_review_result(values).payload)


@pytest.mark.asyncio
async def test_compose_ingress_starts_review_and_resolves_local_date_once() -> None:
    repository = InMemoryDraftRepository()
    expected_values = ComposedDraftInput(
        TransactionType.EXPENSE,
        50_050,
        datetime(2026, 8, 20, 15, 16, 17, tzinfo=ZoneInfo("Europe/Moscow")),
        description="обед",
    )
    composer = _Composer(_review_result(expected_values))
    use_cases = DraftIngressUseCases(
        DraftUseCases(repository),
        _Repeats(),  # type: ignore[arg-type]
        _OwnerQuery(),
        _Clock(),
        compose_drafts=composer,
    )

    result = await use_cases.begin_compose(
        BeginComposeDraftCommand(
            OWNER_ID,
            ComposeDraftSpec(
                TransactionType.EXPENSE,
                50_050,
                occurred_on=date(2026, 8, 20),
                description="обед",
            ),
        )
    )

    assert result.operation is DraftIngressOperation.COMPOSE
    assert result.status is DraftIngressStatus.STARTED
    assert result.draft.state == "review"
    assert composer.commands == [PrepareComposedDraftCommand(OWNER_ID, expected_values)]


@pytest.mark.asyncio
async def test_compose_ingress_stages_typed_conflict_without_preparing_or_overwriting() -> None:
    repository = InMemoryDraftRepository()
    current = await DraftUseCases(repository).create(
        CreateDraftCommand(OWNER_ID, "wizard_amount", {"flow": "wizard"})
    )
    composer = _Composer(_review_result())
    use_cases = DraftIngressUseCases(
        DraftUseCases(repository),
        _Repeats(),  # type: ignore[arg-type]
        _OwnerQuery(),
        _Clock(),
        compose_drafts=composer,
    )

    result = await use_cases.begin_compose(
        ComposeDraftSpec(TransactionType.EXPENSE, 50_050, description="обед").bind(OWNER_ID)
    )

    assert result.status is DraftIngressStatus.CONFLICT
    assert result.draft.draft_id == current.draft_id
    assert result.draft.state == current.state
    assert composer.commands == []
    pending = result.draft.payload["pending_intent"]
    assert isinstance(pending, dict)
    assert pending["kind"] == "compose"
    assert pending["amount_minor"] == 50_050
    assert "amount" not in pending
    assert "body" not in pending


@pytest.mark.asyncio
async def test_compose_ingress_keeps_bank_import_exclusive() -> None:
    repository = InMemoryDraftRepository()
    current = await DraftUseCases(repository).create(
        CreateDraftCommand(OWNER_ID, "review", {"flow": "bank_import"})
    )
    use_cases = DraftIngressUseCases(
        DraftUseCases(repository),
        _Repeats(),  # type: ignore[arg-type]
        _OwnerQuery(),
        _Clock(),
        compose_drafts=_Composer(_review_result()),
    )

    with pytest.raises(BankImportDraftIngressBlockedError):
        await use_cases.begin_compose(
            ComposeDraftSpec(TransactionType.EXPENSE, 50_050).bind(OWNER_ID)
        )

    assert await repository.get_active(OWNER_ID) == current
