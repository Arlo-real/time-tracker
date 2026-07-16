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


def _iter_evdev(hint, grab):
    from evdev import categorize, ecodes
    dev = _resolve_device(hint)
    print(f"[reader] using evdev device: {dev.name} ({dev.path})", file=sys.stderr)
    if grab:
        dev.grab()  # take exclusive control so scans don't reach the console
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
    finally:
        if grab:
            try:
                dev.ungrab()
            except Exception:
                pass


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
