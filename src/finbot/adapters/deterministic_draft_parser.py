import re

from finbot.adapters.telegram.parser import DeterministicParser
from finbot.application.draft_preparation import (
    AmountOnlyQuickDraft,
    QuickDraftParseResult,
    SignedAmountOnlyQuickDraftError,
)
from finbot.domain.money import parse_minor

_UNGROUPED_INTEGER = r"(?:0|[1-9][0-9]*)"
_GROUPED_INTEGER = r"[1-9][0-9]{0,2}(?P<separator>[ \u00a0])[0-9]{3}(?:(?P=separator)[0-9]{3})*"
_UNSIGNED_AMOUNT_ONLY = re.compile(
    rf"^(?P<amount>(?:{_UNGROUPED_INTEGER}|{_GROUPED_INTEGER})(?:[.,][0-9]{{1,2}})?)$"
)
_SIGNED_AMOUNT_ONLY = re.compile(
    rf"^(?P<sign>[+-])(?P<amount>(?:{_UNGROUPED_INTEGER}|{_GROUPED_INTEGER})(?:[.,][0-9]{{1,2}})?)$"
)
_NUMERIC_LIKE_INPUT = re.compile(r"^[+-]?[0-9][0-9.,\s\u00a0]*$", re.ASCII)
_LOOSE_AMOUNT = r"[+-]?[0-9][0-9., \u00a0]*"
_CURRENCY_TOKEN = r"(?:RUB|USD|EUR|GBP|CNY|JPY|₽|\$|€|£|¥|руб(?:\.|ль|ля|лей)?)"
_AMOUNT_WITH_CURRENCY = re.compile(
    rf"^(?:{_LOOSE_AMOUNT}\s*{_CURRENCY_TOKEN}|{_CURRENCY_TOKEN}\s*{_LOOSE_AMOUNT})$",
    re.IGNORECASE,
)


def _classify_amount_only(text: str) -> AmountOnlyQuickDraft | None:
    clean = text.strip()
    match = _UNSIGNED_AMOUNT_ONLY.fullmatch(clean)
    if match is not None:
        return AmountOnlyQuickDraft(parse_minor(match.group("amount")))

    signed = _SIGNED_AMOUNT_ONLY.fullmatch(clean)
    if signed is not None:
        # Validate the unsigned body first so malformed/zero signed values keep
        # the generic fail-closed error instead of receiving sign guidance.
        parse_minor(signed.group("amount"))
        raise SignedAmountOnlyQuickDraftError

    if _NUMERIC_LIKE_INPUT.fullmatch(clean):
        raise ValueError("Amount-only quick draft has an invalid format")
    if _AMOUNT_WITH_CURRENCY.fullmatch(clean):
        raise ValueError("Amount-only quick draft must not contain a currency")
    return None


class DeterministicQuickDraftParser:
    """Channel-neutral adapter for the existing deterministic parser."""

    def parse(self, text: str, *, timezone: str) -> QuickDraftParseResult:
        amount_only = _classify_amount_only(text)
        if amount_only is not None:
            return amount_only
        return DeterministicParser(timezone).parse(text)
