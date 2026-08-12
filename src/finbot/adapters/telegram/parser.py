import re
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from finbot.domain.categories import (
    CATEGORY_ALIASES,
    EXPENSE_CATEGORIES,
    INCOME_CATEGORIES,
    KEYWORDS,
)
from finbot.domain.category_rules import normalize_rule_text, phrase_occurs
from finbot.domain.money import parse_minor
from finbot.domain.transactions import TransactionDraft, TransactionType

_AMOUNT = re.compile(
    r"^(?P<sign>[+-]?)(?P<amount>[\d][\d .\u00a0]*(?:[,\.]\d{1,2})?)\s+(?P<rest>.+)$"
)
_DATE = re.compile(r"^(?P<day>\d{1,2})\.(?P<month>\d{1,2})(?:\.(?P<year>\d{4}))?\s+")
_AMBIGUOUS_DOT = re.compile(r"^\d{1,3}(?:\.\d{3})+$")
_HINT = re.compile(r'(?<!\S)(?P<marker>[@#])(?:"(?P<quoted>[^"\r\n]+)"|(?P<bare>[^\s"]+))')
_BROKEN_HINT = re.compile(r'(?<!\S)[@#](?:"|\s|$)')


def _extract_hints(text: str) -> tuple[str, str | None, str | None]:
    hints: dict[str, str] = {}

    def remove_hint(match: re.Match[str]) -> str:
        marker = match.group("marker")
        if marker in hints:
            raise ValueError(f"Укажите {'счёт' if marker == '@' else 'категорию'} только один раз")
        value = match.group("quoted") or match.group("bare") or ""
        value = " ".join(value.strip().split())
        if not value:
            raise ValueError("Название счёта или категории не может быть пустым")
        hints[marker] = value
        return " "

    description = " ".join(_HINT.sub(remove_hint, text).split())
    if _BROKEN_HINT.search(description):
        raise ValueError('Многословный счёт или категорию заключите в кавычки: @"Карта Мир"')
    return description, hints.get("@"), hints.get("#")


class DeterministicParser:
    def __init__(self, timezone: str = "Europe/Moscow") -> None:
        self.zone = ZoneInfo(timezone)

    def parse(self, text: str) -> TransactionDraft:
        clean = " ".join(text.strip().split())
        if not clean:
            raise ValueError("Введите сумму и описание, например: 1450 ресторан")
        if len(clean) > 1024:
            raise ValueError("Слишком длинный ввод")
        date_value = None
        lowered = clean.lower()
        now = datetime.now(self.zone)
        if lowered.startswith("сегодня "):
            date_value = now
            clean = clean[8:]
        elif lowered.startswith("вчера "):
            date_value = now - timedelta(days=1)
            clean = clean[6:]
        else:
            match_date = _DATE.match(clean)
            if match_date:
                day = int(match_date.group("day"))
                month = int(match_date.group("month"))
                year_raw = match_date.group("year")
                year = int(year_raw) if year_raw else now.year
                date_value = datetime(year, month, day, now.hour, now.minute, tzinfo=self.zone)
                clean = clean[match_date.end() :]
        match = _AMOUNT.match(clean)
        if not match:
            raise ValueError("Введите сумму и описание, например: 1450 ресторан")
        amount_raw = match.group("amount").replace(" ", "").replace("\u00a0", "")
        if _AMBIGUOUS_DOT.fullmatch(amount_raw):
            raise ValueError("Сумма с точкой неоднозначна. Напишите 1500 или 1,50")
        amount = parse_minor(amount_raw)
        kind = TransactionType.INCOME if match.group("sign") == "+" else TransactionType.EXPENSE
        rest, account, category = _extract_hints(match.group("rest"))
        category_explicit = category is not None
        if not rest:
            raise ValueError("Добавьте описание операции")
        if len(rest) > 500:
            raise ValueError("Описание должно быть не длиннее 500 символов")

        if account is not None:
            account = unicodedata.normalize("NFKC", account)
        if category is not None:
            category = normalize_rule_text(category.replace("_", " "))
            category = CATEGORY_ALIASES.get(category, category)
        if category is None:
            allowed = INCOME_CATEGORIES if kind is TransactionType.INCOME else EXPENSE_CATEGORIES
            normalized_description = normalize_rule_text(rest)
            for word, found in KEYWORDS.items():
                if found in allowed and phrase_occurs(normalized_description, word):
                    category = found
                    break
        needs_confirmation = bool(
            date_value
            and (date_value > now + timedelta(minutes=1) or date_value < now - timedelta(days=366))
        )
        return TransactionDraft(
            amount_minor=amount,
            type=kind,
            occurred_at=date_value,
            category_hint=category,
            category_explicit=category_explicit,
            account_hint=account,
            description=rest,
            needs_confirmation=needs_confirmation,
        )
