"""Local admin website for the time-tracker.

Server-rendered Flask app, protected by a single admin password. Runs on the Pi
alongside the reader. Start with:  python3 app.py   (default http://0.0.0.0:8080)
Default password is 'admin' -- change it under Settings on first login.
"""

import io
import csv
import re
import zipfile
import calendar
from datetime import date, datetime
from functools import wraps

from flask import (Flask, session, request, redirect, url_for,
                   render_template, flash, abort, Response)

import db

db.init_db()

app = Flask(__name__)
app.secret_key = db.get_secret_key()

WEEKDAYS = list(zip(range(7), db.WEEKDAY_NAMES))
MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]


# ── helpers ───────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)
    return wrapper


def fmt_hm(seconds) -> str:
    seconds = int(round(seconds or 0))
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    return f"{sign}{seconds // 3600}:{(seconds % 3600) // 60:02d}"


def month_arg():
    today = date.today()
    try:
        year = int(request.args.get("year", today.year))
        month = int(request.args.get("month", today.month))
        date(year, month, 1)
    except (ValueError, TypeError):
        year, month = today.year, today.month
    return year, month


app.jinja_env.filters["hm"] = fmt_hm
app.jinja_env.globals.update(MONTH_NAMES=MONTH_NAMES, WEEKDAYS=WEEKDAYS)


# ── auth ──────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if db.verify_admin(request.form.get("password", "")):
            session["admin"] = True
            nxt = request.args.get("next") or url_for("index")
            return redirect(nxt)
        flash("Wrong password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── main: employee month view ─────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    employees = db.list_employees()
    if employees:
        return redirect(url_for("employee_view", employee_id=employees[0]["id"]))
    return redirect(url_for("employees"))


@app.route("/employee/<int:employee_id>")
@login_required
def employee_view(employee_id):
    emp = db.get_employee(employee_id)
    if emp is None:
        abort(404)
    year, month = month_arg()
    summary = db.month_summary(employee_id, year, month)

    # build a calendar grid (weeks of DayRow-or-None), Monday first
    rows_by_date = {r.date: r for r in summary["rows"]}
    weeks = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
        cells = []
        for d in week:
            key = d.strftime("%Y-%m-%d")
            cells.append(rows_by_date.get(key) if d.month == month else None)
        weeks.append(cells)

    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)

    return render_template(
        "employee.html", emp=emp, employees=db.list_employees(),
        year=year, month=month, summary=summary, weeks=weeks,
        prev_y=prev_y, prev_m=prev_m, next_y=next_y, next_m=next_m,
        workdays_csv=",".join(str(w) for w in sorted(summary["workdays"])),
        chips=db.list_chips(employee_id))


@app.route("/employee/<int:employee_id>/target", methods=["POST"])
@login_required
def set_target(employee_id):
    year, month = month_arg()
    try:
        hours = float(request.form.get("target_hours", "0") or 0)
    except ValueError:
        hours = 0
    workdays = ",".join(request.form.getlist("workdays"))
    db.set_monthly_target(employee_id, year, month, hours, workdays)
    flash("Monthly target saved.", "ok")
    return redirect(url_for("employee_view", employee_id=employee_id,
                            year=year, month=month))


@app.route("/employee/<int:employee_id>/absence", methods=["POST"])
@login_required
def set_absence(employee_id):
    d = request.form.get("date", "")
    kind = request.form.get("kind", "")
    if kind == "none":
        db.remove_absence(employee_id, d)
    elif kind in ("sick", "vacation"):
        db.set_absence(employee_id, d, kind, request.form.get("note", ""))
    return redirect(url_for("day_view", employee_id=employee_id, day=d))


@app.route("/employee/<int:employee_id>/absence-range", methods=["POST"])
@login_required
def set_absence_range(employee_id):
    year, month = month_arg()
    start = request.form.get("start", "")
    end = request.form.get("end", "") or start
    kind = request.form.get("kind", "")
    try:
        datetime.strptime(start, "%Y-%m-%d")
        datetime.strptime(end, "%Y-%m-%d")
    except ValueError:
        flash("Pick a valid start and end date.", "error")
        return redirect(url_for("employee_view", employee_id=employee_id,
                                year=year, month=month))
    if kind in ("sick", "vacation", "none"):
        n = db.set_absence_range(employee_id, start, end, kind,
                                 request.form.get("note", ""))
        verb = "cleared" if kind == "none" else f"set to {kind}"
        flash(f"{n} day(s) {verb}.", "ok")
    return redirect(url_for("employee_view", employee_id=employee_id,
                            year=year, month=month))


