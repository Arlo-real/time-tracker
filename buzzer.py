"""Audio-jack buzzer feedback for the scan station.

A passive buzzer (or small speaker) is plugged into the Pi's 3.5mm audio jack.
We give feedback by playing short tones through the sound card -- no GPIO, no
extra Python packages: tones are synthesised as a WAV in memory and piped to
``aplay`` (from alsa-utils, already present on Raspberry Pi OS).

Distinct patterns so the sound alone tells the employee what happened:
  * clock IN    -> two short rising beeps  (ok, you're in)
  * clock OUT   -> two short falling beeps  (ok, you're out)
  * duplicate   -> one short neutral blip  (already scanned, nothing stored)
  * enrolled    -> three quick rising blips  (chip linked to an employee)
  * error       -> one long low buzz  (unknown chip / failure)
  * alarm       -> three long low buzzes  (clock not trusted, nothing recorded)
"""

import math
import struct
import subprocess
import sys
import wave

_RATE = 44100          # samples per second
_AMPLITUDE = 0.6       # 0..1, headroom to avoid clipping


def _tone_samples(freq, seconds):
    """Yield int16 sample values for a sine tone, with a short fade in/out
    so the buzzer doesn't click at the edges."""
    n = int(_RATE * seconds)
    fade = max(1, int(_RATE * 0.005))  # 5 ms fade
    for i in range(n):
        env = 1.0
        if i < fade:
            env = i / fade
        elif i > n - fade:
            env = (n - i) / fade
        val = _AMPLITUDE * env * math.sin(2 * math.pi * freq * i / _RATE)
        yield int(val * 32767)


def _build_wav(segments):
    """segments: list of (freq_hz, seconds); freq 0 == silence. Returns WAV bytes."""
    import io
    frames = bytearray()
    for freq, seconds in segments:
        if freq <= 0:
            frames += b"\x00\x00" * int(_RATE * seconds)
        else:
            for s in _tone_samples(freq, seconds):
                frames += struct.pack("<h", s)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_RATE)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def _play(segments):
    """Play a sequence of tones, blocking until done. Never raises: audio
    feedback must not take the scan station down."""
    wav = _build_wav(segments)
    # Hard timeout: this runs in the scan loop, so aplay blocking on a busy or
    # misrouted audio device must never wedge the station. A few seconds is far
    # longer than any pattern here (longest is the ~1.6s alarm).
    budget = sum(seconds for _, seconds in segments) + 3.0
    try:
        subprocess.run(
            ["aplay", "-q"],
            input=wav,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=budget,
        )
    except subprocess.TimeoutExpired:
        print("[buzzer] aplay timed out; check audio routing (HDMI vs jack)",
              file=sys.stderr)
    except FileNotFoundError:
        print("[buzzer] aplay not found (install alsa-utils)", file=sys.stderr)
    except Exception as e:
        print(f"[buzzer] playback failed: {e}", file=sys.stderr)


def beep_in():
    """Clocked IN: two short rising beeps."""
    _play([(880, 0.12), (0, 0.05), (1320, 0.16)])


def beep_out():
    """Clocked OUT: two short falling beeps."""
    _play([(1320, 0.12), (0, 0.05), (880, 0.16)])


def beep_duplicate():
    """Already scanned within the debounce window: one short neutral blip."""
    _play([(1000, 0.08)])


def beep_enrolled():
    """Chip linked to an employee: three quick rising blips. Clearly different
    from a punch, so nobody mistakes enrolling for clocking in."""
    _play([(784, 0.09), (0, 0.04), (988, 0.09), (0, 0.04), (1319, 0.14)])


def beep_error():
    """Something went wrong: one long low buzz."""
    _play([(220, 0.6)])


def beep_alarm():
    """Clock is not time-synced, so nothing was recorded: three long low buzzes.
    Deliberately the longest, ugliest pattern -- it means punches are being
    refused and someone needs to fix the network/NTP."""
    _play([(180, 0.45), (0, 0.12), (180, 0.45), (0, 0.12), (180, 0.45)])


if __name__ == "__main__":
    # Quick manual test: play each pattern with a gap between.
    from time import sleep
    print("IN...");       beep_in();        sleep(0.5)
    print("OUT...");      beep_out();       sleep(0.5)
    print("DUP...");      beep_duplicate(); sleep(0.5)
    print("ENROLLED..."); beep_enrolled();  sleep(0.5)
    print("ERROR...");    beep_error();     sleep(0.5)
    print("ALARM...");    beep_alarm()
