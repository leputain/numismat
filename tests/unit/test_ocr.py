import io

import pytest
from PIL import Image

from finbot.adapters.ocr.tesseract import (
    MAX_IMAGE_BYTES,
    BoundedImageBuffer,
    TesseractTextExtractor,
    prepare_image,
)
from finbot.adapters.telegram.presenters import wizard_summary
from finbot.application.ocr import (
    MAX_OCR_TRANSACTIONS,
    OcrImportError,
    parse_ocr_transaction,
    parse_ocr_transactions,
)
from finbot.domain.transactions import TransactionType


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (80, 40), "white").save(output, format="PNG")
    return output.getvalue()


def test_receipt_parser_prefers_total_and_keeps_money_integer() -> None:
    draft = parse_ocr_transaction(
        """
        ООО Ромашка
        КАССОВЫЙ ЧЕК
        Молоко 120,00
        Хлеб 80,00
        ИТОГО 200,00 ₽
        12.08.2026 10:30
        """,
        "Europe/Moscow",
        "RUB",
    )

    assert draft.amount_minor == 20_000
    assert isinstance(draft.amount_minor, int)
    assert draft.type is TransactionType.EXPENSE
    assert draft.description == "ООО Ромашка"
    assert draft.occurred_at is not None
    assert draft.occurred_at.isoformat() == "2026-08-12T10:30:00+03:00"


def test_bank_screenshot_parser_detects_income_from_sign() -> None:
    draft = parse_ocr_transaction(
        "СБП\nПеревод от Иван Иванов\n+12 500,00 ₽\n12.08.2026 09:15",
        "Europe/Moscow",
        "RUB",
    )

    assert draft.amount_minor == 1_250_000
    assert draft.type is TransactionType.INCOME
    assert draft.description == "Перевод от Иван Иванов"


def test_currency_prefix_does_not_consume_merchant_name() -> None:
    draft = parse_ocr_transaction(
        "-1450,00 Ресторан Ромашка",
        "Europe/Moscow",
        "RUB",
    )

    assert draft.amount_minor == 145_000
    assert draft.description == "Ресторан Ромашка"


@pytest.mark.parametrize(
    "value",
    ["TOTAL 1,234.56 USD", "ИТОГО 1.234,56 ₽", "ИТОГО 1’450,00 ₽"],
)
def test_unsupported_number_grouping_is_never_partially_parsed(value: str) -> None:
    with pytest.raises(OcrImportError, match="найти сумму"):
        parse_ocr_transaction(value, "Europe/Moscow", "RUB")


def test_currency_on_adjacent_line_is_enforced() -> None:
    with pytest.raises(OcrImportError, match="USD"):
        parse_ocr_transaction("TOTAL\n14.50\nUSD", "Europe/Moscow", "RUB")


@pytest.mark.parametrize("currency", ["GBP", "CNY", "AMD"])
def test_other_explicit_currency_is_never_assumed_to_be_rubles(currency: str) -> None:
    with pytest.raises(OcrImportError, match=currency):
        parse_ocr_transaction(f"TOTAL 14.50 {currency}", "Europe/Moscow", "RUB")


def test_total_label_and_datetime_can_be_on_adjacent_lines() -> None:
    draft = parse_ocr_transaction(
        "ООО Ромашка\nМолоко 120,00\nИТОГО\n200,00 ₽\n12.08.2026\n10:30",
        "Europe/Moscow",
        "RUB",
    )

    assert draft.amount_minor == 20_000
    assert draft.occurred_at is not None
    assert draft.occurred_at.isoformat() == "2026-08-12T10:30:00+03:00"


def test_balance_is_not_imported_as_an_operation() -> None:
    with pytest.raises(OcrImportError, match="однозначно"):
        parse_ocr_transaction("Баланс 200,00 ₽", "Europe/Moscow", "RUB")


def test_adjacent_discount_is_not_imported_as_an_operation() -> None:
    with pytest.raises(OcrImportError, match="однозначно"):
        parse_ocr_transaction("Скидка\n100,00 ₽", "Europe/Moscow", "RUB")


def test_operation_date_wins_over_print_date() -> None:
    draft = parse_ocr_transaction(
        "Дата печати 11.08.2026\nДата операции 12.08.2026 10:30\nИТОГО 200,00 ₽",
        "Europe/Moscow",
        "RUB",
    )

    assert draft.occurred_at is not None
    assert draft.occurred_at.isoformat() == "2026-08-12T10:30:00+03:00"


