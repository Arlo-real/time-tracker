"""USB-stick backups for the time tracker.

The database is the record of people's working hours and lives on an SD card,
which is the part of a Pi most likely to die. This copies it to any USB stick
that gets plugged in -- no configuration, no fixed device, no fstab entry, and
whatever filesystem the stick happens to have (vfat/exFAT/NTFS/ext4).

Backups happen:
  * when a stick is plugged in   -> copy, then beep
  * every day at 01:00 localtime -> if a stick is still plugged in
  * on program start (reboot)    -> if a stick is plugged in

Everything is best-effort: no stick, an unreadable stick or a failed mount must
never affect the scan station. Failures are logged and buzzed, never raised.

Two deliberate choices:
  * The stick is mounted only for the seconds it takes to copy, then unmounted.
    It is therefore always safe to pull, and never sits mounted for months.
  * The snapshot is taken with VACUUM INTO to a temp file on the SD card and
    verified there *before* being copied to the stick. A plain copy of a live
    WAL database is torn, and verifying after landing on the stick would mean
    trusting a file we could not check.

Run:
    sudo python3 backup.py --watch      # the daemon (systemd service)
    sudo python3 backup.py --once       # back up now, if a stick is present
    sudo python3 backup.py --list       # show backups found on plugged-in sticks
    sudo python3 backup.py --restore    # interactively restore one (see setup.sh)
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime

import db

BACKUP_DIR = "timetracker-backups"   # created on the stick
KEEP_PER_STICK = 30                  # newest N kept, older pruned
DAILY_HOUR = 1                       # 01:00 localtime
POLL_SECONDS = 3                     # how often we look for a new stick


def log(msg):
    print(f"[backup] {msg}", flush=True)


def _beep(name):
    """Buzzer feedback, if the buzzer is reachable. Never fatal: a backup that
    worked must not be reported as failed because audio is misconfigured."""
    try:
        import buzzer
        getattr(buzzer, name)()
    except Exception as e:
        log(f"buzzer unavailable ({e})")


# ── finding sticks ────────────────────────────────────────────────────────────

def usb_filesystems() -> list:
    """Every mountable filesystem on USB-attached storage.

    Handles both partitioned sticks and ones formatted without a partition
    table. Identity includes uuid/label/size, not just the device path, so
    swapping stick A for stick B is seen even if both land on /dev/sda1.
    """
    try:
        out = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,PATH,TYPE,FSTYPE,TRAN,RM,LABEL,UUID,SIZE"],
            capture_output=True, text=True, timeout=15)
        data = json.loads(out.stdout or "{}")
    except Exception as e:
        log(f"lsblk failed: {e}")
        return []

    found = []
    for disk in data.get("blockdevices", []):
        if disk.get("tran") != "usb":
            continue
        # partitions if it has any, otherwise the whole device
        for part in (disk.get("children") or [disk]):
            if not part.get("fstype"):
                continue  # no filesystem (empty/extended/encrypted partition)
            found.append({
                "path": part["path"],
                "fstype": part["fstype"],
                "label": part.get("label") or "",
                "uuid": part.get("uuid") or "",
                "size": part.get("size") or "",
            })
    return found


def _identity(fs) -> tuple:
    return (fs["path"], fs["uuid"], fs["label"], fs["size"], fs["fstype"])


@contextmanager
def mounted(fs):
    """Mount a filesystem read-write at a temp dir, unmount on the way out.

    Filesystem type is left to the kernel/mount helpers, so vfat, exFAT, NTFS
    and ext4 all work provided the helper packages are installed (setup.sh does
    that). Yields None if the mount did not actually take.
    """
    mp = tempfile.mkdtemp(prefix="ttbackup-")
    ok = False
    try:
        try:
            subprocess.run(["mount", fs["path"], mp],
                           check=True, capture_output=True, text=True, timeout=60)
        except subprocess.CalledProcessError as e:
            log(f"mount {fs['path']} ({fs['fstype']}) failed: "
                f"{(e.stderr or '').strip()}")
            yield None
            return
        except Exception as e:
            log(f"mount {fs['path']} failed: {e}")
            yield None
            return

        # Guard against writing into the bare temp dir on the SD card: if the
        # mount silently did not take, that is where the "backup" would land.
        if not os.path.ismount(mp):
            log(f"{fs['path']} did not actually mount; refusing to write")
            yield None
            return
        ok = True
        yield mp
    finally:
        if ok:
            try:
                subprocess.run(["umount", mp], capture_output=True, timeout=60)
            except Exception as e:
                log(f"umount {mp} failed: {e}")
        try:
            os.rmdir(mp)
        except OSError:
            pass


# ── taking the snapshot ───────────────────────────────────────────────────────

def snapshot(dest: str) -> None:
    """Consistent copy of the live database to `dest`, then verify it.

    VACUUM INTO works on an open WAL database without stopping the scan
    station. Raises if the result does not open or fails integrity_check.
    """
    if os.path.exists(dest):
        os.remove(dest)
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("VACUUM INTO ?", (dest,))

    check = sqlite3.connect(dest)
    try:
        res = check.execute("PRAGMA integrity_check").fetchone()[0]
        if res != "ok":
            raise RuntimeError(f"integrity_check said: {res}")
        # sanity: the tables we care about must be readable
        check.execute("SELECT COUNT(*) FROM punches").fetchone()
        check.execute("SELECT COUNT(*) FROM employees").fetchone()
    finally:
        check.close()


def _prune(dirpath: str) -> None:
    files = sorted(f for f in os.listdir(dirpath)
                   if f.startswith("attendance-") and f.endswith(".db"))
    for old in files[:-KEEP_PER_STICK] if len(files) > KEEP_PER_STICK else []:
        try:
            os.remove(os.path.join(dirpath, old))
            log(f"pruned old backup {old}")
        except OSError as e:
            log(f"could not prune {old}: {e}")


def backup_to_stick(fs, reason: str) -> bool:
    """Snapshot the DB onto one stick. Returns True on success."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"attendance-{stamp}.db"
    tmp = os.path.join(tempfile.gettempdir(), f"tt-snapshot-{stamp}.db")
    try:
        snapshot(tmp)
    except Exception as e:
        log(f"FAILED to snapshot the database: {e}")
        _cleanup(tmp)
        return False

    try:
        with mounted(fs) as mp:
            if mp is None:
                return False
            outdir = os.path.join(mp, BACKUP_DIR)
            os.makedirs(outdir, exist_ok=True)
            shutil.copyfile(tmp, os.path.join(outdir, name))
            os.sync()  # push it to the stick before we unmount
            _prune(outdir)
            size = os.path.getsize(tmp)
        label = fs["label"] or fs["path"]
        log(f"OK ({reason}): {name} -> {label} [{fs['fstype']}], {size} bytes")
        return True
    except Exception as e:
        log(f"FAILED writing to {fs['path']}: {e}")
        return False
    finally:
        _cleanup(tmp)


