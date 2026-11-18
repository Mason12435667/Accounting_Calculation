"""Отчёт по реальной выгрузке из data/ на разные даты."""
import unittest

from contract_report.engine import (P_NOT_YET, P_PAID, P_QUEUED, P_REFUNDED, TO_ADVANCE, TO_OVERPAY, W_CANCELLED,
                                    W_DONE, W_PLANNED, ZERO, build_report)
from contract_report.loader import load_dataset

from .helpers import D, DATA_DIR, allocated, by_contract, by_payment, by_work, d, date_range


class RealDataTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_dataset(DATA_DIR)

    def report(self, on):
        return build_report(self.data, d(on))


class ReportOn20261110(RealDataTestCase):
    """Контрольная дата бухгалтера."""

    def setUp(self):
        self.r = self.report("2026-11-10")

    def assertWork(self, wid, state, price, direct, advance, remaining):
        w = by_work(self.r, wid)
        self.assertEqual(w.state, state, wid)
        self.assertEqual(w.work.price, D(price), wid)
        self.assertEqual(w.direct_paid, D(direct), wid)
        self.assertEqual(w.advance_credited, D(advance), wid)
        self.assertEqual(w.remaining, D(remaining), wid)

    def test_d1_works(self):
        self.assertWork("W-11", W_DONE, "100000.00", "0.00", "100000.00", "0.00")
        self.assertWork("W-12", W_DONE, "100000.00", "0.00", "100000.00", "0.00")
        self.assertWork("W-13", W_CANCELLED, "100000.00", "0.00", "0.00", "0.00")
        self.assertWork("W-14", W_DONE, "100000.00", "15000.00", "85000.00", "0.00")
        self.assertWork("W-15", W_PLANNED, "100000.00", "0.00", "0.00", "100000.00")
        self.assertWork("W-16", W_PLANNED, "100000.00", "0.00", "0.00", "100000.00")

    def test_d1_totals(self):
        c = by_contract(self.r, "D-1")
        self.assertEqual(c.active_price, D("500000.00"))
        self.assertEqual(c.total_paid, D("315000.00"))       # P-1 + P-3; P-2 возвращён, P-4 в очереди
        self.assertEqual(c.unapplied_advance, D("15000.00"))
        self.assertEqual(c.overpayment, ZERO)
        self.assertEqual(c.remaining, D("185000.00"))
        self.assertEqual(c.queued_total, D("150000.00"))
        self.assertEqual(c.remaining_after_queue, D("35000.00"))

    def test_d2_works_and_totals(self):
        self.assertWork("W-21", W_DONE, "20000.00", "0.00", "20000.00", "0.00")
        self.assertWork("W-22", W_DONE, "30000.00", "0.00", "30000.00", "0.00")
        # принята 11.11 - на 10.11 ещё не принята, аванс в неё не зачитывается
        self.assertWork("W-23", W_PLANNED, "40000.00", "0.00", "0.00", "40000.00")
        c = by_contract(self.r, "D-2")
        self.assertEqual(c.total_paid, D("75000.00"))         # P-8 оплачен 12.11 - не учтён
        self.assertEqual(c.unapplied_advance, D("25000.00"))
        self.assertEqual(c.remaining, D("15000.00"))
        self.assertEqual(c.overpayment, ZERO)

    def test_payment_states(self):
        self.assertEqual(by_payment(self.r, "P-2").state, P_REFUNDED)
        self.assertEqual(by_payment(self.r, "P-2").counted, ZERO)
        self.assertEqual(by_payment(self.r, "P-4").state, P_QUEUED)
        self.assertEqual(by_payment(self.r, "P-4").counted, ZERO)
        self.assertEqual(by_payment(self.r, "P-8").state, P_NOT_YET)

    def test_advance_fifo_by_acceptance_date(self):
        p1 = by_payment(self.r, "P-1")
        self.assertEqual([(a.target, a.amount) for a in p1.allocations],
                         [("W-11", D("100000.00")), ("W-12", D("100000.00")),
                          ("W-14", D("85000.00")), (TO_ADVANCE, D("15000.00"))])

    def test_contract_amount_discrepancy_is_reported(self):
        texts = [i.message for i in self.r.issues if i.contract_id == "D-1"]
        self.assertTrue(any("отменены W-13" in t for t in texts), texts)