def test_multiple_dates_are_visibly_flagged_for_review() -> None:
    draft = parse_ocr_transaction(
        "Дата 11.08.2026\nДата 12.08.2026\nИТОГО 200,00 ₽",
        "Europe/Moscow",
        "RUB",
    )
    assert draft.needs_confirmation
    assert draft.occurred_at is not None

    summary = wizard_summary(
        {
            "type": "expense",
            "amount": draft.amount_minor,
            "category_name": "Прочее",
            "account_name": "Карта",
            "occurred_at": draft.occurred_at.isoformat(),
            "needs_confirmation": draft.needs_confirmation,
        },
        "RUB",
        "Europe/Moscow",
    )

    assert "Проверьте дату и время" in summary


@pytest.mark.parametrize(
    "status",
    ["Операция не выполнена", "Платёж отклонён", "Payment declined"],
)
def test_failed_bank_operation_is_not_imported(status: str) -> None:
    with pytest.raises(OcrImportError, match="неуспешная"):
        parse_ocr_transaction(f"{status}\nИТОГО 200,00 ₽", "Europe/Moscow", "RUB")


def test_unrelated_failed_word_does_not_reject_valid_batch() -> None:
    drafts = parse_ocr_transactions(
        "Failed Cafe -250,00 ₽\nMetro -100,00 ₽\nFailed attempts: 0",
        "Europe/Moscow",
        "RUB",
    )

    assert [draft.amount_minor for draft in drafts] == [25_000, 10_000]
    assert drafts[0].description == "Failed Cafe"


def test_multiple_signed_bank_rows_are_all_preserved_in_screen_order() -> None:
    drafts = parse_ocr_transactions(
        """
        12.08.2026
        10:15 Кофейня -250,00 ₽
        11:20 Перевод от Анны +2 000,00 ₽
        12:45 Метро -250,00 ₽
        """,
        "Europe/Moscow",
        "RUB",
    )

    assert [draft.amount_minor for draft in drafts] == [25_000, 200_000, 25_000]
    assert [draft.type for draft in drafts] == [
        TransactionType.EXPENSE,
        TransactionType.INCOME,
        TransactionType.EXPENSE,
    ]
    assert [draft.description for draft in drafts] == ["Кофейня", "Перевод от Анны", "Метро"]
    assert [draft.occurred_at.isoformat() for draft in drafts if draft.occurred_at] == [
        "2026-08-12T10:15:00+03:00",
        "2026-08-12T11:20:00+03:00",
        "2026-08-12T12:45:00+03:00",
    ]


def test_singular_ocr_api_refuses_to_drop_batch_items() -> None:
    with pytest.raises(OcrImportError, match="несколько операций"):
        parse_ocr_transaction(
            "Кофейня -250,00 ₽\nМетро -250,00 ₽",
            "Europe/Moscow",
            "RUB",
        )


def test_receipt_items_with_one_total_stay_one_transaction() -> None:
    drafts = parse_ocr_transactions(
        "Молоко 120,00 ₽\nХлеб 80,00 ₽\nИТОГО 200,00 ₽",
        "Europe/Moscow",
        "RUB",
    )

    assert len(drafts) == 1
    assert drafts[0].amount_minor == 20_000


def test_receipt_discount_does_not_turn_items_into_a_batch() -> None:
    drafts = parse_ocr_transactions(
        "Молоко 100,00 ₽\nХлеб 50,00 ₽\nСкидка -10,00 ₽\nИТОГО 140,00 ₽",
        "Europe/Moscow",
        "RUB",
    )

    assert len(drafts) == 1
    assert drafts[0].amount_minor == 14_000


def test_batch_accepts_currency_split_to_adjacent_lines() -> None:
    drafts = parse_ocr_transactions(
        "Кофейня\n-250,00\n₽\nМетро\n-100,00\n₽",
        "Europe/Moscow",
        "RUB",
    )

    assert [draft.amount_minor for draft in drafts] == [25_000, 10_000]
    assert [draft.description for draft in drafts] == ["Кофейня", "Метро"]


