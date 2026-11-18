"""Исходные данные выгрузки: договоры, работы, платежи.

Все денежные значения - Decimal с точностью до копейки.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class Contract:
    contract_id: str
    contractor: str
    amount: Decimal
    signed_at: date | None


@dataclass(frozen=True)
class Work:
    work_id: str
    contract_id: str
    title: str
    price: Decimal
    status: str  # planned | done | cancelled - статус на момент выгрузки
    completed_at: date | None
    cancelled_at: date | None


@dataclass(frozen=True)
class Payment:
    payment_id: str
    contract_id: str
    work_id: str | None  # None - платёж по договору без привязки к работе
    purpose: str
    amount: Decimal
    status: str  # paid | queued | refunded - статус на момент выгрузки
    created_at: date | None
    paid_at: date | None
    refunded_at: date | None


@dataclass(frozen=True)
class Dataset:
    contracts: tuple[Contract, ...]
    works: tuple[Work, ...]
    payments: tuple[Payment, ...]

    def latest_date(self) -> date | None:
        """Самая поздняя дата события в выгрузке (нижняя граница даты выгрузки)."""
        dates = [c.signed_at for c in self.contracts]
        dates += [d for w in self.works for d in (w.completed_at, w.cancelled_at)]
        dates += [d for p in self.payments for d in (p.created_at, p.paid_at, p.refunded_at)]
        dates = [d for d in dates if d is not None]
        return max(dates) if dates else None
