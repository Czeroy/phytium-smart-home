import os
import random
import re
import shlex
import subprocess
import tempfile
import time
from collections.abc import MutableMapping
from pathlib import Path

from dht11_sensor import DHT11_ENABLED, read_dht11
from gpio_ir import backend_status as gpio_backend_status
from gpio_ir import capture_raw_gpio, send_raw_pwm
from ir_forwarder import backend_status as forwarder_backend_status
from ir_forwarder import learn_to_slot as learn_forwarder_to_slot
from ir_forwarder import load_code_map as load_forwarder_code_map
from ir_forwarder import send_stored_code as send_forwarder_stored_code
from storage import BASE_DIR, now_iso, read_json_file, write_json_file


REMOTE_NAME = "my_ac_remote"
MOCK_IR_DEFAULT = "1" if os.name == "nt" else ""
MOCK_IR = os.getenv("MOCK_IR", MOCK_IR_DEFAULT).lower() in {"1", "true", "yes", "on"}
IR_RECEIVE_DEVICE = os.getenv("IR_RECEIVE_DEVICE", "/dev/lirc0")
IR_SEND_DEVICE = os.getenv("IR_SEND_DEVICE", "/dev/lirc1")


def _default_ir_backend():
    has_gpio_ir = os.path.exists("/dev/gpiochip2") and os.path.exists("/sys/class/pwm/pwmchip0")
    has_lirc = os.path.exists(IR_RECEIVE_DEVICE) or os.path.exists(IR_SEND_DEVICE)
    if os.name != "nt" and has_gpio_ir and not has_lirc:
        return "gpio_pwm"
    return "lirc"


IR_BACKEND = os.getenv("IR_BACKEND", _default_ir_backend()).strip().lower()
IR_FORWARDER_PORT = os.getenv("IR_FORWARDER_PORT", "/dev/ttyAMA2")
try:
    IR_FORWARDER_BAUDRATE = int(os.getenv("IR_FORWARDER_BAUDRATE", "9600"))
except ValueError:
    IR_FORWARDER_BAUDRATE = 9600
_forwarder_map_value = Path(os.getenv("IR_FORWARDER_MAP", "ir_forwarder_codes.json"))
IR_FORWARDER_MAP_PATH = _forwarder_map_value if _forwarder_map_value.is_absolute() else BASE_DIR / _forwarder_map_value
if not IR_FORWARDER_MAP_PATH.exists() and (BASE_DIR / "data" / _forwarder_map_value.name).exists():
    IR_FORWARDER_MAP_PATH = BASE_DIR / "data" / _forwarder_map_value.name
IR_FORWARDER_MAX_CODES = 248
IR_RX_CHIP = os.getenv("IR_RX_CHIP", "gpiochip2")


def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_modes(name, default):
    raw = os.getenv(name, default)
    modes = {
        part.strip().lower()
        for part in str(raw).replace("，", ",").replace("、", ",").split(",")
        if part.strip()
    }
    return {mode for mode in modes if mode in MODE_CODE_NAMES}


IR_RX_LINE = _env_int("IR_RX_LINE", 10)
IR_PWM_CHIP = os.getenv("IR_PWM_CHIP", "pwmchip2")
IR_PWM_CHANNEL = _env_int("IR_PWM_CHANNEL", 0)
IR_CARRIER_HZ = _env_int("IR_CARRIER_HZ", 38000)
IR_DUTY_CYCLE = _env_float("IR_DUTY_CYCLE", 0.33)
IR_PWM_DRIVER = os.getenv("IR_PWM_DRIVER", "auto").strip().lower()
IR_PWM_POLARITY = os.getenv("IR_PWM_POLARITY", "normal").strip().lower()
IR_SEND_FRAME_GAP_US = _env_int("IR_SEND_FRAME_GAP_US", 15000)
IR_SEND_FIRST_FRAME_ONLY = os.getenv("IR_SEND_FIRST_FRAME_ONLY", "1").lower() in {"1", "true", "yes", "on"}
IR_CAPTURE_FRAME_GAP_US = _env_int("IR_CAPTURE_FRAME_GAP_US", 120000)
IR_CAPTURE_MAX_EDGES = _env_int("IR_CAPTURE_MAX_EDGES", 1200)
IR_CODES_PATH = BASE_DIR / "ir_codes.json"
LIRC_CONFIG_PATH = BASE_DIR / "my_ac_remote.conf"

MOCK_LIGHT_DEFAULT = "1" if os.name == "nt" else ""
MOCK_LIGHT = os.getenv("MOCK_LIGHT", MOCK_LIGHT_DEFAULT).lower() in {"1", "true", "yes", "on"}
LIGHT_ON_CMD = os.getenv("LIGHT_ON_CMD", "")
LIGHT_OFF_CMD = os.getenv("LIGHT_OFF_CMD", "")

COMMAND_LABELS = {
    "power": "空调电源",
    "ac_on": "打开空调",
    "ac_off": "关闭空调",
    "temp_up": "温度升高",
    "temp_down": "温度降低",
    "mode_cool": "制冷",
    "mode_heat": "制热",
    "mode_dry": "除湿",
    "mode_fan": "送风",
    "mode_auto": "自动模式",
    "fan_auto": "自动风",
    "fan_low": "低风",
    "fan_mid": "中风",
    "fan_high": "高风",
    "light_toggle": "切换灯光",
    "light_on": "开灯",
    "light_off": "关灯",
}

