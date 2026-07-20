"""Attendance storage for the time-tracker.

Data model is deliberately simple: every time an employee scans their NFC chip
we append one *raw punch* row.  We do not decide in/out at scan time -- pairing
into work sessions happens later when a day is summarised (see ``day_summary``).

The only thing the scan station has to call is:

    result = db.record_scan(serial)      # NFC chip -> serial/UID string

It returns a small ``PunchResult`` the caller uses to pick buzzer feedback.
"""

import os
import hashlib
import secrets
import sqlite3
import calendar
from datetime import datetime, timedelta, date as _date
from collections import namedtuple

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DEFAULT_WORKDAYS = "0,1,2,3,4"  # Mon-Fri

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "attendance.db")
    
# Ignore a second scan of the same chip within this many seconds. Protects
# against the reader firing twice or someone tapping the chip nervously.
DEBOUNCE_SECONDS = 8

TS_FMT = "%Y-%m-%d %H:%M:%S"
DATE_FMT = "%Y-%m-%d"


# ── result the scan station uses to pick buzzer feedback ──────────────────────

# ok           : True if a punch was stored
# employee_id  : matched employee (None when rejected)
# name         : employee name (None when rejected)
# method       : "chip"
# status       : "recorded" | "ignored_duplicate" | "unknown_chip"
# punched_at   : timestamp string of the stored punch (None when not stored)
# direction    : "in" or "out" for a recorded punch, else None. Computed in the
#                same transaction as the insert so it can't disagree with it.
PunchResult = namedtuple(
    "PunchResult", "ok employee_id name method status punched_at direction"
)


