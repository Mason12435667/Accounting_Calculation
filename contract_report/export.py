"""Выгрузка отчёта в CSV (для Excel) и краткая сводка в консоль."""
from __future__ import annotations

import csv
import os
from pathlib import Path

from .engine import (P_NOT_YET, P_QUEUED, P_REFUNDED, TO_ADVANCE, TO_OVERPAY, ZERO,
                     ContractResult, PaymentLine, Report, WorkLine)
from .formatting import fmt_date, fmt_money, fmt_money_human as hm

# excel - для Excel с русскими настройками: разделитель ";", десятичная запятая, UTF-8 с BOM.
# standard - обычный CSV: разделитель ",", десятичная точка, UTF-8.
CSV_STYLES = {
    "excel": {"delimiter": ";", "decimal": ",", "encoding": "utf-8-sig"},
    "standard": {"delimiter": ",", "decimal": ".", "encoding": "utf-8"},
}

MAIN_HEADER = [
    "Договор", "Подрядчик", "Работа", "Наименование", "Статус на дату отчёта", "Дата приёмки / отмены",
    "Цена", "Оплачено привязанными платежами", "Зачтено из общего аванса", "Выплачено всего",
    "Осталось оплатить", "В очереди на оплату", "Комментарий",
]

PAYMENTS_HEADER = [
    "Платёж", "Договор", "Привязка к работе", "Назначение", "Сумма", "Статус в выгрузке",
    "Статус на дату отчёта", "Заведён", "Оплачен", "Возвращён", "Учтено как выплачено",
    "Как распределён", "Комментарий",
]

_TARGET_LABELS = {TO_ADVANCE: "аванс, не зачтён в работы", TO_OVERPAY: "переплата"}


def _sources_text(w: WorkLine) -> str:
    parts = [f"{pid} {hm(amount)}" + ("" if direct else " (из аванса)") for pid, amount, direct in w.sources]
    return ("Оплачено: " + "; ".join(parts)) if parts else ""


def _work_row(cr: ContractResult, w: WorkLine, m) -> list[str]:
    comments = []
    if not w.is_active:
        comments.append("Отменена: в итоги не входит, оплате не подлежит")
    elif w.state == "done" and w.remaining > ZERO:
        comments.append("Принята, но оплачена не полностью")
    src = _sources_text(w)
    if src:
        comments.append(src)
    return [cr.contract.contract_id, cr.contract.contractor, w.work.work_id, w.work.title, w.state_label,
            fmt_date(w.state_date), m(w.work.price), m(w.direct_paid), m(w.advance_credited), m(w.paid),
            m(w.remaining), m(w.queued), ". ".join(comments)]


def _payments_of(cr: ContractResult, target: str) -> str:
    parts = [f"{pl.payment.payment_id} {hm(a.amount)}"
             for pl in cr.payments for a in pl.allocations if a.target == target]
    return "; ".join(parts)


def _contract_rows(cr: ContractResult, m) -> list[list[str]]:
    c = cr.contract
    head = [c.contract_id, c.contractor]
    rows = [_work_row(cr, w, m) for w in cr.works]

    if cr.unapplied_advance > ZERO:
        pending = ", ".join(w.work.work_id for w in cr.pending_works)
        rows.append(head + ["", "Аванс по договору, ещё не зачтённый в работы", "", "", "", "", "",
                            m(cr.unapplied_advance), m(-cr.unapplied_advance), "",
                            f"Из платежей: {_payments_of(cr, TO_ADVANCE)}. Будет зачтён при приёмке "
                            f"работ ({pending}), поэтому уменьшает остаток к оплате"])
    if cr.overpayment > ZERO:
        rows.append(head + ["", "ПЕРЕПЛАТА по договору", "", "", "", "", "", m(cr.overpayment), "", "",
                            f"Выплачено больше суммы действующих работ. По порядку зачёта излишек: "
                            f"{_payments_of(cr, TO_OVERPAY)}. Нужно решение: возврат или зачёт"])
    if cr.queued_unbound > ZERO:
        queued = "; ".join(f"{pl.payment.payment_id} {hm(pl.payment.amount)}" for pl in cr.payments
                           if pl.state == P_QUEUED and not pl.payment.work_id)
        rows.append(head + ["", "Платежи в очереди без привязки к работе", "", "", "", "", "", "", "",
                            m(cr.queued_unbound),
                            f"{queued}. Ещё не оплачены - в «Выплачено» не входят"])

    total_comment = [f"Сумма договора {hm(c.amount)}", f"сумма действующих работ {hm(cr.active_price)}"]
    if cr.overpayment > ZERO:
        total_comment.append(f"ПЕРЕПЛАТА {hm(cr.overpayment)}")
    if cr.queued_total > ZERO:
        after = cr.remaining_after_queue
        total_comment.append(f"после оплаты очереди останется {hm(after)}" if after >= ZERO
                             else f"после оплаты очереди будет ПЕРЕПЛАТА {hm(-after)}")
    rows.append(head + ["ИТОГО", "Итого по договору (действующие работы)", "", "", m(cr.active_price),
                        m(cr.direct_paid), m(cr.advance_credited), m(cr.total_paid), m(cr.remaining),
                        m(cr.queued_total), "; ".join(total_comment)])
    return rows


