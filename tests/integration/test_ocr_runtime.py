import io

import pytest
from PIL import Image, ImageDraw, ImageFont

from finbot.adapters.ocr.tesseract import TesseractTextExtractor
from finbot.application.ocr import parse_ocr_transaction, parse_ocr_transactions


@pytest.mark.asyncio
async def test_container_tesseract_recognizes_russian_receipt_total() -> None:
    image = Image.new("RGB", (1600, 640), "white")
    font = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        58,
    )
    ImageDraw.Draw(image).multiline_text(
        (70, 60),
        "ООО РОМАШКА\nКАССОВЫЙ ЧЕК\nИТОГО 1 450,00 РУБ\n12.08.2026 10:30",
        fill="black",
        font=font,
        spacing=30,
    )
    content = io.BytesIO()
    image.save(content, format="PNG")

    text = await TesseractTextExtractor().extract_text(content.getvalue(), "image/png")
    draft = parse_ocr_transaction(text, "Europe/Moscow", "RUB")

    assert draft.amount_minor == 145_000
    assert "РОМАШКА" in draft.description.upper()


@pytest.mark.asyncio
async def test_container_tesseract_keeps_multiple_bank_rows() -> None:
    image = Image.new("RGB", (1500, 470), "white")
    font = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        48,
    )
    ImageDraw.Draw(image).multiline_text(
        (50, 45),
        "12.08.2026\n10:15 Кофейня -250,00 ₽\n11:20 Метро -250,00 ₽",
        fill="black",
        font=font,
        spacing=30,
    )
    content = io.BytesIO()
    image.save(content, format="PNG")

    text = await TesseractTextExtractor().extract_text(content.getvalue(), "image/png")
    drafts = parse_ocr_transactions(text, "Europe/Moscow", "RUB")

    assert [draft.amount_minor for draft in drafts] == [25_000, 25_000]
    assert [draft.description for draft in drafts] == ["Кофейня", "Метро"]
