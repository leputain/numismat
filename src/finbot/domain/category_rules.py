import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from finbot.domain.transactions import TransactionType

MAX_RULE_TOKENS = 5
MAX_RULE_PATTERN_LENGTH = 200
_TOKEN = re.compile(r"[^\W_]+(?:[-'’][^\W_]+)*", re.UNICODE)
_STOP_WORDS = frozenset(
    {
        "а",
        "без",
        "в",
        "для",
        "до",
        "за",
        "и",
        "из",
        "к",
        "на",
        "не",
        "о",
        "от",
        "по",
        "с",
        "со",
        "у",
    }
)


def rule_tokens(value: str) -> tuple[str, ...]:
    """Return NFKC/casefold tokens, treating ``ё`` and ``е`` as equal."""
    folded = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return tuple(match.group(0) for match in _TOKEN.finditer(folded))


def normalize_rule_text(value: str) -> str:
    return " ".join(rule_tokens(value))


def normalize_rule_pattern(value: str) -> str:
    tokens = rule_tokens(value)
    if not tokens:
        raise ValueError("Фраза правила не может быть пустой")
    if len(tokens) > MAX_RULE_TOKENS:
        raise ValueError(f"Фраза правила должна содержать не больше {MAX_RULE_TOKENS} слов")
    normalized = " ".join(tokens)
    if len(normalized) > MAX_RULE_PATTERN_LENGTH:
        raise ValueError("Фраза правила слишком длинная")
    return normalized


def phrase_occurs(description: str, pattern: str) -> bool:
    """Match a normalized phrase only as a contiguous sequence of whole tokens."""
    description_tokens = rule_tokens(description)
    pattern_tokens = rule_tokens(pattern)
    if not pattern_tokens or len(pattern_tokens) > len(description_tokens):
        return False
    size = len(pattern_tokens)
    return any(
        description_tokens[start : start + size] == pattern_tokens
        for start in range(len(description_tokens) - size + 1)
    )


def validate_rule_pattern(pattern: str, description: str) -> str:
    normalized = normalize_rule_pattern(pattern)
    if not phrase_occurs(description, normalized):
        raise ValueError("Фраза правила должна встречаться в описании целиком")
    return normalized


def suggest_rule_candidates(description: str, limit: int = 4) -> tuple[str, ...]:
    """Suggest deterministic phrases that are guaranteed to occur in the description."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("Количество подсказок не может быть отрицательным")
    if limit == 0:
        return ()
    tokens = rule_tokens(description)
    if not tokens:
        return ()

    scored: list[tuple[tuple[int, int, int, int], str]] = []
    maximum = min(MAX_RULE_TOKENS, len(tokens))
    for size in range(1, maximum + 1):
        for start in range(len(tokens) - size + 1):
            phrase_tokens = tokens[start : start + size]
            content_count = sum(
                token not in _STOP_WORDS and not token.isdecimal() for token in phrase_tokens
            )
            if content_count == 0:
                continue
            phrase = " ".join(phrase_tokens)
            score = (content_count, size, len(phrase), -start)
            scored.append((score, phrase))

    unique: list[str] = []
    seen: set[str] = set()
    for _score, phrase in sorted(scored, key=lambda item: item[0], reverse=True):
        if phrase in seen:
            continue
        seen.add(phrase)
        unique.append(phrase)
        if len(unique) == limit:
            break
    return tuple(unique)


@dataclass(frozen=True, slots=True)
class CategoryRuleSpec:
    id: UUID
    category_id: UUID
    kind: TransactionType
    normalized_pattern: str
    account_id: UUID | None = None
    version: int = 1
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        normalized = normalize_rule_pattern(self.normalized_pattern)
        if normalized != self.normalized_pattern:
            raise ValueError("Фраза правила должна быть нормализована")
        if self.version < 1:
            raise ValueError("Версия правила должна быть положительной")
        if self.updated_at is not None and self.updated_at.utcoffset() is None:
            raise ValueError("Дата изменения правила должна содержать часовой пояс")


def choose_category_rule(
    description: str,
    rules: tuple[CategoryRuleSpec, ...] | list[CategoryRuleSpec],
    *,
    kind: TransactionType,
    account_id: UUID | None,
) -> CategoryRuleSpec | None:
    """Choose by scope, token count, phrase length and recency, in that order."""
    matches = [
        rule
        for rule in rules
        if rule.kind == kind
        and (rule.account_id is None or rule.account_id == account_id)
        and phrase_occurs(description, rule.normalized_pattern)
    ]
    if not matches:
        return None

    epoch = datetime.min.replace(tzinfo=UTC)

    def priority(rule: CategoryRuleSpec) -> tuple[bool, int, int, datetime, int, int]:
        return (
            rule.account_id is not None,
            len(rule_tokens(rule.normalized_pattern)),
            len(rule.normalized_pattern),
            rule.updated_at or epoch,
            rule.version,
            rule.id.int,
        )

    return max(matches, key=priority)
