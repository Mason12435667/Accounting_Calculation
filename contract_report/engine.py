"""Расчёт отчёта на дату: статусы на дату и распределение платежей по работам.

Алгоритм (подробно и с обоснованием - в DECISIONS.md):

1. Статусы работ и платежей восстанавливаются на дату отчёта по датам событий
   (события, датированные днём отчёта, включаются).
2. Учитываются только деньги, которые на дату отчёта ушли подрядчику и не
   вернулись: paid с paid_at <= даты; refunded - только до даты возврата.
   Платежи в очереди показываются отдельно и в "выплачено" не входят.
3. Платёж, привязанный к работе, засчитывается этой работе (в пределах её цены).
   Излишек сверх цены и платёж по отменённой работе уходят в общий аванс договора.
4. Общий аванс зачитывается только в работы, принятые на дату отчёта,
   в порядке даты приёмки; деньги берутся из платежей по порядку даты оплаты (FIFO).
5. Остаток аванса - "аванс, не зачтённый в работы" (в пределах неоплаченной
   цены ещё не принятых работ); всё, что сверх суммы действующих работ - переплата.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .formatting import fmt_date, fmt_money_human as hm
from .models import Contract, Dataset, Payment, Work

ZERO = Decimal("0.00")

# Состояние работы на дату отчёта
W_DONE = "done"          # выполнена и принята
W_PLANNED = "planned"    # действует, но не принята
W_CANCELLED = "cancelled"

# Состояние платежа на дату отчёта
P_PAID = "paid"          # деньги у подрядчика
P_QUEUED = "queued"      # заведён, но не оплачен
P_REFUNDED = "refunded"  # оплачен и уже возвращён
P_NOT_YET = "not_yet"    # на дату отчёта ещё не существовал

# Специальные "получатели" распределения
TO_ADVANCE = "__advance__"   # аванс, ещё не зачтённый в работы
TO_OVERPAY = "__overpay__"   # переплата

ERROR, WARNING, INFO = "ОШИБКА", "ВНИМАНИЕ", "ИНФО"
_LEVEL_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}


def natural_key(text: str):
    """W-2 < W-10: сортировка номеров с учётом чисел."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


@dataclass
class Issue:
    level: str
    contract_id: str
    object_id: str
    message: str


@dataclass
class Allocation:
    target: str       # work_id, TO_ADVANCE или TO_OVERPAY
    amount: Decimal
    direct: bool      # True - по привязке платежа к работе, False - из общего аванса


@dataclass
class WorkLine:
    work: Work
    state: str
    state_label: str
    state_date: date | None
    direct_paid: Decimal = ZERO       # по платежам, привязанным к этой работе
    advance_credited: Decimal = ZERO  # зачтено из общего аванса договора
    queued: Decimal = ZERO            # привязанные платежи в очереди
    sources: list[tuple[str, Decimal, bool]] = field(default_factory=list)  # (платёж, сумма, напрямую)

    @property
    def is_active(self) -> bool:
        return self.state != W_CANCELLED

    @property
    def paid(self) -> Decimal:
        return self.direct_paid + self.advance_credited

    @property
    def remaining(self) -> Decimal:
        return self.work.price - self.paid if self.is_active else ZERO


@dataclass
class PaymentLine:
    payment: Payment
    state: str
    state_label: str
    counted: Decimal = ZERO   # сколько учтено как выплаченное на дату отчёта
    valid_binding: bool = True
    allocations: list[Allocation] = field(default_factory=list)
    note: str = ""


