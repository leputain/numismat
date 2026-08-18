from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from fakes.draft_preparation import (
    FixedClock,
    StubCategoryRuleReader,
    StubDraftCatalogResolver,
    StubOwnerReader,
    StubQuickDraftParser,
)

from finbot.application.draft_preparation import (
    DraftPreparationState,
    PreparedDraftResult,
    PrepareParsedDraftCommand,
    PrepareQuickDraftCommand,
)
from finbot.application.dto import AccountSnapshot, CategorySnapshot, OwnerSnapshot
from finbot.application.errors import ApplicationValidationError, EntityNotFoundError
from finbot.application.use_cases.draft_preparation import (
    PrepareParsedDraft,
    PrepareQuickDraft,
)
from finbot.application.use_cases.ocr_queue import SharedOcrDraftPreparer
from finbot.domain.category_rules import CategoryRuleSpec
from finbot.domain.transactions import TransactionDraft, TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000001")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000002")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000003")
LEARNED_CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000004")
RULE_ID = UUID("00000000-0000-7000-8000-000000000005")
NOW = datetime(2026, 8, 13, 12, 30, tzinfo=ZoneInfo("Europe/Moscow"))


def _owner() -> OwnerSnapshot:
    return OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", ACCOUNT_ID)


def _account() -> AccountSnapshot:
    return AccountSnapshot(ACCOUNT_ID, "Основной", "card", "RUB", None, 1)


def _category(
    category_id: UUID = CATEGORY_ID,
    name: str = "Другое",
) -> CategorySnapshot:
    return CategorySnapshot(category_id, TransactionType.EXPENSE, name, "▫️", None, 1)


def _harness(
    *,
    account: AccountSnapshot | None = None,
    category: CategorySnapshot | None = None,
    rules: tuple[CategoryRuleSpec, ...] = (),
    learned_categories: dict[UUID, CategorySnapshot] | None = None,
    clock_value: datetime = NOW,
) -> tuple[
    PrepareParsedDraft,
    StubDraftCatalogResolver,
    StubCategoryRuleReader,
    FixedClock,
]:
    catalogs = StubDraftCatalogResolver(
        account,
        category,
        categories_by_id=learned_categories or {},
    )
    rule_reader = StubCategoryRuleReader(rules)
    clock = FixedClock(clock_value)
    use_case = PrepareParsedDraft(
        StubOwnerReader({OWNER_ID: _owner()}),
        catalogs,
        rule_reader,
        clock,
    )
    return use_case, catalogs, rule_reader, clock


@pytest.mark.asyncio
async def test_prepare_parsed_draft_resolves_defaults_and_uses_aware_clock() -> None:
    use_case, catalogs, rules, clock = _harness(account=_account(), category=_category())

    result = await use_case.execute(
        PrepareParsedDraftCommand(
            OWNER_ID,
            TransactionDraft(12_345, TransactionType.EXPENSE, description="покупка"),
        )
    )

    assert result.state is DraftPreparationState.REVIEW
    assert dict(result.payload) == {
        "flow": "quick",
        "type": "expense",
        "amount_minor": 12_345,
        "occurred_at": NOW.isoformat(),
        "description": "покупка",
        "category_explicit": False,
        "needs_confirmation": False,
        "account_id": str(ACCOUNT_ID),
        "account_name": "Основной",
        "currency": "RUB",
        "category_id": str(CATEGORY_ID),
        "category_name": "Другое",
        "category_emoji": "▫️",
    }
    assert catalogs.account_calls == [(OWNER_ID, None, ACCOUNT_ID)]
    assert catalogs.category_calls == [(OWNER_ID, TransactionType.EXPENSE, None)]
    assert rules.calls == [(OWNER_ID, TransactionType.EXPENSE, ACCOUNT_ID)]
    assert clock.requested_timezones == ["Europe/Moscow"]


@pytest.mark.asyncio
async def test_explicit_hints_bypass_learned_category() -> None:
    use_case, catalogs, rules, _clock = _harness(account=_account(), category=_category())

    result = await use_case.execute(
        PrepareParsedDraftCommand(
            OWNER_ID,
            TransactionDraft(
                50_000,
                TransactionType.EXPENSE,
                occurred_at=NOW,
                account_hint="Карта Мир",
                category_hint="Кафе",
                category_explicit=True,
                description="обед",
            ),
        )
    )

    assert result.state is DraftPreparationState.REVIEW
    assert catalogs.account_calls == [(OWNER_ID, "Карта Мир", ACCOUNT_ID)]
    assert catalogs.category_calls == [(OWNER_ID, TransactionType.EXPENSE, "Кафе")]
    assert rules.calls == []


@pytest.mark.asyncio
async def test_learned_category_precedes_parser_hint_for_non_explicit_input() -> None:
    learned = _category(LEARNED_CATEGORY_ID, "Кофейни")
    rule = CategoryRuleSpec(
        RULE_ID,
        LEARNED_CATEGORY_ID,
        TransactionType.EXPENSE,
        "кофейня",
        ACCOUNT_ID,
        updated_at=NOW,
    )
    use_case, catalogs, rules, _clock = _harness(
        account=_account(),
        category=_category(),
        rules=(rule,),
        learned_categories={LEARNED_CATEGORY_ID: learned},
    )

    result = await use_case.execute(
        PrepareParsedDraftCommand(
            OWNER_ID,
            TransactionDraft(
                42_000,
                TransactionType.EXPENSE,
                category_hint="Другое",
                description="Любимая кофейня",
            ),
        )
    )

    assert result.state is DraftPreparationState.REVIEW
    assert result.payload["category_id"] == str(LEARNED_CATEGORY_ID)
    assert rules.calls == [(OWNER_ID, TransactionType.EXPENSE, ACCOUNT_ID)]
    assert catalogs.get_category_calls == [(OWNER_ID, TransactionType.EXPENSE, LEARNED_CATEGORY_ID)]
    assert catalogs.category_calls == []


