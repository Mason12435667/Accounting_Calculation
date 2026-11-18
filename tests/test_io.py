"""Чтение данных, CSV-отчёт и запуск из командной строки."""
import csv
import hashlib
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from contract_report.cli import main
from contract_report.loader import DataError, load_dataset, parse_amount

from .helpers import D, DATA_DIR, PROJECT_DIR


def read_csv(path, delimiter=";", encoding="utf-8-sig"):
    with open(path, encoding=encoding, newline="") as fh:
        return list(csv.reader(fh, delimiter=delimiter))


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(args))
    return code, out.getvalue(), err.getvalue()


class LoaderTests(unittest.TestCase):

    def test_amounts_are_decimal(self):
        self.assertEqual(parse_amount("15000.00", "x"), D("15000.00"))
        self.assertEqual(parse_amount("7", "x"), D("7.00"))
        for bad in ("1e5", "12.345", "-5.00", "abc", "", "1 000.00", "1000,00"):
            with self.assertRaises(DataError, msg=bad):
                parse_amount(bad, "x")

    def test_real_data_loads(self):
        data = load_dataset(DATA_DIR)
        self.assertEqual((len(data.contracts), len(data.works), len(data.payments)), (2, 9, 8))
        self.assertEqual(data.contracts[1].contractor, "Иван Петров, дизайнер")  # запятая в кавычках
        self.assertTrue(all(isinstance(p.amount, type(D("1"))) for p in data.payments))

    def test_source_files_are_not_modified(self):
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in DATA_DIR.glob("*.csv")}
        with tempfile.TemporaryDirectory() as tmp:
            run_cli("--date", "2026-11-10", "--out", tmp)
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in DATA_DIR.glob("*.csv")}
        self.assertEqual(before, after)

    def test_bad_data_stops_with_clear_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("contracts.csv", "works.csv", "payments.csv"):
                shutil.copy(DATA_DIR / name, tmp)
            path = Path(tmp) / "payments.csv"
            path.write_text(path.read_text(encoding="utf-8").replace("15000.00", "15000.5x"), encoding="utf-8")
            code, _, err = run_cli("--date", "2026-11-10", "--data", tmp, "--out", tmp)
        self.assertEqual(code, 2)
        self.assertIn("некорректная сумма", err)

    def test_duplicate_ids_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("contracts.csv", "works.csv", "payments.csv"):
                shutil.copy(DATA_DIR / name, tmp)
            path = Path(tmp) / "payments.csv"
            text = path.read_text(encoding="utf-8")
            path.write_text(text + text.splitlines()[1] + "\n", encoding="utf-8")
            with self.assertRaises(DataError):
                load_dataset(Path(tmp))


class CsvReportTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_report_files_and_totals(self):
        code, stdout, _ = run_cli("--date", "2026-11-10", "--out", str(self.out))
        self.assertEqual(code, 0)
        self.assertIn("Отчёт на 10.11.2026", stdout)
        rows = read_csv(self.out / "report_2026-11-10.csv")
        header = rows[3]
        col = {name: i for i, name in enumerate(header)}
        totals = {r[0]: r for r in rows if len(r) > 2 and r[2] == "ИТОГО"}
        d1, d2 = totals["D-1"], totals["D-2"]
        self.assertEqual(d1[col["Цена"]], "500000,00")
        self.assertEqual(d1[col["Выплачено всего"]], "315000,00")
        self.assertEqual(d1[col["Осталось оплатить"]], "185000,00")
        self.assertEqual(d1[col["В очереди на оплату"]], "150000,00")
        self.assertEqual(d2[col["Выплачено всего"]], "75000,00")
        self.assertEqual(d2[col["Осталось оплатить"]], "15000,00")
        # столбцы "Выплачено" и "Осталось" по строкам договора складываются в итог
        for cid, total in totals.items():
            lines = [r for r in rows[4:] if len(r) > 10 and r[0] == cid and r[2] != "ИТОГО"]
            for name in ("Выплачено всего", "Осталось оплатить"):
                s = sum(D(r[col[name]].replace(",", ".")) for r in lines if r[col[name]])
                self.assertEqual(s, D(total[col[name]].replace(",", ".")), (cid, name))
        self.assertTrue((self.out / "payments_2026-11-10.csv").is_file())

    def test_rerun_gives_identical_files(self):
        run_cli("--date", "2026-11-10", "--out", str(self.out))
        first = {p.name: p.read_bytes() for p in self.out.iterdir()}
        run_cli("--date", "2026-11-10", "--out", str(self.out))
        second = {p.name: p.read_bytes() for p in self.out.iterdir()}
        self.assertEqual(first, second)
        self.assertEqual(sorted(first), ["payments_2026-11-10.csv", "report_2026-11-10.csv"])  # без .tmp

    def test_standard_csv_style(self):
        run_cli("--date", "2026-11-10", "--out", str(self.out), "--csv-style", "standard")
        rows = read_csv(self.out / "report_2026-11-10.csv", delimiter=",", encoding="utf-8")
        d1 = next(r for r in rows if len(r) > 2 and r[0] == "D-1" and r[2] == "ИТОГО")
        self.assertIn("315000.00", d1)

    def test_russian_date_format_accepted(self):
        code, _, _ = run_cli("--date", "10.11.2026", "--out", str(self.out))
        self.assertEqual(code, 0)
        self.assertTrue((self.out / "report_2026-11-10.csv").is_file())

    def test_bad_date_rejected(self):
        with self.assertRaises(SystemExit) as ctx:
            run_cli("--date", "2026-13-40", "--out", str(self.out))
        self.assertEqual(ctx.exception.code, 2)

    def test_main_py_runs_as_script(self):
        proc = subprocess.run([sys.executable, str(PROJECT_DIR / "main.py"), "--date", "2026-11-10",
                               "--out", str(self.out)], capture_output=True, cwd=str(self.out))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertIn("ИТОГО", proc.stdout.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
