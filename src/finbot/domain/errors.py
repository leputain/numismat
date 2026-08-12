class FinbotError(Exception):
    """Base error safe to present to the owner."""


class ObjectNotFoundError(FinbotError):
    pass


class StaleObjectError(FinbotError):
    pass


class UnknownCategoryError(FinbotError):
    def __init__(self, category: str) -> None:
        super().__init__(f"Категория «{category}» не найдена")


class UnknownAccountError(FinbotError):
    def __init__(self, account: str) -> None:
        super().__init__(f"Счёт «{account}» не найден")
