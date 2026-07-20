"""Audio-jack buzzer feedback for the scan station.

A passive buzzer (or small speaker) is plugged into the Pi's 3.5mm audio jack.
We give feedback by playing short tones through the sound card -- no GPIO, no
extra Python packages: tones are synthesised as a WAV in memory and piped to
``aplay`` (from alsa-utils, already present on Raspberry Pi OS).

Distinct patterns so the sound alone tells the employee what happened:
  * startup     -> four-note rising chime  (scan station started)
  * clock IN    -> two short rising beeps  (ok, you're in)
  * clock OUT   -> two short falling beeps  (ok, you're out)
  * duplicate   -> one short neutral blip  (already scanned, nothing stored)
  * enrolled    -> three quick rising blips  (chip linked to an employee)
  * backup      -> two identical high blips  (USB backup written)
  * backup fail -> two mid-low buzzes  (USB stick present but backup failed)
  * error       -> one long low buzz  (unknown chip / failure)
  * alarm       -> three long low buzzes  (clock not trusted, nothing recorded)

Employees can also have their own sound for clocking in/out, uploaded from the
admin site; see ``convert_to_wav`` (upload side) and ``play_wav`` (reader side).
"""

import io
import os
import struct
import subprocess
import sys
import tempfile
import wave

_RATE = 44100          # samples per second
# 0..1 peak. Tones are played one at a time and never summed, so a full-scale
# square wave does not clip -- the small margin is for the DAC's sake.
_AMPLITUDE = 0.95

# ── custom per-employee sounds ────────────────────────────────────────────────

# Uploads are decoded once, at upload time, into exactly this format, so the
# reader never has to decode anything mid-scan -- it just pipes bytes to aplay.
SOUND_RATE = 22050     # Hz. Far more bandwidth than a jack buzzer reproduces,
                       # and half the bytes of 44.1k in every database backup.
MAX_SOUND_SECONDS = 5.0  # Anything longer holds up the queue at the reader.


class SoundError(Exception):
    """An upload could not be turned into a playable sound.

    The message is written to be shown to the admin as-is, so it says what to
    do about it rather than what ffmpeg said.
    """


def _wav_seconds(wav: bytes) -> float:
    """Read back a WAV we just produced, checking it is what we expect.

    Verifying at upload time means a broken clip is refused while the admin is
    standing there, instead of becoming silence at the reader weeks later.
    """
    try:
        with wave.open(io.BytesIO(wav)) as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise SoundError("Converted audio came out in the wrong format.")
            return w.getnframes() / float(w.getframerate())
    except wave.Error as e:
        raise SoundError(f"Converted audio is unreadable ({e}).")


def convert_to_wav(data: bytes, max_seconds: float = MAX_SOUND_SECONDS):
    """Decode an uploaded audio file into the one format the reader plays.

    Takes whatever the admin picked (MP3, WAV, OGG, M4A...) and returns
    ``(wav_bytes, seconds)``: mono, 16-bit, SOUND_RATE, trimmed to max_seconds
    and loudness-normalised. Doing this once here rather than at scan time keeps
    the reader's job to "pipe bytes at aplay", and means an unplayable file is
    caught by the person who chose it.

    Raises SoundError with an admin-readable message.
    """
    if not data:
        raise SoundError("That file is empty.")
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "upload")
        dst = os.path.join(tmp, "out.wav")
        with open(src, "wb") as f:
            f.write(data)
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", src,
            "-t", f"{max_seconds:g}",
            "-ac", "1", "-ar", str(SOUND_RATE),
            # People record clips at wildly different levels, and a quiet one is
            # useless next to a noisy machine, so every upload is levelled to
            # the same loudness -- "custom sound" must never mean "can't hear
            # it". I=-5 is far hotter than the -14 used for music: this is a
            # doorbell, not an album, and it has to hold its own against the
            # built-in beeps (loud square waves). TP=-1.0 is a true-peak ceiling,
            # so however hard we push, it cannot clip.
            "-af", "loudnorm=I=-5:TP=-1.0:LRA=5",
            "-c:a", "pcm_s16le", "-f", "wav", dst,
        ]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=60)
        except FileNotFoundError:
            raise SoundError("ffmpeg is not installed — run: sudo apt install ffmpeg")
        except subprocess.TimeoutExpired:
            raise SoundError("That file took too long to convert; try a shorter clip.")
        if p.returncode != 0 or not os.path.exists(dst):
            raise SoundError("That file isn't audio we can read "
                             "(try WAV, MP3, OGG, FLAC or M4A).")
        with open(dst, "rb") as f:
            wav = f.read()
    seconds = _wav_seconds(wav)
    if seconds <= 0:
        raise SoundError("That file contains no audio.")
    return wav, seconds