VALID_COMMANDS = set(COMMAND_LABELS)

MODE_CODE_NAMES = {
    "auto": "AUTO",
    "cool": "COOL",
    "heat": "HEAT",
    "dry": "DRY",
    "fan": "FAN",
}

MODE_LABELS = {
    "auto": "自动",
    "cool": "制冷",
    "heat": "制热",
    "dry": "除湿",
    "fan": "送风",
}

TEMP_ADJUST_MODES = _env_modes("AC_TEMP_ADJUST_MODES", "cool,heat")
TEMP_ADJUST_MODES.discard("auto")
NON_TEMP_MODES = set(MODE_CODE_NAMES) - TEMP_ADJUST_MODES

FAN_CODE_NAMES = {
    "auto": "AUTO",
    "low": "LOW",
    "mid": "MID",
    "high": "HIGH",
}

MODE_PRESETS = {
    "mode_auto": {"mode": "auto", "fan": "auto"},
    "mode_cool": {"mode": "cool", "temperature": 26, "fan": "auto"},
    "mode_heat": {"mode": "heat", "temperature": 24, "fan": "mid"},
    "mode_dry": {"mode": "dry", "fan": "low"},
    "mode_fan": {"mode": "fan", "fan": "mid"},
}

DEFAULT_IR_PROFILES = [
    {"code": "AC_OFF", "label": "空调关闭", "hint": "请用原遥控器按关机"},
    {"code": "AC_AUTO", "label": "自动模式", "hint": "请把遥控器调到自动模式后发送，自动模式不学习温度"},
    {"code": "AC_COOL_26_AUTO", "label": "制冷 26度 自动风", "hint": "请把遥控器调到制冷 26度 自动风后发送"},
    {"code": "AC_COOL_24_MID", "label": "制冷 24度 中风", "hint": "请把遥控器调到制冷 24度 中风后发送"},
    {"code": "AC_COOL_28_HIGH", "label": "制冷 28度 高风", "hint": "请把遥控器调到制冷 28度 高风后发送"},
    {"code": "AC_HEAT_24_MID", "label": "制热 24度 中风", "hint": "请把遥控器调到制热 24度 中风后发送"},
    {"code": "AC_HEAT_26_AUTO", "label": "制热 26度 自动风", "hint": "请把遥控器调到制热 26度 自动风后发送"},
    {"code": "AC_DRY_LOW", "label": "除湿 低风", "hint": "请把遥控器调到除湿 低风后发送，除湿不学习温度"},
    {"code": "AC_FAN_MID", "label": "送风 中风", "hint": "请把遥控器调到送风 中风后发送，送风不学习温度"},
    {"code": "AC_FAN_HIGH", "label": "送风 高风", "hint": "请把遥控器调到送风 高风后发送，送风不学习温度"},
]

IR_FAN_OPTIONS = [
    ("AUTO", "自动风"),
    ("LOW", "低风"),
    ("MID", "中风"),
    ("HIGH", "高风"),
]


def build_default_ir_profiles():
    profiles = [
        {"code": "AC_OFF", "label": "空调关闭", "hint": "请用遥控器发送关机"},
        {"code": "AC_ON", "label": "空调开机", "hint": "如果遥控器有单独开机键，请在空调关闭时发送；没有单独开机键可以跳过"},
        {"code": "AC_AUTO", "label": "自动模式", "hint": "请把遥控器调到自动模式后发送，自动模式不学习温度"},
    ]

    for mode_code, mode_label in (("COOL", "制冷"), ("HEAT", "制热")):
        for temperature in range(16, 31):
            profiles.append({
                "code": f"AC_{mode_code}_{temperature}",
                "label": f"{mode_label} {temperature}度",
                "hint": f"如果遥控器不支持风速调节，请只把遥控器调到{mode_label} {temperature}度后发送",
            })
            for fan_code, fan_label in IR_FAN_OPTIONS:
                profiles.append({
                    "code": f"AC_{mode_code}_{temperature}_{fan_code}",
                    "label": f"{mode_label} {temperature}度 {fan_label}",
                    "hint": f"请把遥控器调到{mode_label} {temperature}度 {fan_label}后发送",
                })

    for mode_code, mode_label in (("DRY", "除湿"), ("FAN", "送风")):
        profiles.append({
            "code": f"AC_{mode_code}",
            "label": mode_label,
            "hint": f"如果遥控器在{mode_label}模式下不支持风速调节，请只把遥控器调到{mode_label}后发送",
        })
        for fan_code, fan_label in IR_FAN_OPTIONS:
            profiles.append({
                "code": f"AC_{mode_code}_{fan_code}",
                "label": f"{mode_label} {fan_label}",
                "hint": f"请把遥控器调到{mode_label} {fan_label}后发送，{mode_label}不学习温度",
            })
        for temperature in range(16, 31):
            for fan_code, fan_label in IR_FAN_OPTIONS:
                profiles.append({
                    "code": f"AC_{mode_code}_{temperature}_{fan_code}",
                    "label": f"{mode_label} {temperature}度 {fan_label}",
                    "hint": f"只有你的空调在{mode_label}模式支持温度时才需要学习：{mode_label} {temperature}度 {fan_label}",
                })

    return profiles