# ── connection ────────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    # FULL rather than the WAL default NORMAL: a committed punch must survive a
    # sudden power cut, which on a shop-floor Pi is the normal way it turns off.
    conn.execute("PRAGMA synchronous=FULL;")
    # WAL still allows only one writer at a time; without a busy timeout a scan
    # that lands while the admin site is mid-write fails instantly with
    # "database is locked". Wait a few seconds for the other writer instead.
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db():
    with get_conn() as conn:
        _drop_legacy_absent_profiles(conn)
        _drop_legacy_employee_sounds(conn)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS employees (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                name     TEXT NOT NULL,
                active   INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS chips (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL REFERENCES employees(id),
                serial      TEXT NOT NULL UNIQUE
            );
            -- Armed chip enrollment. The admin site inserts a row here; the
            -- scan station links the next *unassigned* chip scanned before
            -- expires_at to this employee, then deletes the row. At most one
            -- request exists at a time (see request_enroll).
            CREATE TABLE IF NOT EXISTS enroll_requests (
                employee_id INTEGER NOT NULL REFERENCES employees(id),
                expires_at  TEXT NOT NULL   -- 'YYYY-MM-DD HH:MM:SS'
            );
            CREATE TABLE IF NOT EXISTS punches (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL REFERENCES employees(id),
                punched_at  TEXT NOT NULL,   -- 'YYYY-MM-DD HH:MM:SS'
                work_date   TEXT NOT NULL,   -- 'YYYY-MM-DD' (date of punched_at)
                method      TEXT NOT NULL    -- 'chip' or 'code'
            );
            CREATE INDEX IF NOT EXISTS idx_punches_emp_date
                ON punches(employee_id, work_date);

            -- Per-employee absences. kind: 'sick' | 'vacation'
            CREATE TABLE IF NOT EXISTS absences (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL REFERENCES employees(id),
                date        TEXT NOT NULL,      -- 'YYYY-MM-DD'
                kind        TEXT NOT NULL,
                note        TEXT,
                UNIQUE(employee_id, date)
            );
            -- Company-wide non-working days. kind: 'holiday' | 'closed'
            CREATE TABLE IF NOT EXISTS special_days (
                date  TEXT PRIMARY KEY,         -- 'YYYY-MM-DD'
                kind  TEXT NOT NULL,
                label TEXT
            );
            -- Same, but repeating every year on a fixed day (e.g. 24.12).
            -- A one-off special_days row for a concrete date overrides these,
            -- so a single year can be changed or cancelled.
            CREATE TABLE IF NOT EXISTS recurring_special_days (
                md    TEXT PRIMARY KEY,         -- 'MM-DD'
                kind  TEXT NOT NULL,
                label TEXT
            );
            -- "Absent profile": weekdays an employee is credited a fixed number
            -- of hours without scanning (home office etc). Per employee per
            -- month so it can be changed month to month; carried forward from
            -- the latest earlier month like monthly_targets.
            CREATE TABLE IF NOT EXISTS absent_profiles (
                employee_id INTEGER NOT NULL REFERENCES employees(id),
                year        INTEGER NOT NULL,
                month       INTEGER NOT NULL,
                -- Per-weekday credited hours as a 'weekday:hours' CSV, e.g.
                -- '0:5,1:2' = 5h Mon, 2h Tue. Weekdays are Mon=0. An empty
                -- string means the profile is explicitly off for this month
                -- (which is why the row still exists: it stops the carry
                -- forward in get_absent_profile).
                hours_map   TEXT NOT NULL,
                label       TEXT,               -- e.g. 'Home office'
                PRIMARY KEY (employee_id, year, month)
            );
            -- Monthly working-time target + which weekdays are workdays, per
            -- employee per month. workdays is a CSV of weekday ints (Mon=0).
            CREATE TABLE IF NOT EXISTS monthly_targets (
                employee_id  INTEGER NOT NULL REFERENCES employees(id),
                year         INTEGER NOT NULL,
                month        INTEGER NOT NULL,
                target_hours REAL NOT NULL,
                workdays     TEXT NOT NULL,
                PRIMARY KEY (employee_id, year, month)
            );
            -- Optional per-employee sound played instead of the standard in/out
            -- beep. The audio lives in the database on purpose: a backup is a
            -- copy of this one file (see backup.py), so sounds kept as loose
            -- files on the SD card would silently not be backed up and would
            -- not come back on a restore. Stored already decoded, so the reader
            -- never decodes anything mid-scan.
            CREATE TABLE IF NOT EXISTS employee_sounds (
                employee_id INTEGER NOT NULL REFERENCES employees(id),
                direction   TEXT NOT NULL CHECK (direction IN ('in','out')),
                filename    TEXT NOT NULL,   -- original name, shown in the UI
                -- Which part of the upload was taken, and how long the upload
                -- was. Kept for the UI only -- the original is not stored, so
                -- a different selection means uploading the file again.
                start_s     REAL NOT NULL,
                end_s       REAL NOT NULL,
                source_seconds REAL NOT NULL,
                -- start_s..end_s, already cut and decoded: mono 16-bit WAV, what
                -- the reader pipes straight to aplay.
                audio       BLOB NOT NULL,
                seconds     REAL NOT NULL,   -- length of audio, = end_s - start_s
                updated_at  TEXT NOT NULL,
                PRIMARY KEY (employee_id, direction)
            );
            CREATE TABLE IF NOT EXISTS admin_config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        # First-run defaults: admin password 'admin' and a Flask session key.
        if not conn.execute("SELECT 1 FROM admin_config WHERE key='admin_hash'").fetchone():
            salt = secrets.token_hex(16)
            conn.execute("INSERT INTO admin_config VALUES ('admin_salt', ?)", (salt,))
            conn.execute("INSERT INTO admin_config VALUES ('admin_hash', ?)",
                         (_hash_pin("admin", salt),))
        if not conn.execute("SELECT 1 FROM admin_config WHERE key='secret_key'").fetchone():
            conn.execute("INSERT INTO admin_config VALUES ('secret_key', ?)",
                         (secrets.token_hex(32),))


def _drop_legacy_absent_profiles(conn) -> None:
    """Discard the pre-per-weekday absent_profiles table if we find it.

    The old layout stored one hours value for every selected weekday, which
    cannot express "5h Mon, 2h Tue". CREATE TABLE IF NOT EXISTS won't alter an
    existing table, so the old one is dropped here (before that CREATE runs)
    and recreated empty. Old profiles are discarded and must be re-entered;
    nothing else references this table.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(absent_profiles)")}
    if cols and "hours_map" not in cols:
        conn.execute("DROP TABLE absent_profiles")


def _drop_legacy_employee_sounds(conn) -> None:
    """Discard an employee_sounds table from either older layout.

    Two have existed: one storing a fixed 5-second clip with no start/end, and
    one that also kept a copy of the upload to re-trim from. Neither converts
    into the current shape -- the first has no selection to report, and the
    second's stored original is exactly what we no longer keep. CREATE TABLE IF
    NOT EXISTS won't alter an existing table, so the old one is dropped here
    (before that CREATE runs) and recreated empty. Custom sounds must be
    uploaded again; nothing else references this table.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(employee_sounds)")}
    if cols and ("start_s" not in cols or "source" in cols):
        conn.execute("DROP TABLE employee_sounds")


# ── helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now()


def _hash_pin(pin: str, salt: str) -> str:
    return hashlib.sha256((salt + pin).encode()).hexdigest()


def _norm_serial(serial) -> str:
    """Chip UUIDs are hex typed by the reader; normalise so matching doesn't
    depend on letter case or stray whitespace (reader vs admin-entered form)."""
    return str(serial).strip().upper()


def _last_punch_at(conn, employee_id: int):
    row = conn.execute(
        "SELECT punched_at FROM punches WHERE employee_id=?"
        " ORDER BY punched_at DESC LIMIT 1",
        (employee_id,),
    ).fetchone()
    return datetime.strptime(row["punched_at"], TS_FMT) if row else None


def _insert_punch(conn, employee_id: int, method: str, when: datetime):
    conn.execute(
        "INSERT INTO punches (employee_id, punched_at, work_date, method)"
        " VALUES (?,?,?,?)",
        (employee_id, when.strftime(TS_FMT), when.strftime(DATE_FMT), method),
    )


def _record(employee_id: int, name: str, method: str) -> PunchResult:
    """Shared path once we know which employee scanned. Applies debounce and,
    for a stored punch, reports in/out direction."""
    now = _now()
    work_date = now.strftime(DATE_FMT)
    with get_conn() as conn:
        last = _last_punch_at(conn, employee_id)
        if last is not None and (now - last) < timedelta(seconds=DEBOUNCE_SECONDS):
            return PunchResult(False, employee_id, name, method,
                               "ignored_duplicate", None, None)
        _insert_punch(conn, employee_id, method, now)
        # Punches alternate in/out within a work day. Count this employee's
        # punches for the same work_date the insert used (this one included), so
        # the direction can't drift from the stored row across a midnight tick.
        n = conn.execute(
            "SELECT COUNT(*) FROM punches WHERE employee_id=? AND work_date=?",
            (employee_id, work_date),
        ).fetchone()[0]
    direction = "in" if n % 2 == 1 else "out"
    return PunchResult(True, employee_id, name, method,
                       "recorded", now.strftime(TS_FMT), direction)


# ── the entry point the scan station calls ────────────────────────────────────

def record_scan(serial: str) -> PunchResult:
    """Record a punch from an NFC chip. ``serial`` is the chip's UID/serial.

    Unknown chips are rejected (nothing stored). Duplicate scans within
    DEBOUNCE_SECONDS are ignored.
    """
    serial = _norm_serial(serial)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT e.id, e.name FROM chips c"
            " JOIN employees e ON e.id = c.employee_id"
            " WHERE c.serial = ? AND e.active = 1",
            (serial,),
        ).fetchone()
    if row is None:
        return PunchResult(False, None, None, "chip", "unknown_chip", None, None)
    return _record(row["id"], row["name"], "chip")