def play_wav(wav: bytes, seconds: float) -> bool:
    """Play an employee's own sound. Returns False if it did not play, so the
    caller can fall back to the standard beep -- the sound is the employee's
    only confirmation that the punch was stored, so a broken custom clip must
    not turn into silence."""
    return _play_bytes(wav, seconds + 3.0)


def _tone_samples(freq, seconds):
    """Yield int16 samples for one tone, with a short fade in/out so the
    buzzer doesn't click at the edges.

    Square wave rather than sine, for loudness: at the same peak level a square
    carries ~3 dB more RMS energy, and its harmonics sit where a small piezo or
    speaker actually radiates. A sine puts everything at the fundamental, which
    is exactly where a tiny transducer is least efficient -- the same tone comes
    out noticeably quieter for the same peak voltage.
    """
    n = int(_RATE * seconds)
    fade = max(1, int(_RATE * 0.005))  # 5 ms fade
    period = _RATE / freq
    for i in range(n):
        env = 1.0
        if i < fade:
            env = i / fade
        elif i > n - fade:
            env = (n - i) / fade
        high = (i % period) < (period / 2.0)
        val = _AMPLITUDE * env * (1.0 if high else -1.0)
        yield int(val * 32767)


def _build_wav(segments):
    """segments: list of (freq_hz, seconds); freq 0 == silence. Returns WAV bytes."""
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


def _play_bytes(wav, budget):
    """Pipe WAV bytes to aplay, blocking until done. Returns True if it played.
    Never raises: audio feedback must not take the scan station down.

    The timeout is not optional -- this runs in the scan loop, so aplay blocking
    on a busy or misrouted audio device would wedge the station for good.
    """
    try:
        p = subprocess.run(
            ["aplay", "-q"],
            input=wav,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=budget,
        )
        return p.returncode == 0
    except subprocess.TimeoutExpired:
        print("[buzzer] aplay timed out; check audio routing (HDMI vs jack)",
              file=sys.stderr)
    except FileNotFoundError:
        print("[buzzer] aplay not found (install alsa-utils)", file=sys.stderr)
    except Exception as e:
        print(f"[buzzer] playback failed: {e}", file=sys.stderr)
    return False


def _play(segments):
    """Play a sequence of tones, blocking until done."""
    # A few seconds is far longer than any pattern here (longest is the ~1.6s
    # alarm), so the budget only ever fires on a genuinely stuck device.
    budget = sum(seconds for _, seconds in segments) + 3.0
    return _play_bytes(_build_wav(segments), budget)


def beep_startup():
    """Scan station started: a four-note rising chime.

    Four notes is the tell -- no other pattern here has more than three, so a
    startup is never mistaken for a punch or an enrollment. It also means an
    unexpected chime during the day is worth noticing: it says the service
    restarted (crash, power cut) when nobody asked it to.
    """
    _play([(523, 0.10), (659, 0.10), (784, 0.10), (1047, 0.22)])


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


def beep_backup():
    """USB backup written: two identical high blips. Nothing else uses two
    equal high notes, so it is not mistaken for a punch."""
    _play([(1568, 0.10), (0, 0.06), (1568, 0.10)])


def beep_backup_failed():
    """A stick was there but the backup did not happen: two mid-low buzzes.
    Worth a sound of its own -- a silent failure means believing you have
    backups when you do not."""
    _play([(300, 0.25), (0, 0.08), (300, 0.25)])


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
    print("STARTUP...");     beep_startup();       sleep(0.5)
    print("IN...");          beep_in();            sleep(0.5)
    print("OUT...");         beep_out();           sleep(0.5)
    print("DUP...");         beep_duplicate();     sleep(0.5)
    print("ENROLLED...");    beep_enrolled();      sleep(0.5)
    print("BACKUP...");      beep_backup();        sleep(0.5)
    print("BACKUP FAIL..."); beep_backup_failed(); sleep(0.5)
    print("ERROR...");       beep_error();         sleep(0.5)
    print("ALARM...");       beep_alarm()
