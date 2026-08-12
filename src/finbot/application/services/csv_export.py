import csv
import io
from zoneinfo import ZoneInfo

from finbot.application.queries.transactions import TransactionDetails


def amount_major(amount_minor: int) -> str:
    sign = "-" if amount_minor < 0 else ""
    major, fraction = divmod(abs(amount_minor), 100)
    return f"{sign}{major},{fraction:02d}"


def spreadsheet_safe(value: str) -> str:
    """Prevent CSV cells controlled by the owner from becoming formulas."""
    return f"'{value}" if value.lstrip().startswith(("=", "+", "-", "@")) else value


def build_csv(rows: list[TransactionDetails], timezone: str = "UTC") -> bytes:
    zone = ZoneInfo(timezone)
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";", lineterminator="\n")
    writer.writerow(("тип", "сумма", "валюта", "счёт", "категория", "дата", "описание"))
    for item in rows:
        writer.writerow(
            (
                "расход" if item.type == "expense" else "доход",
                amount_major(item.amount_minor),
                item.currency,
                spreadsheet_safe(item.account_name),
                spreadsheet_safe(item.category_name),
                item.occurred_at.astimezone(zone).isoformat(),
                spreadsheet_safe(item.description),
            )
        )
    return output.getvalue().encode("utf-8-sig")
