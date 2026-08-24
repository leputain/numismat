import json
from datetime import UTC, datetime
from uuid import uuid7

import pytest
from aiogram.types import InlineKeyboardMarkup
from hypothesis import given
from hypothesis import strategies as st

from finbot.adapters.telegram.parser import DeterministicParser
from finbot.adapters.telegram.presenters import money, transaction_card, wizard_summary
from finbot.adapters.telegram.ui import (
    MAIN_MENU,
    Choice,
    account_keyboard,
    category_keyboard,
    edit_date_keyboard,
    edit_input_keyboard,
    edit_keyboard,
    history_keyboard,
    settings_account_archive_keyboard,
    settings_account_keyboard,
    settings_accounts_keyboard,
    settings_archived_accounts_keyboard,
    settings_archived_categories_keyboard,
    settings_categories_keyboard,
    settings_category_archive_keyboard,
    settings_category_keyboard,
    settings_help_keyboard,
    settings_keyboard,
    settings_text_input_keyboard,
    settings_timezones_keyboard,
    transaction_keyboard,
    wizard_confirm_keyboard,
    wizard_date_keyboard,
    wizard_description_keyboard,
    wizard_input_keyboard,
    wizard_type_keyboard,
)
from finbot.application.interactions import DraftAction, DraftInteraction
from finbot.application.queries.transactions import TransactionDetails
from finbot.application.services.catalogs import catalog_slug, normalize_catalog_name
from finbot.application.services.csv_export import amount_major, build_csv
from finbot.domain.dates import parse_local_datetime
from finbot.domain.money import parse_minor
from finbot.observability.logging import JsonFormatter, redact


@given(st.integers(min_value=1, max_value=10**12))
def test_money_major_round_trip(amount_minor: int) -> None:
    rendered = amount_major(amount_minor)
    assert parse_minor(rendered) == amount_minor


@given(st.integers(min_value=1, max_value=10**9))
def test_parser_never_creates_zero_amount(amount: int) -> None:
    draft = DeterministicParser().parse(f"{amount} ресторан")
    assert draft.amount_minor > 0


def test_parser_rejects_ambiguous_dot_amount() -> None:
    with pytest.raises(ValueError, match="неоднозначна"):
        DeterministicParser().parse("1.500 ресторан")


def test_parser_only_confirms_suspicious_dates() -> None:
    parser = DeterministicParser()
    assert not parser.parse("вчера 799 кино").needs_confirmation
    assert parser.parse("01.01.2020 799 кино").needs_confirmation


def test_local_date_parser() -> None:
    now = datetime(2026, 8, 7, 15, 30, tzinfo=UTC)
    parsed = parse_local_datetime("12.08.2025", "Europe/Moscow", now=now)
    assert parsed.isoformat() == "2025-08-12T18:30:00+03:00"
    with pytest.raises(ValueError, match="ДД.ММ"):
        parse_local_datetime("завтра вечером", "Europe/Moscow", now=now)


def test_callback_data_is_compact_and_contains_no_financial_data() -> None:
    item_id = uuid7()
    keyboards = [
        wizard_type_keyboard(),
        wizard_date_keyboard(),
        transaction_keyboard(item_id, 123),
        category_keyboard([Choice(uuid7(), "Очень длинная пользовательская категория")]),
        settings_account_archive_keyboard(item_id, 2),
        settings_category_archive_keyboard(item_id, 2),
    ]
    for keyboard in keyboards:
        for row in keyboard.inline_keyboard:
            for button in row:
                if button.callback_data:
                    assert len(button.callback_data.encode("utf-8")) <= 64
                    assert "1450" not in button.callback_data


def test_main_menu_has_clear_primary_action() -> None:
    labels = [button.text for row in MAIN_MENU.keyboard for button in row]
    assert labels[0] == "➕ Добавить"
    assert "⚡ Быстрый ввод" not in labels
    assert "🧾 Операции" in labels
    assert "••• Ещё" in labels
    assert "⚙️ Настройки" not in labels
    assert MAIN_MENU.is_persistent is True
    assert MAIN_MENU.one_time_keyboard is not True


def _callback_values(keyboard: InlineKeyboardMarkup) -> set[str]:
    rows = keyboard.inline_keyboard
    return {
        button.callback_data for row in rows for button in row if button.callback_data is not None
    }


def test_wizard_screens_have_back_without_replacing_cancel() -> None:
    choices = [Choice(uuid7(), "Тест")]
    for keyboard in (
        wizard_input_keyboard(),
        category_keyboard(choices),
        account_keyboard(choices),
        wizard_date_keyboard(),
        wizard_description_keyboard(),
        wizard_confirm_keyboard(False),
    ):
        callbacks = _callback_values(keyboard)
        assert "w:back" in callbacks
        assert "w:cancel" in callbacks

    assert "w:back" not in _callback_values(wizard_type_keyboard())


