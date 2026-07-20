#!/usr/bin/env bash
#
# One-shot setup for the time-tracker on a Raspberry Pi (Debian/Raspberry Pi OS).
#
#   sudo ./setup.sh              # install deps, init DB, install & start services
#   sudo ./setup.sh --no-services  # deps + DB only, no systemd services
#   sudo ./setup.sh --restore      # restore the DB from a backup on a USB stick
#
# Installs system packages, gives the run user access to the reader (input),
# initialises the database, and installs three systemd services:
#   timetracker-scan   -> the scan station (main.py: NFC reader + buzzer)
#   timetracker-admin  -> the admin website (app.py: http://<pi>:8080)
#   timetracker-backup -> copies the DB to any USB stick (backup.py)
#
set -euo pipefail

INSTALL_SERVICES=1
[ "${1:-}" = "--no-services" ] && INSTALL_SERVICES=0

# Project dir is wherever this script lives; run user is the human (not root).
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
PY=/usr/bin/python3

# ── restore mode: pick a backup off a USB stick and make it the live DB ───────
if [ "${1:-}" = "--restore" ]; then
  if [ "$(id -u)" -ne 0 ]; then
    echo "Please run with sudo:  sudo ./setup.sh --restore" >&2
    exit 1
  fi
  echo "== Restore from USB stick =="
  echo "-- Stopping services so nothing writes while we swap the database..."
  systemctl stop timetracker-scan timetracker-admin timetracker-backup 2>/dev/null || true
  set +e
  "$PY" "$PROJECT_DIR/backup.py" --restore
  rc=$?
  set -e
  if [ $rc -eq 0 ]; then
    chown "$RUN_USER":"$RUN_USER" "$PROJECT_DIR/attendance.db" 2>/dev/null || true
  fi
  echo "-- Restarting services..."
  systemctl start timetracker-scan timetracker-admin timetracker-backup 2>/dev/null || true
  exit $rc
fi

echo "== Time-tracker setup =="
echo "  project : $PROJECT_DIR"
echo "  user    : $RUN_USER"
echo "  services: $([ $INSTALL_SERVICES -eq 1 ] && echo yes || echo no)"
echo

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo:  sudo ./setup.sh" >&2
  exit 1
fi

# ── 1. system packages ────────────────────────────────────────────────────────
echo "-- Installing system packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update
# Flask + waitress (production WSGI server for the admin site) + evdev (reader)
apt-get install -y python3 python3-flask python3-waitress python3-evdev
# alsa-utils provides 'aplay', used to sound the buzzer on the audio jack.
apt-get install -y alsa-utils
# ffmpeg converts custom per-employee scan sounds uploaded from the admin site
# (any format in -> the one format the reader plays). Without it, uploads are
# refused with a message saying so; everything else still works.
apt-get install -y ffmpeg || \
  echo "!! ffmpeg missing; custom per-employee scan sounds can't be uploaded."
# Mount helpers so a backup stick works whatever it is formatted with:
# NTFS (Windows-formatted), exFAT (big sticks), FAT32. ext4 needs nothing.
apt-get install -y ntfs-3g exfatprogs dosfstools || \
  apt-get install -y ntfs-3g exfat-fuse exfat-utils dosfstools || \
  echo "!! Some filesystem helpers missing; sticks in those formats won't mount."

# ── 2. hardware access groups ─────────────────────────────────────────────────
echo "-- Granting $RUN_USER access to input (reader) and audio (buzzer)..."
for g in input audio dialout; do
  if getent group "$g" >/dev/null; then usermod -aG "$g" "$RUN_USER"; fi
done

# ── 2b. buzzer volume ─────────────────────────────────────────────────────────
# The mixer control is not called the same thing on every card: the Pi's
# headphone jack exposes 'PCM', while USB/HDMI cards usually have 'Master'.
# Guessing wrong just prints "Unable to find simple control", so look first.
echo "-- Setting the audio jack to full volume..."
VOL_CTL=""
for c in PCM Master Headphone Speaker; do
  if amixer sget "$c" >/dev/null 2>&1; then VOL_CTL="$c"; break; fi
