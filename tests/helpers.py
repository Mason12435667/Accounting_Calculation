"""Вспомогательные функции для тестов: короткая сборка синтетических данных."""
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from contract_report.engine import ZERO, Report
from contract_report.models import Contract, Dataset, Payment, Work

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"

D = Decimal


def d(text):
    return date.fromisoformat(text) if text else None


def contract(cid="C-1", amount="1000.00", signed="2026-01-01"):
    return Contract(cid, "Подрядчик", D(amount), d(signed))


def work(wid, price, status="planned", done=None, cancelled=None, cid="C-1"):
    return Work(wid, cid, f"Работа {wid}", D(price), status, d(done), d(cancelled))


def payment(pid, amount, status="paid", paid=None, created=None, refunded=None, work_id=None, cid="C-1"):
    created = created or paid or "2026-01-01"
    return Payment(pid, cid, work_id, "назначение", D(amount), status, d(created), d(paid), d(refunded))


def dataset(contracts, works, payments):
    return Dataset(tuple(contracts), tuple(works), tuple(payments))


def by_contract(report: Report, cid):
    return next(c for c in report.contracts if c.contract.contract_id == cid)


def by_work(report: Report, wid):
    for cr in report.contracts:
        for w in cr.works:
            if w.work.work_id == wid:
                return w
    raise KeyError(wid)


def by_payment(report: Report, pid):
    return next(p for p in report.payments if p.payment.payment_id == pid)


def date_range(start, end):
    current = d(start)
    while current <= d(end):
        yield current
        current += timedelta(days=1)


def allocated(pl):
    return sum((a.amount for a in pl.allocations), ZERO)