# ── management functions (admin side wires these up later) ─────────────────────

def add_employee(name: str) -> int:
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO employees (name) VALUES (?)", (name,))
        return cur.lastrowid


def learn_chip(employee_id: int, serial: str) -> None:
    """Link an NFC chip serial to an employee by hand. Re-assigns if already
    known. This is the manual escape hatch; see request_enroll for the
    scan-to-link flow."""
    serial = _norm_serial(serial)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO chips (employee_id, serial) VALUES (?,?)"
            " ON CONFLICT(serial) DO UPDATE SET employee_id=excluded.employee_id",
            (employee_id, serial),
        )


# ── scan-to-link enrollment ───────────────────────────────────────────────────
#
# The scan station grabs the reader exclusively, so a chip can never be scanned
# into a browser field. Instead the admin site *arms* enrollment for an employee
# and the station links the next unassigned chip it sees. The two processes are
# separate, so the request lives in the DB rather than in memory.

ENROLL_WINDOW_SECONDS = 120

# status : "enrolled"
# name   : employee the chip was linked to
EnrollResult = namedtuple("EnrollResult", "status employee_id name serial")


def request_enroll(employee_id: int, window: int = ENROLL_WINDOW_SECONDS) -> str:
    """Arm enrollment for one employee. Only one request exists at a time, so
    arming for someone else simply replaces the previous one. Returns the
    expiry timestamp."""
    expires = _now() + timedelta(seconds=window)
    with get_conn() as conn:
        conn.execute("DELETE FROM enroll_requests")
        conn.execute(
            "INSERT INTO enroll_requests (employee_id, expires_at) VALUES (?,?)",
            (employee_id, expires.strftime(TS_FMT)),
        )
    return expires.strftime(TS_FMT)


def cancel_enroll() -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM enroll_requests")


