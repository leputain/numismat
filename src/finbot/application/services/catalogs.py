from finbot.domain.errors import StaleObjectError


def normalize_catalog_name(value: str) -> str:
    """Validate a catalog label without depending on persistence details."""

    clean = " ".join(value.strip().split())
    if not clean:
        raise ValueError("Название не может быть пустым")
    if len(clean) > 60:
        raise ValueError("Название должно быть короче 60 символов")
    if not clean.isprintable():
        raise ValueError("Название содержит недопустимые символы")
    return clean


def catalog_slug(value: str) -> str:
    """Build the stable, case-insensitive key used by catalog adapters."""

    return "-".join(value.casefold().strip().split())[:100]


def ensure_expected_version(
    entity_label: str, current_version: int, expected_version: int | None
) -> None:
    """Reject a stale catalog command while allowing legacy unversioned callers."""

    if expected_version is not None and current_version != expected_version:
        raise StaleObjectError(f"{entity_label} уже изменён. Обновите список")