def test_batch_keeps_signed_decimal_row_when_one_currency_is_missing() -> None:
    drafts = parse_ocr_transactions(
        "Кофейня -250,00 ₽\nМетро -100,00 ₽\nТакси -300,00",
        "Europe/Moscow",
        "RUB",
    )

    assert [draft.amount_minor for draft in drafts] == [25_000, 10_000, 30_000]


def test_batch_accepts_signed_decimal_when_tesseract_damages_one_currency() -> None:
    drafts = parse_ocr_transactions(
        "Кофейня -250,00 ₽\nМетро -100,00 ВУВ",
        "Europe/Moscow",
        "RUB",
    )

    assert [draft.amount_minor for draft in drafts] == [25_000, 10_000]


def test_batch_never_collapses_one_resolved_and_one_unresolved_integer_row() -> None:
    with pytest.raises(OcrImportError, match="разделить все операции"):
        parse_ocr_transactions(
            "Кофейня -250 ₽\nМетро -100",
            "Europe/Moscow",
            "RUB",
        )


def test_receipt_fallback_rejects_multi_amount_statement_without_total_marker() -> None:
    with pytest.raises(OcrImportError, match="найдено несколько сумм"):
        parse_ocr_transactions(
            "ООО Супермаркет\n"
            "Кошелек\n"
            "01.08.2026 10:10\n"
            "Покупка 250,00 ₽\n"
            "Счёт 20,00\n"
            "Возврат 10,00\n",
            "Europe/Moscow",
            "RUB",
        )


def test_batch_enforces_currency_from_a_document_header() -> None:
    with pytest.raises(OcrImportError, match="USD"):
        parse_ocr_transactions(
            "USD\nОперации\nCoffee -14.50\nMetro -2.50",
            "Europe/Moscow",
            "RUB",
        )


def test_bank_summary_total_does_not_collapse_signed_rows() -> None:
    drafts = parse_ocr_transactions(
        "Итого расходов\nКофе -250,00 ₽\nМетро -100,00 ₽",
        "Europe/Moscow",
        "RUB",
    )

    assert [draft.amount_minor for draft in drafts] == [25_000, 10_000]


def test_signed_bank_summary_amount_is_not_imported_twice() -> None:
    drafts = parse_ocr_transactions(
        "Итого -350,00 ₽\nКофе -250,00 ₽\nМетро -100,00 ₽",
        "Europe/Moscow",
        "RUB",
    )

    assert [draft.amount_minor for draft in drafts] == [25_000, 10_000]


def test_merchant_whose_name_contains_total_is_not_dropped() -> None:
    drafts = parse_ocr_transactions(
        "Total Fitness -500,00 ₽\nCoffee -250,00 ₽\nMetro -100,00 ₽",
        "Europe/Moscow",
        "RUB",
    )

    assert [draft.amount_minor for draft in drafts] == [50_000, 25_000, 10_000]
    assert drafts[0].description == "Total Fitness"


@pytest.mark.parametrize("merchant", ["Кафе Баланс", "Cashback Cafe"])
def test_merchant_whose_name_contains_auxiliary_word_is_not_dropped(merchant: str) -> None:
    drafts = parse_ocr_transactions(
        f"{merchant} -500,00 ₽\nCoffee -250,00 ₽\nMetro -100,00 ₽",
        "Europe/Moscow",
        "RUB",
    )

    assert [draft.amount_minor for draft in drafts] == [50_000, 25_000, 10_000]
    assert drafts[0].description == merchant


def test_merchant_digits_do_not_hide_signed_batch_amounts() -> None:
    drafts = parse_ocr_transactions(
        "Магнит 24 -500,00 ₽\nАптека 36 -250,00 ₽\n7-Eleven -100,00 ₽",
        "Europe/Moscow",
        "RUB",
    )

    assert [draft.amount_minor for draft in drafts] == [50_000, 25_000, 10_000]
    assert [draft.description for draft in drafts] == ["Магнит 24", "Аптека 36", "7-Eleven"]


def test_auxiliary_signed_row_never_disappears_from_a_batch_silently() -> None:
    with pytest.raises(OcrImportError, match="разделить все операции"):
        parse_ocr_transactions(
            "Coffee -250,00 ₽\nКэшбэк +50,00 ₽\nMetro -100,00 ₽",
            "Europe/Moscow",
            "RUB",
        )


