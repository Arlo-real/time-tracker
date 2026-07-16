import subprocess
import sys
import time

def is_time_synchronized():
    """True only if the system clock is NTP-synchronised.

    Fails closed: if timedatectl is missing or errors, we report "not synced"
    rather than raising, because the caller refuses to record punches on an
    untrusted clock and a raised exception would kill the checker instead.
    """
    try:
        result = subprocess.run(
            ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        print("[timesync] timedatectl not found; treating clock as unsynced",
              file=sys.stderr)
        return False
    except Exception as e:
        print(f"[timesync] sync check failed ({e}); treating clock as unsynced",
              file=sys.stderr)
        return False
    return result.stdout.strip() == "yes"

def wait_for_sync(timeout=120, interval=5):
    print("Waiting for time synchronization...")
    elapsed = 0
    while elapsed < timeout:
        if is_time_synchronized():
            print("Time is synchronized!")
            return True
        print(f"Not yet synchronized, retrying in {interval}s... ({elapsed}s elapsed)")
        time.sleep(interval)
        elapsed += interval
    print("Timed out waiting for time synchronization.")
    return False

if __name__ == "__main__":
    wait_for_sync()