def get_pending_enroll():
    """The armed request (employee_id, name, expires_at) if one is still live,
    else None. Expired requests are treated as absent."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT r.employee_id, r.expires_at, e.name FROM enroll_requests r"
            " JOIN employees e ON e.id = r.employee_id LIMIT 1"
        ).fetchone()
    if row is None or datetime.strptime(row["expires_at"], TS_FMT) <= _now():
        return None
    return row


def try_enroll_scan(serial: str):
    """Consume an armed enrollment with this scan, if it applies.

    Returns an EnrollResult when the chip was linked, or None when the caller
    should treat the scan as an ordinary punch -- i.e. when nothing is armed,
    the window has expired, or the chip already belongs to someone. Keeping
    already-assigned chips on the punch path means people can still clock in
    and out normally while enrollment is armed for a colleague.

    Look-up, insert and disarm happen in one transaction so two scans racing
    the same request cannot both be enrolled.
    """
    serial = _norm_serial(serial)
    with get_conn() as conn:
        req = conn.execute(
            "SELECT r.employee_id, r.expires_at, e.name FROM enroll_requests r"
            " JOIN employees e ON e.id = r.employee_id LIMIT 1"
        ).fetchone()
        if req is None:
            return None
        if datetime.strptime(req["expires_at"], TS_FMT) <= _now():
            conn.execute("DELETE FROM enroll_requests")  # expired: tidy up
            return None
        if conn.execute("SELECT 1 FROM chips WHERE serial=?", (serial,)).fetchone():
            return None  # already someone's chip -> normal punch
        conn.execute("INSERT INTO chips (employee_id, serial) VALUES (?,?)",
                     (req["employee_id"], serial))
        conn.execute("DELETE FROM enroll_requests")
    return EnrollResult("enrolled", req["employee_id"], req["name"], serial)


def forget_chip(serial: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM chips WHERE serial=?", (_norm_serial(serial),))


def list_employees(include_inactive: bool = False):
    q = "SELECT id, name, active FROM employees"
    if not include_inactive:
        q += " WHERE active = 1"
    q += " ORDER BY name"
    with get_conn() as conn:
        return conn.execute(q).fetchall()


# ── read side: per-day summary line for the admin view ────────────────────────

DaySummary = namedtuple(
    "DaySummary",
    "date first_in last_out worked_seconds pause_seconds"
    " supposed_seconds methods incomplete punches",
)


def get_punches(employee_id: int, work_date: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT punched_at, method FROM punches"
            " WHERE employee_id=? AND work_date=? ORDER BY punched_at",
            (employee_id, work_date),
        ).fetchall()


def day_summary(employee_id: int, work_date: str,
                supposed_seconds: int | None = None) -> DaySummary:
    """Pair a day's raw punches into worked time and pauses.

    Punches alternate in/out in scan order: 1st=in, 2nd=out, 3rd=in, ...
    - worked_seconds : sum of every in->out interval
    - pause_seconds  : sum of every out->in gap between pairs
    - first_in/last_out : bookends for the day
    - incomplete : True if the last 'in' has no matching 'out' (forgot to scan
      out) or there were no punches at all
    - methods : set of how the day was punched, e.g. {'chip'} or {'chip','code'}

    ``supposed_seconds`` (planned working time for the day) comes from the work
    schedule feature, which isn't built yet -- pass it in for now, or leave None.
    """
    rows = get_punches(employee_id, work_date)
    times = [datetime.strptime(r["punched_at"], TS_FMT) for r in rows]
    methods = {r["method"] for r in rows}

    worked = 0
    pause = 0
    # in/out pairs
    for i in range(0, len(times) - 1, 2):
        worked += int((times[i + 1] - times[i]).total_seconds())
    # gaps between one pair's out and the next pair's in
    for i in range(1, len(times) - 1, 2):
        pause += int((times[i + 1] - times[i]).total_seconds())

    incomplete = (len(times) == 0) or (len(times) % 2 == 1)
    first_in = rows[0]["punched_at"] if rows else None
    last_out = rows[-1]["punched_at"] if len(times) % 2 == 0 and rows else None

    return DaySummary(work_date, first_in, last_out, worked, pause,
                      supposed_seconds, methods, incomplete, rows)


# ── employees (admin) ─────────────────────────────────────────────────────────

def get_employee(employee_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, name, active FROM employees WHERE id=?",
            (employee_id,)).fetchone()


def delete_employee(employee_id: int) -> None:
    """Delete an employee and everything belonging to them: punches, chips,
    absences, monthly targets, absent profiles, custom sounds and any armed
    enrollment.

    Irreversible, and it destroys the working-time record -- the caller is
    expected to have re-checked the admin password first. Children go before
    the parent because foreign_keys is ON, and it is one transaction so a
    failure part-way cannot leave an employee half-deleted.

    Every table with an employee_id must be listed here: a missed one is not a
    leak but a hard FOREIGN KEY failure that makes the employee undeletable.
    """
    with get_conn() as conn:
        conn.execute("DELETE FROM punches WHERE employee_id=?", (employee_id,))
        conn.execute("DELETE FROM chips WHERE employee_id=?", (employee_id,))
        conn.execute("DELETE FROM absences WHERE employee_id=?", (employee_id,))
        conn.execute("DELETE FROM monthly_targets WHERE employee_id=?", (employee_id,))
        conn.execute("DELETE FROM absent_profiles WHERE employee_id=?", (employee_id,))
        conn.execute("DELETE FROM employee_sounds WHERE employee_id=?", (employee_id,))
        conn.execute("DELETE FROM enroll_requests WHERE employee_id=?", (employee_id,))
        conn.execute("DELETE FROM employees WHERE id=?", (employee_id,))


# ── custom in/out sounds (per employee) ───────────────────────────────────────

def _check_direction(direction: str) -> None:
    """direction reaches here straight from a form field, and it goes into a
    CHECK-constrained column -- catch it as a plain error rather than an
    IntegrityError from three frames down."""
    if direction not in ("in", "out"):
        raise ValueError(f"direction must be 'in' or 'out', got {direction!r}")


def set_employee_sound(employee_id: int, direction: str, filename: str,
                       start_s: float, end_s: float, source_seconds: float,
                       audio: bytes, seconds: float) -> None:
    """Store an employee's own clock-in/out sound, replacing any previous one.

    ``audio`` is the finished clip from buzzer.make_clip -- already cut to
    start_s..end_s and decoded to the format the reader plays. The times are
    stored alongside it purely so the UI can say which part was taken.
    """
    _check_direction(direction)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO employee_sounds"
            " (employee_id, direction, filename, start_s, end_s, source_seconds,"
            "  audio, seconds, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(employee_id, direction) DO UPDATE SET"
            "   filename=excluded.filename,"
            "   start_s=excluded.start_s, end_s=excluded.end_s,"
            "   source_seconds=excluded.source_seconds,"
            "   audio=excluded.audio, seconds=excluded.seconds,"
            "   updated_at=excluded.updated_at",
            (employee_id, direction, filename, start_s, end_s, source_seconds,
             audio, seconds, _now()))


def clear_employee_sound(employee_id: int, direction: str) -> None:
    """Drop a custom sound; the employee goes back to the standard beep."""
    _check_direction(direction)
    with get_conn() as conn:
        conn.execute("DELETE FROM employee_sounds WHERE employee_id=? AND direction=?",
                     (employee_id, direction))


def get_employee_sound(employee_id: int, direction: str):
    """The audio to play for this punch, or None to use the standard beep.
    Called by the scan station on every recorded punch, so it selects only the
    clip -- never the much larger source blob."""
    _check_direction(direction)
    with get_conn() as conn:
        return conn.execute(
            "SELECT audio, seconds, filename FROM employee_sounds"
            " WHERE employee_id=? AND direction=?",
            (employee_id, direction)).fetchone()


def list_employee_sounds(employee_id: int) -> dict:
    """{'in': row, 'out': row} for the admin UI. Deliberately selects no audio
    -- the page only needs names, times and sizes, and pulling the clips into a
    template render would be pure waste."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT direction, filename, seconds, start_s, end_s, source_seconds,"
            "       updated_at, length(audio) AS bytes"
            " FROM employee_sounds WHERE employee_id=?", (employee_id,)).fetchall()
    return {r["direction"]: r for r in rows}


