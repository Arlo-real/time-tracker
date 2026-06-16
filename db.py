import sqlite3
from datetime import datetime
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "attendance.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # WAL mode = power-loss safe, much faster writes
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS employees (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                code  TEXT UNIQUE NOT NULL,
                name  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attendance (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL REFERENCES employees(id),
                arrived_at  TEXT NOT NULL,
                left_at     TEXT,
                duration_s  INTEGER,
                date        TEXT NOT NULL
            );
        """)

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def check_in(code: str):
    """Call this when an employee enters their code on arrival."""
    with get_conn() as conn:
        emp = conn.execute(
            "SELECT id, name FROM employees WHERE code = ?", (code,)
        ).fetchone()

        if not emp:
            return {"code": 1, "msg": "Unknown code"}

        # Prevent double check-in (no open session)
        open_session = conn.execute("""
            SELECT id FROM attendance
            WHERE employee_id = ? AND left_at IS NULL
        """, (emp["id"],)).fetchone()

        if open_session:
          raise ValueError("check_in called but session already open")  

        ts = now_str()
        conn.execute("""
            INSERT INTO attendance (employee_id, arrived_at, date)
            VALUES (?, ?, ?)
        """, (emp["id"], ts, ts[:10]))

        return {"code": 0, "inout": "in", "name": emp["name"]}

def check_out(code: str):
    """Call this when an employee enters their code on departure."""
    with get_conn() as conn:
        emp = conn.execute(
            "SELECT id, name FROM employees WHERE code = ?", (code,)
        ).fetchone()

        if not emp:
            return {"code": 1, "msg": "Unknown code"}

        session = conn.execute("""
            SELECT id, arrived_at FROM attendance
            WHERE employee_id = ? AND left_at IS NULL
        """, (emp["id"],)).fetchone()

        if not session:
          raise ValueError("check_out called without open session")  

        ts = now_str()
        arrived = datetime.strptime(session["arrived_at"], "%Y-%m-%d %H:%M:%S")
        left    = datetime.strptime(ts,                    "%Y-%m-%d %H:%M:%S")
        duration = int((left - arrived).total_seconds())

        conn.execute("""
            UPDATE attendance
            SET left_at = ?, duration_s = ?
            WHERE id = ?
        """, (ts, duration, session["id"]))

        return {"code": 0, "inout": "out", "name": emp["name"]}
    
def handle_code(code: str):
    """Single entry point — device calls this on every code input."""
    with get_conn() as conn:
        emp = conn.execute(
            "SELECT id FROM employees WHERE code = ?", (code,)
        ).fetchone()
        if not emp:
            return {"code": 1, "msg": "Unknown code"}

        open_session = conn.execute(
            "SELECT id FROM attendance WHERE employee_id = ? AND left_at IS NULL",
            (emp["id"],)
        ).fetchone()

    if open_session:
        return check_out(code)
    else:
        return check_in(code)

def parse_date(s: str) -> str | None:
    """Returns the date string if valid, None if not."""
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        return None

def export_csv(date_from: str, date_to: str, out_path: str):
    """Export a date range to CSV. Dates as 'YYYY-MM-DD'."""
    import csv

    if not parse_date(date_from):
        return {"ok": False, "msg": f"Invalid date_from: '{date_from}'. Use YYYY-MM-DD format."}
    if not parse_date(date_to):
        return {"ok": False, "msg": f"Invalid date_to: '{date_to}'. Use YYYY-MM-DD format."}
    if date_from > date_to:
        return {"ok": False, "msg": f"date_from ({date_from}) must be before date_to ({date_to})"}

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT e.name, e.code, a.date, a.arrived_at, a.left_at,
                   ROUND(a.duration_s / 3600.0, 2) AS hours
            FROM attendance a
            JOIN employees e ON e.id = a.employee_id
            WHERE a.date BETWEEN ? AND ?
            ORDER BY a.date, a.arrived_at
        """, (date_from, date_to)).fetchall()

    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Name", "Code", "Date", "Arrived", "Left", "Hours"])
        writer.writerows(rows)

    return {"ok": True, "msg": f"Exported {len(rows)} rows to {out_path}"}