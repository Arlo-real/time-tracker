import os
from datetime import datetime
import db
import export as exp

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["", "January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]


# ── helpers ───────────────────────────────────────────────────────────────────

def pause():
    input("\nPress Enter to continue...")


def prompt(msg: str, allow_empty: bool = False) -> str:
    while True:
        val = input(msg).strip()
        if val or allow_empty:
            return val
        print("  Cannot be empty.")


def pick_employee(msg: str = "Select: ") -> dict | None:
    employees = db.list_employees()
    if not employees:
        print("  No active employees.")
        return None
    print()
    for i, e in enumerate(employees, 1):
        print(f"  {i}. {e['name']}")
    print("  0. Cancel")
    while True:
        sel = input(f"  {msg}").strip()
        if sel == "0":
            return None
        if sel.isdigit() and 1 <= int(sel) <= len(employees):
            return employees[int(sel) - 1]
        print("  Invalid selection.")


def parse_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def parse_dt(s: str, date_prefix: str = "") -> str | None:
    """Accept HH:MM (with date_prefix prepended) or full YYYY-MM-DD HH:MM[:SS]."""
    import re
    if re.fullmatch(r"\d{2}:\d{2}", s) and date_prefix:
        s = f"{date_prefix} {s}"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return None


def authenticate(label: str = "Identify yourself") -> dict | None:
    """Authenticate via PIN (if enabled) or chip serial. Returns {id, name, method} or None."""
    print(f"\n  {label}")
    if db.is_pin_login_enabled():
        val = prompt("  PIN: ")
        emp = db.find_employee_by_pin(val)
        if emp:
            return {**emp, "method": "pin"}
        emp = db.find_employee_by_chip(val)
        if emp:
            return {**emp, "method": "chip"}
    else:
        val = prompt("  Chip serial: ")
        emp = db.find_employee_by_chip(val)
        if emp:
            return {**emp, "method": "chip"}
    return None


def show_sessions(employee_id: int, date_s: str):
    sessions = db.get_sessions(employee_id, date_s)
    if sessions:
        for s in sessions:
            left = s["left_at"][11:16] if s["left_at"] else "open"
            print(f"    ID {s['id']}:  {s['arrived_at'][11:16]} – {left}  [{s['login_method']}]")
    else:
        print("    (no sessions)")
    return sessions


# ── non-protected flows ───────────────────────────────────────────────────────

def flow_checkinout():
    """PIN-based check in / check out for the normal console."""
    if not db.is_pin_login_enabled():
        print("\n  PIN login is currently disabled. Use your chip to clock in/out.")
        pause()
        return
    print("\n-- Check in / Check out --")
    pin = prompt("  PIN: ")
    result = db.handle_pin(pin)
    if result["code"] == 0:
        if result["inout"] == "in":
            print(f"  Welcome, {result['name']}!")
        else:
            h, m = divmod((result.get("worked_s") or 0) // 60, 60)
            print(f"  Goodbye, {result['name']} — worked {h}:{m:02d} this session.")
    elif result["code"] == 2:
        print("  PIN login is disabled.")
    else:
        print("  Unknown PIN.")
    pause()


def flow_lookup():
    """Quick attendance lookup for any employee on any day."""
    print("\n-- Attendance lookup --")
    emp = pick_employee("Employee: ")
    if not emp:
        return
    date_s = prompt("  Date (YYYY-MM-DD): ")
    if not parse_date(date_s):
        print("  Invalid date.")
        pause()
        return
    print(f"\n  {emp['name']} on {date_s}:")
    sessions = show_sessions(emp["id"], date_s)
    total = sum(s["duration_s"] or 0 for s in sessions)
    if total:
        h, m = divmod(total // 60, 60)
        print(f"    Total: {h}h {m:02d}min")
    pause()


def flow_add_absence():
    print("\n-- Add absence --")

    # Operator authenticates first to be recorded
    operator = authenticate("Who are you? (to be recorded)")
    if not operator:
        print("  Authentication failed.")
        pause()
        return
    print(f"  Recorded as: {operator['name']}")

    # Pick the employee whose absence to record
    emp = pick_employee("Absence for employee: ")
    if not emp:
        return

    date_s = prompt("  Date (YYYY-MM-DD): ")
    if not parse_date(date_s):
        print("  Invalid date.")
        pause()
        return

    print("  Reason:")
    print("    1. Sick")
    print("    2. Holiday (paid leave)")
    sel = prompt("  Select: ")
    reasons = {"1": "sick", "2": "holiday"}
    reason = reasons.get(sel)
    if not reason:
        print("  Invalid selection.")
        pause()
        return

    db.add_absence(emp["id"], date_s, reason,
                   added_by_id=operator["id"], added_by_method=operator["method"])
    print(f"  Absence ({reason}) recorded for {emp['name']} on {date_s}.")
    pause()


def flow_export():
    print("\n-- Export monthly report --")
    emp = pick_employee("Employee: ")
    if not emp:
        return
    year_s = prompt("  Year (YYYY): ")
    month_s = prompt("  Month (1-12): ")
    if not year_s.isdigit() or not month_s.isdigit():
        print("  Invalid input.")
        pause()
        return
    year, month = int(year_s), int(month_s)
    if not (1 <= month <= 12):
        print("  Invalid month.")
        pause()
        return
    out_dir = prompt("  Output folder (Enter for current): ", allow_empty=True) or "."
    try:
        fname = exp.export_employee_month(emp["id"], year, month, out_dir)
        print(f"  Exported: {fname}")
    except Exception as e:
        print(f"  Error: {e}")
    pause()


def flow_change_pin():
    print("\n-- Change PIN --")
    pin = prompt("  Current PIN: ")
    emp = db.find_employee_by_pin(pin)
    if not emp:
        print("  Unknown PIN.")
        pause()
        return
    print(f"  Hello, {emp['name']}.")
    new_pin = prompt("  New PIN: ")
    confirm = prompt("  Confirm new PIN: ")
    if new_pin != confirm:
        print("  PINs do not match.")
        pause()
        return
    if len(new_pin) < 4:
        print("  PIN must be at least 4 characters.")
        pause()
        return
    if db.is_pin_taken(new_pin, exclude_id=emp["id"]):
        print("  PIN already in use by another employee.")
        pause()
        return
    db.change_pin(emp["id"], pin, new_pin)
    print("  PIN changed.")
    pause()


def flow_who_is_in():
    print("\n-- Currently at work --")
    people = db.get_who_is_in()
    if not people:
        print("  Nobody is at work right now.")
    else:
        for p in people:
            print(f"  {p['name']}  (since {p['arrived_at'][11:16]})")
    pause()


# ── admin flows ───────────────────────────────────────────────────────────────

def admin_employees():
    while True:
        print("\n-- Manage employees --")
        print("  1. List employees")
        print("  2. Add employee")
        print("  3. Remove employee")
        print("  0. Back")
        sel = prompt("  Select: ")
        if sel == "0":
            break
        elif sel == "1":
            employees = db.list_employees()
            if not employees:
                print("  (none)")
            for e in employees:
                chips = db.list_chips(e["id"])
                chip_s = f"  {len(chips)} chip(s)" if chips else ""
                print(f"  [{e['id']}] {e['name']}{chip_s}")
            pause()
        elif sel == "2":
            name = prompt("  Name: ")
            pin = prompt("  PIN (Enter to skip): ", allow_empty=True)
            if pin and db.is_pin_taken(pin):
                print("  PIN already in use by another employee.")
                pause()
                continue
            eid = db.add_employee(name, pin or None)
            print(f"  Added '{name}' (ID {eid}).")
            if not pin:
                print("  No PIN set — link a chip or reset PIN later.")
            pause()
        elif sel == "3":
            emp = pick_employee("Remove employee: ")
            if emp:
                confirm = prompt(f"  Remove '{emp['name']}'? Type YES: ")
                if confirm == "YES":
                    db.remove_employee(emp["id"])
                    print(f"  '{emp['name']}' deactivated.")
                else:
                    print("  Cancelled.")
            pause()


def admin_chips():
    while True:
        print("\n-- Manage chips --")
        print("  1. Link chip to employee")
        print("  2. Unlink chip")
        print("  3. List chips for employee")
        print("  0. Back")
        sel = prompt("  Select: ")
        if sel == "0":
            break
        elif sel == "1":
            emp = pick_employee("Link to employee: ")
            if emp:
                serial = prompt("  Chip serial: ")
                result = db.link_chip(emp["id"], serial)
                if result["ok"]:
                    print(f"  Chip '{serial}' linked to {emp['name']}.")
                else:
                    print(f"  Error: {result['msg']}")
            pause()
        elif sel == "2":
            serial = prompt("  Chip serial to unlink: ")
            db.unlink_chip(serial)
            print(f"  Chip '{serial}' unlinked.")
            pause()
        elif sel == "3":
            emp = pick_employee("Employee: ")
            if emp:
                chips = db.list_chips(emp["id"])
                for c in chips:
                    print(f"  {c}")
                if not chips:
                    print("  No chips linked.")
            pause()


def admin_reset_pin():
    print("\n-- Reset PIN --")
    emp = pick_employee("Reset PIN for: ")
    if not emp:
        return
    new_pin = prompt("  New PIN: ")
    confirm = prompt("  Confirm: ")
    if new_pin != confirm:
        print("  PINs do not match.")
        pause()
        return
    if db.is_pin_taken(new_pin, exclude_id=emp["id"]):
        print("  PIN already in use by another employee.")
        pause()
        return
    db.reset_pin(emp["id"], new_pin)
    print(f"  PIN for '{emp['name']}' reset.")
    pause()


def admin_schedule():
    print("\n-- Work schedule --")
    emp = pick_employee("Schedule for: ")
    if not emp:
        return

    history = db.get_schedule_history(emp["id"])
    if history:
        print(f"\n  History for {emp['name']}:")
        for v in history:
            print(f"\n  From {v['effective_from']}:")
            for i, day in enumerate(DAYS):
                print(f"    {day}: {v['schedule'].get(i, 0.0)}h")
    else:
        print("  No schedule set yet.")

    today = datetime.now().strftime("%Y-%m-%d")
    ef_s = prompt(f"\n  Effective from (YYYY-MM-DD, Enter = today {today}): ", allow_empty=True)
    if ef_s and not parse_date(ef_s):
        print("  Invalid date.")
        pause()
        return
    effective_from = ef_s or today

    current = db.get_work_schedule(emp["id"], on_date=effective_from)
    print("\n  Hours per day (Enter to keep, 0 = day off):")
    new_schedule = {}
    for i, day in enumerate(DAYS):
        cur = current.get(i, 0.0)
        val = input(f"    {day} [{cur}h]: ").strip()
        if val:
            try:
                new_schedule[i] = float(val.replace(",", "."))
            except ValueError:
                print(f"    Invalid — keeping {cur}h.")
                new_schedule[i] = cur
        else:
            new_schedule[i] = cur

    db.set_work_schedule(emp["id"], new_schedule, effective_from)
    print(f"  Schedule saved, effective from {effective_from}.")
    pause()


def admin_attendance():
    print("\n-- Modify attendance --")
    emp = pick_employee("Employee: ")
    if not emp:
        return
    date_s = prompt("  Date (YYYY-MM-DD): ")
    if not parse_date(date_s):
        print("  Invalid date.")
        pause()
        return

    while True:
        print(f"\n  {emp['name']} on {date_s}:")
        sessions = show_sessions(emp["id"], date_s)
        print()
        print("  1. Add session")
        print("  2. Edit session")
        print("  3. Delete session")
        print("  4. Clock in/out at a time (retroactive)")
        print("  0. Done")
        sel = prompt("  Select: ")
        if sel == "0":
            break

        elif sel == "4":
            t_s = prompt("  Time (HH:MM or full date): ")
            ts = parse_dt(t_s, date_s)
            if not ts:
                print("  Invalid time.")
                continue
            res = db.toggle_at(emp["id"], ts)
            if res["inout"] == "in":
                print(f"  Logged IN at {ts[11:16]} (open session).")
            else:
                h, m = divmod((res["worked_s"] or 0) // 60, 60)
                print(f"  Logged OUT at {ts[11:16]} (session now {h}:{m:02d}).")

        elif sel == "1":
            arr_s = prompt(f"  Arrived (HH:MM or full date): ")
            arrived = parse_dt(arr_s, date_s)
            if not arrived:
                print("  Invalid time.")
                continue
            left_s = prompt(f"  Left (HH:MM or full date, Enter = still in): ", allow_empty=True)
            left = parse_dt(left_s, date_s) if left_s else None
            db.add_session(emp["id"], arrived, left)
            print("  Session added.")

        elif sel == "2":
            if not sessions:
                print("  No sessions to edit.")
                continue
            sid_s = prompt("  Session ID: ")
            if not sid_s.isdigit():
                print("  Invalid.")
                continue
            sid = int(sid_s)
            s = next((x for x in sessions if x["id"] == sid), None)
            if not s:
                print("  Not found.")
                continue
            arr_s = prompt(f"  Arrived [{s['arrived_at'][11:16]}]: ", allow_empty=True)
            arrived = parse_dt(arr_s or s["arrived_at"][11:16], date_s)
            left_cur = s["left_at"][11:16] if s["left_at"] else ""
            left_s = prompt(f"  Left [{left_cur or 'open'}]: ", allow_empty=True)
            if left_s:
                left = parse_dt(left_s, date_s)
            else:
                left = s["left_at"]
            if not arrived:
                print("  Invalid time.")
                continue
            db.edit_session(sid, arrived, left)
            print("  Session updated.")

        elif sel == "3":
            if not sessions:
                print("  No sessions to delete.")
                continue
            sid_s = prompt("  Session ID to delete: ")
            if not sid_s.isdigit():
                print("  Invalid.")
                continue
            db.delete_session(int(sid_s))
            print("  Session deleted (reversible via revert menu).")


def admin_special_days():
    while True:
        print("\n-- Special days --")
        print("  1. List one-time special days")
        print("  2. Add one-time national holiday")
        print("  3. Add one-time work-closed day")
        print("  4. Remove one-time special day")
        print("  5. Manage recurring holidays")
        print("  0. Back")
        sel = prompt("  Select: ")
        if sel == "0":
            break
        elif sel == "1":
            year_s = prompt("  Year (YYYY, Enter = all): ", allow_empty=True)
            year = int(year_s) if year_s.isdigit() else None
            days = db.list_special_days(year)
            if not days:
                print("  (none)")
            for d in days:
                label = f"  {d['label']}" if d["label"] else ""
                print(f"  {d['date']}  {d['reason']}{label}")
            pause()
        elif sel in ("2", "3"):
            reason = "national_holiday" if sel == "2" else "work_closed"
            date_s = prompt("  Date (YYYY-MM-DD): ")
            if not parse_date(date_s):
                print("  Invalid date.")
                pause()
                continue
            label = prompt("  Label (Enter to skip): ", allow_empty=True)
            db.add_special_day(date_s, reason, label)
            print(f"  Added {reason} on {date_s}.")
            pause()
        elif sel == "4":
            date_s = prompt("  Date to remove: ")
            db.remove_special_day(date_s)
            print(f"  Removed {date_s}.")
            pause()
        elif sel == "5":
            admin_recurring_days()


def admin_recurring_days():
    while True:
        print("\n-- Recurring special days --")
        print("  1. List")
        print("  2. Add fixed date (e.g. every July 14)")
        print("  3. Add relative date (e.g. 2nd Tuesday of March)")
        print("  4. Remove")
        print("  0. Back")
        sel = prompt("  Select: ")
        if sel == "0":
            break

        elif sel == "1":
            rows = db.list_recurring_special_days()
            if not rows:
                print("  (none)")
            for r in rows:
                label = f"  {r['label']}" if r["label"] else ""
                print(f"  [{r['id']}] {db.describe_recurring(r)}  →  {r['reason']}{label}")
            pause()

        elif sel == "2":
            month_s = prompt("  Month (1-12): ")
            day_s = prompt("  Day of month: ")
            if not month_s.isdigit() or not day_s.isdigit():
                print("  Invalid.")
                pause()
                continue
            month, day = int(month_s), int(day_s)
            if not (1 <= month <= 12 and 1 <= day <= 31):
                print("  Out of range.")
                pause()
                continue
            print("  Reason:")
            print("    1. National holiday")
            print("    2. Work closed")
            rsel = prompt("  Select: ")
            reason = {"1": "national_holiday", "2": "work_closed"}.get(rsel)
            if not reason:
                print("  Invalid.")
                pause()
                continue
            label = prompt("  Label (Enter to skip): ", allow_empty=True)
            rid = db.add_recurring_special_day(month, reason, label, day_of_month=day)
            print(f"  Added: every {MONTHS[month]} {day} (ID {rid}).")
            pause()

        elif sel == "3":
            month_s = prompt("  Month (1-12): ")
            if not month_s.isdigit() or not (1 <= int(month_s) <= 12):
                print("  Invalid month.")
                pause()
                continue
            month = int(month_s)
            print("  Weekday: 0=Mon 1=Tue 2=Wed 3=Thu 4=Fri 5=Sat 6=Sun")
            wd_s = prompt("  Weekday: ")
            print("  Occurrence: 1=first 2=second 3=third 4=fourth -1=last")
            occ_s = prompt("  Occurrence: ")
            if not wd_s.lstrip("-").isdigit() or not occ_s.lstrip("-").isdigit():
                print("  Invalid.")
                pause()
                continue
            weekday, occurrence = int(wd_s), int(occ_s)
            if not (0 <= weekday <= 6) or occurrence == 0:
                print("  Out of range.")
                pause()
                continue
            print("  Reason:")
            print("    1. National holiday")
            print("    2. Work closed")
            rsel = prompt("  Select: ")
            reason = {"1": "national_holiday", "2": "work_closed"}.get(rsel)
            if not reason:
                print("  Invalid.")
                pause()
                continue
            label = prompt("  Label (Enter to skip): ", allow_empty=True)
            rid = db.add_recurring_special_day(month, reason, label, weekday=weekday, occurrence=occurrence)
            print(f"  Added: {db.describe_recurring({'month': month, 'day_of_month': None, 'weekday': weekday, 'occurrence': occurrence})} (ID {rid}).")
            pause()

        elif sel == "4":
            rid_s = prompt("  ID to remove: ")
            if not rid_s.isdigit():
                print("  Invalid.")
                pause()
                continue
            db.remove_recurring_special_day(int(rid_s))
            print(f"  Removed ID {rid_s}.")
            pause()


def admin_revert():
    print("\n-- Reverse a modification --")
    changes = db.list_recent_changes(30)
    if not changes:
        print("  No recorded changes.")
        pause()
        return
    print()
    for c in changes:
        who = c["done_by_name"] or "admin"
        print(f"  [{c['id']}] {c['timestamp']}  {c['action']}  #{c['record_id']}  by {who}")
    print()
    sid_s = prompt("  Change ID to reverse (0 to cancel): ")
    if sid_s == "0" or not sid_s.isdigit():
        return
    result = db.revert_change(int(sid_s))
    if result["ok"]:
        print("  Change reversed.")
    else:
        print(f"  Error: {result['msg']}")
    pause()


def admin_logout_all():
    print("\n-- Log everyone out --")
    people = db.get_who_is_in()
    if not people:
        print("  Nobody is currently checked in.")
        pause()
        return
    print(f"  {len(people)} currently checked in:")
    for p in people:
        print(f"    {p['name']}  (since {p['arrived_at'][11:16]})")
    if prompt("  Log all of them out now? Type YES: ") != "YES":
        print("  Cancelled.")
        pause()
        return
    closed = db.logout_all()
    print()
    for c in closed:
        h, m = divmod(c["worked_s"] // 60, 60)
        print(f"    {c['name']}: worked {h}:{m:02d}")
    print(f"  Logged out {len(closed)} employee(s).")
    pause()


def admin_pin_toggle():
    current = db.is_pin_login_enabled()
    state = "ENABLED" if current else "DISABLED"
    print(f"\n  PIN login is currently {state}.")
    print(f"  1. {'Disable' if current else 'Enable'} PIN login")
    print("  0. Back")
    sel = prompt("  Select: ")
    if sel == "1":
        db.set_pin_login_enabled(not current)
        new_state = "disabled" if current else "enabled"
        print(f"  PIN login {new_state}.")
        pause()


# ── admin panel ───────────────────────────────────────────────────────────────

def admin_menu():
    print("\n  Admin password required.")
    pw = prompt("  Password: ")
    if not db.verify_admin(pw):
        print("  Wrong password.")
        pause()
        return
    while True:
        pin_s = "ON" if db.is_pin_login_enabled() else "OFF"
        print(f"\n=== ADMIN PANEL ===  [PIN login: {pin_s}]")
        print("  -- Admin --")
        print("  1. Manage employees")
        print("  2. Manage chips")
        print("  3. Reset employee PIN")
        print("  4. Set work schedule")
        print("  5. Modify attendance history")
        print("  6. Special days & recurring holidays")
        print("  7. Toggle PIN login")
        print("  8. Reverse a modification")
        print("  9. Change admin password")
        print(" 10. Log everyone out")
        print("  -- General --")
        print("  A. Check in / Check out (PIN)")
        print("  B. Add absence")
        print("  C. Export monthly report")
        print("  D. Change my PIN")
        print("  E. Who is at work now")
        print("  F. Attendance lookup")
        print("  0. Back")
        sel = prompt("  Select: ").upper()
        if sel == "0":
            break
        elif sel == "1":
            admin_employees()
        elif sel == "2":
            admin_chips()
        elif sel == "3":
            admin_reset_pin()
        elif sel == "4":
            admin_schedule()
        elif sel == "5":
            admin_attendance()
        elif sel == "6":
            admin_special_days()
        elif sel == "7":
            admin_pin_toggle()
        elif sel == "8":
            admin_revert()
        elif sel == "10":
            admin_logout_all()
        elif sel == "9":
            old = prompt("  Current password: ")
            new_pw = prompt("  New password: ")
            if db.change_admin_password(old, new_pw):
                print("  Admin password changed.")
            else:
                print("  Wrong password.")
            pause()
        elif sel == "A":
            flow_checkinout()
        elif sel == "B":
            flow_add_absence()
        elif sel == "C":
            flow_export()
        elif sel == "D":
            flow_change_pin()
        elif sel == "E":
            flow_who_is_in()
        elif sel == "F":
            flow_lookup()


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    db.init_db()
    while True:
        pin_s = "" if db.is_pin_login_enabled() else "  [PIN login disabled]"
        print(f"\n=== TIME TRACKER ==={pin_s}")
        print("  1. Check in / Check out (PIN)")
        print("  2. Add absence (sick / holiday)")
        print("  3. Attendance lookup")
        print("  4. Export monthly report")
        print("  5. Change my PIN")
        print("  6. Who is at work now")
        print("  7. Admin panel")
        print("  0. Exit")
        sel = prompt("  Select: ")
        if sel == "0":
            break
        elif sel == "1":
            flow_checkinout()
        elif sel == "2":
            flow_add_absence()
        elif sel == "3":
            flow_lookup()
        elif sel == "4":
            flow_export()
        elif sel == "5":
            flow_change_pin()
        elif sel == "6":
            flow_who_is_in()
        elif sel == "7":
            admin_menu()


if __name__ == "__main__":
    main()