# ── edit a single day's punches ───────────────────────────────────────────────

@app.route("/employee/<int:employee_id>/day/<day>")
@login_required
def day_view(employee_id, day):
    emp = db.get_employee(employee_id)
    if emp is None:
        abort(404)
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        abort(404)
    punches = db.get_day_punches(employee_id, day)
    absences = db.get_absences(employee_id, d.year, d.month)
    return render_template("day.html", emp=emp, day=day, d=d,
                           punches=punches, absence=absences.get(day))


@app.route("/employee/<int:employee_id>/day/<day>/punch", methods=["POST"])
@login_required
def punch_edit(employee_id, day):
    action = request.form.get("action")
    if action == "add":
        t = request.form.get("time", "")  # HH:MM
        try:
            datetime.strptime(f"{day} {t}", "%Y-%m-%d %H:%M")
            db.add_manual_punch(employee_id, f"{day} {t}:00")
        except ValueError:
            flash("Invalid time (use HH:MM).", "error")
    elif action == "update":
        pid = int(request.form["punch_id"])
        t = request.form.get("time", "")
        try:
            datetime.strptime(f"{day} {t}", "%Y-%m-%d %H:%M")
            db.update_punch(pid, f"{day} {t}:00")
        except ValueError:
            flash("Invalid time (use HH:MM).", "error")
    elif action == "delete":
        db.delete_punch(int(request.form["punch_id"]))
    return redirect(url_for("day_view", employee_id=employee_id, day=day))


# ── company special days (holidays / closures) ────────────────────────────────

@app.route("/special-days", methods=["GET", "POST"])
@login_required
def special_days():
    if request.method == "POST":
        if request.form.get("action") == "delete":
            db.remove_special_day(request.form.get("date", ""))
        else:
            d = request.form.get("date", "")
            try:
                datetime.strptime(d, "%Y-%m-%d")
                db.set_special_day(d, request.form.get("kind", "holiday"),
                                   request.form.get("label", ""))
            except ValueError:
                flash("Invalid date.", "error")
        return redirect(url_for("special_days"))
    return render_template("special_days.html", days=db.list_special_days())


# ── employees + chip enrollment ───────────────────────────────────────────────

@app.route("/employees", methods=["GET", "POST"])
@login_required
def employees():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name = request.form.get("name", "").strip()
            if name:
                db.add_employee(name)
        elif action == "rename":
            db.rename_employee(int(request.form["id"]), request.form.get("name", "").strip())
        elif action == "active":
            db.set_active(int(request.form["id"]), request.form.get("active") == "1")
        elif action == "chip":
            db.learn_chip(int(request.form["id"]), request.form.get("serial", "").strip())
        elif action == "unchip":
            db.forget_chip(request.form.get("serial", "").strip())
        return redirect(url_for("employees"))
    emps = db.list_employees(include_inactive=True)
    chips = {e["id"]: db.list_chips(e["id"]) for e in emps}
    today = date.today()
    month_totals = {e["id"]: db.month_summary(e["id"], today.year, today.month)["totals"]
                    for e in emps}
    return render_template("employees.html", employees=emps, chips=chips,
                           month_totals=month_totals,
                           month_label=f"{MONTH_NAMES[today.month]} {today.year}")


# ── settings ──────────────────────────────────────────────────────────────────

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        pw = request.form.get("password", "")
        if len(pw) < 4:
            flash("Password too short (min 4 chars).", "error")
        else:
            db.set_admin_password(pw)
            flash("Admin password changed.", "ok")
        return redirect(url_for("settings"))
    return render_template("settings.html")


# ── CSV export (all employees) ────────────────────────────────────────────────

EXPORT_HEADER = ["Employee", "Year", "Month", "Worked h", "Supposed h",
                 "Vacation h", "Sick h", "Balance h", "Worked days",
                 "Supposed days", "Sick days", "Vacation days", "Holiday days",
                 "Closed days", "Target h"]


