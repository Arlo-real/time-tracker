#!/usr/bin/env bash
#
# One-shot setup for the time-tracker on a Raspberry Pi (Debian/Raspberry Pi OS).
#
#   sudo ./setup.sh              # install deps, init DB, install & start services
#   sudo ./setup.sh --no-services  # deps + DB only, no systemd services
#
# Installs system packages, gives the run user access to the reader (input),
# initialises the database, and installs two systemd services:
#   timetracker-scan   -> the scan station (main.py: NFC reader + buzzer)
#   timetracker-admin  -> the admin website (app.py: http://<pi>:8080)
#
set -euo pipefail

INSTALL_SERVICES=1
[ "${1:-}" = "--no-services" ] && INSTALL_SERVICES=0

# Project dir is wherever this script lives; run user is the human (not root).
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
PY=/usr/bin/python3

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

# ── 2. hardware access groups ─────────────────────────────────────────────────
echo "-- Granting $RUN_USER access to input (reader) and audio (buzzer)..."
for g in input audio dialout; do
  if getent group "$g" >/dev/null; then usermod -aG "$g" "$RUN_USER"; fi
done

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

systemctl daemon-reload
systemctl enable --now timetracker-admin.service
systemctl enable --now timetracker-scan.service

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "== Done =="
echo "Admin site : http://${IP:-<pi-ip>}:8080   (default password: admin — change under Settings)"
echo "Services   : timetracker-admin, timetracker-scan  (systemctl status <name>)"
echo
echo "NOTE:"
echo " * '$RUN_USER' was added to input/gpio groups — log out/in (or reboot) for it to take effect,"
echo "   then: systemctl restart timetracker-scan"
echo " * Find your reader's device name with:"
echo "     $PY -c \"from evdev import InputDevice,list_devices as l; [print(p, InputDevice(p).name) for p in l()]\""
echo "   then set READER_DEVICE in /etc/systemd/system/timetracker-scan.service and daemon-reload."
echo " * Plug the buzzer into the 3.5mm audio jack. Force jack output and set volume with:"
echo "     sudo raspi-config nonint do_audio 1   # 1 = headphones/jack"
echo "     amixer set Master 90%"
echo "   Test the tones with:  python3 $PROJECT_DIR/buzzer.py"