DEFAULT_IR_PROFILES = build_default_ir_profiles()

STATE_CODE_PATTERN = re.compile(
    r"^(AC_OFF|AC_ON|AC_AUTO|AC_(DRY|FAN)|AC_(DRY|FAN)_(AUTO|LOW|MID|HIGH)|AC_(COOL|HEAT)_(1[6-9]|2\d|30)|AC_(COOL|HEAT)_(1[6-9]|2\d|30)_(AUTO|LOW|MID|HIGH)|AC_(DRY|FAN)_(1[6-9]|2\d|30)_(AUTO|LOW|MID|HIGH))$"
)

DEVICE_STORE_PATH = BASE_DIR / "devices.json"
DEFAULT_DEVICE_ID = "ac-main"
DEFAULT_DEVICE_NAME = "这里的空调"

current_environment = {
    "room_temperature": 26.5,
    "humidity": 52,
    "air_quality": "未接入",
    "source": "dht11" if DHT11_ENABLED else "simulated",
    "sensor_valid": not DHT11_ENABLED,
    "stale": False,
    "error": "",
    "updated_at": "",
}

_device_store_cache = None


def default_state():
    return {
        "power": False,
        "temperature": 26,
        "mode": "cool",
        "fan": "auto",
        "light": False,
    }


def empty_ir_store():
    return {
        "remote_name": REMOTE_NAME,
        "updated_at": "",
        "codes": {},
    }


def _slugify_device_id(value):
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    if not slug:
        slug = f"device-{random.randint(1000, 9999)}"
    return slug


def _make_device(device_id=None, name=None, location=""):
    return {
        "id": device_id or DEFAULT_DEVICE_ID,
        "name": (name or DEFAULT_DEVICE_NAME).strip(),
        "location": str(location or "").strip(),
        "kind": "ac",
        "remote_name": REMOTE_NAME,
        "state": default_state(),
        "codes": {},
        "updated_at": "",
    }


def empty_device_store():
    return {
        "selected_device_id": DEFAULT_DEVICE_ID,
        "updated_at": "",
        "devices": [_make_device()],
    }


def _normalize_device(device, fallback_index=0):
    normalized = _make_device(
        device_id=str(device.get("id") or f"{DEFAULT_DEVICE_ID}-{fallback_index}"),
        name=device.get("name") or f"{DEFAULT_DEVICE_NAME}{fallback_index + 1 if fallback_index else ''}",
        location=device.get("location", ""),
    )
    state = device.get("state", {})
    if isinstance(state, dict):
        normalized["state"].update({
            key: state.get(key, normalized["state"][key])
            for key in normalized["state"]
        })
    codes = device.get("codes", {})
    normalized["codes"] = codes if isinstance(codes, dict) else {}
    normalized["remote_name"] = str(device.get("remote_name") or REMOTE_NAME)
    normalized["updated_at"] = str(device.get("updated_at") or "")
    return normalized


def _migrate_legacy_codes():
    legacy = read_json_file(IR_CODES_PATH, empty_ir_store())
    if not isinstance(legacy, dict):
        return {}
    codes = legacy.get("codes", {})
    return codes if isinstance(codes, dict) else {}


def load_device_store(force=False):
    global _device_store_cache
    if _device_store_cache is not None and not force:
        return _device_store_cache

    changed = False
    store_exists = DEVICE_STORE_PATH.exists()
    raw = read_json_file(DEVICE_STORE_PATH, empty_device_store())
    if not isinstance(raw, dict):
        raw = empty_device_store()
        changed = True

    devices = raw.get("devices", [])
    if not isinstance(devices, list) or not devices:
        devices = [_make_device()]
        changed = True

    normalized_devices = []
    seen_ids = set()
    for index, device in enumerate(devices):
        if not isinstance(device, dict):
            changed = True
            continue
        normalized = _normalize_device(device, fallback_index=index)
        base_id = normalized["id"]
        dedup_id = base_id
        suffix = 2
        while dedup_id in seen_ids:
            dedup_id = f"{base_id}-{suffix}"
            suffix += 1
        if dedup_id != normalized["id"]:
            normalized["id"] = dedup_id
            changed = True
        seen_ids.add(normalized["id"])
        normalized_devices.append(normalized)

    if not normalized_devices:
        normalized_devices = [_make_device()]
        changed = True

    selected_id = str(raw.get("selected_device_id") or normalized_devices[0]["id"])
    if selected_id not in {device["id"] for device in normalized_devices}:
        selected_id = normalized_devices[0]["id"]
        changed = True

    legacy_codes = _migrate_legacy_codes() if not store_exists else {}
    default_device = next((device for device in normalized_devices if device["id"] == DEFAULT_DEVICE_ID), normalized_devices[0])
    if legacy_codes and not default_device.get("codes"):
        default_device["codes"] = legacy_codes
        changed = True

    store = {
        "selected_device_id": selected_id,
        "updated_at": str(raw.get("updated_at") or ""),
        "devices": normalized_devices,
    }
    _device_store_cache = store
    if changed:
        save_device_store(store)
    return store


