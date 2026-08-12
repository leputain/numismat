import pytest

from finbot.adapters.telegram.parser import DeterministicParser

CASES = [f"{amount} ресторан" for amount in range(100, 150)]


@pytest.mark.parametrize("text", CASES)
def test_parser_accepts_representative_inputs(text: str) -> None:
    draft = DeterministicParser().parse(text)
    assert draft.amount_minor > 0
    assert draft.description
