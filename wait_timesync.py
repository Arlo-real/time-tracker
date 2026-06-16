import subprocess
import time

def is_time_synchronized():
    result = subprocess.run(
        ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
        capture_output=True, text=True
    )
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