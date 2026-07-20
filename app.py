"""Local admin website for the time-tracker.

Server-rendered Flask app, protected by a single admin password. Runs on the Pi
alongside the reader. Start with:  python3 app.py   (default http://0.0.0.0:8080)
Default password is 'admin' -- change it under Settings on first login.
"""

import io
import csv
import re
import calendar
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, session, request, redirect, url_for,
                   render_template, flash, abort, Response)

import buzzer
import db

db.init_db()

# Uploaded sounds are cut to a few seconds and re-encoded, so the stored audio
# is small whatever arrives. This cap only stops someone feeding the Pi a whole
# album before we get the chance to trim it.
MAX_UPLOAD_MB = 16

# Sign back in after this long without using the site. Idle, not absolute: the
# cookie is reissued on every request, so an admin working through the afternoon
# is never interrupted, while a browser left open on the shop floor stops being
# a way in overnight.
SESSION_HOURS = 12

app = Flask(__name__)
app.secret_key = db.get_secret_key()
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.permanent_session_lifetime = timedelta(hours=SESSION_HOURS)
# The session cookie is the credential: keep it away from JavaScript and off
# cross-site requests. Not marked Secure -- this is served over plain HTTP on
# the LAN, and a Secure cookie would simply never be sent.
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

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
            # permanent: without this the cookie carries no expiry at all and
            # lasts as long as the browser feels like keeping it.
            session.permanent = True
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
        chips=db.list_chips(employee_id),
        sounds=db.list_employee_sounds(employee_id),
        max_clip_seconds=buzzer.MAX_CLIP_SECONDS)


@app.route("/employee/<int:employee_id>/rename", methods=["POST"])
@login_required
def rename_employee(employee_id):
    if db.get_employee(employee_id) is None:
        abort(404)
    name = request.form.get("name", "").strip()
    if not name:
        flash("The name can't be empty.", "error")
    else:
        db.rename_employee(employee_id, name)
        flash(f"Renamed to {name}.", "ok")
    year, month = month_arg()
    return redirect(url_for("employee_view", employee_id=employee_id,
                            year=year, month=month))


@app.route("/employee/<int:employee_id>/delete", methods=["POST"])
@login_required
def delete_employee(employee_id):
    """Delete an employee and everything belonging to them.

    Re-checks the admin password even though the session is already signed in:
    this destroys the working-time record, and a logged-in browser left open is
    exactly the way it would happen by accident.
    """
    emp = db.get_employee(employee_id)
    if emp is None:
        abort(404)
    if not db.verify_admin(request.form.get("password", "")):
        flash(f"Wrong password — {emp['name']} was NOT deleted.", "error")
        return redirect(url_for("employee_view", employee_id=employee_id))
    db.delete_employee(employee_id)
    flash(f"Deleted {emp['name']} and all their punches, chips and absences.", "ok")
    # They no longer exist, so there is no employee page to go back to.
    return redirect(url_for("employees"))


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


@app.route("/employee/<int:employee_id>/profile", methods=["POST"])
@login_required
def set_profile(employee_id):
    """School time: hours credited per weekday without scanning (vocational
    school), e.g. 5h Mon and 2h Tue. Set per month; carries forward until
    changed.

    The route and the storage still say "profile" -- renaming the tables and
    URLs would be churn for a wording change, and the stored data is the same
    thing either way.
    """
    year, month = month_arg()
    hours_by_weekday = {}
    for wd in range(7):
        raw = request.form.get(f"h{wd}", "").strip()
        if not raw:
            continue
        try:
            # "3,25" is how a decimal gets typed here; a browser set to a
            # German locale hands the comma straight through.
            h = float(raw.replace(",", "."))
        except ValueError:
            flash(f"Ignored an invalid hours value for {db.WEEKDAY_NAMES[wd]}.",
                  "error")
            continue
        if h > 0:
            hours_by_weekday[wd] = h
    db.set_absent_profile(employee_id, year, month, hours_by_weekday,
                          request.form.get("label", "").strip())
    if hours_by_weekday:
        flash("School time saved.", "ok")
    else:
        flash("School time turned off from this month on.", "ok")
    return redirect(url_for("employee_view", employee_id=employee_id,
                            year=year, month=month))