done
if [ -n "$VOL_CTL" ]; then
  # 100%, not 90%: this drives a buzzer that has to be heard over a workshop,
  # and on the Pi the scale is in dB -- 90% is -6.6dB, i.e. under half the
  # amplitude of full. Turn it down here if it distorts on your hardware.
  amixer -q sset "$VOL_CTL" 100% unmute 2>/dev/null || true
  echo "   '$VOL_CTL' set to 100%."
else
  echo "!! No mixer control found; set the volume by hand (amixer scontrols)."
fi

# ── 3. initialise the database (owned by the run user) ────────────────────────
echo "-- Initialising database..."
sudo -u "$RUN_USER" "$PY" "$PROJECT_DIR/db.py"

if [ $INSTALL_SERVICES -eq 0 ]; then
  echo
  echo "Done (no services installed). Run manually:"
  echo "   $PY $PROJECT_DIR/app.py     # admin site on :8080"
  echo "   $PY $PROJECT_DIR/main.py    # scan station"
  exit 0
fi

# ── 4. systemd services ───────────────────────────────────────────────────────
echo "-- Installing systemd services..."

cat > /etc/systemd/system/timetracker-admin.service <<EOF
[Unit]
Description=Time-tracker admin website
After=network.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PY $PROJECT_DIR/app.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/timetracker-scan.service <<EOF
[Unit]
Description=Time-tracker scan station (NFC reader + buzzer)
After=network-online.target systemd-timesyncd.service
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$PROJECT_DIR
# Reader capture. Set READER_DEVICE to your reader's name (see below) if the
# Pi has more than one input device. READER_MODE=evdev is right for a service.
Environment=READER_MODE=evdev
# Environment=READER_DEVICE=RFID
ExecStart=$PY $PROJECT_DIR/main.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

> /etc/systemd/system/timetracker-backup.service cat <<EOF
[Unit]
Description=Time-tracker USB backup (copies the DB to any stick plugged in)
After=local-fs.target

[Service]
Type=simple
# root: mounting arbitrary USB sticks needs it. The service only reads the
# database and writes to sticks it mounts itself.
User=root
WorkingDirectory=$PROJECT_DIR
ExecStart=$PY $PROJECT_DIR/backup.py --watch
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now timetracker-admin.service
systemctl enable --now timetracker-scan.service
systemctl enable --now timetracker-backup.service

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "== Done =="
echo "Admin site : http://${IP:-<pi-ip>}:8080   (default password: admin — change under Settings)"
echo "Services   : timetracker-admin, timetracker-scan, timetracker-backup"
echo "             (systemctl status <name>)"
echo
echo "BACKUPS:"
echo " * Plug in any USB stick (NTFS/exFAT/FAT32/ext4) — it is copied to"
echo "   <stick>/timetracker-backups/ and the buzzer beeps twice when done."
echo "   It is unmounted straight away, so the stick is always safe to pull."
echo " * If left plugged in it also backs up daily at 01:00 and on every boot."
echo " * See what is on a stick:   sudo $PY $PROJECT_DIR/backup.py --list"
echo " * Restore one:              sudo $PROJECT_DIR/setup.sh --restore"
echo
echo "NOTE:"
echo " * '$RUN_USER' was added to input/gpio groups — log out/in (or reboot) for it to take effect,"
echo "   then: systemctl restart timetracker-scan"
echo " * Find your reader's device name with:"
echo "     $PY -c \"from evdev import InputDevice,list_devices as l; [print(p, InputDevice(p).name) for p in l()]\""
echo "   then set READER_DEVICE in /etc/systemd/system/timetracker-scan.service and daemon-reload."
echo " * Plug the buzzer into the 3.5mm audio jack, then force output to the jack:"
echo "     sudo raspi-config nonint do_audio 1   # 1 = headphones/jack"
echo "   Volume was set above via '${VOL_CTL:-<none found>}'. To change it later:"
echo "     amixer sset ${VOL_CTL:-PCM} 100%      # 'amixer scontrols' lists the controls"
echo "     alsamixer                             # or set it interactively"
echo "   Test the tones with:  python3 $PROJECT_DIR/buzzer.py"