def _h(seconds):
    return round((seconds or 0) / 3600, 2)


def _summary_row(emp, year, month):
    s = db.month_summary(emp["id"], year, month)
    t = s["totals"]
    return [emp["name"], year, month, _h(t["worked_s"]), _h(t["supposed_s"]),
            _h(t["vacation_s"]), _h(t["sick_s"]), _h(t["balance_s"]),
            t["worked_days"], t["supposed_days"], t["sick_days"],
            t["vacation_days"], t["holiday_days"], t["closed_days"],
            round(s["target_hours"], 2)]


def _csv_response(rows, filename):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_HEADER)
    w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.route("/export")
@login_required
def export_page():
    return render_template("export.html", this_year=date.today().year,
                           this_month=date.today().month)


@app.route("/export/month.csv")
@login_required
def export_month():
    year, month = month_arg()
    rows = [_summary_row(e, year, month) for e in db.list_employees()]
    return _csv_response(rows, f"attendance_{year}-{month:02d}.csv")


@app.route("/export/year.csv")
@login_required
def export_year():
    year, _ = month_arg()
    rows = [_summary_row(e, year, m)
            for e in db.list_employees() for m in range(1, 13)]
    return _csv_response(rows, f"attendance_{year}.csv")


# ── detailed per-employee CSV (one file per employee per month) ───────────────

DETAIL_HEADER = ["Date", "Weekday", "Type", "In", "Out", "Pause h",
                 "Worked h", "Supposed h", "Via", "Note"]


def _safe(name):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "employee"


def _detail_csv(emp, year, month):
    """Full daily breakdown for one employee/month as CSV text."""
    s = db.month_summary(emp["id"], year, month)
    t = s["totals"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"{emp['name']} - {MONTH_NAMES[month]} {year}"])
    w.writerow(DETAIL_HEADER)
    for r in s["rows"]:
        w.writerow([r.date, r.weekday, r.category, r.in_time, r.out_time,
                    _h(r.pause_s), _h(r.worked_s), _h(r.supposed_s),
                    " ".join(r.methods), r.note])
    w.writerow([])
    w.writerow(["Worked h", _h(t["worked_s"]), "Supposed h", _h(t["supposed_s"])])
    w.writerow(["Vacation h", _h(t["vacation_s"]), "Sick h", _h(t["sick_s"])])
    w.writerow(["Balance h", _h(t["balance_s"]),
                "Worked days", t["worked_days"], "Supposed days", t["supposed_days"]])
    return buf.getvalue()


@app.route("/employee/<int:employee_id>/export/month.csv")
@login_required
def export_employee_month(employee_id):
    emp = db.get_employee(employee_id)
    if emp is None:
        abort(404)
    year, month = month_arg()
    text = _detail_csv(emp, year, month)
    fn = f"{_safe(emp['name'])}_{year}-{month:02d}.csv"
    return Response(text, mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fn}"'})


def _zip_response(files, filename):
    """files: list of (path_in_zip, text_content)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, text in files:
            z.writestr(path, text)
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.route("/export/detailed-month.zip")
@login_required
def export_detailed_month():
    year, month = month_arg()
    files = [(f"{_safe(e['name'])}_{year}-{month:02d}.csv",
              _detail_csv(e, year, month)) for e in db.list_employees()]
    return _zip_response(files, f"attendance_detailed_{year}-{month:02d}.zip")


@app.route("/export/detailed-year.zip")
@login_required
def export_detailed_year():
    year, _ = month_arg()
    files = [(f"{year}-{m:02d}/{_safe(e['name'])}.csv", _detail_csv(e, year, m))
             for m in range(1, 13) for e in db.list_employees()]
    return _zip_response(files, f"attendance_detailed_{year}.zip")


if __name__ == "__main__":
    host, port = "0.0.0.0", 8080
    try:
        from waitress import serve
        print(f"Serving admin site on http://{host}:{port}  (waitress)")
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        print("waitress not installed — using Flask's dev server "
              "(ok for local use; `apt install python3-waitress` for production).")
        app.run(host=host, port=port, debug=False)