def save_device_store(data=None):
    global _device_store_cache
    store = data or load_device_store()
    store["updated_at"] = now_iso()
    _device_store_cache = store
    write_json_file(DEVICE_STORE_PATH, store)
    return store


def list_devices():
    devices = []
    store = load_device_store()
    selected_id = store["selected_device_id"]
    for device in store["devices"]:
        codes = device.get("codes", {})
        learned = len([code for code, item in codes.items() if item and item.get("raw")])
        devices.append({
            "id": device["id"],
            "name": device["name"],
            "location": device.get("location", ""),
            "kind": device.get("kind", "ac"),
            "selected": device["id"] == selected_id,
            "learned_count": learned,
        })
    return devices


def get_selected_device_id():
    return load_device_store()["selected_device_id"]


def get_device(device_id=None):
    store = load_device_store()
    target_id = str(device_id or store["selected_device_id"])
    for device in store["devices"]:
        if device["id"] == target_id:
            return device
    return None if device_id is not None else store["devices"][0]


def select_device(device_id):
    store = load_device_store()
    target = get_device(device_id)
    if not target:
        return None
    store["selected_device_id"] = target["id"]
    save_device_store(store)
    return target


def create_device(name, location=""):
    store = load_device_store()
    display_name = str(name or "").strip() or f"空调 {len(store['devices']) + 1}"
    base_id = _slugify_device_id(display_name)
    device_id = base_id
    suffix = 2
    existing_ids = {device["id"] for device in store["devices"]}
    while device_id in existing_ids:
        device_id = f"{base_id}-{suffix}"
        suffix += 1
    device = _make_device(device_id=device_id, name=display_name, location=location)
    store["devices"].append(device)
    store["selected_device_id"] = device["id"]
    save_device_store(store)
    return device


def delete_device(device_id):
    store = load_device_store()
    if len(store["devices"]) <= 1:
        raise ValueError("至少保留一个设备")
    remaining = [device for device in store["devices"] if device["id"] != str(device_id)]
    if len(remaining) == len(store["devices"]):
        return False
    store["devices"] = remaining
    if store["selected_device_id"] == str(device_id):
        store["selected_device_id"] = remaining[0]["id"]
    save_device_store(store)
    return True


def clear_ir_store(device_id=None):
    device = get_device(device_id)
    device["codes"] = {}
    device["updated_at"] = now_iso()
    save_device_store()
    return device


class CurrentStateProxy(MutableMapping):
    def _state(self):
        return get_device()["state"]

    def __getitem__(self, key):
        return self._state()[key]

    def __setitem__(self, key, value):
        self._state()[key] = value
        save_device_store()

    def __delitem__(self, key):
        del self._state()[key]
        save_device_store()

    def __iter__(self):
        return iter(dict(self._state()))

    def __len__(self):
        return len(self._state())

    def get(self, key, default=None):
        return self._state().get(key, default)

    def update(self, *args, **kwargs):
        self._state().update(*args, **kwargs)
        save_device_store()

    def copy(self):
        return dict(self._state())


current_state = CurrentStateProxy()


def optimize_commands(commands):
    optimized = []
    has_mode_command = any(command.startswith("mode_") for command in commands)
    mode_command = next((command for command in commands if command.startswith("mode_")), None)
    mode_preset = MODE_PRESETS.get(mode_command or "", {})
    for command in commands:
        if has_mode_command and command == "ac_on":
            continue
        if command.startswith("fan_") and mode_preset.get("fan") == command.replace("fan_", "", 1):
            continue
        if command not in optimized:
            optimized.append(command)
    return optimized


def update_environment():
    if DHT11_ENABLED:
        reading = read_dht11()
        current_environment["room_temperature"] = reading.get("temperature")
        current_environment["humidity"] = reading.get("humidity")
        current_environment.update({
            "air_quality": "未接入",
            "source": "dht11",
            "sensor_valid": bool(reading.get("success")),
            "stale": bool(reading.get("stale")),
            "error": str(reading.get("error") or ""),
            "updated_at": str(reading.get("updated_at") or ""),
            "sensor": reading,
        })
        return dict(current_environment)

    temp_base = float(current_state["temperature"])
    current_environment["room_temperature"] = round(temp_base + random.uniform(-1.0, 1.0), 1)
    current_environment["humidity"] = max(30, min(75, int(current_environment["humidity"] + random.randint(-2, 2))))
    current_environment["air_quality"] = random.choice(["优", "良好", "良好", "一般"])
    current_environment["source"] = "simulated"
    current_environment["sensor_valid"] = True
    current_environment["stale"] = False
    current_environment["error"] = ""
    current_environment.pop("sensor", None)
    current_environment["updated_at"] = now_iso()
    return dict(current_environment)


def load_ir_store(device_id=None):
    device = get_device(device_id) or get_device()
    return {
        "remote_name": str(device.get("remote_name") or REMOTE_NAME),
        "updated_at": str(device.get("updated_at") or ""),
        "codes": dict(device.get("codes", {})),
    }