# ── custom in/out sounds ──────────────────────────────────────────────────────

def _sound_word(direction):
    return "welcome" if direction == "in" else "leaving"


def _back_to_sounds(employee_id):
    """Return to the Scan sounds card rather than the top of a long page."""
    return redirect(url_for("employee_view", employee_id=employee_id) + "#sounds")


@app.route("/employee/<int:employee_id>/sound", methods=["POST"])
@login_required
def set_sound(employee_id):
    """Upload, re-trim, or clear an employee's own clock-in / clock-out sound."""
    if db.get_employee(employee_id) is None:
        abort(404)
    direction = request.form.get("direction", "")
    if direction not in ("in", "out"):
        abort(400)
    word = _sound_word(direction)

    if request.form.get("action") == "clear":
        db.clear_employee_sound(employee_id, direction)
        flash(f"Custom {word} sound removed — back to the standard beep.", "ok")
        return _back_to_sounds(employee_id)

    f = request.files.get("sound")
    if f is None or not f.filename:
        flash("Pick an audio file first.", "error")
        return _back_to_sounds(employee_id)

    # Blank start/end means the whole file -- the trim is optional, not a step
    # you have to fill in to upload something short.
    try:
        start = _seconds_field(request.form, "start_s", default=0.0)
        end = _seconds_field(request.form, "end_s", default=None)
    except ValueError:
        flash("Start and end have to be numbers, in seconds (or left empty).",
              "error")
        return _back_to_sounds(employee_id)

    try:
        wav, seconds, source_seconds = buzzer.make_clip(f.read(), start, end)
    except buzzer.SoundError as e:
        # buzzer writes its messages for this exact spot.
        flash(str(e), "error")
        return _back_to_sounds(employee_id)

    end = start + seconds
    db.set_employee_sound(employee_id, direction, f.filename,
                          start, end, source_seconds, wav, seconds)
    msg = f"Custom {word} sound saved: {f.filename} — plays {seconds:.1f}s"
    if seconds < source_seconds - 0.05:
        msg += f" ({start:.2f}s–{end:.2f}s of {source_seconds:.1f}s)"
    flash(msg + ".", "ok")
    return _back_to_sounds(employee_id)


def _seconds_field(form, field, default):
    """A time in seconds from the form, or ``default`` if the box was left
    empty. Raises ValueError if it isn't a number."""
    raw = (form.get(field, "") or "").strip()
    return default if not raw else float(raw)


@app.route("/employee/<int:employee_id>/sound/<direction>.wav")
@login_required
def sound_preview(employee_id, direction):
    """The clip as the reader will play it -- already cut, levelled and faded."""
    if direction not in ("in", "out"):
        abort(404)
    row = db.get_employee_sound(employee_id, direction)
    if row is None:
        abort(404)
    return Response(row["audio"], mimetype="audio/wav")


@app.errorhandler(413)
def too_large(e):
    """Flask aborts oversized uploads before our route runs, so the friendly
    message has to happen here."""
    flash(f"That file is too big (limit {MAX_UPLOAD_MB} MB). "
          f"At most {buzzer.MAX_CLIP_SECONDS:g}s of it is used anyway "
          f"— try a shorter file.", "error")
    return redirect(request.referrer or url_for("index"))


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
        action = request.form.get("action")
        if action == "delete":
            db.remove_special_day(request.form.get("date", ""))
        elif action == "delete_recurring":
            db.remove_recurring_special_day(request.form.get("md", ""))
        elif action == "add_range":
            # A shutdown week, works holiday, etc. -- stored as one row per day.
            start = request.form.get("start", "")
            end = request.form.get("end", "") or start
            try:
                datetime.strptime(start, "%Y-%m-%d")
                datetime.strptime(end, "%Y-%m-%d")
            except ValueError:
                flash("Pick a valid start and end date.", "error")
                return redirect(url_for("special_days"))
            kind = request.form.get("kind", "closed")
            if kind in ("closed", "holiday", "none"):
                n = db.set_special_day_range(start, end, kind,
                                             request.form.get("label", ""))
                verb = "cleared" if kind == "none" else f"marked {kind}"
                flash(f"{n} day(s) {verb}.", "ok")
        elif action == "add_recurring":
            try:
                db.set_recurring_special_day(
                    int(request.form.get("month", 0)),
                    int(request.form.get("day", 0)),
                    request.form.get("kind", "holiday"),
                    request.form.get("label", ""))
            except (ValueError, TypeError):
                flash("Invalid day for that month.", "error")
        else:
            d = request.form.get("date", "")
            try:
                datetime.strptime(d, "%Y-%m-%d")
                db.set_special_day(d, request.form.get("kind", "holiday"),
                                   request.form.get("label", ""))
            except ValueError:
                flash("Invalid date.", "error")
        return redirect(url_for("special_days"))
    return render_template("special_days.html", days=db.list_special_days(),
                           recurring=db.list_recurring_special_days())


