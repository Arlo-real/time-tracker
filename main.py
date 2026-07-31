"""Time-tracker scan station.

Reads chip UUIDs from the keyboard-emulation NFC reader (see reader.py) and
records a punch for the matching employee. Feedback is audible only: a buzzer
plugged into the 3.5mm audio jack beeps one way on clock-in, another on
clock-out, a short blip on a duplicate, and a low buzz on error (see buzzer.py).
There is no display or keypad.

The clock is a hard requirement: a punch is only worth storing if its timestamp
is right, and a Pi has no battery-backed RTC, so after a power cut its clock can
be plausibly-but-badly wrong until NTP lands. While the clock is not synced we
refuse to record anything and sound the alarm pattern on every scan; a
background thread keeps re-checking and recording resumes by itself once the
clock is trusted again.

Run:  python3 main.py   (set READER_MODE/READER_DEVICE env vars as needed)
"""

import threading
import time

import buzzer
import db
import reader
import wait_timesync

# How often the background thread re-checks the clock.
CLOCK_CHECK_SECONDS = 15

# Set only while the system clock is NTP-synced. Nothing is recorded unless set.
_clock_ok = threading.Event()


def _clock_watchdog():
    """Re-check the clock forever, flipping _clock_ok. Never dies: a dead
    watchdog would leave the station refusing punches with no way back."""
    while True:
        try:
            synced = wait_timesync.is_time_synchronized()
            if synced and not _clock_ok.is_set():
                _clock_ok.set()
                print("[main] clock synced; recording enabled", flush=True)
            elif not synced and _clock_ok.is_set():
                _clock_ok.clear()
                print("[main] clock lost sync; recording disabled", flush=True)
        except Exception as e:
            print(f"[main] clock check failed: {e}", flush=True)
        time.sleep(CLOCK_CHECK_SECONDS)


def _confirm(res):
    """Sound the confirmation for a stored punch, trying in order: the employee's
    own sound, then the company default, then the standard in/out beep.

    Each step falls through to the next whenever the sound doesn't play. That
    confirmation is the employee's only evidence the punch was stored, so a bad
    clip must cost them their nice noise -- never their confirmation, which is
    why the plain beep is always the last resort.
    """
    try:
        snd = (db.get_employee_sound(res.employee_id, res.direction)
               or db.get_default_sound(res.direction))
        if snd is not None and buzzer.play_wav(snd["audio"], snd["seconds"]):
            return
    except Exception as e:
        print(f"[main] custom sound failed for {res.name}: {e}", flush=True)
    buzzer.beep_in() if res.direction == "in" else buzzer.beep_out()


def handle(serial):
    """Record one scan and sound the matching buzzer feedback."""
    # Enrollment first, and deliberately not behind the clock gate: linking a
    # chip stores no timestamp, so an unsynced clock is no reason to block it.
    # Returns None unless enrollment is armed AND this chip is unassigned, so
    # normal punches are unaffected while a colleague is being enrolled.
    enrolled = db.try_enroll_scan(serial)
    if enrolled is not None:
        print(f"[main] enrolled chip {serial!r} -> {enrolled.name}", flush=True)
        buzzer.beep_enrolled()
        return

    if not _clock_ok.is_set():
        # Untrusted clock: storing this punch would mean a wrong timestamp, so
        # drop it and make sure whoever scanned can hear that it didn't count.
        print(f"[main] clock not synced; refused scan {serial!r}", flush=True)
        buzzer.beep_alarm()
        return

    res = db.record_scan(serial)
    # Log every scan: the buzzer is the only other feedback, so without this an
    # unrecognised chip fails completely silently and there is nothing to debug.
    detail = f" ({res.name}, {res.direction})" if res.status == "recorded" else ""
    print(f"[main] scan {serial!r} -> {res.status}{detail}", flush=True)

    if res.status == "recorded":
        # direction is decided by db.record_scan in the same transaction as the
        # insert, so it always matches the stored punch.
        _confirm(res)
    elif res.status == "ignored_duplicate":
        buzzer.beep_duplicate()
    else:  # unknown_chip or any unexpected status
        buzzer.beep_error()


def main():
    db.init_db()

    # Sound off once the database is open but before the clock gate: this says
    # "the software is up and the DB is healthy", which is exactly the part a
    # silent box leaves you guessing about after a power cut. Waiting until
    # after wait_for_sync() would be worse -- that call can block for a minute,
    # so the chime would arrive too late to mean "it booted".
    buzzer.beep_startup()

    if wait_timesync.wait_for_sync():
        _clock_ok.set()
    else:
        print("[main] time sync failed; refusing to record until the clock "
              "is trusted", flush=True)
        buzzer.beep_alarm()

    threading.Thread(target=_clock_watchdog, daemon=True).start()
    print("[main] scan station ready", flush=True)

    for serial in reader.read_uuids():
        # One bad scan (e.g. a transient DB lock) must not take the station
        # down: log it, buzz an error, and keep reading.
        try:
            handle(serial)
        except Exception as e:
            print(f"[main] scan error for {serial!r}: {e}", flush=True)
            try:
                buzzer.beep_error()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