def _cleanup(path):
    try:
        os.remove(path)
    except OSError:
        pass


def backup_all(reason: str, sticks=None, beep=True) -> int:
    """Back up to every plugged-in stick. Returns how many succeeded."""
    sticks = usb_filesystems() if sticks is None else sticks
    if not sticks:
        log(f"({reason}) no USB stick plugged in; nothing to do")
        return 0
    good = sum(1 for fs in sticks if backup_to_stick(fs, reason))
    if beep:
        _beep("beep_backup" if good else "beep_backup_failed")
    return good


# ── the daemon ────────────────────────────────────────────────────────────────

def watch() -> None:
    """Back up on start, whenever a stick appears, and daily at 01:00."""
    log("watching for USB sticks "
        f"(daily at {DAILY_HOUR:02d}:00, poll {POLL_SECONDS}s)")

    backup_all("startup")
    known = {_identity(fs) for fs in usb_filesystems()}
    last_daily = None

    while True:
        try:
            current = usb_filesystems()
            by_id = {_identity(fs): fs for fs in current}

            new = [fs for ident, fs in by_id.items() if ident not in known]
            if new:
                for fs in new:
                    log(f"stick plugged in: {fs['path']} "
                        f"[{fs['fstype']}] {fs['label']}")
                backup_all("plugged in", sticks=new)
            known = set(by_id)

            now = datetime.now()
            today = now.date()
            if now.hour == DAILY_HOUR and last_daily != today:
                last_daily = today
                backup_all("daily", sticks=current, beep=False)
        except Exception as e:
            # A watcher that dies leaves the system silently unbacked-up.
            log(f"watch loop error: {e}")
        time.sleep(POLL_SECONDS)