def rename_employee(employee_id: int, name: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE employees SET name=? WHERE id=?", (name, employee_id))


def set_active(employee_id: int, active: bool) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE employees SET active=? WHERE id=?",
                     (1 if active else 0, employee_id))


def list_chips(employee_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, serial FROM chips WHERE employee_id=? ORDER BY serial",
            (employee_id,)).fetchall()


# ── absences (per employee) ───────────────────────────────────────────────────

def set_absence(employee_id: int, date_str: str, kind: str, note: str = "") -> None:
    """kind is 'sick' or 'vacation'. Upserts one absence per employee per day."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO absences (employee_id, date, kind, note) VALUES (?,?,?,?)"
            " ON CONFLICT(employee_id, date) DO UPDATE SET"
            " kind=excluded.kind, note=excluded.note",
            (employee_id, date_str, kind, note))


def remove_absence(employee_id: int, date_str: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM absences WHERE employee_id=? AND date=?",
                     (employee_id, date_str))


def set_absence_range(employee_id: int, start: str, end: str,
                      kind: str, note: str = "") -> int:
    """Apply (or clear, when kind=='none') an absence to every day in the
    inclusive range start..end. Returns the number of days touched."""
    d0 = datetime.strptime(start, DATE_FMT).date()
    d1 = datetime.strptime(end, DATE_FMT).date()
    if d1 < d0:
        d0, d1 = d1, d0
    n = 0
    with get_conn() as conn:
        cur = d0
        while cur <= d1:
            ds = cur.strftime(DATE_FMT)
            if kind == "none":
                conn.execute("DELETE FROM absences WHERE employee_id=? AND date=?",
                             (employee_id, ds))
            else:
                conn.execute(
                    "INSERT INTO absences (employee_id, date, kind, note) VALUES (?,?,?,?)"
                    " ON CONFLICT(employee_id, date) DO UPDATE SET"
                    " kind=excluded.kind, note=excluded.note",
                    (employee_id, ds, kind, note))
            cur += timedelta(days=1)
            n += 1
    return n


def get_absences(employee_id: int, year: int, month: int) -> dict:
    prefix = f"{year:04d}-{month:02d}-"
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT date, kind, note FROM absences"
            " WHERE employee_id=? AND date LIKE ?",
            (employee_id, prefix + "%")).fetchall()
    return {r["date"]: r for r in rows}


# ── special days (company-wide holidays / closures) ───────────────────────────

def set_special_day(date_str: str, kind: str, label: str = "") -> None:
    """kind is 'holiday' or 'closed'."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO special_days (date, kind, label) VALUES (?,?,?)"
            " ON CONFLICT(date) DO UPDATE SET kind=excluded.kind, label=excluded.label",
            (date_str, kind, label))


