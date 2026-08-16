#!/usr/bin/env bash
# BLDC PHM bench: Raspberry Pi 5 demo dashboard installer (Pi 5 / Debian 13).
# Idempotent. Run as the Pi's login user (whoever that is; the script reads it
# from `whoami`, it is not hard-coded); uses sudo where needed.
#   - frees the GPIO UART from the serial login console
#   - installs a boot service for the telemetry reader (stdlib only)
#   - launches Chromium fullscreen at login, replacing the old sysmon widget
set -e
USER_NAME="$(whoami)"
APP_DIR="$HOME/bench_dashboard"
echo "== BLDC PHM dashboard install for $USER_NAME =="

# ---- 1. free the GPIO UART (Pi 5: /dev/serial0 -> ttyAMA10) ----------------
# The Pi ships with a login console on serial0; that holds the port we need.
sudo systemctl disable --now serial-getty@ttyAMA10.service 2>/dev/null || true
sudo systemctl disable --now serial-getty@ttyS0.service    2>/dev/null || true
sudo systemctl disable --now serial-getty@serial0.service  2>/dev/null || true
CMD=/boot/firmware/cmdline.txt; [ -f "$CMD" ] || CMD=/boot/cmdline.txt
if [ -f "$CMD" ] && grep -q "console=serial0" "$CMD"; then
  sudo sed -i 's/console=serial0,[0-9]* //' "$CMD"
  echo "  + removed serial console from $CMD (applies on reboot)"
fi
# UART itself is already enabled on Pi 5 (serial0 exists); ensure the flag is set
CFG=/boot/firmware/config.txt; [ -f "$CFG" ] || CFG=/boot/config.txt
grep -q "^enable_uart=1" "$CFG" || { echo "enable_uart=1" | sudo tee -a "$CFG" >/dev/null; echo "  + enable_uart=1"; }
sudo usermod -aG dialout "$USER_NAME" || true

# ---- 2. telemetry reader as a boot service --------------------------------
sudo tee /etc/systemd/system/bench-dashboard.service >/dev/null <<UNIT
[Unit]
Description=BLDC PHM bench telemetry reader + dashboard server
After=multi-user.target

[Service]
Type=simple
User=$USER_NAME
ExecStart=/usr/bin/python3 $APP_DIR/bench_reader.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable bench-dashboard.service
sudo systemctl restart bench-dashboard.service
echo "  + bench-dashboard.service enabled + started"

# ---- 3. Chromium kiosk at desktop login (replaces the sysmon widget) ------
AUTO="$HOME/.config/autostart"
mkdir -p "$AUTO"
# retire the old always-on-top system monitor so the dashboard owns the screen
if [ -f "$AUTO/sysmon.desktop" ]; then
  mv "$AUTO/sysmon.desktop" "$AUTO/sysmon.desktop.disabled"
  echo "  + retired old sysmon.desktop autostart"
fi
pkill -f sysmon_widget.py 2>/dev/null || true
CHROME=$(command -v chromium-browser || command -v chromium || echo chromium)
cat > "$AUTO/bench-kiosk.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=BLDC Bench Dashboard
Exec=$APP_DIR/kiosk.sh
X-GNOME-Autostart-enabled=true
DESK
cat > "$APP_DIR/kiosk.sh" <<KIOSK
#!/usr/bin/env bash
for i in \$(seq 1 30); do
  curl -s http://localhost:8080/api/state >/dev/null 2>&1 && break
  sleep 1
done
xset s off -dpms 2>/dev/null || true
unclutter -idle 0.5 >/dev/null 2>&1 &
$CHROME --kiosk --incognito --noerrdialogs --disable-infobars \\
  --password-store=basic --disable-features=Translate \\
  --disable-session-crashed-bubble --check-for-update-interval=31536000 \\
  --app=http://localhost:8080 >/dev/null 2>&1 &
KIOSK
chmod +x "$APP_DIR/kiosk.sh"
echo "  + Chromium kiosk autostart installed"

echo
echo "== DONE =="
systemctl --no-pager --lines=3 status bench-dashboard.service || true
echo
echo "Dashboard is live now at http://localhost:8080"
echo "It auto-opens fullscreen at every boot."
echo "Reboot ONCE to release the serial console cleanly:  sudo reboot"
