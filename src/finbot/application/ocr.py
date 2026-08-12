import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from finbot.domain.errors import FinbotError
from finbot.domain.money import MoneyError, parse_minor
from finbot.domain.transactions import TransactionDraft, TransactionType

MAX_OCR_TEXT_LENGTH = 32_768
MAX_OCR_TRANSACTIONS = 20


class OcrImportError(FinbotError):
    """A bounded, owner-safe OCR error that contains no recognized text."""


class ImageTextExtractor(Protocol):
    async def extract_text(self, content: bytes, mime_type: str) -> str: ...


@dataclass(frozen=True, slots=True)
class _AmountCandidate:
    amount_minor: int
    currency: str | None
    sign: str
    line_index: int
    score: int


_CURRENCY_FRAGMENT = (
    r"₽|\$|€|£|¥|₸|֏|₾|₺|₴|₪|₹|₼|"
    r"(?:р(?:уб(?:\.?|ля|лей)?|\.)?|RUB|USD|EUR|GBP|CNY|JPY|KZT|BYN|AMD|GEL|TRY|UAH|ILS|INR|AZN)"
    r"(?![A-Za-zА-Яа-яЁё])"
)
_AMOUNT = re.compile(
    r"(?<![\d.,:'’])(?P<sign>[+\-−–—]?)\s*"
    r"(?P<number>(?:\d{1,3}(?:[ \u00a0]\d{3})+|\d{1,12})(?:[,.]\d{1,2})?)"
    rf"(?![\d.,:'’])\s*(?:(?P<currency>{_CURRENCY_FRAGMENT}))?",
    re.IGNORECASE,
)
_CURRENCY = re.compile(
    rf"(?:₽|\$|€|£|¥|₸|֏|₾|₺|₴|₪|₹|₼|(?<![A-Za-zА-Яа-яЁё])(?:{_CURRENCY_FRAGMENT}))",
    re.IGNORECASE,
)
_DATE = re.compile(
    r"(?<!\d)(?:(?P<day>\d{1,2})[./-](?P<month>\d{1,2})[./-](?P<year>\d{4})|"
    r"(?P<iso_year>\d{4})-(?P<iso_month>\d{1,2})-(?P<iso_day>\d{1,2}))(?!\d)"
)
_TIME = re.compile(r"(?<!\d)(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)(?!\d)")
_LETTERS = re.compile(r"[A-Za-zА-Яа-яЁё]")

_TOTAL_WEIGHTS = (
    ("сумма операции", 140),
    ("сумма покупки", 140),
    ("к оплате", 135),
    ("итого к оплате", 135),
    ("итого", 125),
    ("total", 125),
    ("оплачено", 115),
    ("списано", 110),
    ("зачислено", 110),
    ("поступление", 95),
    ("оплата", 90),
    ("покупка", 90),
    ("перевод", 80),
)
_AMOUNT_NEGATIVE = (
    "подытог",
    "subtotal",
    "скидка",
    "discount",
    "сдача",
    "налог",
    "ндс",
    "комиссия",
    "баланс",
    "кэшбэк",
    "cashback",
)
_AMOUNT_AGGREGATE_LABELS = frozenset(
    {
        "итог",
        "итого",
        "итого к оплате",
        "итого расходов",
        "итого доходов",
        "всего",
        "всего расходов",
        "всего доходов",
        "total",
        "total expenses",
        "total income",
        "сумма операций",
        "сумма расходов",
        "сумма доходов",
        "общая сумма",
    }
)
_AMOUNT_AUXILIARY_LABELS = frozenset(
    {
        "подытог",
        "subtotal",
        "скидка",
        "сумма скидки",
        "discount",
        "сдача",
        "налог",
        "ндс",
        "комиссия",
        "комиссия банка",
        "баланс",
        "баланс карты",
        "доступный баланс",
        "остаток",
        "кэшбэк",
        "cashback",
    }
)
_INCOME_WORDS = (
    "зачисление",
    "зачислено",
    "поступление",
    "пополнение",
    "перевод от",
    "возврат",
    "refund",
    "income",
)
_EXPENSE_WORDS = (
    "списание",
    "списано",
    "покупка",
    "оплата",
    "платеж",
    "payment",
    "purchase",
    "paid",
    "перевод",
)
_FAILED_OPERATION_PHRASES = (
    "операция отменена",
    "оплата отменена",
    "платеж отменен",
    "платеж отменён",
    "платеж отклонен",
    "платеж отклонён",
    "операция отклонена",
    "операция не выполнена",
    "платеж не выполнен",
)
_FAILED_ENGLISH = re.compile(
    r"\b(?:(?:payment|transaction|operation|purchase|transfer)\s+(?:was\s+)?"
    r"(?:failed|declined|cancelled|canceled)|"
    r"(?:failed|declined|cancelled|canceled)\s+"
    r"(?:payment|transaction|operation|purchase|transfer))\b",
    re.IGNORECASE,
)
_DESCRIPTION_NOISE = (
    "кассовый чек",
    "товарный чек",
    "операция выполнена",
    "операция успешна",
    "платеж выполнен",
    "успешно",
    "сумма операции",
    "сумма покупки",
    "к оплате",
    "итого",
    "total",
    "оплачено",
    "списано",
    "зачислено",
    "дата операции",
    "время операции",
    "комиссия",
    "баланс",
)
_CURRENCIES = {
    "₽": "RUB",
    "Р": "RUB",
    "Р.": "RUB",
    "РУБ": "RUB",
    "РУБ.": "RUB",
    "РУБЛЯ": "RUB",
    "РУБЛЕЙ": "RUB",
    "RUB": "RUB",
    "$": "USD",
    "USD": "USD",
    "€": "EUR",
    "EUR": "EUR",
    "£": "GBP",
    "GBP": "GBP",
    "¥": "CNY",
    "CNY": "CNY",
    "JPY": "JPY",
    "₸": "KZT",
    "KZT": "KZT",
    "BYN": "BYN",
    "֏": "AMD",
    "AMD": "AMD",
    "₾": "GEL",
    "GEL": "GEL",
    "₺": "TRY",
    "TRY": "TRY",
    "₴": "UAH",
    "UAH": "UAH",
    "₪": "ILS",
    "ILS": "ILS",
    "₹": "INR",
    "INR": "INR",
    "₼": "AZN",
    "AZN": "AZN",
}