def remove_special_day(date_str: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM special_days WHERE date=?", (date_str,))


def set_special_day_range(start: str, end: str, kind: str, label: str = "") -> int:
    """Mark every day in the inclusive range start..end, e.g. a works holiday
    or a shutdown week. kind is 'holiday', 'closed', or 'none' to clear.

    Stored as one row per day rather than as a period: every other part of the
    system asks "what is this date?", and a stored range would mean teaching all
    of them about overlaps. Returns the number of days touched.
    """
    d0 = datetime.strptime(start, DATE_FMT).date()
    d1 = datetime.strptime(end, DATE_FMT).date()
    if d1 < d0:
        d0, d1 = d1, d0
    n = 0
    with get_conn() as conn:
        cur = d0
        while cur <= d1:
            ds = cur.strftime(DATE_FMT)
            if kind == "none":
                conn.execute("DELETE FROM special_days WHERE date=?", (ds,))
            else:
                conn.execute(
                    "INSERT INTO special_days (date, kind, label) VALUES (?,?,?)"
                    " ON CONFLICT(date) DO UPDATE SET"
                    " kind=excluded.kind, label=excluded.label",
                    (ds, kind, label))
            n += 1
            cur += timedelta(days=1)
    return n


def get_special_days(year: int, month: int) -> dict:
    """Concrete 'YYYY-MM-DD' -> special day for one month.

    Merges the yearly recurring entries (expanded to this year) with the one-off
    ones. A one-off row for the same date wins, so a given year can override or
    cancel a recurring rule without touching the rule itself.
    """
    out = {}
    with get_conn() as conn:
        for r in conn.execute(
                "SELECT md, kind, label FROM recurring_special_days WHERE md LIKE ?",
                (f"{month:02d}-%",)).fetchall():
            try:
                ds = _date(year, month, int(r["md"][3:5])).strftime(DATE_FMT)
            except ValueError:
                continue  # e.g. a 02-29 rule in a non-leap year
            out[ds] = {"date": ds, "kind": r["kind"], "label": r["label"],
                       "recurring": True}
        for r in conn.execute(
                "SELECT date, kind, label FROM special_days WHERE date LIKE ?",
                (f"{year:04d}-{month:02d}-%",)).fetchall():
            out[r["date"]] = {"date": r["date"], "kind": r["kind"],
                              "label": r["label"], "recurring": False}
    return out


def list_special_days():
    with get_conn() as conn:
        return conn.execute(
            "SELECT date, kind, label FROM special_days ORDER BY date DESC").fetchall()


# ── recurring special days (same calendar day every year, e.g. 24.12) ─────────

def set_recurring_special_day(month: int, day: int, kind: str,
                              label: str = "") -> None:
    """kind is 'holiday' or 'closed'. Raises ValueError on an impossible day."""
    month, day = int(month), int(day)
    # 2024 is a leap year, so 29.02 validates and is kept as a rule; it is then
    # simply skipped in years where it does not exist.
    _date(2024, month, day)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO recurring_special_days (md, kind, label) VALUES (?,?,?)"
            " ON CONFLICT(md) DO UPDATE SET kind=excluded.kind, label=excluded.label",
            (f"{month:02d}-{day:02d}", kind, label))


def remove_recurring_special_day(md: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM recurring_special_days WHERE md=?", (md,))


def list_recurring_special_days():
    with get_conn() as conn:
        return conn.execute(
            "SELECT md, kind, label FROM recurring_special_days ORDER BY md").fetchall()


# ── monthly target + workdays (per employee, per month) ───────────────────────

def set_monthly_target(employee_id: int, year: int, month: int,
                       target_hours: float, workdays: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO monthly_targets (employee_id, year, month, target_hours, workdays)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(employee_id, year, month) DO UPDATE SET"
            " target_hours=excluded.target_hours, workdays=excluded.workdays",
            (employee_id, year, month, target_hours, workdays))


def get_monthly_target(employee_id: int, year: int, month: int):
    """Return (target_hours, workdays_csv, configured).

    Carries forward: if this exact month isn't set, use the most recent earlier
    month that was, so the admin doesn't re-enter it every month.
    """
    ym = year * 12 + (month - 1)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT year, month, target_hours, workdays FROM monthly_targets"
            " WHERE employee_id=?", (employee_id,)).fetchall()
    best = None
    for r in rows:
        rym = r["year"] * 12 + (r["month"] - 1)
        if rym <= ym and (best is None or rym > best[0]):
            best = (rym, r)
    if best is None:
        return (0.0, DEFAULT_WORKDAYS, False)
    r = best[1]
    return (r["target_hours"], r["workdays"], True)


# ── absent profiles (credited hours without scanning, e.g. home office) ───────

def _encode_hours_map(hours_by_weekday: dict) -> str:
    """{0: 5, 1: 2} -> '0:5,1:2'. Weekdays with no hours are dropped, so an
    all-zero profile encodes to '' -- explicitly off."""
    parts = []
    for wd in sorted(hours_by_weekday):
        h = float(hours_by_weekday[wd])
        if h > 0:
            parts.append(f"{int(wd)}:{h:g}")
    return ",".join(parts)


