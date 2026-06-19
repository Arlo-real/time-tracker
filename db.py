import sqlite3
import hashlib
import json
import os
import secrets
import calendar as _cal
from datetime import datetime, date as _date

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "attendance.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    # FULL (not the WAL default NORMAL) so a committed check-in survives a power cut.
    conn.execute("PRAGMA synchronous=FULL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _hash(value: str, salt: str) -> str:
    return hashlib.sha256((salt + value).encode()).hexdigest()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(action: str, table: str, record_id, old_data, new_data, done_by_id=None):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO audit_log"
            " (timestamp, done_by_id, action, table_name, record_id, old_data, new_data)"
            " VALUES (?,?,?,?,?,?,?)",
            (now_str(), done_by_id, action, table, record_id,
             json.dumps(old_data) if old_data is not None else None,
             json.dumps(new_data) if new_data is not None else None)
        )


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS employees (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT NOT NULL,
                pin_hash  TEXT,
                pin_salt  TEXT,
                active    INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS chips (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL REFERENCES employees(id),
                serial      TEXT UNIQUE NOT NULL
            );
            CREATE TABLE IF NOT EXISTS work_schedules (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id    INTEGER NOT NULL REFERENCES employees(id),
                day_of_week    INTEGER NOT NULL,
                planned_hours  REAL NOT NULL DEFAULT 0,
                effective_from TEXT NOT NULL DEFAULT '1970-01-01'
            );
            CREATE TABLE IF NOT EXISTS attendance (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id  INTEGER NOT NULL REFERENCES employees(id),
                arrived_at   TEXT NOT NULL,
                left_at      TEXT,
                duration_s   INTEGER,
                date         TEXT NOT NULL,
                login_method TEXT NOT NULL DEFAULT 'pin',
                deleted      INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS absences (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id     INTEGER NOT NULL REFERENCES employees(id),
                date            TEXT NOT NULL,
                reason          TEXT NOT NULL,
                added_by_id     INTEGER REFERENCES employees(id),
                added_by_method TEXT,
                deleted         INTEGER NOT NULL DEFAULT 0,
                UNIQUE(employee_id, date)
            );
            CREATE TABLE IF NOT EXISTS special_days (
                date   TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                label  TEXT
            );
            CREATE TABLE IF NOT EXISTS recurring_special_days (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                month        INTEGER NOT NULL,
                reason       TEXT NOT NULL,
                label        TEXT,
                day_of_month INTEGER,
                weekday      INTEGER,
                occurrence   INTEGER
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                done_by_id  INTEGER REFERENCES employees(id),
                action      TEXT NOT NULL,
                table_name  TEXT NOT NULL,
                record_id   INTEGER,
                old_data    TEXT,
                new_data    TEXT
            );
            CREATE TABLE IF NOT EXISTS admin_config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        if not conn.execute("SELECT 1 FROM admin_config WHERE key='admin_hash'").fetchone():
            salt = secrets.token_hex(16)
            conn.execute("INSERT INTO admin_config VALUES ('admin_hash', ?)", (_hash("admin", salt),))
            conn.execute("INSERT INTO admin_config VALUES ('admin_salt', ?)", (salt,))
        for key, default in (("pin_login_enabled", "1"),):
            if not conn.execute("SELECT 1 FROM admin_config WHERE key=?", (key,)).fetchone():
                conn.execute("INSERT INTO admin_config VALUES (?,?)", (key, default))

    _run_migrations()


def _run_migrations():
    conn = sqlite3.connect(DB_PATH)
    for stmt in (
        "ALTER TABLE attendance ADD COLUMN login_method TEXT NOT NULL DEFAULT 'pin'",
        "ALTER TABLE attendance ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE employees ADD COLUMN pin_hash TEXT",
        "ALTER TABLE employees ADD COLUMN pin_salt TEXT",
        "ALTER TABLE employees ADD COLUMN active INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE absences ADD COLUMN added_by_id INTEGER",
        "ALTER TABLE absences ADD COLUMN added_by_method TEXT",
        "ALTER TABLE absences ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            conn.execute(stmt)
            conn.commit()
        except sqlite3.OperationalError:
            pass

    # work_schedules: recreate with effective_from if the old composite-PK schema is present
    cols = {row[1] for row in conn.execute("PRAGMA table_info(work_schedules)").fetchall()}
    if cols and "effective_from" not in cols:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript("""
            CREATE TABLE work_schedules_new (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id    INTEGER NOT NULL,
                day_of_week    INTEGER NOT NULL,
                planned_hours  REAL NOT NULL DEFAULT 0,
                effective_from TEXT NOT NULL DEFAULT '1970-01-01'
            );
            INSERT INTO work_schedules_new (employee_id, day_of_week, planned_hours, effective_from)
                SELECT employee_id, day_of_week, planned_hours, '1970-01-01' FROM work_schedules;
            DROP TABLE work_schedules;
            ALTER TABLE work_schedules_new RENAME TO work_schedules;
        """)
        conn.execute("PRAGMA foreign_keys=ON")

    conn.close()


# ── device entry points ───────────────────────────────────────────────────────

def _toggle(employee_id: int, method: str) -> dict:
    with get_conn() as conn:
        open_s = conn.execute(
            "SELECT id, arrived_at FROM attendance"
            " WHERE employee_id=? AND left_at IS NULL AND deleted=0",
            (employee_id,)
        ).fetchone()
        ts = now_str()
        if open_s:
            arrived_dt = datetime.strptime(open_s["arrived_at"], "%Y-%m-%d %H:%M:%S")
            dur = int((datetime.strptime(ts, "%Y-%m-%d %H:%M:%S") - arrived_dt).total_seconds())
            conn.execute(
                "UPDATE attendance SET left_at=?, duration_s=? WHERE id=?",
                (ts, dur, open_s["id"])
            )
            return {"inout": "out", "worked_s": dur}
        else:
            conn.execute(
                "INSERT INTO attendance (employee_id, arrived_at, date, login_method)"
                " VALUES (?,?,?,?)",
                (employee_id, ts, ts[:10], method)
            )
            return {"inout": "in"}


def handle_pin(pin: str) -> dict:
    """Device calls this when a PIN is submitted."""
    if not is_pin_login_enabled():
        return {"code": 2, "msg": "PIN login disabled"}
    emp = find_employee_by_pin(pin)
    if not emp:
        return {"code": 1, "msg": "Unknown PIN"}
    result = _toggle(emp["id"], "pin")
    result.update({"code": 0, "name": emp["name"]})
    return result


def handle_chip(serial: str) -> dict:
    """Device calls this when a chip is scanned."""
    emp = find_employee_by_chip(serial)
    if not emp:
        return {"code": 1, "msg": "Unknown chip"}
    result = _toggle(emp["id"], "chip")
    result.update({"code": 0, "name": emp["name"]})
    return result


def get_who_is_in() -> list:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT e.name, a.arrived_at
            FROM attendance a JOIN employees e ON e.id=a.employee_id
            WHERE a.left_at IS NULL AND a.deleted=0
            ORDER BY a.arrived_at
        """).fetchall()
    return [{"name": r["name"], "arrived_at": r["arrived_at"]} for r in rows]


def logout_all() -> list:
    """Close every currently-open session at the current time (bulk check-out).
    Returns [{name, worked_s}] for each session closed."""
    ts = now_str()
    now_dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    closed = []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT a.id, a.arrived_at, e.name FROM attendance a"
            " JOIN employees e ON e.id=a.employee_id"
            " WHERE a.left_at IS NULL AND a.deleted=0"
        ).fetchall()
        for r in rows:
            arrived_dt = datetime.strptime(r["arrived_at"], "%Y-%m-%d %H:%M:%S")
            dur = int((now_dt - arrived_dt).total_seconds())
            conn.execute(
                "UPDATE attendance SET left_at=?, duration_s=? WHERE id=?",
                (ts, dur, r["id"])
            )
            closed.append({"name": r["name"], "worked_s": dur})
    return closed


# ── employee management ───────────────────────────────────────────────────────

def add_employee(name: str, pin: str | None = None) -> int:
    salt, h = None, None
    if pin:
        salt = secrets.token_hex(16)
        h = _hash(pin, salt)
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO employees (name, pin_hash, pin_salt) VALUES (?,?,?)", (name, h, salt)
        )
        return cur.lastrowid


def remove_employee(employee_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE employees SET active=0 WHERE id=?", (employee_id,))


def list_employees(include_inactive: bool = False) -> list:
    with get_conn() as conn:
        where = "" if include_inactive else "WHERE active=1"
        rows = conn.execute(
            f"SELECT id, name, active FROM employees {where} ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def is_pin_taken(pin: str, exclude_id: int | None = None) -> bool:
    """Returns True if another active employee already uses this PIN."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, pin_hash, pin_salt FROM employees WHERE active=1 AND pin_hash IS NOT NULL"
        ).fetchall()
    for row in rows:
        if exclude_id is not None and row["id"] == exclude_id:
            continue
        if _hash(pin, row["pin_salt"]) == row["pin_hash"]:
            return True
    return False


def find_employee_by_pin(pin: str) -> dict | None:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, pin_hash, pin_salt FROM employees WHERE active=1 AND pin_hash IS NOT NULL"
        ).fetchall()
    for row in rows:
        if _hash(pin, row["pin_salt"]) == row["pin_hash"]:
            return {"id": row["id"], "name": row["name"]}
    return None


def find_employee_by_chip(serial: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT e.id, e.name FROM chips c
            JOIN employees e ON e.id=c.employee_id
            WHERE c.serial=? AND e.active=1
        """, (serial,)).fetchone()
    return {"id": row["id"], "name": row["name"]} if row else None


# ── chips ─────────────────────────────────────────────────────────────────────

def link_chip(employee_id: int, serial: str) -> dict:
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO chips (employee_id, serial) VALUES (?,?)", (employee_id, serial)
            )
        return {"ok": True}
    except sqlite3.IntegrityError:
        return {"ok": False, "msg": "Chip already linked to another employee"}


def unlink_chip(serial: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM chips WHERE serial=?", (serial,))


def list_chips(employee_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT serial FROM chips WHERE employee_id=?", (employee_id,)
        ).fetchall()
    return [r["serial"] for r in rows]


# ── PIN management ────────────────────────────────────────────────────────────

def reset_pin(employee_id: int, new_pin: str) -> None:
    salt = secrets.token_hex(16)
    with get_conn() as conn:
        conn.execute(
            "UPDATE employees SET pin_hash=?, pin_salt=? WHERE id=?",
            (_hash(new_pin, salt), salt, employee_id)
        )


def change_pin(employee_id: int, old_pin: str, new_pin: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT pin_hash, pin_salt FROM employees WHERE id=?", (employee_id,)
        ).fetchone()
    if not row or not row["pin_hash"]:
        return False
    if _hash(old_pin, row["pin_salt"]) != row["pin_hash"]:
        return False
    reset_pin(employee_id, new_pin)
    return True


# ── work schedule ─────────────────────────────────────────────────────────────

def set_work_schedule(employee_id: int, schedule: dict, effective_from: str = "1970-01-01") -> None:
    """schedule = {0: 8.0, …, 6: 0.0}; effective_from = 'YYYY-MM-DD'."""
    with get_conn() as conn:
        for day, hours in schedule.items():
            conn.execute(
                "INSERT INTO work_schedules (employee_id, day_of_week, planned_hours, effective_from)"
                " VALUES (?,?,?,?)",
                (employee_id, int(day), float(hours), effective_from)
            )


def get_work_schedule(employee_id: int, on_date: str | None = None) -> dict:
    if on_date is None:
        on_date = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT ws.day_of_week, ws.planned_hours
            FROM work_schedules ws
            WHERE ws.employee_id = ?
              AND ws.effective_from = (
                  SELECT MAX(ws2.effective_from) FROM work_schedules ws2
                  WHERE ws2.employee_id = ws.employee_id
                    AND ws2.day_of_week = ws.day_of_week
                    AND ws2.effective_from <= ?
              )
        """, (employee_id, on_date)).fetchall()
    return {r["day_of_week"]: r["planned_hours"] for r in rows}


def get_all_schedule_rows(employee_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT day_of_week, planned_hours, effective_from"
            " FROM work_schedules WHERE employee_id=? ORDER BY effective_from",
            (employee_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_schedule_history(employee_id: int) -> list:
    rows = get_all_schedule_rows(employee_id)
    versions: dict = {}
    for r in rows:
        versions.setdefault(r["effective_from"], {})[r["day_of_week"]] = r["planned_hours"]
    return [{"effective_from": ef, "schedule": versions[ef]}
            for ef in sorted(versions, reverse=True)]


# ── absences ──────────────────────────────────────────────────────────────────

def add_absence(employee_id: int, date: str, reason: str,
                added_by_id: int | None = None, added_by_method: str | None = None) -> None:
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM absences WHERE employee_id=? AND date=?", (employee_id, date)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE absences SET reason=?, added_by_id=?, added_by_method=?, deleted=0 WHERE id=?",
                (reason, added_by_id, added_by_method, existing["id"])
            )
            rid = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO absences (employee_id, date, reason, added_by_id, added_by_method)"
                " VALUES (?,?,?,?,?)",
                (employee_id, date, reason, added_by_id, added_by_method)
            )
            rid = cur.lastrowid
    _log("add_absence", "absences", rid,
         None, {"employee_id": employee_id, "date": date, "reason": reason},
         done_by_id=added_by_id)


def remove_absence(employee_id: int, date: str, done_by_id: int | None = None) -> None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, reason FROM absences WHERE employee_id=? AND date=? AND deleted=0",
            (employee_id, date)
        ).fetchone()
        if not row:
            return
        conn.execute("UPDATE absences SET deleted=1 WHERE id=?", (row["id"],))
    _log("remove_absence", "absences", row["id"],
         {"employee_id": employee_id, "date": date, "reason": row["reason"]}, None, done_by_id)


