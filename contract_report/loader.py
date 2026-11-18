"""Чтение и проверка CSV-файлов выгрузки.

Грубые ошибки формата (нечитаемая сумма или дата, неизвестный статус,
дубли номеров) останавливают программу: считать по таким данным нельзя.
Логические несоответствия (например, работа "done" без даты приёмки)
не останавливают расчёт - их показывает отчёт в разделе замечаний.
"""
from __future__ import annotations

import csv
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from .models import Contract, Dataset, Payment, Work

_AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")

WORK_STATUSES = {"planned", "done", "cancelled"}
PAYMENT_STATUSES = {"paid", "queued", "refunded"}

CONTRACT_COLUMNS = ["contract_id", "contractor", "amount", "signed_at"]
WORK_COLUMNS = ["work_id", "contract_id", "title", "price", "status", "completed_at", "cancelled_at"]
PAYMENT_COLUMNS = ["payment_id", "contract_id", "work_id", "purpose", "amount", "status",
                   "created_at", "paid_at", "refunded_at"]


class DataError(Exception):
    """Данные нельзя прочитать или они противоречат формату."""


def parse_amount(raw: str, where: str) -> Decimal:
    text = (raw or "").strip()
    if not _AMOUNT_RE.match(text):
        raise DataError(f"{where}: некорректная сумма {raw!r} (ожидается неотрицательное число "
                        f"с точкой и не более чем двумя знаками после неё, например 15000.00)")
    return Decimal(text).quantize(Decimal("0.01"))


def parse_date(raw: str, where: str) -> date | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise DataError(f"{where}: некорректная дата {raw!r} (ожидается ГГГГ-ММ-ДД)") from None


def _required(row: dict, column: str, where: str) -> str:
    value = (row.get(column) or "").strip()
    if not value:
        raise DataError(f"{where}: не заполнено поле {column}")
    return value


def _read_rows(path: Path, columns: list[str]):
    if not path.is_file():
        raise DataError(f"Файл не найден: {path}")
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in columns if c not in (reader.fieldnames or [])]
        if missing:
            raise DataError(f"{path.name}: нет колонок {', '.join(missing)}")
        for row in reader:
            if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
                continue  # пустая строка
            yield f"{path.name}, строка {reader.line_num}", row


def _check_unique(ids: list[str], what: str) -> None:
    seen = set()
    for item in ids:
        if item in seen:
            raise DataError(f"{what}: повторяется номер {item}")
        seen.add(item)


def load_dataset(data_dir: Path) -> Dataset:
    data_dir = Path(data_dir)

    contracts = []
    for where, row in _read_rows(data_dir / "contracts.csv", CONTRACT_COLUMNS):
        contracts.append(Contract(
            contract_id=_required(row, "contract_id", where),
            contractor=(row["contractor"] or "").strip(),
            amount=parse_amount(row["amount"], where),
            signed_at=parse_date(row["signed_at"], where),
        ))

    works = []
    for where, row in _read_rows(data_dir / "works.csv", WORK_COLUMNS):
        status = (row["status"] or "").strip()
        if status not in WORK_STATUSES:
            raise DataError(f"{where}: неизвестный статус работы {status!r}")
        works.append(Work(
            work_id=_required(row, "work_id", where),
            contract_id=_required(row, "contract_id", where),
            title=(row["title"] or "").strip(),
            price=parse_amount(row["price"], where),
            status=status,
            completed_at=parse_date(row["completed_at"], where),
            cancelled_at=parse_date(row["cancelled_at"], where),
        ))

    payments = []
    for where, row in _read_rows(data_dir / "payments.csv", PAYMENT_COLUMNS):
        status = (row["status"] or "").strip()
        if status not in PAYMENT_STATUSES:
            raise DataError(f"{where}: неизвестный статус платежа {status!r}")
        payments.append(Payment(
            payment_id=_required(row, "payment_id", where),
            contract_id=_required(row, "contract_id", where),
            work_id=(row["work_id"] or "").strip() or None,
            purpose=(row["purpose"] or "").strip(),
            amount=parse_amount(row["amount"], where),
            status=status,
            created_at=parse_date(row["created_at"], where),
            paid_at=parse_date(row["paid_at"], where),
            refunded_at=parse_date(row["refunded_at"], where),
        ))

    _check_unique([c.contract_id for c in contracts], "contracts.csv")
    _check_unique([w.work_id for w in works], "works.csv")
    _check_unique([p.payment_id for p in payments], "payments.csv")
    return Dataset(tuple(contracts), tuple(works), tuple(payments))