def test_ocr_batch_review_offers_save_next_and_skip() -> None:
    draft_id = uuid7()
    batch = wizard_confirm_keyboard(
        True,
        draft_id=draft_id,
        revision=2,
        ocr_batch=True,
        ocr_has_more=True,
    )
    labels = [button.text for row in batch.inline_keyboard for button in row]
    actions = {
        DraftInteraction.decode(value).action
        for value in _callback_values(batch)
        if value.startswith("d")
    }

    assert "✅ Сохранить и дальше" in labels
    assert "⏭ Пропустить эту операцию" in labels
    assert DraftAction.CONFIRM in actions
    assert DraftAction.SKIP_OCR_ITEM in actions

    final_labels = [
        button.text for row in wizard_confirm_keyboard(False).inline_keyboard for button in row
    ]
    assert "✅ Сохранить" in final_labels
    assert "⏭ Пропустить эту операцию" not in final_labels

    final_batch_labels = [
        button.text
        for row in wizard_confirm_keyboard(False, ocr_batch=True).inline_keyboard
        for button in row
    ]
    assert "✅ Сохранить" in final_batch_labels
    assert "⏭ Пропустить эту операцию" in final_batch_labels


def test_context_navigation_returns_to_previous_screen_and_history_page() -> None:
    item_id = uuid7()
    callbacks = _callback_values(transaction_keyboard(item_id, 4, history_page=3))
    assert "h:3" in callbacks
    assert f"tx:edit:{item_id}:4:3" in callbacks
    assert "e:back" in _callback_values(edit_input_keyboard())
    assert "e:dateback" in _callback_values(edit_input_keyboard(date_menu=True))
    assert "s:back" in _callback_values(settings_help_keyboard())

    history_callbacks = _callback_values(
        history_keyboard([(item_id, 4, "−1 450 ₽ · Рестораны")], 3, 5)
    )
    assert f"tx:view:{item_id}:4:3" in history_callbacks


def test_financial_edit_keyboards_bind_every_action_to_draft_revision() -> None:
    draft_id = uuid7()
    transaction_id = uuid7()
    category = Choice(uuid7(), "Продукты", version=7)
    account = Choice(uuid7(), "Карта", version=9)

    menu_actions = {
        DraftInteraction.decode(value).action
        for value in _callback_values(
            edit_keyboard(
                transaction_id,
                4,
                3,
                draft_id=draft_id,
                revision=11,
            )
        )
        if value.startswith("d")
    }
    assert menu_actions == {
        DraftAction.TX_EDIT_AMOUNT,
        DraftAction.TX_EDIT_CATEGORY,
        DraftAction.TX_EDIT_ACCOUNT,
        DraftAction.TX_EDIT_DATE,
        DraftAction.TX_EDIT_DESCRIPTION,
    }

    category_callbacks = _callback_values(
        category_keyboard([category], edit=True, draft_id=draft_id, revision=12)
    )
    category_choice = next(
        DraftInteraction.decode(value)
        for value in category_callbacks
        if DraftInteraction.decode(value).action == DraftAction.TX_SELECT_CATEGORY
    )
    assert (category_choice.object_id, category_choice.object_version) == (
        category.id,
        category.version,
    )

    account_callbacks = _callback_values(
        account_keyboard([account], edit=True, draft_id=draft_id, revision=13)
    )
    account_choice = next(
        DraftInteraction.decode(value)
        for value in account_callbacks
        if DraftInteraction.decode(value).action == DraftAction.TX_SELECT_ACCOUNT
    )
    assert (account_choice.object_id, account_choice.object_version) == (
        account.id,
        account.version,
    )

    date_actions = {
        DraftInteraction.decode(value).action
        for value in _callback_values(
            edit_date_keyboard(
                transaction_id,
                4,
                3,
                draft_id=draft_id,
                revision=14,
            )
        )
    }
    assert date_actions == {DraftAction.TX_SELECT_DATE, DraftAction.TX_BACK}
    input_back = next(
        iter(
            _callback_values(
                edit_input_keyboard(
                    date_menu=True,
                    draft_id=draft_id,
                    revision=15,
                )
            )
        )
    )
    assert DraftInteraction.decode(input_back).action == DraftAction.TX_DATE_BACK


def test_settings_catalog_keyboards_have_safe_navigation() -> None:
    item_id = uuid7()
    choices = [Choice(item_id, "Наличные", "💵", 7)]

    account_list = _callback_values(settings_accounts_keyboard(choices, item_id, 1))
    assert f"sa:view:{item_id}:7" in account_list
    assert "sa:new" in account_list
    assert "sa:archived" in account_list
    assert "s:back" in account_list

    account = _callback_values(settings_account_keyboard(item_id, 7, is_default=False))
    assert f"sa:default:{item_id}:7" in account
    assert f"sa:rename:{item_id}:7" in account
    assert f"sa:archive:ask:{item_id}:7" in account
    assert "sa:list" in account
    assert f"sa:archive:do:{item_id}:7" in _callback_values(
        settings_account_archive_keyboard(item_id, 7)
    )
    assert f"sa:restore:{item_id}:7" in _callback_values(
        settings_archived_accounts_keyboard(choices)
    )

    categories = _callback_values(settings_categories_keyboard(12, 5))
    assert categories >= {"sc:list:expense", "sc:list:income", "s:back"}
    category = _callback_values(settings_category_keyboard(item_id, "expense", 7))
    assert f"sc:rename:{item_id}:7" in category
    assert f"sc:archive:ask:{item_id}:7" in category
    assert "sc:list:expense" in category
    assert f"sc:archive:do:{item_id}:7" in _callback_values(
        settings_category_archive_keyboard(item_id, 7)
    )
    assert f"sc:restore:{item_id}:7" in _callback_values(
        settings_archived_categories_keyboard(choices, "expense")
    )


