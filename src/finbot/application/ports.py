from dataclasses import dataclass
from typing import Protocol

from finbot.domain.transactions import TransactionDraft, TransactionView


class TransactionParser(Protocol):
    def parse(self, text: str) -> TransactionDraft: ...


class TransactionRepository(Protocol):
    async def add(self, draft: TransactionDraft, user_id: object) -> TransactionView: ...


@dataclass(frozen=True, slots=True)
class ParserResult:
    draft: TransactionDraft
    category: str | None