def test_batch_refuses_to_silently_drop_unsigned_currency_row() -> None:
    with pytest.raises(OcrImportError, match="разделить все операции"):
        parse_ocr_transactions(
            "Кофе -250,00 ₽\nМетро -100,00 ₽\nАптека 500,00 ₽",
            "Europe/Moscow",
            "RUB",
        )


@pytest.mark.parametrize(
    "text",
    [
        "Кофейня 250,00 ₽\nМетро 250,00 ₽",
        "Покупка кофе 250,00 ₽\nМетро 100,00 ₽",
    ],
)
def test_unsigned_bank_rows_are_never_silently_collapsed(text: str) -> None:
    with pytest.raises(OcrImportError, match="разделить все операции"):
        parse_ocr_transactions(text, "Europe/Moscow", "RUB")


def test_each_batch_row_keeps_its_own_date() -> None:
    drafts = parse_ocr_transactions(
        "12.08.2026 10:15 Кофе -250,00 ₽\n"
        "11.08.2026 11:20 Метро -100,00 ₽\n"
        "10.08.2026 12:30 Такси -200,00 ₽",
        "Europe/Moscow",
        "RUB",
    )

    assert [draft.occurred_at.isoformat() for draft in drafts if draft.occurred_at] == [
        "2026-08-12T10:15:00+03:00",
        "2026-08-11T11:20:00+03:00",
        "2026-08-10T12:30:00+03:00",
    ]


def test_batch_transaction_limit_accepts_twenty_and_rejects_twenty_one() -> None:
    twenty = "\n".join(f"Магазин -{index},00 ₽" for index in range(1, MAX_OCR_TRANSACTIONS + 1))
    twenty_one = twenty + "\nМагазин -21,00 ₽"

    assert len(parse_ocr_transactions(twenty, "Europe/Moscow", "RUB")) == 20
    with pytest.raises(OcrImportError, match="больше 20"):
        parse_ocr_transactions(twenty_one, "Europe/Moscow", "RUB")


def test_ocr_parser_refuses_to_guess_between_unlabelled_amounts() -> None:
    with pytest.raises(OcrImportError, match="несколько сумм"):
        parse_ocr_transaction(
            "Молоко 120,00\nХлеб 80,00",
            "Europe/Moscow",
            "RUB",
        )


def test_ocr_parser_rejects_currency_mismatch() -> None:
    with pytest.raises(OcrImportError, match="USD"):
        parse_ocr_transaction(
            "Coffee shop\nTOTAL 14.50 USD",
            "Europe/Moscow",
            "RUB",
        )


def test_ocr_parser_requires_a_financial_amount() -> None:
    with pytest.raises(OcrImportError, match="найти сумму"):
        parse_ocr_transaction("Просто картинка с текстом", "Europe/Moscow", "RUB")


@pytest.mark.asyncio
async def test_tesseract_adapter_normalizes_image_and_returns_text() -> None:
    received = b""

    async def runner(content: bytes) -> bytes:
        nonlocal received
        received = content
        return "ООО Ромашка\nИТОГО 200,00 ₽".encode()

    extractor = TesseractTextExtractor(runner)

    text = await extractor.extract_text(_png_bytes(), "image/png")

    assert received.startswith(b"\x89PNG\r\n\x1a\n")
    assert "ИТОГО" in text


def test_image_validation_rejects_spoofed_and_oversized_content() -> None:
    with pytest.raises(OcrImportError, match="не совпадает"):
        prepare_image(_png_bytes(), "image/jpeg")
    with pytest.raises(OcrImportError, match="максимум 10 МБ"):
        prepare_image(b"x" * (MAX_IMAGE_BYTES + 1), "image/png")
    with pytest.raises(OcrImportError, match="корректным изображением"):
        prepare_image(b"not-an-image", "image/png")


def test_bounded_download_buffer_rejects_before_buffering_extra_bytes() -> None:
    buffer = BoundedImageBuffer()
    buffer.seek(MAX_IMAGE_BYTES)

    with pytest.raises(OcrImportError, match="максимум 10 МБ"):
        buffer.write(b"x")


@pytest.mark.asyncio
async def test_tesseract_adapter_rejects_empty_recognition() -> None:
    async def runner(_content: bytes) -> bytes:
        return b""

    with pytest.raises(OcrImportError, match="читаемый текст"):
        await TesseractTextExtractor(runner).extract_text(_png_bytes(), "image/png")