def _clean_lines(text: str) -> list[str]:
    if len(text) > MAX_OCR_TEXT_LENGTH:
        raise OcrImportError("На изображении слишком много текста")
    normalized = unicodedata.normalize("NFKC", text).replace("\x00", " ")
    lines = [" ".join(line.split()) for line in normalized.splitlines()]
    cleaned = [line for line in lines if line]
    if not cleaned:
        raise OcrImportError("На изображении не удалось найти читаемый текст")
    return cleaned


def _currency_code(raw: str | None) -> str | None:
    if raw is None:
        return None
    return _CURRENCIES.get(raw.strip().upper())


def _line_weight(lowered: str) -> int:
    score = 0
    for phrase, weight in _TOTAL_WEIGHTS:
        if phrase in lowered:
            score = max(score, weight)
    if any(phrase in lowered for phrase in _AMOUNT_NEGATIVE):
        score -= 120
    return score


def _amount_candidates(lines: list[str]) -> list[_AmountCandidate]:
    candidates: list[_AmountCandidate] = []
    for line_index, line in enumerate(lines):
        lowered = line.casefold().replace("ё", "е")
        line_weight = _line_weight(lowered)
        if line_index > 0:
            previous_weight = _line_weight(lines[line_index - 1].casefold().replace("ё", "е"))
            if previous_weight < 0 and line_weight <= 0:
                line_weight = previous_weight
            else:
                line_weight = max(line_weight, previous_weight)
        for match in _AMOUNT.finditer(line):
            number = match.group("number")
            currency = _currency_code(match.group("currency"))
            sign = "-" if match.group("sign") in {"-", "−", "–", "—"} else match.group("sign")
            has_fraction = "," in number or "." in number
            if currency is None and not sign and line_weight <= 0 and not has_fraction:
                continue
            try:
                amount_minor = parse_minor(number)
            except MoneyError:
                continue
            score = line_weight
            if currency is not None:
                score += 30
            if sign:
                score += 20
            if has_fraction:
                score += 10
            candidates.append(
                _AmountCandidate(
                    amount_minor=amount_minor,
                    currency=currency,
                    sign=sign,
                    line_index=line_index,
                    score=score,
                )
            )
    return candidates


