from datetime import datetime
from zoneinfo import ZoneInfo


def parse_local_datetime(raw: str, timezone: str, *, now: datetime | None = None) -> datetime:
    zone = ZoneInfo(timezone)
    current = now.astimezone(zone) if now is not None else datetime.now(zone)
    clean = raw.strip().casefold()
    if clean in {"сегодня", "today"}:
        return current
    if clean in {"вчера", "yesterday"}:
        from datetime import timedelta

        return current - timedelta(days=1)
    parts = clean.split(".")
    if len(parts) in {2, 3}:
        try:
            day, month = int(parts[0]), int(parts[1])
            year = int(parts[2]) if len(parts) == 3 else current.year
            return datetime(year, month, day, current.hour, current.minute, tzinfo=zone)
        except ValueError:
            pass
    raise ValueError("Введите дату как ДД.ММ или ДД.ММ.ГГГГ")