def test_settings_discard_is_bound_to_the_current_draft() -> None:
    draft_id = uuid7()
    callbacks = _callback_values(settings_keyboard(False, draft_id, 7))
    encoded = next(value for value in callbacks if value.startswith("d"))
    interaction = DraftInteraction.decode(encoded)
    assert interaction.action == DraftAction.DISCARD
    assert interaction.draft_id == draft_id
    assert interaction.revision == 7
    assert "s:reset" not in _callback_values(settings_keyboard(False))

    input_callbacks = _callback_values(settings_text_input_keyboard(draft_id, 8))
    assert len(input_callbacks) == 1
    input_discard = DraftInteraction.decode(next(iter(input_callbacks)))
    assert input_discard.action == DraftAction.DISCARD
    assert (input_discard.draft_id, input_discard.revision) == (draft_id, 8)


def test_timezone_buttons_are_bound_to_the_rendered_settings_version() -> None:
    callbacks = _callback_values(settings_timezones_keyboard("Europe/Moscow", 17))
    assert "s:timezone:0:17" in callbacks


def test_catalog_names_are_normalized_and_validated() -> None:
    assert normalize_catalog_name("  Карта   Мир  ") == "Карта Мир"
    assert catalog_slug("  Карта   Мир  ") == "карта-мир"
    with pytest.raises(ValueError, match="пустым"):
        normalize_catalog_name("   ")
    with pytest.raises(ValueError, match="короче"):
        normalize_catalog_name("я" * 61)


def test_presenters_escape_user_text() -> None:
    item = TransactionDetails(
        id=uuid7(),
        type="expense",
        amount_minor=145000,
        currency="RUB",
        account_id=uuid7(),
        account_name="Карта <script>",
        category_id=uuid7(),
        category_name="Кафе & рестораны",
        category_emoji="🍽",
        occurred_at=datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
        description="<b>не HTML</b>",
        source="manual",
        deleted_at=None,
        version=1,
    )
    rendered = transaction_card(item, "Europe/Moscow")
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "&lt;b&gt;не HTML&lt;/b&gt;" in rendered


def test_wizard_summary_contains_all_important_fields() -> None:
    payload: dict[str, object] = {
        "type": "expense",
        "amount": 145000,
        "category_name": "Кафе и рестораны",
        "account_name": "Основная карта",
        "occurred_at": "2026-08-07T19:42:00+03:00",
        "description": "ужин",
    }
    rendered = wizard_summary(payload, "RUB", "Europe/Moscow")
    assert "−1 450 ₽" in rendered
    assert "Кафе и рестораны" in rendered
    assert "Основная карта" in rendered
    assert "ужин" in rendered


def test_csv_has_bom_semicolon_and_exact_amount() -> None:
    item = TransactionDetails(
        id=uuid7(),
        type="income",
        amount_minor=12345,
        currency="RUB",
        account_id=uuid7(),
        account_name="Карта",
        category_id=uuid7(),
        category_name="Возврат",
        category_emoji="↩️",
        occurred_at=datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
        description="тест; с разделителем",
        source="manual",
        deleted_at=None,
        version=1,
    )
    exported = build_csv([item])
    assert exported.startswith(b"\xef\xbb\xbf")
    decoded = exported.decode("utf-8-sig")
    assert "123,45" in decoded
    assert "счёт;категория;дата;описание" in decoded


def test_log_redaction_removes_secrets_and_numeric_ids() -> None:
    raw = (
        "https://api.telegram.org/bot123456789:ABC_secret/getMe "
        + "postgresql"
        + "://user:password@db/database owner=999888777"
    )
    cleaned = redact(raw)
    assert "ABC_secret" not in cleaned
    assert "password" not in cleaned
    assert "999888777" not in cleaned
    formatter = JsonFormatter()
    record = __import__("logging").LogRecord("test", 20, "", 0, raw, (), None)
    payload = json.loads(formatter.format(record))
    assert payload["correlation_id"] == "system"
    assert "secret" not in payload["event"].casefold() or "[secret]" in payload["event"]


def test_money_presenter_uses_currency_symbol_without_float() -> None:
    assert money(145050, "RUB", sign="−") == "−1 450,50 ₽"
    assert money(25000000, "RUB", sign="+") == "+250 000 ₽"