@dataclass
class ContractResult:
    contract: Contract
    works: list[WorkLine]
    payments: list[PaymentLine]
    unapplied_advance: Decimal = ZERO
    overpayment: Decimal = ZERO
    queued_unbound: Decimal = ZERO

    @property
    def active_price(self) -> Decimal:
        return sum((w.work.price for w in self.works if w.is_active), ZERO)

    @property
    def all_works_price(self) -> Decimal:
        return sum((w.work.price for w in self.works), ZERO)

    @property
    def total_paid(self) -> Decimal:
        return sum((p.counted for p in self.payments), ZERO)

    @property
    def direct_paid(self) -> Decimal:
        return sum((w.direct_paid for w in self.works), ZERO)

    @property
    def advance_credited(self) -> Decimal:
        return sum((w.advance_credited for w in self.works), ZERO)

    @property
    def remaining(self) -> Decimal:
        return max(ZERO, self.active_price - self.total_paid)

    @property
    def queued_total(self) -> Decimal:
        return self.queued_unbound + sum((w.queued for w in self.works), ZERO)

    @property
    def remaining_after_queue(self) -> Decimal:
        """Отрицательное значение - переплата, если оплатить очередь."""
        return self.active_price - self.total_paid - self.queued_total

    @property
    def pending_works(self) -> list[WorkLine]:
        return [w for w in self.works if w.state == W_PLANNED and w.remaining > 0]


@dataclass
class Report:
    report_date: date
    contracts: list[ContractResult]
    payments: list[PaymentLine]
    issues: list[Issue]


# --------------------------------------------------------------------------- статусы на дату

def _work_line(w: Work, on: date, issues: list[Issue]) -> WorkLine:
    if w.status == "done":
        if w.completed_at is None:
            issues.append(Issue(ERROR, w.contract_id, w.work_id,
                                "Статус «выполнена», но нет даты приёмки - считаю работу не принятой"))
            return WorkLine(w, W_PLANNED, "не принята (нет даты приёмки)", None)
        if w.completed_at <= on:
            return WorkLine(w, W_DONE, "выполнена и принята", w.completed_at)
        return WorkLine(w, W_PLANNED, f"не принята на дату (принята позже, {fmt_date(w.completed_at)})", None)
    if w.status == "cancelled":
        if w.cancelled_at is None:
            issues.append(Issue(ERROR, w.contract_id, w.work_id,
                                "Статус «отменена», но нет даты отмены - считаю отменённой на любую дату"))
            return WorkLine(w, W_CANCELLED, "отменена (дата не указана)", None)
        if w.cancelled_at <= on:
            return WorkLine(w, W_CANCELLED, "отменена", w.cancelled_at)
        return WorkLine(w, W_PLANNED, f"действует на дату (отменена позже, {fmt_date(w.cancelled_at)})", None)
    return WorkLine(w, W_PLANNED, "запланирована, не принята", None)


def _payment_line(p: Payment, on: date, issues: list[Issue]) -> PaymentLine:
    appeared = p.created_at or p.paid_at
    exists = appeared is None or appeared <= on
    later = f"заведён {fmt_date(p.created_at)}, после даты отчёта" if p.created_at else "после даты отчёта"

    if p.status == "queued":
        if p.paid_at:
            issues.append(Issue(WARNING, p.contract_id, p.payment_id,
                                "Статус «в очереди», но указана дата оплаты - считаю неоплаченным"))
        if exists:
            return PaymentLine(p, P_QUEUED, "в очереди на оплату")
        return PaymentLine(p, P_NOT_YET, f"не учтён: {later}")

    # paid / refunded
    if p.paid_at is None:
        issues.append(Issue(ERROR, p.contract_id, p.payment_id,
                            f"Статус «{p.status}», но нет даты оплаты - в выплаченное не включён"))
        return PaymentLine(p, P_QUEUED if exists else P_NOT_YET, "нет даты оплаты")

    if p.paid_at > on:
        issues.append(Issue(INFO, p.contract_id, p.payment_id,
                            f"Оплачен {fmt_date(p.paid_at)} ({hm(p.amount)}) - после даты отчёта, не учтён"))
        if exists:
            return PaymentLine(p, P_QUEUED, f"в очереди на дату (оплачен позже, {fmt_date(p.paid_at)})")
        return PaymentLine(p, P_NOT_YET, f"не учтён: {later}")

    if p.status == "refunded":
        if p.refunded_at is None:
            issues.append(Issue(ERROR, p.contract_id, p.payment_id,
                                "Статус «возвращён», но нет даты возврата - считаю возвращённым сразу"))
            return PaymentLine(p, P_REFUNDED, "возвращён (дата не указана)")
        if p.refunded_at < p.paid_at:
            issues.append(Issue(WARNING, p.contract_id, p.payment_id, "Дата возврата раньше даты оплаты"))
        if p.refunded_at <= on:
            return PaymentLine(p, P_REFUNDED, f"возвращён {fmt_date(p.refunded_at)}")
        issues.append(Issue(INFO, p.contract_id, p.payment_id,
                            f"Возвращён подрядчиком {fmt_date(p.refunded_at)} - после даты отчёта, "
                            f"на дату отчёта учтён как выплаченный"))
        return PaymentLine(p, P_PAID, f"оплачен {fmt_date(p.paid_at)} (вернётся {fmt_date(p.refunded_at)})",
                           counted=p.amount)

    return PaymentLine(p, P_PAID, f"оплачен {fmt_date(p.paid_at)}", counted=p.amount)