def _select_amount(lines: list[str], base_currency: str) -> _AmountCandidate:
    candidates = _amount_candidates(lines)
    if not candidates:
        raise OcrImportError("Не удалось найти сумму операции")

    # Candidate occurrences are intentionally not deduplicated by value: two rows with
    # the same amount are still two possible operations. A strong total label can win,
    # while equal unlabelled occurrences remain ambiguous instead of being dropped.
    ranked = sorted(candidates, key=lambda item: item.score, reverse=True)
    best = ranked[0]
    if best.score <= 0:
        raise OcrImportError("Не удалось однозначно определить сумму операции")
    if len(ranked) > 1:
        second = ranked[1]
        if best.score < 80 or best.score == second.score:
            raise OcrImportError("На изображении найдено несколько сумм — укажите нужную текстом")

    document_currencies = {
        code
        for line in lines
        for match in _CURRENCY.finditer(line)
        if (code := _currency_code(match.group())) is not None
    }
    if len(document_currencies) > 1:
        raise OcrImportError("На изображении найдено несколько валют")
    detected_currency = next(iter(document_currencies), best.currency)
    expected_currency = base_currency.strip().upper()
    if detected_currency is not None and detected_currency != expected_currency:
        raise OcrImportError(
            f"Распознана валюта {detected_currency}, а основной счёт использует {expected_currency}"
        )
    return best


def _transaction_type(lines: list[str], amount: _AmountCandidate) -> TransactionType:
    if amount.sign == "+":
        return TransactionType.INCOME
    if amount.sign == "-":
        return TransactionType.EXPENSE
    lowered = " ".join(lines).casefold().replace("ё", "е")
    if any(phrase in lowered for phrase in _INCOME_WORDS):
        return TransactionType.INCOME
    if any(phrase in lowered for phrase in _EXPENSE_WORDS):
        return TransactionType.EXPENSE
    return TransactionType.EXPENSE


def _occurred_at(lines: list[str], timezone: str) -> tuple[datetime | None, bool]:
    zone = ZoneInfo(timezone)
    now = datetime.now(zone)
    candidates: list[tuple[int, int, re.Match[str]]] = []
    for index, line in enumerate(lines):
        lowered = line.casefold().replace("ё", "е")
        score = 0
        if any(label in lowered for label in ("дата операции", "дата покупки", "дата платежа")):
            score += 100
        if any(label in lowered for label in ("дата печати", "срок действия", "действителен до")):
            score -= 100
        candidates.extend((score, index, match) for match in _DATE.finditer(line))
    if not candidates:
        return None, False
    best_score = max(candidate[0] for candidate in candidates)
    best_dates = [candidate for candidate in candidates if candidate[0] == best_score]
    score, index, date_match = best_dates[0]
    del score
    try:
        if date_match.group("year") is not None:
            year = int(date_match.group("year"))
            month = int(date_match.group("month"))
            day = int(date_match.group("day"))
        else:
            year = int(date_match.group("iso_year"))
            month = int(date_match.group("iso_month"))
            day = int(date_match.group("iso_day"))
        time_match = _TIME.search(lines[index])
        if time_match is None and index + 1 < len(lines):
            time_match = _TIME.search(lines[index + 1])
        hour = int(time_match.group("hour")) if time_match else 12
        minute = int(time_match.group("minute")) if time_match else 0
        value = datetime(year, month, day, hour, minute, tzinfo=zone)
    except ValueError:
        return None, True
    suspicious = (
        value > now + timedelta(minutes=1)
        or value < now - timedelta(days=366)
        or len(best_dates) > 1
    )
    return value, suspicious


def _description(lines: list[str], amount: _AmountCandidate) -> str:
    choices: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        lowered = line.casefold().replace("ё", "е")
        if any(phrase in lowered for phrase in _DESCRIPTION_NOISE):
            continue
        if _DATE.fullmatch(line) or _TIME.fullmatch(line):
            continue
        without_amount = _AMOUNT.sub(" ", line)
        value = " ".join(without_amount.strip(" ·:;|—-_").split())
        letters = len(_LETTERS.findall(value))
        if letters < 3 or not 2 <= len(value) <= 120:
            continue
        distance = abs(index - amount.line_index)
        proximity = max(0, 25 - distance * 5)
        early = max(0, 12 - index)
        legal_name = 15 if any(token in value.upper() for token in ("ООО", "ИП ")) else 0
        choices.append((letters + proximity + early + legal_name, value))
    if not choices:
        return "Операция по изображению"
    return max(choices, key=lambda item: item[0])[1][:120]


def parse_ocr_transaction(text: str, timezone: str, base_currency: str) -> TransactionDraft:
    """Convert OCR text only when it contains exactly one transaction."""
    transactions = parse_ocr_transactions(text, timezone, base_currency)
    if len(transactions) != 1:
        raise OcrImportError("На изображении найдено несколько операций")
    return transactions[0]