def _decode_hours_map(s: str) -> dict:
    """'0:5,1:2' -> {0: 5.0, 1: 2.0}. Unparseable entries are skipped rather
    than raising: a bad row must not take the month view down."""
    out = {}
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        wd, _, h = part.partition(":")
        try:
            wd, h = int(wd), float(h)
        except ValueError:
            continue
        if 0 <= wd <= 6 and h > 0:
            out[wd] = h
    return out


def set_absent_profile(employee_id: int, year: int, month: int,
                       hours_by_weekday: dict, label: str = "") -> None:
    """Set the profile for one month, e.g. {0: 5, 1: 2} for 5h Mon and 2h Tue.

    An empty/all-zero mapping turns the profile off for this month onwards
    without disturbing earlier months -- the row still exists, which is what
    stops get_absent_profile carrying an older month forward over it.
    """
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO absent_profiles (employee_id, year, month, hours_map, label)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(employee_id, year, month) DO UPDATE SET"
            " hours_map=excluded.hours_map, label=excluded.label",
            (employee_id, year, month, _encode_hours_map(hours_by_weekday), label))


def get_absent_profile(employee_id: int, year: int, month: int):
    """Return (hours_by_weekday, label, configured).

    hours_by_weekday maps weekday int (Mon=0) -> credited hours, holding only
    the days that actually have hours. Carries forward like get_monthly_target:
    a standing arrangement keeps applying until a later month changes it.
    """
    ym = year * 12 + (month - 1)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT year, month, hours_map, label FROM absent_profiles"
            " WHERE employee_id=?", (employee_id,)).fetchall()
    best = None
    for r in rows:
        rym = r["year"] * 12 + (r["month"] - 1)
        if rym <= ym and (best is None or rym > best[0]):
            best = (rym, r)
    if best is None:
        return ({}, "", False)
    r = best[1]
    return (_decode_hours_map(r["hours_map"]), r["label"] or "", True)


# ── punch editing (admin: "modify when he scanned his chip") ───────────────────

def get_day_punches(employee_id: int, work_date: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, punched_at, method FROM punches"
            " WHERE employee_id=? AND work_date=? ORDER BY punched_at",
            (employee_id, work_date)).fetchall()


def add_manual_punch(employee_id: int, punched_at: str) -> None:
    """punched_at: 'YYYY-MM-DD HH:MM:SS'. Recorded with method 'manual'."""
    dt = datetime.strptime(punched_at, TS_FMT)
    with get_conn() as conn:
        _insert_punch(conn, employee_id, "manual", dt)


def update_punch(punch_id: int, punched_at: str) -> None:
    """Move a punch to a different time, re-labelling it 'manual'.

    Once an admin changes the time, the row no longer says what the reader saw
    -- so it stops counting as a chip scan. The method column is the audit
    trail separating what actually happened at the reader from what someone
    decided afterwards, and a silently-edited 'chip' punch would destroy that.
    """
    dt = datetime.strptime(punched_at, TS_FMT)
    with get_conn() as conn:
        conn.execute(
            "UPDATE punches SET punched_at=?, work_date=?, method='manual'"
            " WHERE id=?",
            (dt.strftime(TS_FMT), dt.strftime(DATE_FMT), punch_id))