# --------------------------------------------------------------------------- распределение

def _pay_order(pl: PaymentLine):
    return (pl.payment.paid_at or date.max, natural_key(pl.payment.payment_id))


def _settle_contract(c: Contract, works: list[WorkLine], pays: list[PaymentLine],
                     on: date, issues: list[Issue]) -> ContractResult:
    res = ContractResult(c, works, pays)
    by_id = {w.work.work_id: w for w in works}
    pool: deque[list] = deque()  # [PaymentLine, ещё не распределённая сумма]

    # 1) привязанные платежи и очередь
    for pl in sorted(pays, key=_pay_order):
        p = pl.payment
        w = by_id.get(p.work_id) if p.work_id else None
        if pl.state == P_QUEUED:
            if w is None:
                res.queued_unbound += p.amount
            else:
                w.queued += p.amount
                if not w.is_active:
                    issues.append(Issue(WARNING, c.contract_id, p.payment_id,
                                        f"Платёж в очереди ({hm(p.amount)}) привязан к отменённой работе "
                                        f"{w.work.work_id} - проверьте, нужно ли его оплачивать"))
            continue
        if pl.counted == ZERO:
            continue
        if w is None:
            pool.append([pl, pl.counted])
            continue
        if not w.is_active:
            issues.append(Issue(WARNING, c.contract_id, p.payment_id,
                                f"Платёж {hm(pl.counted)} привязан к отменённой работе {w.work.work_id}: "
                                f"учтён как общий аванс по договору"))
            pool.append([pl, pl.counted])
            continue
        take = min(w.work.price - w.direct_paid, pl.counted)
        if take > ZERO:
            w.direct_paid += take
            w.sources.append((p.payment_id, take, True))
            pl.allocations.append(Allocation(w.work.work_id, take, True))
        excess = pl.counted - take
        if excess > ZERO:
            issues.append(Issue(WARNING, c.contract_id, p.payment_id,
                                f"Привязанные к работе {w.work.work_id} платежи превышают её цену; излишек "
                                f"{hm(excess)} учтён как общий аванс по договору"))
            pool.append([pl, excess])

    # 2) общий аванс -> принятые работы, по дате приёмки, деньги по дате оплаты (FIFO)
    accepted = sorted((w for w in works if w.state == W_DONE),
                      key=lambda w: (w.state_date, natural_key(w.work.work_id)))
    for w in accepted:
        need = w.work.price - w.direct_paid
        while need > ZERO and pool:
            item = pool[0]
            take = min(need, item[1])
            w.advance_credited += take
            w.sources.append((item[0].payment.payment_id, take, False))
            item[0].allocations.append(Allocation(w.work.work_id, take, False))
            item[1] -= take
            need -= take
            if item[1] == ZERO:
                pool.popleft()

    # 3) остаток: аванс под непринятые работы, сверх этого - переплата
    capacity = sum((w.remaining for w in works if w.state == W_PLANNED), ZERO)
    overpay_sources = []
    for pl, left in pool:
        to_advance = min(left, capacity)
        if to_advance > ZERO:
            capacity -= to_advance
            res.unapplied_advance += to_advance
            pl.allocations.append(Allocation(TO_ADVANCE, to_advance, False))
        over = left - to_advance
        if over > ZERO:
            res.overpayment += over
            pl.allocations.append(Allocation(TO_OVERPAY, over, False))
            overpay_sources.append(f"{pl.payment.payment_id} ({hm(over)})")

    _contract_checks(res, overpay_sources, issues)
    _check_integrity(res)
    return res