def save_ir_store(data, device_id=None):
    device = get_device(device_id) or get_device()
    device["remote_name"] = str(data.get("remote_name") or REMOTE_NAME)
    device["updated_at"] = now_iso()
    codes = data.get("codes", {})
    device["codes"] = codes if isinstance(codes, dict) else {}
    save_device_store()


def state_code_label(code):
    for profile in DEFAULT_IR_PROFILES:
        if profile["code"] == code:
            return profile["label"]

    if code == "AC_OFF":
        return "空调关闭"
    if code == "AC_ON":
        return "空调开机"
    if code == "AC_AUTO":
        return "自动模式"
    if code == "AC_DRY":
        return "除湿"
    if code == "AC_FAN":
        return "送风"

    match = re.match(r"^AC_(DRY|FAN)_(AUTO|LOW|MID|HIGH)$", code)
    if match:
        mode_text = {"DRY": "除湿", "FAN": "送风"}[match.group(1)]
        fan_text = {"AUTO": "自动风", "LOW": "低风", "MID": "中风", "HIGH": "高风"}[match.group(2)]
        return f"{mode_text} {fan_text}"

    match = re.match(r"^AC_(COOL|HEAT)_(\d+)$", code)
    if match:
        mode_text = {"COOL": "制冷", "HEAT": "制热"}[match.group(1)]
        return f"{mode_text} {match.group(2)}度"

    match = re.match(r"^AC_(COOL|HEAT|DRY|FAN)_(\d+)_(AUTO|LOW|MID|HIGH)$", code)
    if not match:
        return code

    mode_text = {"COOL": "制冷", "HEAT": "制热", "DRY": "除湿", "FAN": "送风"}[match.group(1)]
    fan_text = {"AUTO": "自动风", "LOW": "低风", "MID": "中风", "HIGH": "高风"}[match.group(3)]
    return f"{mode_text} {match.group(2)}度 {fan_text}"


def is_valid_state_code(code):
    return bool(STATE_CODE_PATTERN.match(str(code).upper()))


def build_ac_state_code(state):
    if state.get("mode") == "auto":
        return "AC_AUTO"
    mode_code = MODE_CODE_NAMES.get(state.get("mode", "cool"), "COOL")
    fan_code = FAN_CODE_NAMES.get(state.get("fan", "auto"), "AUTO")
    if state.get("mode") not in TEMP_ADJUST_MODES:
        return f"AC_{mode_code}_{fan_code}"
    temperature = max(16, min(30, int(state.get("temperature", 26))))
    return f"AC_{mode_code}_{temperature}_{fan_code}"


def target_for_command(command):
    target = dict(current_state)

    if command == "power":
        command = "ac_off" if current_state["power"] else "ac_on"

    if command == "ac_off":
        target["power"] = False
        return target, "AC_OFF"
    if command == "ac_on":
        target["power"] = True
        return target, "AC_ON"
    if command == "temp_up":
        if target.get("mode") in NON_TEMP_MODES:
            return target, None
        target["power"] = True
        target["temperature"] = min(30, target["temperature"] + 1)
        return target, build_ac_state_code(target)
    if command == "temp_down":
        if target.get("mode") in NON_TEMP_MODES:
            return target, None
        target["power"] = True
        target["temperature"] = max(16, target["temperature"] - 1)
        return target, build_ac_state_code(target)
    if command.startswith("mode_"):
        target["power"] = True
        target.update(MODE_PRESETS.get(command, {"mode": command.replace("mode_", "", 1)}))
        return target, build_ac_state_code(target)
    if command.startswith("fan_"):
        target["power"] = True
        target["fan"] = command.replace("fan_", "", 1)
        return target, build_ac_state_code(target)

    return target, None


def send_light(turn_on):
    action = "开灯" if turn_on else "关灯"
    if MOCK_LIGHT:
        return True, f"模拟{action}成功"

    command_text = LIGHT_ON_CMD if turn_on else LIGHT_OFF_CMD
    if not command_text:
        return False, f"灯光不是红外控制，请先设置 {'LIGHT_ON_CMD' if turn_on else 'LIGHT_OFF_CMD'} 或设置 MOCK_LIGHT=1 调试"

    try:
        subprocess.run(
            shlex.split(command_text),
            check=True,
            timeout=3,
        )
        return True, f"{action}成功"
    except FileNotFoundError:
        return False, f"找不到灯光控制命令: {command_text}"
    except subprocess.TimeoutExpired:
        return False, f"{action}命令执行超时"
    except subprocess.CalledProcessError as exc:
        return False, f"{action}命令执行失败: {exc}"


def parse_mode2_output(output):
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="ignore")

    raw = []
    for line in str(output or "").splitlines():
        match = re.search(r"\b(?:pulse|space)\s+(\d+)", line)
        if match:
            raw.append(int(match.group(1)))
    return raw


