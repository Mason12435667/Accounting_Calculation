"""Правила распределения на синтетических данных: случаи, которых нет в выгрузке."""
import unittest

from contract_report.engine import ERROR, TO_ADVANCE, TO_OVERPAY, ZERO, build_report

from .helpers import D, allocated, by_contract, by_payment, by_work, contract, d, dataset, payment, work


class AllocationRules(unittest.TestCase):

    def test_kopecks_are_exact(self):
        data = dataset([contract(amount="100.01")],
                       [work("W-1", "100.01", "done", done="2026-02-01")],
                       [payment("P-1", "33.33", paid="2026-01-10"),
                        payment("P-2", "33.33", paid="2026-01-11"),
                        payment("P-3", "33.34", paid="2026-01-12")])
        r = build_report(data, d("2026-03-01"))
        w = by_work(r, "W-1")
        self.assertEqual(w.paid, D("100.00"))
        self.assertEqual(w.remaining, D("0.01"))
        self.assertEqual(by_contract(r, "C-1").remaining, D("0.01"))

    def test_advance_goes_to_earliest_accepted_work_first(self):
        data = dataset([contract(amount="300.00")],
                       [work("W-1", "100.00", "done", done="2026-03-01"),
                        work("W-2", "100.00", "done", done="2026-02-01"),
                        work("W-3", "100.00")],
                       [payment("P-1", "150.00", paid="2026-01-10")])
        r = build_report(data, d("2026-04-01"))
        self.assertEqual(by_work(r, "W-2").paid, D("100.00"))   # принята раньше
        self.assertEqual(by_work(r, "W-1").paid, D("50.00"))
        self.assertEqual(by_work(r, "W-3").paid, ZERO)          # не принята - аванс не зачитывается
        self.assertEqual(by_contract(r, "C-1").remaining, D("150.00"))

    def test_bound_payment_excess_goes_to_pool(self):
        data = dataset([contract(amount="200.00")],
                       [work("W-1", "100.00", "done", done="2026-02-01"),
                        work("W-2", "100.00", "done", done="2026-02-02")],
                       [payment("P-1", "130.00", paid="2026-01-10", work_id="W-1")])
        r = build_report(data, d("2026-03-01"))
        self.assertEqual(by_work(r, "W-1").direct_paid, D("100.00"))
        self.assertEqual(by_work(r, "W-2").advance_credited, D("30.00"))
        self.assertEqual(allocated(by_payment(r, "P-1")), D("130.00"))
        self.assertTrue(any("превышают её цену" in i.message for i in r.issues))

    def test_payment_bound_to_cancelled_work_becomes_advance(self):
        data = dataset([contract(amount="200.00")],
                       [work("W-1", "100.00", "cancelled", cancelled="2026-02-01"),
                        work("W-2", "100.00", "done", done="2026-02-05")],
                       [payment("P-1", "40.00", paid="2026-01-10", work_id="W-1")])
        r = build_report(data, d("2026-03-01"))
        self.assertEqual(by_work(r, "W-1").paid, ZERO)
        self.assertEqual(by_work(r, "W-2").advance_credited, D("40.00"))
        self.assertTrue(any("отменённой работе" in i.message for i in r.issues))
        # до отмены платёж засчитан своей работе
        r_before = build_report(data, d("2026-01-31"))
        self.assertEqual(by_work(r_before, "W-1").direct_paid, D("40.00"))

    def test_overpayment_is_shown_not_hidden(self):
        data = dataset([contract(amount="100.00")],
                       [work("W-1", "100.00", "done", done="2026-02-01")],
                       [payment("P-1", "100.00", paid="2026-01-10"),
                        payment("P-2", "100.00", paid="2026-01-11")])   # дубль
        r = build_report(data, d("2026-03-01"))
        c = by_contract(r, "C-1")
        self.assertEqual(c.overpayment, D("100.00"))
        self.assertEqual(c.remaining, ZERO)
        self.assertEqual([(a.target, a.amount) for a in by_payment(r, "P-2").allocations],
                         [(TO_OVERPAY, D("100.00"))])

    def test_advance_within_pending_works_is_not_overpayment(self):
        data = dataset([contract(amount="200.00")],
                       [work("W-1", "100.00", "done", done="2026-02-01"), work("W-2", "100.00")],
                       [payment("P-1", "200.00", paid="2026-01-10")])
        c = by_contract(build_report(data, d("2026-03-01")), "C-1")
        self.assertEqual(c.unapplied_advance, D("100.00"))
        self.assertEqual(c.overpayment, ZERO)
        self.assertEqual(c.remaining, ZERO)

    def test_queue_that_would_overpay_is_flagged(self):
        data = dataset([contract(amount="100.00")],
                       [work("W-1", "100.00", "done", done="2026-02-01")],
                       [payment("P-1", "80.00", paid="2026-01-10"),
                        payment("P-2", "50.00", status="queued", created="2026-02-02")])
        r = build_report(data, d("2026-03-01"))
        c = by_contract(r, "C-1")
        self.assertEqual(c.total_paid, D("80.00"))
        self.assertEqual(c.remaining_after_queue, D("-30.00"))
        self.assertTrue(any("возникнет переплата 30,00" in i.message for i in r.issues))

    def test_payment_to_unknown_work_is_not_allocated(self):
        data = dataset([contract()],
                       [work("W-1", "100.00", "done", done="2026-02-01")],
                       [payment("P-1", "100.00", paid="2026-01-10", work_id="W-404")])
        r = build_report(data, d("2026-03-01"))
        self.assertFalse(by_payment(r, "P-1").valid_binding)
        self.assertEqual(by_contract(r, "C-1").total_paid, ZERO)
        self.assertTrue(any(i.level == ERROR and "W-404" in i.message for i in r.issues))

    def test_payment_to_work_of_other_contract_is_not_allocated(self):
        data = dataset([contract("C-1"), contract("C-2")],
                       [work("W-1", "100.00", "done", done="2026-02-01", cid="C-2")],
                       [payment("P-1", "100.00", paid="2026-01-10", work_id="W-1", cid="C-1")])
        r = build_report(data, d("2026-03-01"))
        self.assertEqual(by_contract(r, "C-1").total_paid, ZERO)
        self.assertEqual(by_contract(r, "C-2").total_paid, ZERO)
        self.assertTrue(any(i.level == ERROR for i in r.issues))

    def test_done_without_date_is_not_accepted(self):
        data = dataset([contract()], [work("W-1", "100.00", "done")],
                       [payment("P-1", "100.00", paid="2026-01-10")])
        r = build_report(data, d("2026-03-01"))
        self.assertEqual(by_work(r, "W-1").advance_credited, ZERO)
        self.assertEqual(by_contract(r, "C-1").unapplied_advance, D("100.00"))
        self.assertTrue(any(i.level == ERROR for i in r.issues))

    def test_paid_after_creation_is_queued_in_between(self):
        data = dataset([contract()], [work("W-1", "100.00")],
                       [payment("P-1", "100.00", created="2026-01-05", paid="2026-01-20")])
        r = build_report(data, d("2026-01-10"))
        c = by_contract(r, "C-1")
        self.assertEqual(c.total_paid, ZERO)
        self.assertEqual(c.queued_total, D("100.00"))

    def test_report_is_deterministic(self):
        data = dataset([contract()], [work("W-1", "100.00", "done", done="2026-02-01")],
                       [payment("P-1", "60.00", paid="2026-01-10")])
        r1 = build_report(data, d("2026-03-01"))
        r2 = build_report(data, d("2026-03-01"))
        self.assertEqual(r1, r2)
        self.assertEqual(by_contract(r1, "C-1").advance_credited, D("60.00"))
        self.assertNotIn(TO_ADVANCE, [a.target for a in by_payment(r1, "P-1").allocations])


if __name__ == "__main__":
    unittest.main()