def get_absences(employee_id: int, year: int, month: int) -> dict:
    """Returns {date: {reason, added_by_name, added_by_method}}."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT a.date, a.reason, e.name AS added_by_name, a.added_by_method
            FROM absences a
            LEFT JOIN employees e ON e.id = a.added_by_id
            WHERE a.employee_id=? AND a.date LIKE ? AND a.deleted=0
        """, (employee_id, f"{year:04d}-{month:02d}-%")).fetchall()
    return {
        r["date"]: {
            "reason": r["reason"],
            "added_by_name": r["added_by_name"],
            "added_by_method": r["added_by_method"],
        }
        for r in rows
    }


# ── special days ──────────────────────────────────────────────────────────────

def add_special_day(date: str, reason: str, label: str = "") -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO special_days (date, reason, label) VALUES (?,?,?)
            ON CONFLICT(date) DO UPDATE SET reason=excluded.reason, label=excluded.label
        """, (date, reason, label))


def remove_special_day(date: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM special_days WHERE date=?", (date,))


def list_special_days(year: int | None = None) -> list:
    with get_conn() as conn:
        if year:
            rows = conn.execute(
                "SELECT date, reason, label FROM special_days WHERE date LIKE ? ORDER BY date",
                (f"{year}-%",)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT date, reason, label FROM special_days ORDER BY date"
            ).fetchall()
    return [dict(r) for r in rows]


def get_special_days_in_month(year: int, month: int) -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT date, reason FROM special_days WHERE date LIKE ?",
            (f"{year:04d}-{month:02d}-%",)
        ).fetchall()
    return {r["date"]: r["reason"] for r in rows}


# ── recurring special days ────────────────────────────────────────────────────

def add_recurring_special_day(month: int, reason: str, label: str = "",
                               day_of_month: int | None = None,
                               weekday: int | None = None,
                               occurrence: int | None = None) -> int:
    """
    Fixed date:  day_of_month set, weekday/occurrence None.
    Relative:    weekday + occurrence set, day_of_month None.
      weekday 0=Mon…6=Sun, occurrence 1=first…-1=last.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO recurring_special_days"
            " (month, reason, label, day_of_month, weekday, occurrence)"
            " VALUES (?,?,?,?,?,?)",
            (month, reason, label, day_of_month, weekday, occurrence)
        )
        return cur.lastrowid


