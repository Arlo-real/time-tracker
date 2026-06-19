import csv
import os
from datetime import date, timedelta
import db

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _planned_hours_on(schedule_rows: list, date_str: str, dow: int) -> float:
    best_ef, best_hours = None, 0.0
    for row in schedule_rows:
        if row["day_of_week"] == dow and row["effective_from"] <= date_str:
            if best_ef is None or row["effective_from"] > best_ef:
                best_ef = row["effective_from"]
                best_hours = row["planned_hours"]
    return best_hours


def _fmt_hm(seconds: int) -> str:
    negative = seconds < 0
    seconds = abs(seconds)
    return f"{'-' if negative else ''}{seconds // 3600}:{(seconds % 3600) // 60:02d}"


def _fmt_time(ts: str | None) -> str:
    return ts[11:16] if ts else ""


def export_employee_month(employee_id: int, year: int, month: int, out_dir: str = ".") -> str:
    emps = {e["id"]: e["name"] for e in db.list_employees(include_inactive=True)}
    if employee_id not in emps:
        raise ValueError(f"Employee {employee_id} not found")
    emp_name = emps[employee_id]

    schedule_rows = db.get_all_schedule_rows(employee_id)
    absences = db.get_absences(employee_id, year, month)
    special = db.get_all_special_days_in_month(year, month)

    first = date(year, month, 1)
    last = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year + 1, 1, 1) - timedelta(days=1)

    data_rows = []
    total_planned_s = total_worked_s = total_work_days = total_absent_days = 0

    d = first
    while d <= last:
        date_str = d.strftime("%Y-%m-%d")
        dow = d.weekday()

        planned_s = int(_planned_hours_on(schedule_rows, date_str, dow) * 3600)
        is_special = date_str in special
        if is_special:
            planned_s = 0  # holidays / work-closed days have no planned hours
        absence_info = absences.get(date_str)
        sessions = db.get_sessions(employee_id, date_str)
        worked_s = sum(s["duration_s"] or 0 for s in sessions)

        # Presence / absence classification
        if is_special:
            presence, reason = "A", special[date_str]
        elif dow >= 5 and planned_s == 0:
            presence, reason = "A", "weekend"
        elif absence_info:
            presence = "A"
            base = absence_info["reason"]
            adder = absence_info["added_by_name"]
            reason = f"{base} ({adder})" if adder else base
        elif sessions:
            presence, reason = "P", ""
        elif planned_s > 0:
            presence, reason = "A", "unexplained"
        else:
            presence, reason = "A", ""

        first_in = _fmt_time(sessions[0]["arrived_at"]) if sessions else ""
        last_out = _fmt_time(sessions[-1]["left_at"]) if sessions else ""
        worked_fmt = _fmt_hm(worked_s) if worked_s else ""
        planned_fmt = _fmt_hm(planned_s)

        break_cells = []
        for i in range(len(sessions) - 1):
            bs = _fmt_time(sessions[i]["left_at"])
            be = _fmt_time(sessions[i + 1]["arrived_at"])
            if bs and be:
                break_cells += [bs, be]

        day_label = f"{DAYS[dow]} {d.day:02d}.{month:02d}.{year}"
        data_rows.append(
            [day_label, presence, first_in, last_out, reason, worked_fmt, planned_fmt] + break_cells
        )

        total_worked_s += worked_s
        if not is_special and planned_s > 0:
            total_work_days += 1
            total_planned_s += planned_s
            if not sessions:
                total_absent_days += 1

        d += timedelta(days=1)

    os.makedirs(out_dir, exist_ok=True)
    fname = os.path.join(out_dir, f"{emp_name}_{year}_{month:02d}.csv")

    with open(fname, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Day", "P/A", "Start", "End", "Reason / absence adder", "Worked", "Planned",
                    "Breaks (start, end, ...)"])
        w.writerows(data_rows)
        w.writerow([])
        w.writerow(["", "TOTALS"])
        w.writerow(["", "Work days",     total_work_days])
        w.writerow(["", "Absent days",   total_absent_days])
        w.writerow(["", "Planned hours", _fmt_hm(total_planned_s)])
        w.writerow(["", "Worked hours",  _fmt_hm(total_worked_s)])
        w.writerow(["", "Balance",       _fmt_hm(total_worked_s - total_planned_s)])

    return fname