@pytest.mark.asyncio
async def test_missing_category_has_priority_and_preserves_resolved_account() -> None:
    use_case, _catalogs, _rules, _clock = _harness(account=_account(), category=None)

    result = await use_case.execute(
        PrepareParsedDraftCommand(
            OWNER_ID,
            TransactionDraft(100, TransactionType.EXPENSE, description="неизвестно"),
        )
    )

    assert result.state is DraftPreparationState.CATEGORY_REQUIRED
    assert result.payload["account_id"] == str(ACCOUNT_ID)
    assert "category_id" not in result.payload


@pytest.mark.asyncio
async def test_missing_account_returns_account_required_after_category_resolution() -> None:
    use_case, _catalogs, rules, _clock = _harness(account=None, category=_category())

    result = await use_case.execute(
        PrepareParsedDraftCommand(
            OWNER_ID,
            TransactionDraft(100, TransactionType.EXPENSE, description="покупка"),
        )
    )

    assert result.state is DraftPreparationState.ACCOUNT_REQUIRED
    assert result.payload["category_id"] == str(CATEGORY_ID)
    assert "account_id" not in result.payload
    assert rules.calls == [(OWNER_ID, TransactionType.EXPENSE, None)]


@pytest.mark.asyncio
async def test_prepare_quick_uses_owner_timezone_and_translates_parser_failure() -> None:
    draft = TransactionDraft(10_000, TransactionType.EXPENSE, description="метро")
    parser = StubQuickDraftParser(result=draft)
    owners = StubOwnerReader({OWNER_ID: _owner()})
    parsed, _catalogs, _rules, _clock = _harness(account=_account(), category=_category())
    quick = PrepareQuickDraft(owners, parser, parsed)

    result = await quick.execute(PrepareQuickDraftCommand(OWNER_ID, "100 метро"))

    assert result.state is DraftPreparationState.REVIEW
    assert parser.calls == [("100 метро", "Europe/Moscow")]
    assert owners.calls == [OWNER_ID]

    failing = PrepareQuickDraft(
        owners,
        StubQuickDraftParser(error=ValueError("sensitive parser input")),
        parsed,
    )
    with pytest.raises(ApplicationValidationError, match="Быстрый ввод не распознан"):
        await failing.execute(PrepareQuickDraftCommand(OWNER_ID, "секретный текст"))


@pytest.mark.asyncio
async def test_ocr_preparer_reuses_the_shared_policy_with_an_ocr_flow() -> None:
    parsed, _catalogs, _rules, _clock = _harness(account=_account(), category=_category())

    result = await SharedOcrDraftPreparer(parsed).prepare(
        OWNER_ID,
        TransactionDraft(10_000, TransactionType.EXPENSE, description="покупка"),
    )

    assert result.state == DraftPreparationState.REVIEW.value
    assert result.payload["flow"] == "ocr"
    assert result.payload["amount_minor"] == 10_000
    assert "amount" not in result.payload


@pytest.mark.asyncio
async def test_missing_owner_and_naive_clock_are_stable_application_errors() -> None:
    catalogs = StubDraftCatalogResolver(_account(), _category())
    rules = StubCategoryRuleReader()
    missing = PrepareParsedDraft(StubOwnerReader({}), catalogs, rules, FixedClock(NOW))
    command = PrepareParsedDraftCommand(
        OWNER_ID,
        TransactionDraft(100, TransactionType.EXPENSE, description="покупка"),
    )

    with pytest.raises(EntityNotFoundError):
        await missing.execute(command)

    naive, _catalogs, _rules, _clock = _harness(
        account=_account(),
        category=_category(),
        clock_value=datetime(2026, 8, 13, 12, 30),
    )
    with pytest.raises(ApplicationValidationError, match="часовой пояс"):
        await naive.execute(command)


def test_preparation_contract_hides_sensitive_values_and_rejects_adapter_keys() -> None:
    parsed_command = PrepareParsedDraftCommand(
        OWNER_ID,
        TransactionDraft(12_345, TransactionType.EXPENSE, description="тайное описание"),
    )
    quick_command = PrepareQuickDraftCommand(OWNER_ID, "123.45 тайный ввод")
    result = PreparedDraftResult(
        DraftPreparationState.REVIEW,
        {"amount_minor": 12_345, "description": "тайное описание"},
    )

    for rendered in (repr(parsed_command), repr(quick_command), repr(result)):
        assert "12_345" not in rendered
        assert "123.45" not in rendered
        assert "тайн" not in rendered
        assert str(OWNER_ID) not in rendered

    assert "amount" not in result.payload
    for key in (
        "ui_message_id",
        "history_page",
        "pending_intent",
        "category_slug",
    ):
        with pytest.raises(ValueError, match="adapter state"):
            PreparedDraftResult(DraftPreparationState.REVIEW, {key: "adapter-only"})