def delete_punch(punch_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM punches WHERE id=?", (punch_id,))


# ── admin auth / config ───────────────────────────────────────────────────────

def _config(key: str):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM admin_config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def verify_admin(password: str) -> bool:
    salt = _config("admin_salt")
    return salt is not None and _hash_pin(password, salt) == _config("admin_hash")


def set_admin_password(password: str) -> None:
    salt = secrets.token_hex(16)
    with get_conn() as conn:
        conn.execute("UPDATE admin_config SET value=? WHERE key='admin_salt'", (salt,))
        conn.execute("UPDATE admin_config SET value=? WHERE key='admin_hash'",
                     (_hash_pin(password, salt),))


def get_secret_key() -> str:
    return _config("secret_key")


# ── monthly summary (the heart of the admin view) ─────────────────────────────

def _pair_day(times: list) -> tuple:
    """Given sorted datetimes, return (worked_s, pause_s, first, last, incomplete)."""
    worked = sum(int((times[i + 1] - times[i]).total_seconds())
                 for i in range(0, len(times) - 1, 2))
    pause = sum(int((times[i + 1] - times[i]).total_seconds())
                for i in range(1, len(times) - 1, 2))
    incomplete = (len(times) == 0) or (len(times) % 2 == 1)
    first = times[0] if times else None
    last = times[-1] if (times and len(times) % 2 == 0) else None
    return worked, pause, first, last, incomplete


DayRow = namedtuple(
    "DayRow",
    "date weekday category in_time out_time pause_s worked_s school_s supposed_s"
    " methods incomplete note")


def month_summary(employee_id: int, year: int, month: int) -> dict:
    """Per-day rows + monthly totals for one employee, applying the accounting:

    per_day = target_hours / (scheduled workdays in month)
      - holiday / closed : supposed 0 (removed), nothing credited
      - sick             : supposed 0 (removed), hours added to sick tally
      - vacation         : supposed = per_day, hours credited on their own line
      - profile          : supposed = per_day, the profile's fixed hours for
        that weekday are credited without scanning (home office). Hours are per
        weekday, e.g. 5h Mon and 2h Tue. It is a baseline, not a replacement:
        any time actually scanned that day is added on top, so a 4h profile
        plus 3h scanned counts as 7h.
      - worked hours     : physically-present time on ANY day (overtime counts)
    """
    target_hours, workdays_csv, configured = get_monthly_target(employee_id, year, month)
    workday_set = {int(x) for x in workdays_csv.split(",") if x != ""}

    days_in_month = calendar.monthrange(year, month)[1]
    scheduled = sum(1 for d in range(1, days_in_month + 1)
                    if _date(year, month, d).weekday() in workday_set)
    per_day_s = (target_hours * 3600 / scheduled) if scheduled else 0

    specials = get_special_days(year, month)
    absences = get_absences(employee_id, year, month)
    profile_hours, profile_label, _profile_set = \
        get_absent_profile(employee_id, year, month)

    # all punches for the month, grouped by day
    prefix = f"{year:04d}-{month:02d}-"
    with get_conn() as conn:
        punch_rows = conn.execute(
            "SELECT punched_at, work_date, method FROM punches"
            " WHERE employee_id=? AND work_date LIKE ? ORDER BY punched_at",
            (employee_id, prefix + "%")).fetchall()
    by_day = {}
    for p in punch_rows:
        by_day.setdefault(p["work_date"], []).append(p)

    rows = []
    totals = dict(worked_s=0, supposed_s=0, sick_s=0, vacation_s=0, profile_s=0,
                  worked_days=0, supposed_days=0, sick_days=0, vacation_days=0,
                  profile_days=0, holiday_days=0, closed_days=0)

    for d in range(1, days_in_month + 1):
        dt = _date(year, month, d)
        ds = dt.strftime(DATE_FMT)
        wd = dt.weekday()
        scheduled_day = wd in workday_set
        special = specials.get(ds)
        absence = absences.get(ds)

        day_punches = by_day.get(ds, [])
        times = [datetime.strptime(p["punched_at"], TS_FMT) for p in day_punches]
        methods = sorted({p["method"] for p in day_punches})
        worked_s, pause_s, first, last, incomplete = _pair_day(times)
        if not times:
            incomplete = False  # nothing to complete on a day with no punches

        supposed_s = 0
        school_s = 0        # credited school hours for this day, if any
        note = special["label"] if special else (absence["note"] if absence else "")

        if special and special["kind"] in ("holiday", "closed"):
            category = special["kind"]          # removed: supposed stays 0
            if special["kind"] == "holiday":
                totals["holiday_days"] += 1
            else:
                totals["closed_days"] += 1
        elif absence and absence["kind"] == "sick":
            category = "sick"                   # removed + tallied
            if scheduled_day:
                totals["sick_s"] += per_day_s
                totals["sick_days"] += 1
        elif absence and absence["kind"] == "vacation":
            category = "vacation"               # credited toward target
            if scheduled_day:
                supposed_s = per_day_s
                totals["vacation_s"] += per_day_s
                totals["vacation_days"] += 1
        elif scheduled_day and profile_hours.get(wd, 0) > 0:
            # School time: this weekday's credit. Applies whether or not they
            # scanned -- worked_s from any punches is added to the totals below,
            # on top of this credit.
            category = "profile"
            school_s = profile_hours[wd] * 3600
            supposed_s = per_day_s
            totals["profile_s"] += school_s
            totals["profile_days"] += 1
            note = note or profile_label
        elif scheduled_day:
            category = "work"
            supposed_s = per_day_s
        else:
            category = "off"                    # weekend / non-workday

        totals["worked_s"] += worked_s
        totals["supposed_s"] += supposed_s
        if worked_s > 0:
            totals["worked_days"] += 1
        if supposed_s > 0:
            totals["supposed_days"] += 1

        rows.append(DayRow(
            ds, WEEKDAY_NAMES[wd], category,
            first.strftime("%H:%M") if first else "",
            last.strftime("%H:%M") if last else "",
            pause_s, worked_s, school_s, supposed_s, methods, incomplete, note))

    # fulfilled = physically worked + credited vacation + credited profile days
    # (holidays/sick removed)
    totals["fulfilled_s"] = (totals["worked_s"] + totals["vacation_s"]
                             + totals["profile_s"])
    totals["balance_s"] = totals["fulfilled_s"] - totals["supposed_s"]
    return dict(rows=rows, totals=totals, target_hours=target_hours,
                workdays=workday_set, per_day_s=per_day_s,
                scheduled_workdays=scheduled, configured=configured,
                profile_hours=profile_hours, profile_label=profile_label)


if __name__ == "__main__":
    init_db()
    print(f"Initialised {DB_PATH}")
