import pytest

from finbot.application.services.catalogs import ensure_expected_version
from finbot.domain.errors import StaleObjectError


def test_expected_catalog_version_accepts_matching_and_legacy_commands() -> None:
    ensure_expected_version("Счёт", 7, 7)
    ensure_expected_version("Счёт", 7, None)


def test_expected_catalog_version_rejects_stale_command() -> None:
    with pytest.raises(StaleObjectError, match="Счёт уже изменён"):
        ensure_expected_version("Счёт", 8, 7)
