"""Capture chip UUIDs from a keyboard-emulation NFC reader.

The reader acts as a USB keyboard: it "types" the chip's UUID as hex characters
and presses Enter after each scan. This module turns that into a stream of UUID
strings, one per scan, via ``read_uuids()``.

Two capture modes:
  * evdev  - read the reader's input device directly and grab it, so scans work
             headless (systemd service, no terminal) and don't leak to the
             console/getty. Needs python3-evdev and read access to /dev/input.
  * stdin  - read lines from standard input; only works when the program runs
             in a terminal that has keyboard focus.

Mode is chosen by the READER_MODE env var ('evdev' | 'stdin' | 'auto', default
'auto': try evdev, fall back to stdin). READER_DEVICE selects the reader when
several input devices exist -- either a /dev/input/eventN path or a substring of
the device name (e.g. 'RFID'). READER_GRAB=0 disables grabbing.
"""

import os
import sys
import time

# keycode -> character. Only hex digits are meaningful; everything else is
# ignored. Letters are upper-cased (db normalises anyway, but keep it tidy).
_KEYMAP = {}
for _n in range(10):
    _KEYMAP[f"KEY_{_n}"] = str(_n)
    _KEYMAP[f"KEY_KP{_n}"] = str(_n)
for _c in "ABCDEF":
    _KEYMAP[f"KEY_{_c}"] = _c

_ENTER_KEYS = {"KEY_ENTER", "KEY_KPENTER"}


def _iter_stdin():
    for line in sys.stdin:
        s = line.strip()
        if s:
            yield s


def _resolve_device(hint):
    from evdev import InputDevice, list_devices
    paths = list_devices()
    if not paths:
        raise RuntimeError("no input devices found (need access to /dev/input)")
    # explicit event path
    if hint and hint.startswith("/dev/"):
        return InputDevice(hint)
    devices = [InputDevice(p) for p in paths]
    # match by name substring
    if hint:
        for d in devices:
            if hint.lower() in d.name.lower():
                return d
        raise RuntimeError(f"no input device matching {hint!r}; "
                           f"available: {[d.name for d in devices]}")
    # no hint: prefer something that looks like a reader/keyboard
    for needle in ("rfid", "reader", "card", "keyboard"):
        for d in devices:
            if needle in d.name.lower():
                return d
    return devices[0]


def _grab(dev, grab):
    if grab:
        try:
            dev.grab()  # take exclusive control so scans don't reach the console
        except Exception as e:
            print(f"[reader] could not grab {dev.name}: {e}", file=sys.stderr)


def _wait_for_device(hint, grab):
    """Block until a matching reader is present again, then return it opened.

    Used after an unplug/USB reset. Retries quietly forever -- a scan station
    with no reader has nothing else to do but wait for it to come back."""
    announced = False
    while True:
        try:
            dev = _resolve_device(hint)
        except Exception:
            if not announced:
                print("[reader] waiting for the reader to be plugged back in...",
                      file=sys.stderr)
                announced = True
            time.sleep(1.0)
            continue
        _grab(dev, grab)
        print(f"[reader] reader reconnected: {dev.name} ({dev.path})",
              file=sys.stderr)
        return dev


def _iter_evdev(hint, grab):
    from evdev import categorize, ecodes
    # First open may fail (no reader, no evdev access): let it propagate so
    # read_uuids can fall back to stdin in auto mode. Once we have opened the
    # device once, a later disappearance is an unplug, not a config problem --
    # from then on we reconnect instead of giving up.
    dev = _resolve_device(hint)
    _grab(dev, grab)
    print(f"[reader] using evdev device: {dev.name} ({dev.path})", file=sys.stderr)
    while True:
        try:
            buf = []
            for event in dev.read_loop():
                if event.type != ecodes.EV_KEY:
                    continue
                data = categorize(event)
                if data.keystate != data.key_down:  # only on press
                    continue
                key = data.keycode
                if isinstance(key, (list, tuple)):
                    key = key[0]
                if key in _ENTER_KEYS:
                    s = "".join(buf)
                    buf = []
                    if s:
                        yield s
                else:
                    ch = _KEYMAP.get(key)
                    if ch:
                        buf.append(ch)
        except OSError as e:
            # Reader unplugged or the USB port reset: read_loop raises (ENODEV).
            # A half-typed UUID in buf is dropped -- reconnect and start clean.
            print(f"[reader] reader disconnected ({e})", file=sys.stderr)
        finally:
            try:
                dev.ungrab()
            except Exception:
                pass
        dev = _wait_for_device(hint, grab)


def read_uuids(mode=None, device=None, grab=None):
    """Yield chip UUID strings, one per scan. Blocks between scans."""
    mode = (mode or os.environ.get("READER_MODE", "auto")).lower()
    device = device if device is not None else os.environ.get("READER_DEVICE")
    if grab is None:
        grab = os.environ.get("READER_GRAB", "1") != "0"

    if mode == "stdin":
        yield from _iter_stdin()
        return
    try:
        yield from _iter_evdev(device, grab)
    except Exception as e:
        if mode == "evdev":
            raise
        print(f"[reader] evdev unavailable ({e}); falling back to stdin",
              file=sys.stderr)
        yield from _iter_stdin()


if __name__ == "__main__":
    # Quick manual test: prints each UUID as it is scanned.
    print("Scan a chip (Ctrl-C to stop)...", file=sys.stderr)
    for uuid in read_uuids():
        print("scanned:", uuid)