class ReportOnOtherDates(RealDataTestCase):

    def test_before_refund_p2_counts_as_paid(self):
        r = self.report("2026-09-05")
        c = by_contract(r, "D-1")
        self.assertEqual(by_payment(r, "P-2").state, P_PAID)
        self.assertEqual(c.total_paid, D("350000.00"))
        self.assertEqual(c.unapplied_advance, D("350000.00"))  # ни одна работа ещё не принята
        self.assertEqual(c.active_price, D("600000.00"))       # W-13 отменят только 12.10

    def test_on_refund_date_p2_is_excluded(self):
        r = self.report("2026-09-10")
        self.assertEqual(by_payment(r, "P-2").state, P_REFUNDED)
        self.assertEqual(by_contract(r, "D-1").total_paid, D("300000.00"))

    def test_contract_not_signed_yet_is_skipped(self):
        r = self.report("2026-09-10")
        self.assertEqual([c.contract.contract_id for c in r.contracts], ["D-1"])

    def test_bound_payment_before_acceptance(self):
        r = self.report("2026-10-27")  # P-3 оплачен 25.10, W-14 принята 28.10
        w14 = by_work(r, "W-14")
        self.assertEqual(w14.state, W_PLANNED)
        self.assertEqual(w14.direct_paid, D("15000.00"))
        self.assertEqual(w14.advance_credited, ZERO)
        self.assertEqual(by_contract(r, "D-1").unapplied_advance, D("100000.00"))

    def test_cancelled_work_active_before_cancel_date(self):
        self.assertEqual(by_work(self.report("2026-10-11"), "W-13").state, W_PLANNED)
        self.assertEqual(by_work(self.report("2026-10-12"), "W-13").state, W_CANCELLED)

    def test_queue_not_counted_before_creation(self):
        self.assertEqual(by_contract(self.report("2026-10-31"), "D-1").queued_total, ZERO)
        self.assertEqual(by_contract(self.report("2026-11-01"), "D-1").queued_total, D("150000.00"))

    def test_acceptance_after_report_date(self):
        r = self.report("2026-11-11")
        w23 = by_work(r, "W-23")
        self.assertEqual(w23.state, W_DONE)
        self.assertEqual(w23.paid, D("25000.00"))
        self.assertEqual(w23.remaining, D("15000.00"))
        self.assertEqual(by_contract(r, "D-2").unapplied_advance, ZERO)

    def test_overpayment_after_p8(self):
        r = self.report("2026-11-12")
        c = by_contract(r, "D-2")
        self.assertEqual(c.total_paid, D("95000.00"))
        self.assertEqual(c.overpayment, D("5000.00"))
        self.assertEqual(c.remaining, ZERO)
        p8 = by_payment(r, "P-8")
        self.assertIn((TO_OVERPAY, D("5000.00")), [(a.target, a.amount) for a in p8.allocations])
        self.assertTrue(any("Переплата 5 000,00" in i.message for i in r.issues))


class NoDoubleCountingEveryDay(RealDataTestCase):
    """На каждую дату с августа по декабрь: каждый рубль распределён ровно один раз."""

    def test_invariants(self):
        for on in date_range("2026-08-20", "2026-12-31"):
            r = build_report(self.data, on)
            for pl in r.payments:
                self.assertEqual(allocated(pl), pl.counted, (on, pl.payment.payment_id))
                if pl.state != P_PAID:
                    self.assertEqual(pl.counted, ZERO, (on, pl.payment.payment_id))
            for c in r.contracts:
                parts = c.direct_paid + c.advance_credited + c.unapplied_advance + c.overpayment
                self.assertEqual(parts, c.total_paid, (on, c.contract.contract_id))
                counted = sum((p.counted for p in r.payments
                               if p.payment.contract_id == c.contract.contract_id), ZERO)
                self.assertEqual(counted, c.total_paid, (on, c.contract.contract_id))
                self.assertEqual(c.remaining - c.overpayment, c.active_price - c.total_paid)
                for w in c.works:
                    self.assertGreaterEqual(w.remaining, ZERO)
                    self.assertLessEqual(w.paid, w.work.price)
                    # аванс зачитывается только в принятые работы
                    if w.state != W_DONE:
                        self.assertEqual(w.advance_credited, ZERO, (on, w.work.work_id))


if __name__ == "__main__":
    unittest.main()
