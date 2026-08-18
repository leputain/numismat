from datetime import UTC, datetime
from uuid import uuid7

from finbot.adapters.telegram.presenters import (
    transaction_card,
    transaction_details_from_snapshot,
    transaction_snapshot_card,
)
from finbot.application.dto import TransactionSnapshot
from finbot.application.queries.transactions import TransactionDetails
from finbot.domain.transactions import TransactionType


def test_transaction_snapshot_maps_to_the_existing_card_projection() -> None:
    transaction_id = uuid7()
    account_id = uuid7()
    category_id = uuid7()
    deleted_at = datetime(2026, 8, 13, 13, tzinfo=UTC)
    snapshot = TransactionSnapshot(
        transaction_id=transaction_id,
        kind=TransactionType.EXPENSE,
        amount_minor=12_345,
        currency="RUB",
        account_id=account_id,
        account_name="Основной <счёт>",
        category_id=category_id,
        category_name="Кафе & рестораны",
        category_emoji="🍽",
        occurred_at=datetime(2026, 8, 13, 12, tzinfo=UTC),
        description="обед <важно>",
        source="manual",
        deleted_at=deleted_at,
        version=4,
    )

    details = transaction_details_from_snapshot(snapshot)

    assert details == TransactionDetails(
        id=transaction_id,
        type="expense",
        amount_minor=12_345,
        currency="RUB",
        account_id=account_id,
        account_name="Основной <счёт>",
        category_id=category_id,
        category_name="Кафе & рестораны",
        category_emoji="🍽",
        occurred_at=datetime(2026, 8, 13, 12, tzinfo=UTC),
        description="обед <важно>",
        source="manual",
        deleted_at=deleted_at,
        version=4,
    )
    assert transaction_snapshot_card(
        snapshot,
        "Europe/Moscow",
        title="🗑 Операция удалена",
    ) == transaction_card(
        details,
        "Europe/Moscow",
        title="🗑 Операция удалена",
    )
