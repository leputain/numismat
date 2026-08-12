from datetime import datetime
from html import escape
from math import ceil
from zoneinfo import ZoneInfo

from finbot.application.queries.reports import CategoryTotal, ReportTransaction
from finbot.application.queries.transactions import TransactionDetails

CURRENCY_SYMBOLS = {"RUB": "₽", "USD": "$", "EUR": "€"}
MONTH_NAMES = (
    "",
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)


def money(amount_minor: int, currency: str, *, sign: str = "") -> str:
    major, fraction = divmod(abs(amount_minor), 100)
    grouped = f"{major:,}".replace(",", " ")
    number = grouped if fraction == 0 else f"{grouped},{fraction:02d}"
    return f"{sign}{number} {CURRENCY_SYMBOLS.get(currency, currency)}"


def operation_sign(kind: str) -> str:
    return "−" if kind == "expense" else "+"


def local_date(value: datetime, timezone: str, now: datetime | None = None) -> str:
    zone = ZoneInfo(timezone)
    local = value.astimezone(zone)
    current = (now or datetime.now(zone)).astimezone(zone)
    if local.date() == current.date():
        day = "Сегодня"
    elif (current.date() - local.date()).days == 1:
        day = "Вчера"
    else:
        day = local.strftime("%d.%m.%Y")
    return f"{day}, {local:%H:%M}"


def transaction_card(item: TransactionDetails, timezone: str, *, title: str = "Операция") -> str:
    emoji = item.category_emoji or ("💸" if item.type == "expense" else "💰")
    description = f"\n📝 {escape(item.description)}" if item.description else ""
    status = "\n\n<i>Операция удалена из отчётов</i>" if item.deleted_at else ""
    return (
        f"<b>{escape(title)}</b>\n\n"
        f"<b>{money(item.amount_minor, item.currency, sign=operation_sign(item.type))}</b>\n"
        f"{emoji} {escape(item.category_name)}\n"
        f"💳 {escape(item.account_name)}\n"
        f"🕒 {local_date(item.occurred_at, timezone)}"
        f"{description}{status}"
    )


def wizard_summary(payload: dict[str, object], currency: str, timezone: str) -> str:
    kind = str(payload["type"])
    occurred_at = datetime.fromisoformat(str(payload["occurred_at"]))
    description = str(payload.get("description", "")).strip()
    description_line = f"\n📝 {escape(description)}" if description else ""
    warning = (
        "\n\n⚠️ <b>Проверьте дату и время:</b> распознавание может быть неточным."
        if payload.get("needs_confirmation")
        else ""
    )
    return (
        "<b>Новая операция · проверьте</b>\n\n"
        f"<b>{money(int(str(payload['amount'])), currency, sign=operation_sign(kind))}</b>\n"
        f"🏷 {escape(str(payload['category_name']))}\n"
        f"💳 {escape(str(payload['account_name']))}\n"
        f"🕒 {local_date(occurred_at, timezone)}"
        f"{description_line}{warning}"
    )


def history_text(items: list[TransactionDetails], page: int, total: int, timezone: str) -> str:
    if not items:
        return "<b>История</b>\n\nОпераций пока нет. Добавьте первую через меню."
    lines = [
        f"<b>Все операции</b> · {total}",
        "Нажмите на операцию для просмотра или 🗑 для удаления.",
        "",
    ]
    for index, item in enumerate(items, start=1):
        emoji = item.category_emoji or "▫️"
        description = f" · {escape(item.description[:28])}" if item.description else ""
        lines.append(
            f"<b>#{index}</b> {operation_sign(item.type)}{money(item.amount_minor, item.currency)} "
            f"· {emoji} {escape(item.category_name)}\n"
            f"    {local_date(item.occurred_at, timezone)}{description}"
        )
    lines.append(f"\nСтраница {page + 1} из {max(ceil(total / 5), 1)}")
    return "\n".join(lines)