def _contract_checks(res: ContractResult, overpay_sources: list[str], issues: list[Issue]) -> None:
    c = res.contract
    cid = c.contract_id
    if res.all_works_price != c.amount:
        issues.append(Issue(WARNING, cid, cid,
                            f"Сумма всех работ {hm(res.all_works_price)} не совпадает с суммой договора "
                            f"{hm(c.amount)} (разница {hm(c.amount - res.all_works_price)})"))
    cancelled = [w.work.work_id for w in res.works if not w.is_active]
    if cancelled and res.active_price != c.amount:
        issues.append(Issue(WARNING, cid, cid,
                            f"Сумма договора {hm(c.amount)}, а действующих работ - {hm(res.active_price)}: "
                            f"отменены {', '.join(cancelled)}. «Осталось оплатить» считается от суммы "
                            f"действующих работ. Нужно подтвердить, уменьшена ли сумма договора"))
    if res.overpayment > ZERO:
        issues.append(Issue(WARNING, cid, cid,
                            f"Переплата {hm(res.overpayment)}: выплачено {hm(res.total_paid)} при сумме "
                            f"действующих работ {hm(res.active_price)}. По порядку зачёта излишек приходится "
                            f"на {', '.join(overpay_sources)}. Нужно решить: возврат или зачёт"))
    if res.queued_total > ZERO and res.remaining_after_queue < ZERO:
        issues.append(Issue(WARNING, cid, cid,
                            f"Если оплатить платежи из очереди ({hm(res.queued_total)}), возникнет переплата "
                            f"{hm(-res.remaining_after_queue)}"))
    owed_for_accepted = sum((w.remaining for w in res.works if w.state == W_DONE), ZERO)
    prepay_part = min(res.queued_total, res.remaining) - owed_for_accepted
    if res.queued_total > ZERO and prepay_part > ZERO:
        pending = ", ".join(w.work.work_id for w in res.pending_works)
        issues.append(Issue(INFO, cid, cid,
                            f"В очереди {hm(res.queued_total)}, а долг по уже принятым работам - "
                            f"{hm(owed_for_accepted)}: {hm(prepay_part)} из очереди будет предоплатой за ещё "
                            f"не принятые работы ({pending}). Проверьте, что так и задумано"))
    if res.total_paid > c.amount and res.overpayment == ZERO:
        issues.append(Issue(WARNING, cid, cid,
                            f"Выплачено {hm(res.total_paid)} - больше суммы договора {hm(c.amount)}"))
    elif res.queued_total > ZERO and res.total_paid + res.queued_total > c.amount:
        issues.append(Issue(WARNING, cid, cid,
                            f"Выплачено и стоит в очереди {hm(res.total_paid + res.queued_total)} - больше "
                            f"суммы договора {hm(c.amount)}"))
    if c.signed_at:
        for pl in res.payments:
            if pl.counted > ZERO and pl.payment.paid_at < c.signed_at:
                issues.append(Issue(WARNING, cid, pl.payment.payment_id,
                                    f"Оплачен {fmt_date(pl.payment.paid_at)} - раньше подписания договора "
                                    f"{fmt_date(c.signed_at)}"))