# ── listing / restoring ───────────────────────────────────────────────────────

def find_backups() -> list:
    """Every backup file on every plugged-in stick, newest first."""
    out = []
    for fs in usb_filesystems():
        with mounted(fs) as mp:
            if mp is None:
                continue
            d = os.path.join(mp, BACKUP_DIR)
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                if not (f.startswith("attendance-") and f.endswith(".db")):
                    continue
                full = os.path.join(d, f)
                # copy it off the stick now: the caller sees it after unmount
                staged = os.path.join(tempfile.gettempdir(), f"tt-restore-{f}")
                try:
                    shutil.copyfile(full, staged)
                    out.append({"name": f, "staged": staged,
                                "stick": fs["label"] or fs["path"],
                                "size": os.path.getsize(staged)})
                except Exception as e:
                    log(f"could not read {f}: {e}")
    out.sort(key=lambda b: b["name"], reverse=True)
    return out


def restore_interactive() -> int:
    """Pick a backup off a stick and make it the live database.

    The caller (setup.sh) is expected to have stopped the services first. The
    current database is kept as attendance.db.replaced-<stamp> rather than
    deleted, so a mistaken restore is recoverable.
    """
    backups = find_backups()
    if not backups:
        print("No backups found on any plugged-in USB stick.")
        print("(Looking for %s/attendance-*.db)" % BACKUP_DIR)
        return 1

    print("\nBackups found:\n")
    for i, b in enumerate(backups, 1):
        print(f"  {i:2}) {b['name']}   {b['size']:>9} bytes   on {b['stick']}")
    print("   q) cancel\n")

    try:
        choice = input("Restore which? [q] ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return 1
    if not choice or choice.lower() == "q":
        print("Cancelled; nothing changed.")
        return 1
    try:
        pick = backups[int(choice) - 1]
        if int(choice) < 1:
            raise ValueError
    except (ValueError, IndexError):
        print("Not a valid choice; nothing changed.")
        return 1

    # Verify before we touch anything.
    try:
        c = sqlite3.connect(pick["staged"])
        if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("integrity_check failed")
        emps = c.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
        punches = c.execute("SELECT COUNT(*) FROM punches").fetchone()[0]
        c.close()
    except Exception as e:
        print(f"\nThat file is not a usable backup ({e}). Nothing changed.")
        return 1

    print(f"\n{pick['name']} holds {emps} employees and {punches} punches.")
    if input("Replace the live database with it? [yes/N] ").strip().lower() != "yes":
        print("Cancelled; nothing changed.")
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if os.path.exists(db.DB_PATH):
        kept = f"{db.DB_PATH}.replaced-{stamp}"
        shutil.move(db.DB_PATH, kept)
        print(f"Previous database kept at {kept}")
    # A stale WAL belongs to the database we just moved aside, not the new one.
    for suffix in ("-wal", "-shm"):
        _cleanup(db.DB_PATH + suffix)
    shutil.copyfile(pick["staged"], db.DB_PATH)
    _cleanup(pick["staged"])
    print(f"Restored {pick['name']} to {db.DB_PATH}")
    return 0


def main(argv):
    arg = argv[1] if len(argv) > 1 else "--watch"
    if arg == "--watch":
        watch()
    elif arg == "--once":
        return 0 if backup_all("manual") else 1
    elif arg == "--list":
        found = find_backups()
        for b in found:
            print(f"{b['name']}  {b['size']:>9} bytes  on {b['stick']}")
            _cleanup(b["staged"])
        if not found:
            print("No backups found on plugged-in USB sticks.")
    elif arg == "--restore":
        return restore_interactive()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        pass
