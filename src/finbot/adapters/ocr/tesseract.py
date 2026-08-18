import asyncio
import io
import warnings
from collections.abc import Awaitable, Buffer, Callable

from PIL import Image, ImageOps, UnidentifiedImageError

from finbot.application.ocr import (
    MAX_OCR_IMAGE_BYTES,
    MAX_OCR_TEXT_LENGTH,
    SUPPORTED_OCR_IMAGE_MIME_TYPES,
    OcrImportError,
)

MAX_IMAGE_BYTES = MAX_OCR_IMAGE_BYTES
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_EDGE = 5_000
SUPPORTED_IMAGE_MIME_TYPES = SUPPORTED_OCR_IMAGE_MIME_TYPES
_FORMAT_TO_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
_TESSERACT_TIMEOUT_SECONDS = 20.0

CommandRunner = Callable[[bytes], Awaitable[bytes]]


class BoundedImageBuffer(io.BytesIO):
    """Reject a Telegram download as soon as it crosses the encoded-image limit."""

    def write(self, data: Buffer, /) -> int:
        size = memoryview(data).nbytes
        if self.tell() + size > MAX_IMAGE_BYTES:
            raise OcrImportError("Изображение слишком большое — максимум 10 МБ")
        return super().write(data)


def prepare_image(content: bytes, mime_type: str) -> bytes:
    """Validate and normalize an untrusted image without retaining its original bytes."""
    if mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
        raise OcrImportError("Поддерживаются только изображения JPEG, PNG и WebP")
    if not content:
        raise OcrImportError("Получено пустое изображение")
    if len(content) > MAX_IMAGE_BYTES:
        raise OcrImportError("Изображение слишком большое — максимум 10 МБ")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as source:
                actual_mime = _FORMAT_TO_MIME.get(source.format or "")
                if actual_mime is None or actual_mime != mime_type:
                    raise OcrImportError("Формат изображения не совпадает с его типом")
                width, height = source.size
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise OcrImportError("Разрешение изображения слишком большое")
                source.load()
                normalized = ImageOps.exif_transpose(source).convert("L")
    except OcrImportError:
        raise
    except Image.DecompressionBombError, Image.DecompressionBombWarning:
        raise OcrImportError("Разрешение изображения слишком большое") from None
    except UnidentifiedImageError, OSError, ValueError:
        raise OcrImportError("Файл не является корректным изображением") from None

    if max(normalized.size) > MAX_IMAGE_EDGE:
        normalized.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
    normalized = ImageOps.autocontrast(normalized)
    output = io.BytesIO()
    normalized.save(output, format="PNG", optimize=True)
    return output.getvalue()


async def _run_tesseract(image: bytes) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            "tesseract",
            "stdin",
            "stdout",
            "-l",
            "rus+eng",
            "--oem",
            "1",
            "--psm",
            "6",
            "quiet",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        raise OcrImportError("Локальный OCR временно недоступен") from None
    try:
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(image), timeout=_TESSERACT_TIMEOUT_SECONDS
        )
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    except TimeoutError:
        process.kill()
        await process.wait()
        raise OcrImportError("Распознавание заняло слишком много времени") from None
    if process.returncode != 0:
        raise OcrImportError("Не удалось распознать изображение")
    return stdout


class TesseractTextExtractor:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or _run_tesseract

    async def extract_text(self, content: bytes, mime_type: str) -> str:
        prepared = prepare_image(content, mime_type)
        raw = await self._runner(prepared)
        if len(raw) > MAX_OCR_TEXT_LENGTH * 4:
            raise OcrImportError("На изображении слишком много текста")
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            raise OcrImportError("На изображении не удалось найти читаемый текст")
        return text
