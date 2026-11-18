"""Финансовый отчёт по договорам на дату.

Пример: python main.py --date 2026-11-10
"""
import sys

from contract_report.cli import main

if __name__ == "__main__":
    sys.exit(main())
