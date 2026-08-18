from finbot.adapters.telegram.parser import DeterministicParser
from finbot.domain.transactions import TransactionDraft


class DeterministicQuickDraftParser:
    """Channel-neutral adapter for the existing deterministic parser."""

    def parse(self, text: str, *, timezone: str) -> TransactionDraft:
        return DeterministicParser(timezone).parse(text)