def capture_ir_raw(timeout=8, frame_gap_us=None, max_edges=None):
    injected_timeout = os.getenv("IR_RECEIVE_TIMEOUT", "").strip()
    if injected_timeout:
        try:
            time.sleep(max(0.0, min(float(injected_timeout), 1.0)))
        except ValueError:
            pass
        return False, [], "FIT 故障注入：红外接收超时"

    if MOCK_IR:
        base = [9000, 4500, 560, 560, 560, 1690, 560, 560, 560, 1690]
        raw = base + [value for _ in range(20) for value in (560, random.choice([560, 1690]))]
        return True, raw, "模拟学习成功"

    if IR_BACKEND in {"gpio", "gpio_pwm", "pwm"}:
        return capture_raw_gpio(
            chip=IR_RX_CHIP,
            line=IR_RX_LINE,
            timeout=timeout,
            max_edges=max_edges or IR_CAPTURE_MAX_EDGES,
            frame_gap_us=frame_gap_us or IR_CAPTURE_FRAME_GAP_US,
        )

    try:
        completed = subprocess.run(
            ["mode2", "-d", IR_RECEIVE_DEVICE],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = completed.stdout
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
    except FileNotFoundError:
        return False, [], "未找到 mode2，请先安装 lirc"

    raw = parse_mode2_output(output)
    if len(raw) < 12:
        return False, raw, "没有收到有效红外信号，请确认接收头接线后重新学习"
    return True, raw, f"学习成功，共收到 {len(raw)} 个红外脉冲"


def write_raw_send_file(raw):
    temp = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".ir")
    try:
        for index, value in enumerate(raw):
            temp.write(("pulse " if index % 2 == 0 else "space ") + str(int(value)) + "\n")
        return temp.name
    finally:
        temp.close()


def trim_raw_to_first_frame(raw, frame_gap_us=15000):
    trimmed = []
    for index, value in enumerate(raw):
        trimmed.append(int(value))
        if index % 2 == 1 and int(value) >= int(frame_gap_us) and len(trimmed) >= 12:
            break

    if len(trimmed) % 2 == 1:
        trimmed.pop()

    return trimmed or [int(value) for value in raw]


def send_raw_ir(raw):
    effective_raw = [int(value) for value in raw]
    if IR_SEND_FIRST_FRAME_ONLY:
        effective_raw = trim_raw_to_first_frame(effective_raw, frame_gap_us=IR_SEND_FRAME_GAP_US)

    if IR_BACKEND in {"gpio", "gpio_pwm", "pwm"}:
        return send_raw_pwm(
            effective_raw,
            pwm_chip=IR_PWM_CHIP,
            channel=IR_PWM_CHANNEL,
            carrier_hz=IR_CARRIER_HZ,
            duty_cycle=IR_DUTY_CYCLE,
            polarity=IR_PWM_POLARITY,
        )

    path = write_raw_send_file(effective_raw)
    try:
        subprocess.run(
            ["ir-ctl", "-d", IR_SEND_DEVICE, f"--send={path}"],
            check=True,
            timeout=5,
        )
        return True, "红外原始码发送成功"
    except FileNotFoundError:
        return False, "未找到 ir-ctl，请安装 v4l-utils，或用 LIRC 配置发送"
    except subprocess.TimeoutExpired:
        return False, "红外原始码发送超时"
    except subprocess.CalledProcessError as exc:
        return False, f"红外原始码发送失败: {exc}"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def ir_backend_status():
    status = {
        "backend": IR_BACKEND,
        "mock": MOCK_IR,
        "receive_device": IR_RECEIVE_DEVICE,
        "send_device": IR_SEND_DEVICE,
        "temp_adjust_modes": sorted(TEMP_ADJUST_MODES),
    }
    if IR_BACKEND in {"gpio", "gpio_pwm", "pwm"}:
        status.update(gpio_backend_status(
            rx_chip=IR_RX_CHIP,
            rx_line=IR_RX_LINE,
            pwm_chip=IR_PWM_CHIP,
            pwm_channel=IR_PWM_CHANNEL,
        ))
        status.update({
            "driver": IR_PWM_DRIVER,
            "carrier_hz": IR_CARRIER_HZ,
            "duty_cycle": IR_DUTY_CYCLE,
            "polarity": IR_PWM_POLARITY,
            "send_first_frame_only": IR_SEND_FIRST_FRAME_ONLY,
            "send_frame_gap_us": IR_SEND_FRAME_GAP_US,
            "capture_frame_gap_us": IR_CAPTURE_FRAME_GAP_US,
            "capture_max_edges": IR_CAPTURE_MAX_EDGES,
        })
    elif IR_BACKEND == "forwarder":
        status.update(forwarder_backend_status(
            port=IR_FORWARDER_PORT,
            baudrate=IR_FORWARDER_BAUDRATE,
            map_path=IR_FORWARDER_MAP_PATH,
        ))
    return status


def generate_lirc_config(data=None):
    data = data or load_ir_store()
    codes = data.get("codes", {})
    lines = [
        "begin remote",
        f"  name {REMOTE_NAME}",
        "  flags RAW_CODES",
        "  eps            30",
        "  aeps          100",
        "  gap          20000",
        "",
        "  begin raw_codes",
    ]
    for code, item in sorted(codes.items()):
        if not is_valid_state_code(code):
            continue
        raw = item.get("raw", [])
        if not raw:
            continue
        lines.append(f"    name {code}")
        for index in range(0, len(raw), 8):
            lines.append("      " + " ".join(str(int(value)) for value in raw[index:index + 8]))
        lines.append("")
    lines.extend([
        "  end raw_codes",
        "end remote",
        "",
    ])
    return "\n".join(lines)