def _receipt_marked(lines: list[str]) -> bool:
    normalized = " ".join(lines).casefold().replace("ё", "е")
    return any(marker in normalized for marker in ("кассовый чек", "товарный чек", "фискальн"))


def _failed_operation_marked(lines: list[str]) -> bool:
    for line in lines:
        normalized = line.casefold().replace("ё", "е")
        if any(marker.replace("ё", "е") in normalized for marker in _FAILED_OPERATION_PHRASES):
            return True
        if _FAILED_ENGLISH.search(line) is not None:
            return True
        if normalized.strip(" .:;!-") in {"failed", "declined", "cancelled", "canceled"}:
            return True
    return False


def _nearby_currency(lines: list[str], line_index: int, raw: str | None) -> str | None:
    currency = _currency_code(raw)
    if currency is not None:
        return currency
    for neighbor in (line_index - 1, line_index + 1):
        if not 0 <= neighbor < len(lines):
            continue
        currency_match = _CURRENCY.fullmatch(lines[neighbor].strip())
        if currency_match is not None:
            return _currency_code(currency_match.group())
    return None


def _amount_line_label(line: str) -> str:
    label = _AMOUNT.sub(" ", line)
    label = _DATE.sub(" ", label)
    label = _TIME.sub(" ", label)
    label = re.sub(r"[^A-Za-zА-Яа-яЁё ]+", " ", label)
    return " ".join(label.casefold().replace("ё", "е").split())


def _is_aggregate_amount_line(line: str) -> bool:
    return bool(_AMOUNT.search(line)) and _amount_line_label(line) in _AMOUNT_AGGREGATE_LABELS


def _is_auxiliary_amount_line(line: str) -> bool:
    return bool(_AMOUNT.search(line)) and _amount_line_label(line) in _AMOUNT_AUXILIARY_LABELS


def _explicit_row_candidate(lines: list[str], line_index: int) -> _AmountCandidate | None:
    line = lines[line_index]
    if _is_auxiliary_amount_line(line) or _is_aggregate_amount_line(line):
        return None
    matches = [match for match in _AMOUNT.finditer(line) if match.group("sign")]
    if len(matches) != 1:
        return None
    match = matches[0]
    sign = "-" if match.group("sign") in {"-", "−", "–", "—"} else match.group("sign")
    currency = _nearby_currency(lines, line_index, match.group("currency"))
    if not sign:
        return None
    # Tesseract occasionally damages a currency glyph/abbreviation while preserving
    # a signed decimal amount. Such a row can use the selected account currency, but
    # a signed integer without a recognized currency is too close to an identifier.
    if currency is None and not ({",", "."} & set(match.group("number"))):
        return None
    try:
        amount_minor = parse_minor(match.group("number"))
    except MoneyError:
        return None
    return _AmountCandidate(amount_minor, currency, sign, line_index, 200)


def _date_context(lines: list[str], index: int, timezone: str) -> tuple[datetime | None, bool]:
    # A date printed on the operation row always belongs to that row. Looking at a
    # surrounding window first would incorrectly copy the first operation's date to
    # every later item in a bank list.
    occurred, suspicious = _occurred_at([lines[index]], timezone)
    if occurred is not None:
        return occurred, suspicious
    for prior in reversed(lines[:index]):
        occurred, suspicious = _occurred_at([prior], timezone)
        if occurred is not None:
            time_match = _TIME.search(lines[index])
            if time_match is not None:
                occurred = occurred.replace(
                    hour=int(time_match.group("hour")),
                    minute=int(time_match.group("minute")),
                )
            return occurred, suspicious
    return None, True


def _row_description(lines: list[str], candidate: _AmountCandidate) -> str:
    line = lines[candidate.line_index]
    removed_candidate = False

    def remove_signed_amount(match: re.Match[str]) -> str:
        nonlocal removed_candidate
        if not removed_candidate and match.group("sign"):
            removed_candidate = True
            return " "
        return match.group()

    value = " ".join(_AMOUNT.sub(remove_signed_amount, line).strip(" ·:;|—-_+").split())
    value = _DATE.sub(" ", value)
    value = _TIME.sub(" ", value)
    value = " ".join(value.split())
    if len(_LETTERS.findall(value)) >= 3:
        return value[:120]
    for neighbor in (candidate.line_index - 1, candidate.line_index + 1):
        if not 0 <= neighbor < len(lines):
            continue
        neighbor_line = lines[neighbor]
        lowered = neighbor_line.casefold().replace("ё", "е")
        if (
            _DATE.fullmatch(neighbor_line)
            or _TIME.fullmatch(neighbor_line)
            or _CURRENCY.fullmatch(neighbor_line)
            or any(marker in lowered for marker in _DESCRIPTION_NOISE)
            or _AMOUNT.search(neighbor_line)
        ):
            continue
        if len(_LETTERS.findall(neighbor_line)) >= 3:
            return neighbor_line[:120]
    return "Операция по изображению"


