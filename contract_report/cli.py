"""Командная строка: python main.py --date 2026-11-10"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from .engine import build_report
from .export import CSV_STYLES, summary_text, write_report
from .loader import DataError, load_dataset

PROJECT_DIR = Path(__file__).resolve().parent.parent


def parse_report_date(text: str) -> date:
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f"некорректная дата {text!r}: используйте ГГГГ-ММ-ДД или ДД.ММ.ГГГГ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Финансовый отчёт по договорам на дату: по каждой работе - цена, выплачено, "
                    "осталось оплатить; итоги по договорам; замечания по данным.")
    parser.add_argument("--date", required=True, type=parse_report_date,
                        help="дата отчёта, например 2026-11-10 или 10.11.2026 (события этого дня учитываются)")
    parser.add_argument("--data", type=Path, default=PROJECT_DIR / "data",
                        help="папка с contracts.csv, works.csv, payments.csv (по умолчанию data/ проекта)")
    parser.add_argument("--out", type=Path, default=PROJECT_DIR / "output",
                        help="папка для отчётов (по умолчанию output/ проекта)")
    parser.add_argument("--csv-style", choices=sorted(CSV_STYLES), default="excel",
                        help="excel - ';' и десятичная запятая (Excel с русскими настройками, по умолчанию); "
                             "standard - ',' и десятичная точка")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            # в консоль Windows Python пишет Unicode сам; при перенаправлении в файл/трубу - UTF-8
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        data = load_dataset(args.data)
    except DataError as exc:
        print(f"Ошибка в исходных данных: {exc}", file=sys.stderr)
        return 2
    report = build_report(data, args.date)
    try:
        paths = write_report(report, args.out, args.csv_style)
    except PermissionError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    print(summary_text(report))
    print()
    print("Файлы отчёта:")
    for path in paths:
        print(f"  {path}")
    return 0