def _check_integrity(res: ContractResult) -> None:
    """Защита от двойного учёта: каждый рубль платежа распределён ровно один раз."""
    for pl in res.payments:
        allocated = sum((a.amount for a in pl.allocations), ZERO)
        if allocated != pl.counted:
            raise RuntimeError(f"{pl.payment.payment_id}: распределено {allocated}, учтено {pl.counted}")
    for w in res.works:
        if w.paid > w.work.price or (not w.is_active and w.paid != ZERO):
            raise RuntimeError(f"{w.work.work_id}: выплачено {w.paid} при цене {w.work.price}")
    paid_parts = res.direct_paid + res.advance_credited + res.unapplied_advance + res.overpayment
    if paid_parts != res.total_paid:
        raise RuntimeError(f"{res.contract.contract_id}: части {paid_parts} != выплачено {res.total_paid}")


# --------------------------------------------------------------------------- отчёт

def build_report(data: Dataset, on: date) -> Report:
    issues: list[Issue] = []
    contracts = {c.contract_id: c for c in data.contracts}
    works_by_id = {w.work_id: w for w in data.works}

    for w in data.works:
        if w.contract_id not in contracts:
            issues.append(Issue(ERROR, w.contract_id, w.work_id,
                                f"Работа ссылается на неизвестный договор {w.contract_id} - в отчёт не вошла"))
    work_lines = {w.work_id: _work_line(w, on, issues) for w in data.works}
    pay_lines = [_payment_line(p, on, issues) for p in data.payments]

    for pl in pay_lines:
        p = pl.payment
        reason = None
        if p.contract_id not in contracts:
            reason = f"договор {p.contract_id} не найден"
        elif p.work_id and p.work_id not in works_by_id:
            reason = f"работа {p.work_id} не найдена"
        elif p.work_id and works_by_id[p.work_id].contract_id != p.contract_id:
            reason = (f"работа {p.work_id} относится к договору {works_by_id[p.work_id].contract_id}, "
                      f"а платёж - к {p.contract_id}")
        if reason:
            pl.valid_binding = False
            pl.note = f"Не распределён: {reason}"
            if pl.state in (P_PAID, P_QUEUED):
                issues.append(Issue(ERROR, p.contract_id, p.payment_id,
                                    f"Платёж {hm(p.amount)} нельзя распределить: {reason}. "
                                    f"В итоги не включён - нужно уточнить привязку"))
            pl.counted = ZERO

    results = []
    for c in sorted(contracts.values(), key=lambda x: natural_key(x.contract_id)):
        c_works = sorted((work_lines[w.work_id] for w in data.works if w.contract_id == c.contract_id),
                         key=lambda wl: natural_key(wl.work.work_id))
        c_pays = [pl for pl in pay_lines if pl.payment.contract_id == c.contract_id and pl.valid_binding]
        if c.signed_at and c.signed_at > on and all(pl.state == P_NOT_YET for pl in c_pays):
            issues.append(Issue(INFO, c.contract_id, c.contract_id,
                                f"Договор подписан {fmt_date(c.signed_at)} - после даты отчёта, в отчёт не вошёл"))
            continue
        results.append(_settle_contract(c, c_works, c_pays, on, issues))

    latest = data.latest_date()
    if latest and on > latest:
        issues.append(Issue(INFO, "", "",
                            f"Дата отчёта позже последнего события в выгрузке ({fmt_date(latest)}): "
                            f"события после выгрузки в данных отсутствуют"))

    contract_rank = {cr.contract.contract_id: i for i, cr in enumerate(results)}
    issues.sort(key=lambda i: (contract_rank.get(i.contract_id, len(results)), _LEVEL_ORDER[i.level]))
    return Report(on, results, pay_lines, issues)