def save_lirc_config():
    text = generate_lirc_config()
    LIRC_CONFIG_PATH.write_text(text, encoding="utf-8")
    return text


def ir_profiles():
    store = load_ir_store()
    learned = store.get("codes", {})
    try:
        forwarder_map = load_forwarder_map() if forwarder_backend_enabled() else {}
    except (OSError, ValueError):
        forwarder_map = {}
    profiles = []
    seen = set()

    for profile in DEFAULT_IR_PROFILES:
        code = profile["code"]
        item = learned.get(code, {})
        profiles.append({
            **profile,
            "learned": code in learned or code in forwarder_map,
            "updated_at": item.get("updated_at", ""),
            "raw_length": len(item.get("raw", [])),
            "forwarder_id": item.get("forwarder_id", forwarder_map.get(code)),
        })
        seen.add(code)

    for code, item in sorted(learned.items()):
        if code in seen or not is_valid_state_code(code):
            continue
        profiles.append({
            "code": code,
            "label": item.get("label") or state_code_label(code),
            "hint": "自定义状态码",
            "learned": True,
            "updated_at": item.get("updated_at", ""),
            "raw_length": len(item.get("raw", [])),
        })

    return profiles


def forwarder_backend_enabled():
    return IR_BACKEND == "forwarder"


def load_forwarder_map():
    return load_forwarder_code_map(IR_FORWARDER_MAP_PATH)


def save_forwarder_map(mapping):
    normalized = {str(code).upper(): int(slot) for code, slot in mapping.items()}
    write_json_file(IR_FORWARDER_MAP_PATH, dict(sorted(normalized.items())))
    return normalized


def _allocate_forwarder_slot(mapping):
    used = {int(slot) for slot in mapping.values()}
    for slot in range(1, IR_FORWARDER_MAX_CODES + 1):
        if slot not in used:
            return slot
    raise ValueError(f"红外转发模块 1..{IR_FORWARDER_MAX_CODES} 已无空闲编号")


def learn_forwarder_code(state_code, preferred_slot=None, timeout=18):
    code = str(state_code or "").upper()
    if not is_valid_state_code(code):
        return False, "无效状态码", None
    mapping = load_forwarder_map()
    if preferred_slot not in (None, ""):
        try:
            slot = int(str(preferred_slot).strip(), 0)
        except ValueError:
            return False, f"无效转发模块编号: {preferred_slot}", None
    else:
        slot = int(mapping[code]) if code in mapping else _allocate_forwarder_slot(mapping)
    success, message = learn_forwarder_to_slot(
        slot,
        port=IR_FORWARDER_PORT,
        baudrate=IR_FORWARDER_BAUDRATE,
        timeout=max(3, min(int(timeout or 18), 60)),
    )
    if success:
        mapping[code] = slot
        save_forwarder_map(mapping)
    return success, message, slot


def resolve_forwarder_code(state_code):
    mapping = load_forwarder_map()
    code = str(state_code or "").upper()
    if code in mapping:
        return code, mapping[code], []
    tried = []
    for fallback_code in fallback_state_codes(code):
        tried.append(fallback_code)
        if fallback_code in mapping:
            return fallback_code, mapping[fallback_code], tried
    return None, None, tried


def learned_raw_for(codes, code):
    item = codes.get(code)
    if item and item.get("raw"):
        return item.get("raw")
    return None


def fallback_state_codes(state_code):
    code = str(state_code or "").upper()
    fallbacks = []

    def add(candidate):
        if candidate and candidate != code and candidate not in fallbacks:
            fallbacks.append(candidate)

    if code == "AC_ON":
        fallback_state = dict(current_state)
        fallback_state["power"] = True
        add(build_ac_state_code(fallback_state))
        add("AC_COOL_26_AUTO")
        add("AC_COOL_26")
        add("AC_AUTO")
        return fallbacks

    match = re.match(r"^AC_(COOL|HEAT)_(\d+)_(AUTO|LOW|MID|HIGH)$", code)
    if match:
        mode, temperature, fan = match.groups()
        add(f"AC_{mode}_{temperature}")
        if fan != "AUTO":
            add(f"AC_{mode}_{temperature}_AUTO")
        for fan_code, _ in IR_FAN_OPTIONS:
            add(f"AC_{mode}_{temperature}_{fan_code}")
        return fallbacks

    match = re.match(r"^AC_(COOL|HEAT)_(\d+)$", code)
    if match:
        mode, temperature = match.groups()
        for fan_code, _ in IR_FAN_OPTIONS:
            add(f"AC_{mode}_{temperature}_{fan_code}")
        return fallbacks

    match = re.match(r"^AC_(DRY|FAN)_(\d+)_(AUTO|LOW|MID|HIGH)$", code)
    if match:
        mode, _temperature, fan = match.groups()
        add(f"AC_{mode}_{fan}")
        add(f"AC_{mode}")
        if fan != "AUTO":
            add(f"AC_{mode}_AUTO")
        for fan_code, _ in IR_FAN_OPTIONS:
            add(f"AC_{mode}_{fan_code}")
        return fallbacks

    match = re.match(r"^AC_(DRY|FAN)_(AUTO|LOW|MID|HIGH)$", code)
    if match:
        mode, fan = match.groups()
        add(f"AC_{mode}")
        if fan != "AUTO":
            add(f"AC_{mode}_AUTO")
        for fan_code, _ in IR_FAN_OPTIONS:
            add(f"AC_{mode}_{fan_code}")
        return fallbacks

    match = re.match(r"^AC_(DRY|FAN)$", code)
    if match:
        mode = match.group(1)
        for fan_code, _ in IR_FAN_OPTIONS:
            add(f"AC_{mode}_{fan_code}")
        return fallbacks

    return fallbacks


