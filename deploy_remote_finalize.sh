#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${1:-/home/goushi/smart-home}"
cd "$APP_DIR"
mkdir -p logs model

echo "== fix writable runtime files =="
sudo chown goushi:goushi "$APP_DIR" logs model 2>/dev/null || true
sudo find "$APP_DIR" -maxdepth 1 -type f \( -name '*.json' -o -name '*.db' -o -name '*.sqlite*' \) -exec chown goushi:goushi {} +
sudo find "$APP_DIR" -maxdepth 1 -type f \( -name '*.json' -o -name '*.db' -o -name '*.sqlite*' \) -exec chmod 664 {} +
sudo chmod 775 "$APP_DIR" logs model 2>/dev/null || true

PY="$APP_DIR/.venv-torch/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3)"
fi

if [ ! -f ir_forwarder_codes.json ] && [ -f ir_forwarder_codes.example.json ]; then
  cp -av ir_forwarder_codes.example.json ir_forwarder_codes.json
fi

VOSK_ZIP="vosk-model-small-cn-0.22.zip"
if [ ! -f "$VOSK_ZIP" ] && [ -f models/vosk-model-small-cn-0.22.zip ]; then
  VOSK_ZIP="models/vosk-model-small-cn-0.22.zip"
fi
if [ -f "$VOSK_ZIP" ] && [ ! -s model/vosk-model-small-cn-0.22/am/final.mdl ]; then
  echo "== extract vosk model =="
  rm -rf model/vosk-model-small-cn-0.22
  unzip -q "$VOSK_ZIP" -d model
fi

echo "== disable old smart-home.service =="
sudo systemctl stop smart-home.service 2>/dev/null || true
sudo systemctl disable smart-home.service 2>/dev/null || true
sudo systemctl mask smart-home.service 2>/dev/null || true
sudo pkill -f '/home/goushi/smart-home/.venv/bin/python /home/goushi/smart-home/app.py' 2>/dev/null || true

echo "== configure GPIO access for DHT11 =="
sudo groupadd -f gpio
sudo usermod -aG gpio goushi
sudo tee /etc/udev/rules.d/70-smart-home-gpio.rules >/dev/null <<'EOF'
SUBSYSTEM=="gpio", KERNEL=="gpiochip*", GROUP="gpio", MODE="0660"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=gpio --action=add 2>/dev/null || true
if compgen -G '/dev/gpiochip*' >/dev/null; then
  sudo chgrp gpio /dev/gpiochip*
  sudo chmod 0660 /dev/gpiochip*
fi

echo "== install systemd units =="
sudo tee /etc/systemd/system/smart-home-app.service >/dev/null <<EOF
[Unit]
Description=Smart Home Flask + AI on CPU1
After=network-online.target
Wants=network-online.target

[Service]
User=goushi
SupplementaryGroups=gpio
WorkingDirectory=$APP_DIR
Environment=HOME=/home/goushi
Environment=OMP_NUM_THREADS=1
Environment=OPENBLAS_NUM_THREADS=1
Environment=MKL_NUM_THREADS=1
Environment=NUMEXPR_NUM_THREADS=1
Environment=IR_BACKEND=forwarder
Environment=IR_FORWARDER_PORT=/dev/ttyAMA2
Environment=IR_FORWARDER_BAUDRATE=9600
Environment=IR_FORWARDER_MAP=ir_forwarder_codes.json
Environment=DHT11_ENABLED=1
Environment=DHT11_CHIP=/dev/gpiochip3
Environment=DHT11_LINE=1
Environment=DHT11_BOARD_PIN=11
Environment=DHT11_MIN_INTERVAL_SECONDS=2
Environment=DHT11_MAX_STALE_SECONDS=30
Environment=DHT11_RETRIES=2
Environment=JAILHOUSE_SOURCE_DIR=/home/goushi/openamp-jailhouse/phytium-jailhouse/src/jailhouse
Environment=JAILHOUSE_TOOL=/home/goushi/openamp-jailhouse/phytium-jailhouse/src/jailhouse/tools/jailhouse
Environment=JAILHOUSE_CONFIG_DIR=/home/goushi/openamp-jailhouse/phytium-jailhouse/src/jailhouse/configs/arm64
Environment=REALTIME_BACKEND=fusion
Environment=REALTIME_BRIDGE_SOCKET=/run/smart-home-rt/bridge.sock
Environment=REALTIME_BRIDGE_KIND=mailbox
ExecStart=/usr/bin/taskset -c 1 $PY $APP_DIR/app.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/smart-home-voice.service >/dev/null <<EOF
[Unit]
Description=Smart Home Vosk Voice on CPU2
After=network-online.target smart-home-app.service
Wants=network-online.target

[Service]
User=goushi
WorkingDirectory=$APP_DIR
Environment=HOME=/home/goushi
Environment=OMP_NUM_THREADS=1
Environment=OPENBLAS_NUM_THREADS=1
Environment=MKL_NUM_THREADS=1
Environment=NUMEXPR_NUM_THREADS=1
Environment=VOSK_MODEL_PATH=$APP_DIR/model/vosk-model-small-cn-0.22
Environment=VOSK_USE_GRAMMAR=0
ExecStart=/usr/bin/taskset -c 2 $PY $APP_DIR/yuyin.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

echo "== syntax check =="
"$PY" -m py_compile \
  app.py ai.py device_control.py dht11_sensor.py gpio_ir.py ir_forwarder.py \
  health_service.py hardware_layer.py runtime_store.py intent_service.py \
  feedback_store.py realtime_bridge.py project_overview.py storage.py \
  yuyin.py yuyinapi.py generate_ir_forwarder_codes.py \
  jailhouse_mailbox_bridge.py build_jailhouse_mailbox_inmate.py project_acceptance.py fault_injection.py

echo "== restart services =="
sudo systemctl daemon-reload
sudo systemctl enable smart-home-app.service smart-home-voice.service
sudo systemctl restart smart-home-app.service smart-home-voice.service

if systemctl list-unit-files cpolar.service >/dev/null 2>&1; then
  sudo systemctl enable cpolar.service 2>/dev/null || true
  sudo systemctl restart cpolar.service 2>/dev/null || true
fi

echo "== wait app =="
for _ in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:5000/api/auth/status >/dev/null; then
    break
  fi
  sleep 1
done

echo "== final status =="
cat /sys/devices/system/cpu/online 2>/dev/null || true
systemctl --no-pager --full status smart-home-app.service smart-home-voice.service cpolar.service 2>/dev/null | sed -n '1,120p' || true
ps -eo pid,psr,comm,args | grep -E 'app.py|yuyin.py|cpolar' | grep -v grep || true
curl -fsS http://127.0.0.1:5000/api/auth/status || true
echo
echo "DONE"