def remove_recurring_special_day(rid: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM recurring_special_days WHERE id=?", (rid,))


def list_recurring_special_days() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM recurring_special_days ORDER BY month, day_of_month, weekday, occurrence"
        ).fetchall()
    return [dict(r) for r in rows]


_MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"]
_WEEK_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_ORD = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th", -1: "last", -2: "2nd to last"}


def describe_recurring(row: dict) -> str:
    if row["day_of_month"] is not None:
        return f"Every {_MONTH_NAMES[row['month']]} {row['day_of_month']}"
    occ = _ORD.get(row["occurrence"], str(row["occurrence"]))
    wday = _WEEK_NAMES[row["weekday"]]
    return f"Every {occ} {wday} of {_MONTH_NAMES[row['month']]}"


def resolve_recurring_special_days(year: int, month: int) -> dict:
    """Returns {date_str: reason} for recurring entries that fall in this month."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM recurring_special_days WHERE month=?", (month,)
        ).fetchall()
    result = {}
    for row in rows:
        if row["day_of_month"] is not None:
            try:
                d = _date(year, month, row["day_of_month"])
                result[d.isoformat()] = row["reason"]
            except ValueError:
                pass
        else:
            month_cal = _cal.monthcalendar(year, month)
            days = [week[row["weekday"]] for week in month_cal if week[row["weekday"]] != 0]
            try:
                day_num = days[row["occurrence"] - 1] if row["occurrence"] > 0 else days[row["occurrence"]]
                result[_date(year, month, day_num).isoformat()] = row["reason"]
            except IndexError:
                pass
    return result


def get_all_special_days_in_month(year: int, month: int) -> dict:
    """Fixed special days take priority over recurring ones."""
    result = resolve_recurring_special_days(year, month)
    result.update(get_special_days_in_month(year, month))
    return result


# ── attendance sessions ───────────────────────────────────────────────────────

def get_sessions(employee_id: int, date: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, arrived_at, left_at, duration_s, login_method FROM attendance"
            " WHERE employee_id=? AND date=? AND deleted=0 ORDER BY arrived_at",
            (employee_id, date)
        ).fetchall()
    return [dict(r) for r in rows]


def add_session(employee_id: int, arrived_at: str, left_at: str | None,
                done_by_id: int | None = None) -> int:
    if left_at and left_at < arrived_at:        # entered in reverse → switch
        arrived_at, left_at = left_at, arrived_at
    dur = _duration(arrived_at, left_at)
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO attendance (employee_id, arrived_at, left_at, duration_s, date, login_method)"
            " VALUES (?,?,?,?,?,'manual')",
            (employee_id, arrived_at, left_at, dur, arrived_at[:10])
        )
        rid = cur.lastrowid
    _log("add_session", "attendance", rid, None,
         {"employee_id": employee_id, "arrived_at": arrived_at, "left_at": left_at},
         done_by_id)
    return rid


def edit_session(session_id: int, arrived_at: str, left_at: str | None,
                 done_by_id: int | None = None) -> None:
    with get_conn() as conn:
        old = conn.execute(
            "SELECT arrived_at, left_at FROM attendance WHERE id=?", (session_id,)
        ).fetchone()
        old_data = dict(old) if old else None
        if left_at and left_at < arrived_at:        # entered in reverse → switch
            arrived_at, left_at = left_at, arrived_at
        dur = _duration(arrived_at, left_at)
        conn.execute(
            "UPDATE attendance SET arrived_at=?, left_at=?, duration_s=?, date=? WHERE id=?",
            (arrived_at, left_at, dur, arrived_at[:10], session_id)
        )
    _log("edit_session", "attendance", session_id,
         old_data, {"arrived_at": arrived_at, "left_at": left_at}, done_by_id)


def delete_session(session_id: int, done_by_id: int | None = None) -> None:
    with get_conn() as conn:
        old = conn.execute(
            "SELECT arrived_at, left_at, employee_id FROM attendance WHERE id=?", (session_id,)
        ).fetchone()
        old_data = dict(old) if old else None
        conn.execute("UPDATE attendance SET deleted=1 WHERE id=?", (session_id,))
    _log("delete_session", "attendance", session_id, old_data, None, done_by_id)


def toggle_at(employee_id: int, ts: str, done_by_id: int | None = None) -> dict:
    """Retroactive clock-in/out at a given timestamp (admin fix for a forgotten scan).
    Opens a session if none is open, otherwise closes the open one. Reversible via
    the audit log. Returns {'inout': 'in'|'out', 'session_id', 'worked_s'}."""
    with get_conn() as conn:
        open_s = conn.execute(
            "SELECT id, arrived_at FROM attendance"
            " WHERE employee_id=? AND left_at IS NULL AND deleted=0"
            " ORDER BY arrived_at DESC LIMIT 1",
            (employee_id,)
        ).fetchone()
    if open_s:
        arrived, left = open_s["arrived_at"], ts
        if left < arrived:                      # reversed → switch
            arrived, left = left, arrived
        edit_session(open_s["id"], arrived, left, done_by_id)
        return {"inout": "out", "session_id": open_s["id"], "worked_s": _duration(arrived, left)}
    rid = add_session(employee_id, ts, None, done_by_id)
    return {"inout": "in", "session_id": rid, "worked_s": None}


def _duration(arrived_at: str, left_at: str | None) -> int | None:
    if not left_at:
        return None
    a = datetime.strptime(arrived_at, "%Y-%m-%d %H:%M:%S")
    l = datetime.strptime(left_at, "%Y-%m-%d %H:%M:%S")
    return int((l - a).total_seconds())


# ── audit log / revert ────────────────────────────────────────────────────────

def list_recent_changes(limit: int = 30) -> list:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT al.id, al.timestamp, al.action, al.table_name, al.record_id,
                   al.old_data, al.new_data, e.name AS done_by_name
            FROM audit_log al
            LEFT JOIN employees e ON e.id = al.done_by_id
            ORDER BY al.id DESC LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def revert_change(audit_id: int, done_by_id: int | None = None) -> dict:
    with get_conn() as conn:
        entry = conn.execute("SELECT * FROM audit_log WHERE id=?", (audit_id,)).fetchone()
    if not entry:
        return {"ok": False, "msg": "Change not found"}

    action = entry["action"]
    rid = entry["record_id"]
    old = json.loads(entry["old_data"]) if entry["old_data"] else None

    try:
        with get_conn() as conn:
            if action == "add_session":
                conn.execute("UPDATE attendance SET deleted=1 WHERE id=?", (rid,))
            elif action == "edit_session":
                if not old:
                    return {"ok": False, "msg": "No previous data to restore"}
                dur = _duration(old["arrived_at"], old.get("left_at"))
                conn.execute(
                    "UPDATE attendance SET arrived_at=?, left_at=?, duration_s=?, date=? WHERE id=?",
                    (old["arrived_at"], old.get("left_at"), dur, old["arrived_at"][:10], rid)
                )
            elif action == "delete_session":
                conn.execute("UPDATE attendance SET deleted=0 WHERE id=?", (rid,))
            elif action == "add_absence":
                conn.execute("UPDATE absences SET deleted=1 WHERE id=?", (rid,))
            elif action == "remove_absence":
                conn.execute("UPDATE absences SET deleted=0 WHERE id=?", (rid,))
            else:
                return {"ok": False, "msg": f"Cannot revert '{action}'"}
    except sqlite3.Error as e:
        return {"ok": False, "msg": str(e)}

    _log(f"revert:{action}", entry["table_name"], rid, None, old, done_by_id)
    return {"ok": True}


# ── admin config ──────────────────────────────────────────────────────────────

def is_pin_login_enabled() -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM admin_config WHERE key='pin_login_enabled'"
        ).fetchone()
    return (row["value"] == "1") if row else True


def set_pin_login_enabled(enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO admin_config (key, value) VALUES ('pin_login_enabled', ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("1" if enabled else "0",)
        )


def verify_admin(password: str) -> bool:
    with get_conn() as conn:
        h = conn.execute("SELECT value FROM admin_config WHERE key='admin_hash'").fetchone()
        s = conn.execute("SELECT value FROM admin_config WHERE key='admin_salt'").fetchone()
    if not h or not s:
        return False
    return _hash(password, s["value"]) == h["value"]


def change_admin_password(old_pw: str, new_pw: str) -> bool:
    if not verify_admin(old_pw):
        return False
    salt = secrets.token_hex(16)
    with get_conn() as conn:
        conn.execute("UPDATE admin_config SET value=? WHERE key='admin_hash'", (_hash(new_pw, salt),))
        conn.execute("UPDATE admin_config SET value=? WHERE key='admin_salt'", (salt,))
    return True