def resolve_learned_ir(codes, state_code):
    raw = learned_raw_for(codes, state_code)
    if raw:
        return state_code, raw, []

    tried = []
    for fallback_code in fallback_state_codes(state_code):
        tried.append(fallback_code)
        raw = learned_raw_for(codes, fallback_code)
        if raw:
            return fallback_code, raw, tried
    return None, None, tried


def send_ir(state_code):
    if os.getenv("IR_SEND_FAILURE", "").lower() in {"1", "true", "yes", "on"}:
        return False, "FIT 故障注入：红外发送失败"

    if MOCK_IR:
        return True, f"模拟发送状态码成功: {state_code}"

    if forwarder_backend_enabled():
        try:
            mapped_code, forwarder_id, tried = resolve_forwarder_code(state_code)
        except (OSError, ValueError) as exc:
            return False, f"红外转发模块映射读取失败: {type(exc).__name__}: {exc}"
        if forwarder_id is None:
            fallback_text = f"；已尝试降级状态码: {', '.join(tried)}" if tried else ""
            return False, f"{state_code} 尚未分配红外转发模块编号{fallback_text}"
        success, message = send_forwarder_stored_code(
            forwarder_id,
            port=IR_FORWARDER_PORT,
            baudrate=IR_FORWARDER_BAUDRATE,
            timeout=2.0,
        )
        if success and mapped_code != state_code:
            return True, f"{message}；{state_code} 未映射，已降级使用 {mapped_code}"
        return success, message

    store = load_ir_store()
    codes = store.get("codes", {})
    learned_code, raw, tried = resolve_learned_ir(codes, state_code)
    if raw:
        success, message = send_raw_ir(raw)
        if success and learned_code != state_code:
            return True, f"{message}；{state_code} 未学习，已降级使用 {learned_code}"
        return success, message

    if IR_BACKEND in {"gpio", "gpio_pwm", "pwm"}:
        fallback_text = f"，也未找到可降级状态码: {', '.join(tried)}" if tried else ""
        return False, f"{state_code} 还没有学习红外原始码{fallback_text}，请先在红外学习里学习这个状态码"

    try:
        subprocess.run(
            ["irsend", "SEND_ONCE", REMOTE_NAME, state_code],
            check=True,
            timeout=2,
        )
        return True, f"红外状态码发送成功: {state_code}"
    except FileNotFoundError:
        return False, "未找到 irsend，也没有可用原始码；请先在网页学习这个状态码"
    except subprocess.TimeoutExpired:
        return False, "红外状态码发送超时"
    except subprocess.CalledProcessError as exc:
        return False, f"红外状态码发送失败: {exc}"


def run_command(command):
    if command not in VALID_COMMANDS:
        return False, f"无效指令: {command}", None
    if command in {"light_toggle", "light_on", "light_off"}:
        target_light = (not current_state["light"]) if command == "light_toggle" else command == "light_on"
        if target_light == current_state["light"]:
            return True, "灯已经打开" if target_light else "灯已经关闭", "GPIO_LIGHT_ON" if target_light else "GPIO_LIGHT_OFF"

        success, message = send_light(target_light)
        if success:
            current_state["light"] = target_light
        return success, message, "GPIO_LIGHT_ON" if target_light else "GPIO_LIGHT_OFF"

    if command == "ac_off" and not current_state["power"] and MOCK_IR:
        return True, "空调已经关机", "AC_OFF"

    target_state, state_code = target_for_command(command)
    if not state_code:
        if command in {"temp_up", "temp_down"} and current_state.get("mode") in NON_TEMP_MODES:
            mode_text = MODE_LABELS.get(current_state.get("mode"), current_state.get("mode", "当前"))
            adjustable = "、".join(MODE_LABELS.get(mode, mode) for mode in sorted(TEMP_ADJUST_MODES)) or "无"
            return False, f"{mode_text}模式当前未启用调温；可调温模式: {adjustable}。如果你的空调在除湿/送风下支持温度，请设置 AC_TEMP_ADJUST_MODES。", build_ac_state_code(current_state)
        return False, f"无效指令: {command}", None

    success, message = send_ir(state_code)
    if success:
        current_state.update(target_state)
        message = f"{message}（{state_code}）"
    return success, message, state_code
