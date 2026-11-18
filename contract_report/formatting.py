"""Форматирование сумм и дат для отчёта."""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

KOPECK = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    """Округление до копейки (на входных данных округлять нечего - это страховка)."""
    return value.quantize(KOPECK, rounding=ROUND_HALF_UP)


def fmt_money(value: Decimal, decimal_sep: str = ",") -> str:
    """Сумма для CSV: без разделителя тысяч, чтобы Excel распознал число."""
    return f"{money(value):.2f}".replace(".", decimal_sep)


def fmt_money_human(value: Decimal) -> str:
    """Сумма для консоли и комментариев: 300 000,00."""
    return f"{money(value):,.2f}".replace(",", " ").replace(".", ",")


def fmt_date(value: date | None) -> str:
    return value.strftime("%d.%m.%Y") if value else ""
