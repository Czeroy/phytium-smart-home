#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/home/goushi/smart-home}"
JH_DIR="${JH_DIR:-$HOME/openamp-jailhouse/phytium-jailhouse/src/jailhouse}"
ROOT_CELL="${ROOT_CELL:-configs/arm64/e2000q.cell}"
INMATE_CELL="${INMATE_CELL:-configs/arm64/e2000q-ivshmem-demo.cell}"
CELL_NAME="${CELL_NAME:-ivshmem-demo}"
INMATE_BIN="${INMATE_BIN:-inmates/demos/arm64/smart-home-mailbox.bin}"
PY="${PY:-$APP_DIR/.venv-torch/bin/python}"
MAILBOX_PHYS="${MAILBOX_PHYS:-0xb1001000}"
OUT="${1:-$APP_DIR/logs/jailhouse-mailbox-channel-$(date +%Y%m%d-%H%M%S).txt}"

mkdir -p "$(dirname "$OUT")"

run_step() {
  echo
  echo "== $* =="
}

{
  echo "# Jailhouse shared-memory mailbox channel start"
  echo "time: $(date '+%F %T %Z')"
  echo "host: $(hostname)"
  echo "APP_DIR=$APP_DIR"
  echo "JH_DIR=$JH_DIR"
  echo "CELL_NAME=$CELL_NAME"
  echo "MAILBOX_PHYS=$MAILBOX_PHYS"
  echo
  echo "This path does not enable CONFIG_IVSHMEM_TEST and does not start remoteproc0."

  run_step "preflight"
  if [ ! -d "$APP_DIR" ] || [ ! -d "$JH_DIR" ]; then
    echo "FAIL: app or Jailhouse directory is missing"
    exit 1
  fi
  if [ ! -x "$PY" ]; then
    PY="$(command -v python3)"
  fi
  cd "$JH_DIR" || exit 1
  if grep -q '^[[:space:]]*#define[[:space:]]\+CONFIG_IVSHMEM_TEST' configs/arm64/e2000q.c 2>/dev/null; then
    echo "FAIL: root cell still has CONFIG_IVSHMEM_TEST enabled. Roll back e2000q.c first."
    exit 1
  fi
  echo "CPU online before: $(cat /sys/devices/system/cpu/online 2>/dev/null || true)"
  if [ "$(cat /sys/devices/system/cpu/online 2>/dev/null || true)" != "0-3" ]; then
    echo "FAIL: CPU online is not 0-3. Reboot or disable Jailhouse first."
    exit 1
  fi
  sudo systemctl stop smart-home-rt-bridge.service 2>/dev/null || true
  sudo rm -f /run/smart-home-rt/bridge.sock

  run_step "build mailbox inmate"
  "$PY" "$APP_DIR/build_jailhouse_mailbox_inmate.py"
  if [ ! -s "$ROOT_CELL" ] || [ ! -s "$INMATE_CELL" ]; then
    echo "Rebuilding Jailhouse cell files because one is missing or empty."
    rm -f "$ROOT_CELL" "$INMATE_CELL" \
      configs/arm64/e2000q.o configs/arm64/e2000q-ivshmem-demo.o \
      configs/arm64/.e2000q.o.cmd configs/arm64/.e2000q-ivshmem-demo.o.cmd
    make
  fi
  if [ ! -s "$ROOT_CELL" ] || [ ! -s "$INMATE_CELL" ]; then
    echo "FAIL: Jailhouse root/inmate cell files are still missing or empty"
    ls -lh "$ROOT_CELL" "$INMATE_CELL" 2>/dev/null || true
    exit 1
  fi
  ls -lh "$ROOT_CELL" "$INMATE_CELL" "$INMATE_BIN"

  run_step "reset old Jailhouse state"
  sudo rmmod smart_home_mailbox 2>/dev/null || true
  if [ -e /dev/jailhouse ]; then
    sudo timeout 5 ./tools/jailhouse cell shutdown "$CELL_NAME" 2>/dev/null || true
    sudo timeout 5 ./tools/jailhouse cell destroy "$CELL_NAME" 2>/dev/null || true
    sudo timeout 5 ./tools/jailhouse disable 2>/dev/null || true
  fi
  sudo rmmod uio_ivshmem 2>/dev/null || true
  sudo rmmod jailhouse 2>/dev/null || true
  echo "CPU online after reset: $(cat /sys/devices/system/cpu/online 2>/dev/null || true)"

  run_step "enable Jailhouse root and start CPU3 mailbox inmate"
  sudo cp -av hypervisor/jailhouse.bin /lib/firmware/jailhouse.bin
  sudo insmod driver/jailhouse.ko
  sudo timeout 10 ./tools/jailhouse enable "$ROOT_CELL"
  sudo timeout 10 ./tools/jailhouse cell create "$INMATE_CELL"
  sudo timeout 10 ./tools/jailhouse cell load "$CELL_NAME" "$INMATE_BIN"
  sudo timeout 10 ./tools/jailhouse cell start "$CELL_NAME"
  sudo timeout 5 ./tools/jailhouse cell list || true
  echo "CPU online after inmate start: $(cat /sys/devices/system/cpu/online 2>/dev/null || true)"
  dmesg | grep -iE 'smart-home mailbox|jailhouse|cell|error|fail|panic' | tail -120 || true

  run_step "build and load Linux mailbox driver"
  make -C "/lib/modules/$(uname -r)/build" M="$APP_DIR/mailbox_driver" modules
  sudo insmod "$APP_DIR/mailbox_driver/smart_home_mailbox.ko" mailbox_phys="$MAILBOX_PHYS"
  if [ ! -c /dev/smart_home_mailbox ]; then
    echo "FAIL: /dev/smart_home_mailbox was not created"
    exit 1
  fi
  sudo chmod 0660 /dev/smart_home_mailbox
  ls -lh /dev/smart_home_mailbox

  run_step "install and start mailbox bridge"
  sudo tee /etc/systemd/system/smart-home-rt-bridge.service >/dev/null <<EOF
[Unit]
Description=Smart Home Jailhouse shared-memory realtime bridge
After=smart-home-app.service

[Service]
User=root
WorkingDirectory=$APP_DIR
Environment=REALTIME_BRIDGE_SOCKET=/run/smart-home-rt/bridge.sock
Environment=REALTIME_BRIDGE_KIND=mailbox
Environment=REALTIME_MAILBOX_PHYS=$MAILBOX_PHYS
ExecStart=$PY $APP_DIR/jailhouse_mailbox_bridge.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

  sudo mkdir -p /etc/systemd/system/smart-home-app.service.d
  sudo tee /etc/systemd/system/smart-home-app.service.d/realtime-mailbox.conf >/dev/null <<EOF
[Service]
Environment=REALTIME_BACKEND=fusion
Environment=REALTIME_BRIDGE_SOCKET=/run/smart-home-rt/bridge.sock
Environment=REALTIME_BRIDGE_KIND=mailbox
Environment=REALTIME_MAILBOX_PHYS=$MAILBOX_PHYS
EOF

  sudo systemctl daemon-reload
  sudo systemctl enable smart-home-rt-bridge.service
  sudo systemctl restart smart-home-rt-bridge.service
  sudo systemctl restart smart-home-app.service
  sleep 2
  systemctl --no-pager --full status smart-home-rt-bridge.service | sed -n '1,80p' || true
  for _ in $(seq 1 45); do
    if curl -fsS http://127.0.0.1:5000/api/auth/status >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done

  run_step "API command test"
  curl -fsS -X POST http://127.0.0.1:5000/api/realtime/command \
    -H 'Content-Type: application/json' \
    -d '{"command":"ping","payload":{"source":"mailbox_channel_start"}}' || true
  echo
  curl -fsS http://127.0.0.1:5000/api/realtime/status || true
  echo

  run_step "result"
  echo "If /api/realtime/command reports mailbox command accepted, Web/AI can send commands into the CPU3 realtime inmate."
  echo "Cleanup: sudo systemctl stop smart-home-rt-bridge.service; sudo rmmod smart_home_mailbox; cd $JH_DIR && sudo timeout 5 ./tools/jailhouse disable && sudo rmmod jailhouse"
} | tee "$OUT"

echo
echo "run log saved to: $OUT"