def main_report_rows(report: Report, decimal: str = ",") -> list[list[str]]:
    m = lambda value: fmt_money(value, decimal)  # noqa: E731
    rows = [[f"Финансовый отчёт по договорам на {fmt_date(report.report_date)}"],
            ["Суммы в рублях. Учтены события по дату отчёта включительно."],
            [], MAIN_HEADER]
    for cr in report.contracts:
        rows.extend(_contract_rows(cr, m))
        rows.append([])
    rows.append(["ЗАМЕЧАНИЯ"])
    if report.issues:
        rows.append(["Уровень", "Договор", "Объект", "Описание"])
        rows.extend([i.level, i.contract_id, i.object_id, i.message] for i in report.issues)
    else:
        rows.append(["Замечаний нет"])
    return rows


def _distribution_text(pl: PaymentLine) -> str:
    if not pl.valid_binding:
        return pl.note
    if pl.state == P_QUEUED:
        return "не оплачен - не распределялся"
    if pl.state == P_REFUNDED:
        return "возвращён - не учитывается"
    if pl.state == P_NOT_YET:
        return "после даты отчёта - не учитывается"
    parts = []
    for a in pl.allocations:
        label = _TARGET_LABELS.get(a.target, a.target)
        if a.target not in _TARGET_LABELS and not a.direct:
            label += " (из аванса)"
        parts.append(f"{label}: {hm(a.amount)}")
    return "; ".join(parts)


def payments_rows(report: Report, decimal: str = ",") -> list[list[str]]:
    rows = [[f"Платежи и их распределение на {fmt_date(report.report_date)}"], [], PAYMENTS_HEADER]
    status_names = {"paid": "оплачен", "queued": "в очереди", "refunded": "возвращён"}
    for pl in report.payments:
        p = pl.payment
        rows.append([p.payment_id, p.contract_id, p.work_id or "без привязки", p.purpose,
                     fmt_money(p.amount, decimal), status_names.get(p.status, p.status), pl.state_label,
                     fmt_date(p.created_at), fmt_date(p.paid_at), fmt_date(p.refunded_at),
                     fmt_money(pl.counted, decimal), _distribution_text(pl), pl.note])
    return rows


def _write_csv(path: Path, rows: list[list[str]], style: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding=style["encoding"], newline="") as fh:
        csv.writer(fh, delimiter=style["delimiter"]).writerows(rows)
    try:
        os.replace(tmp, path)
    except PermissionError:
        tmp.unlink(missing_ok=True)
        raise PermissionError(f"Не удалось записать {path}: файл, вероятно, открыт в Excel. "
                              f"Закройте его и запустите программу ещё раз") from None


def write_report(report: Report, out_dir: Path, csv_style: str = "excel") -> list[Path]:
    style = CSV_STYLES[csv_style]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.report_date.isoformat()
    main_path = out_dir / f"report_{stamp}.csv"
    payments_path = out_dir / f"payments_{stamp}.csv"
    _write_csv(main_path, main_report_rows(report, style["decimal"]), style)
    _write_csv(payments_path, payments_rows(report, style["decimal"]), style)
    return [main_path, payments_path]


def summary_text(report: Report) -> str:
    lines = [f"Отчёт на {fmt_date(report.report_date)}", ""]
    for cr in report.contracts:
        c = cr.contract
        lines.append(f"{c.contract_id} - {c.contractor} (договор {hm(c.amount)})")
        for w in cr.works:
            lines.append(f"  {w.work.work_id:<6} {w.work.title[:32]:<32} {w.state_label[:28]:<28} "
                         f"цена {hm(w.work.price):>12}  выплачено {hm(w.paid):>12}  осталось {hm(w.remaining):>12}")
        if cr.unapplied_advance > ZERO:
            lines.append(f"  Аванс, не зачтённый в работы: {hm(cr.unapplied_advance)}")
        if cr.overpayment > ZERO:
            lines.append(f"  ПЕРЕПЛАТА: {hm(cr.overpayment)}")
        lines.append(f"  ИТОГО: работы {hm(cr.active_price)}, выплачено {hm(cr.total_paid)}, "
                     f"осталось {hm(cr.remaining)}, в очереди {hm(cr.queued_total)}")
        lines.append("")
    if report.issues:
        lines.append("Замечания:")
        lines.extend(f"  [{i.level}] {i.object_id or '-'}: {i.message}" for i in report.issues)
    return "\n".join(lines)