def trash_text(items: list[TransactionDetails], page: int, total: int, timezone: str) -> str:
    if not items:
        return "<b>Корзина</b>\n\nУдалённых операций нет."
    lines = [f"<b>Корзина</b> · {total}", "Нажмите на операцию для восстановления.", ""]
    for index, item in enumerate(items, start=1):
        lines.append(
            f"<b>#{index}</b> {operation_sign(item.type)}{money(item.amount_minor, item.currency)} "
            f"· {item.category_emoji or '▫️'} {escape(item.category_name)}\n"
            f"    {local_date(item.occurred_at, timezone)}"
        )
    lines.append(f"\nСтраница {page + 1} из {max(ceil(total / 5), 1)}")
    return "\n".join(lines)


def report_text(
    title: str,
    totals: dict[str, dict[str, int]],
    categories: list[CategoryTotal],
    transactions: list[ReportTransaction],
    timezone: str,
    *,
    previous_expense: dict[str, int] | None = None,
) -> str:
    lines = [f"<b>{escape(title)}</b>", ""]
    if not totals:
        return "\n".join(lines + ["Операций за этот период пока нет."])
    for currency, values in sorted(totals.items()):
        income = values.get("income", 0)
        expense = values.get("expense", 0)
        lines.extend(
            [
                f"Доходы     <b>{money(income, currency, sign='+')}</b>",
                f"Расходы    <b>{money(expense, currency, sign='−')}</b>",
                "Итог периода <b>"
                f"{money(abs(income - expense), currency, sign='+' if income >= expense else '−')}"
                "</b>",
            ]
        )
        if previous_expense is not None:
            previous = previous_expense.get(currency, 0)
            if previous:
                change = ((expense - previous) * 100) // previous
                direction = "+" if change > 0 else ""
                lines.append(f"К тому же периоду прошлого месяца: <b>{direction}{change}%</b>")
        lines.append("")
    if categories:
        lines.append("<b>Расходы по категориям</b>")
        for category in categories[:8]:
            lines.append(
                f"{category.emoji or '▫️'} {escape(category.name)} — "
                f"{money(category.amount_minor, category.currency)}"
            )
    if transactions:
        lines.extend(["", "<b>Последние операции</b>"])
        for item in transactions[:8]:
            description = f" · {escape(item.description[:24])}" if item.description else ""
            lines.append(
                f"{operation_sign(item.type)}{money(item.amount_minor, item.currency)} "
                f"· {item.category_emoji or '▫️'} {escape(item.category_name)}{description}"
            )
    return "\n".join(lines)


HELP_TEXT = """<b>Как пользоваться Finbot</b>

<b>Самый быстрый способ</b>
Просто отправьте покупку одним сообщением:
<pre>1450 ресторан
+250000 зарплата
вчера 799 кино
3200 бензин @наличные
1450 #рестораны ужин</pre>

Можно также отправить фото чека или банковский скриншот.
Чек бот покажет как одну операцию с итоговой суммой.
В банковском списке бот может найти до 20 операций и покажет их по одной.
Каждую, включая последнюю, нужно отдельно проверить, сохранить или пропустить.
Если строки нельзя надёжно разделить, бот ничего не сохранит и попросит ввести их текстом.

<b>Как бот читает сообщение</b>
<pre>− без знака   расход
+ плюс        доход
@название     счёт
#название     категория</pre>

Если не хочется запоминать формат, нажмите <b>➕ Добавить операцию</b>.
Бот по шагам спросит тип, сумму, категорию, счёт, дату и комментарий.
Свои категории и счета можно создавать прямо там или управлять ими через <b>⚙️ Настройки</b>:
переименовывать, выбирать основной счёт, архивировать и восстанавливать.

<b>Полезные действия</b>
• <b>Сегодня</b> и <b>Месяц</b> — отчёты и категории расходов.
• <b>Все операции</b> — просмотр, полное редактирование и удаление.
• <b>Отменить действие</b> — отменяет последнее создание, изменение, удаление или восстановление.
• <b>Экспорт</b> — CSV в UTF-8 для Excel и таблиц.

Доступ разрешён только владельцу и только в личном чате."""