# ── employees + chip enrollment ───────────────────────────────────────────────

@app.route("/api/enroll_status")
@login_required
def enroll_status():
    """Polled by the employees page while a chip enrollment is armed, so the
    page can refresh itself the moment the scan station links the chip (or the
    window lapses). The station is a separate process, so there is nothing to
    push -- the browser has to ask."""
    p = db.get_pending_enroll()
    return {"pending": p is not None,
            "employee_id": p["employee_id"] if p else None}


@app.route("/employees", methods=["GET", "POST"])
@login_required
def employees():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name = request.form.get("name", "").strip()
            if name:
                db.add_employee(name)
        elif action == "active":
            db.set_active(int(request.form["id"]), request.form.get("active") == "1")
        elif action == "chip":
            # manual entry: type a serial you already know
            serial = request.form.get("serial", "").strip()
            if serial:
                db.learn_chip(int(request.form["id"]), serial)
        elif action == "enroll":
            # arm the scan station to link the next unassigned chip scanned
            db.request_enroll(int(request.form["id"]))
        elif action == "cancel_enroll":
            db.cancel_enroll()
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
                           pending_enroll=db.get_pending_enroll(),
                           enroll_window=db.ENROLL_WINDOW_SECONDS,
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
            # Rotating the key invalidates every existing session -- including
            # this one, so sign this browser back in with the new key before
            # responding. Flask signs the cookie when the response is built, so
            # swapping the key here means this reply already carries a valid one
            # and whoever changed the password is not thrown out by their own
            # action. Every other browser is.
            app.secret_key = db.set_admin_password(pw)
            session.clear()
            session["admin"] = True
            session.permanent = True
            flash("Admin password changed. Any other signed-in browsers have "
                  "been logged out.", "ok")
        return redirect(url_for("settings"))
    return render_template("settings.html")


# ── per-employee monthly CSV ──────────────────────────────────────────────────
#
# The only export. Whole-company and whole-year exports used to live here too;
# they were dropped because this is the one anybody actually opens, and each
# extra variant was another place the column list had to be kept in step.

DETAIL_HEADER = ["Date", "Weekday", "Type", "In", "Out", "Pause h",
                 "Worked h", "School h", "Supposed h", "Via", "Note"]


def _h(seconds):
    return round((seconds or 0) / 3600, 2)


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
                    _h(r.pause_s), _h(r.worked_s), _h(r.school_s),
                    _h(r.supposed_s), " ".join(r.methods), r.note])
    w.writerow([])
    w.writerow(["Worked h", _h(t["worked_s"]), "Supposed h", _h(t["supposed_s"])])
    w.writerow(["Vacation h", _h(t["vacation_s"]), "Sick h", _h(t["sick_s"])])
    w.writerow(["School h", _h(t["profile_s"]), "School days", t["profile_days"]])
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


if __name__ == "__main__":
    host, port = "0.0.0.0", 8080
    try:
        from waitress import serve
        print(f"Serving admin site on http://{host}:{port}  (waitress)", flush=True)
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        print("waitress not installed — using Flask's dev server "
              "(ok for local use; `apt install python3-waitress` for production).",
              flush=True)
        app.run(host=host, port=port, debug=False)