def _parse_explicit_rows(
    lines: list[str], timezone: str, base_currency: str
) -> tuple[TransactionDraft, ...]:
    if _receipt_marked(lines):
        return ()
    signed_non_aggregate = sum(
        1
        for line in lines
        if not _is_aggregate_amount_line(line)
        and not _is_auxiliary_amount_line(line)
        and any(match.group("sign") for match in _AMOUNT.finditer(line))
    )
    has_unsigned_total = any(
        _is_aggregate_amount_line(line)
        and any(not match.group("sign") for match in _AMOUNT.finditer(line))
        for line in lines
    )
    if signed_non_aggregate == 0 and has_unsigned_total:
        return ()
    signed_auxiliary_lines = {
        index
        for index, line in enumerate(lines)
        if _is_auxiliary_amount_line(line)
        and any(match.group("sign") for match in _AMOUNT.finditer(line))
    }
    eligible_amount_lines = {
        index
        for index, line in enumerate(lines)
        if not _is_auxiliary_amount_line(line)
        and not _is_aggregate_amount_line(line)
        and any(
            match.group("sign") or _nearby_currency(lines, index, match.group("currency"))
            for match in _AMOUNT.finditer(line)
        )
    }
    if signed_auxiliary_lines and len(signed_auxiliary_lines | eligible_amount_lines) >= 2:
        raise OcrImportError("Не удалось надёжно разделить все операции на изображении")
    candidates = tuple(
        candidate
        for index, line in enumerate(lines)
        if (candidate := _explicit_row_candidate(lines, index)) is not None
    )
    if len(eligible_amount_lines) < 2:
        return ()
    if len(candidates) != len(eligible_amount_lines):
        raise OcrImportError("Не удалось надёжно разделить все операции на изображении")
    if len(candidates) > MAX_OCR_TRANSACTIONS:
        raise OcrImportError(f"На изображении больше {MAX_OCR_TRANSACTIONS} операций")
    expected_currency = base_currency.strip().upper()
    document_currencies = {
        code
        for line in lines
        for match in _CURRENCY.finditer(line)
        if (code := _currency_code(match.group())) is not None
    }
    if len(document_currencies) > 1:
        raise OcrImportError("На изображении найдено несколько валют")
    if document_currencies - {expected_currency}:
        detected_currency = sorted(document_currencies - {expected_currency})[0]
        raise OcrImportError(
            f"Распознана валюта {detected_currency}, а основной счёт использует {expected_currency}"
        )
    result: list[TransactionDraft] = []
    for candidate in candidates:
        occurred_at, needs_confirmation = _date_context(lines, candidate.line_index, timezone)
        result.append(
            TransactionDraft(
                amount_minor=candidate.amount_minor,
                type=(TransactionType.INCOME if candidate.sign == "+" else TransactionType.EXPENSE),
                occurred_at=occurred_at,
                description=_row_description(lines, candidate),
                needs_confirmation=needs_confirmation,
            )
        )
    return tuple(result)


def parse_ocr_transactions(
    text: str,
    timezone: str,
    base_currency: str,
) -> tuple[TransactionDraft, ...]:
    """Extract one receipt total or all strong signed bank-operation rows."""
    lines = _clean_lines(text)
    if _failed_operation_marked(lines):
        raise OcrImportError("Операция на изображении отмечена как неуспешная")
    explicit_rows = _parse_explicit_rows(lines, timezone, base_currency)
    if explicit_rows:
        return explicit_rows
    amount = _select_amount(lines, base_currency)
    occurred_at, needs_confirmation = _occurred_at(lines, timezone)
    return (
        TransactionDraft(
            amount_minor=amount.amount_minor,
            type=_transaction_type(lines, amount),
            occurred_at=occurred_at,
            description=_description(lines, amount),
            needs_confirmation=needs_confirmation,
        ),
    )
