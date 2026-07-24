from datetime import datetime, timedelta
import re
import threading
import time

from device_control import (
    COMMAND_LABELS,
    VALID_COMMANDS,
    current_state,
    run_command,
    update_environment,
)
from storage import BASE_DIR, now_iso, read_json_file, write_json_file


TIMER_STORE_PATH = BASE_DIR / "timers.json"
EVENT_LOG_PATH = BASE_DIR / "event_logs.json"
RULE_STORE_PATH = BASE_DIR / "rules.json"
EVENT_LOG_LIMIT = 200

timer_tasks = []
timer_lock = threading.Lock()
timer_next_id = 1

event_logs = []
event_log_lock = threading.Lock()

automation_rules = []
rule_lock = threading.Lock()
rule_next_id = 1

RULE_CONDITIONS = {
    "temperature_gt": {"label": "室温高于", "unit": "°C", "value_type": "number"},
    "temperature_lt": {"label": "室温低于", "unit": "°C", "value_type": "number"},
    "humidity_gt": {"label": "湿度高于", "unit": "%", "value_type": "number"},
    "humidity_lt": {"label": "湿度低于", "unit": "%", "value_type": "number"},
    "time_at": {"label": "每天时间到", "unit": "HH:MM", "value_type": "time"},
}

RULE_COMMANDS = {
    "ac_on",
    "mode_cool",
    "mode_heat",
    "mode_dry",
    "mode_fan",
    "mode_auto",
    "fan_auto",
    "fan_low",
    "fan_mid",
    "fan_high",
    "ac_off",
    "light_on",
    "light_off",
}

RULE_PRESETS = [
    {
        "id": "hot_to_cool",
        "name": "室温高于28度自动制冷",
        "condition": "temperature_gt",
        "value": 28,
        "command": "mode_cool",
        "cooldown_minutes": 30,
    },
    {
        "id": "cold_to_heat",
        "name": "室温低于18度自动制热",
        "condition": "temperature_lt",
        "value": 18,
        "command": "mode_heat",
        "cooldown_minutes": 30,
    },
    {
        "id": "humid_to_dry",
        "name": "湿度高于65%自动除湿",
        "condition": "humidity_gt",
        "value": 65,
        "command": "mode_dry",
        "cooldown_minutes": 40,
    },
    {
        "id": "night_light_off",
        "name": "23:30自动关灯",
        "condition": "time_at",
        "value": "23:30",
        "command": "light_off",
        "cooldown_minutes": 1440,
    },
    {
        "id": "sleep_ac_off",
        "name": "00:30自动关空调",
        "condition": "time_at",
        "value": "00:30",
        "command": "ac_off",
        "cooldown_minutes": 1440,
    },
]

CN_NUMBERS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def load_event_logs():
    global event_logs
    data = read_json_file(EVENT_LOG_PATH, [])
    if isinstance(data, dict):
        data = data.get("logs", [])
    if not isinstance(data, list):
        data = []
    event_logs = data[-EVENT_LOG_LIMIT:]


def save_event_logs_unlocked():
    write_json_file(EVENT_LOG_PATH, {
        "updated_at": now_iso(),
        "logs": event_logs[-EVENT_LOG_LIMIT:],
    })


def add_log(category, message, detail=None, success=True):
    item = {
        "time": now_iso(),
        "category": category,
        "message": message,
        "detail": detail or {},
        "success": bool(success),
    }
    with event_log_lock:
        event_logs.append(item)
        del event_logs[:-EVENT_LOG_LIMIT]
        save_event_logs_unlocked()
    return item


def recent_logs(limit=50):
    with event_log_lock:
        return list(reversed(event_logs[-limit:]))


def clear_logs():
    with event_log_lock:
        event_logs.clear()
        save_event_logs_unlocked()


def parse_amount(value):
    if value.isdigit():
        return int(value)
    if value == "十":
        return 10
    if "十" in value:
        tens_text, ones_text = value.split("十", 1)
        tens = CN_NUMBERS.get(tens_text, 1) if tens_text else 1
        ones = CN_NUMBERS.get(ones_text, 0) if ones_text else 0
        return tens * 10 + ones
    return CN_NUMBERS.get(value)


def normalize_timer_task(task):
    if not isinstance(task, dict):
        return None
    try:
        task_id = int(task.get("id", 0))
    except (TypeError, ValueError):
        return None
    commands = [command for command in task.get("commands", []) if command in VALID_COMMANDS]
    if task_id <= 0 or not commands:
        return None

    status = task.get("status", "pending")
    if status not in {"pending", "running", "done", "failed", "cancelled"}:
        status = "pending"
    if status == "running":
        status = "pending"

    run_at = task.get("run_at") or now_iso()
    try:
        datetime.strptime(run_at, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        run_at = now_iso()

    normalized = {
        "id": task_id,
        "label": str(task.get("label") or "定时任务"),
        "commands": commands,
        "command_labels": [COMMAND_LABELS.get(command, command) for command in commands],
        "run_at": run_at,
        "remaining_seconds": int(task.get("remaining_seconds", 0) or 0),
        "status": status,
        "message": str(task.get("message") or "等待执行"),
        "created_at": task.get("created_at") or now_iso(),
    }
    if task.get("finished_at"):
        normalized["finished_at"] = task["finished_at"]
    return normalized


def load_timers_from_disk():
    global timer_tasks, timer_next_id
    data = read_json_file(TIMER_STORE_PATH, {})
    stored_tasks = data.get("timers", []) if isinstance(data, dict) else data
    if not isinstance(stored_tasks, list):
        stored_tasks = []

    loaded = []
    for task in stored_tasks:
        normalized = normalize_timer_task(task)
        if normalized:
            loaded.append(normalized)

    timer_tasks = loaded[-100:]
    timer_next_id = max([task["id"] for task in timer_tasks], default=0) + 1


def save_timers_unlocked():
    write_json_file(TIMER_STORE_PATH, {
        "updated_at": now_iso(),
        "timers": timer_tasks[-100:],
    })


def schedule_timer(commands, delay_seconds, label):
    global timer_next_id
    run_at = datetime.now() + timedelta(seconds=delay_seconds)
    with timer_lock:
        task = {
            "id": timer_next_id,
            "label": label,
            "commands": commands,
            "command_labels": [COMMAND_LABELS.get(command, command) for command in commands],
            "run_at": run_at.strftime("%Y-%m-%d %H:%M:%S"),
            "remaining_seconds": max(0, int((run_at - datetime.now()).total_seconds())),
            "status": "pending",
            "message": "等待执行",
            "created_at": now_iso(),
        }
        timer_next_id += 1
        timer_tasks.append(task)
        save_timers_unlocked()
        task_copy = dict(task)
    add_log("定时", f"创建定时: {label}", {
        "commands": task_copy["command_labels"],
        "run_at": task_copy["run_at"],
    })
    return task_copy


def visible_timers():
    with timer_lock:
        now = datetime.now()
        result = []
        for task in timer_tasks[-20:]:
            item = dict(task)
            if item["status"] == "pending":
                run_at = datetime.strptime(item["run_at"], "%Y-%m-%d %H:%M:%S")
                item["remaining_seconds"] = max(0, int((run_at - now).total_seconds()))
            result.append(item)
        return result


def cancel_timer_task(timer_id):
    cancelled = False
    with timer_lock:
        for task in timer_tasks:
            if task["id"] == timer_id and task["status"] == "pending":
                task["status"] = "cancelled"
                task["message"] = "已取消"
                cancelled = True
                save_timers_unlocked()
                break
    if cancelled:
        add_log("定时", f"取消定时: #{timer_id}")
    return cancelled


def timer_worker():
    while True:
        due_tasks = []
        with timer_lock:
            now = datetime.now()
            for task in timer_tasks:
                if task["status"] != "pending":
                    continue
                run_at = datetime.strptime(task["run_at"], "%Y-%m-%d %H:%M:%S")
                if run_at <= now:
                    task["status"] = "running"
                    due_tasks.append(task)
            if due_tasks:
                save_timers_unlocked()

        for task in due_tasks:
            messages = []
            ok = True
            add_log("定时", f"开始执行定时: {task['label']}", {
                "commands": task.get("command_labels", task.get("commands", [])),
            })
            for command in task["commands"]:
                success, message, _ = run_command(command)
                messages.append(message)
                ok = ok and success

            with timer_lock:
                task["status"] = "done" if ok else "failed"
                task["message"] = "；".join(messages)
                task["finished_at"] = now_iso()
                save_timers_unlocked()
            add_log("定时", f"定时执行{'成功' if ok else '失败'}: {task['label']}", {
                "message": task["message"],
            }, success=ok)

        time.sleep(1)


def parse_rule_value(condition, value):
    value = str(value).strip()
    if RULE_CONDITIONS[condition]["value_type"] == "time":
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", value):
            raise ValueError("时间格式应为 HH:MM，例如 23:30")
        hour_text, minute_text = value.split(":", 1)
        return f"{int(hour_text):02d}:{int(minute_text):02d}"

    number = float(value)
    if condition.startswith("temperature") and not 0 <= number <= 50:
        raise ValueError("温度阈值建议在 0 到 50 之间")
    if condition.startswith("humidity") and not 0 <= number <= 100:
        raise ValueError("湿度阈值必须在 0 到 100 之间")
    return number


def default_rule_name(condition, value, command):
    condition_label = RULE_CONDITIONS[condition]["label"]
    unit = RULE_CONDITIONS[condition]["unit"]
    value_text = value if isinstance(value, str) else f"{value:g}{unit}"
    return f"{condition_label}{value_text} -> {COMMAND_LABELS.get(command, command)}"


def normalize_rule(rule):
    if not isinstance(rule, dict):
        return None
    try:
        rule_id = int(rule.get("id", 0))
    except (TypeError, ValueError):
        return None

    condition = rule.get("condition")
    command = rule.get("command")
    if rule_id <= 0 or condition not in RULE_CONDITIONS or command not in RULE_COMMANDS:
        return None

    try:
        value = parse_rule_value(condition, rule.get("value", ""))
    except (TypeError, ValueError):
        return None

    try:
        cooldown_minutes = int(rule.get("cooldown_minutes", 30))
    except (TypeError, ValueError):
        cooldown_minutes = 30
    cooldown_minutes = max(1, min(cooldown_minutes, 1440))

    return {
        "id": rule_id,
        "name": str(rule.get("name") or default_rule_name(condition, value, command)),
        "condition": condition,
        "condition_label": RULE_CONDITIONS[condition]["label"],
        "value": value,
        "unit": RULE_CONDITIONS[condition]["unit"],
        "command": command,
        "command_label": COMMAND_LABELS.get(command, command),
        "enabled": bool(rule.get("enabled", True)),
        "cooldown_minutes": cooldown_minutes,
        "last_triggered_at": rule.get("last_triggered_at", ""),
        "last_triggered_date": rule.get("last_triggered_date", ""),
        "trigger_count": int(rule.get("trigger_count", 0) or 0),
        "message": str(rule.get("message") or "等待触发"),
        "created_at": rule.get("created_at") or now_iso(),
    }


def load_rules_from_disk():
    global automation_rules, rule_next_id
    data = read_json_file(RULE_STORE_PATH, {})
    stored_rules = data.get("rules", []) if isinstance(data, dict) else data
    if not isinstance(stored_rules, list):
        stored_rules = []

    loaded = []
    for rule in stored_rules:
        normalized = normalize_rule(rule)
        if normalized:
            loaded.append(normalized)

    automation_rules = loaded[-100:]
    rule_next_id = max([rule["id"] for rule in automation_rules], default=0) + 1


def save_rules_unlocked():
    write_json_file(RULE_STORE_PATH, {
        "updated_at": now_iso(),
        "rules": automation_rules[-100:],
    })


def visible_rules():
    with rule_lock:
        return [dict(rule) for rule in automation_rules]


def visible_rule_presets():
    return [dict(preset) for preset in RULE_PRESETS]


def create_rule(data):
    global rule_next_id
    condition = data.get("condition")
    command = data.get("command")
    if condition not in RULE_CONDITIONS:
        raise ValueError("不支持的规则条件")
    if command not in RULE_COMMANDS:
        raise ValueError("这个命令不适合作为自动化动作")

    value = parse_rule_value(condition, data.get("value", ""))
    try:
        cooldown_minutes = int(data.get("cooldown_minutes", 30))
    except (TypeError, ValueError):
        cooldown_minutes = 30
    cooldown_minutes = max(1, min(cooldown_minutes, 1440))

    with rule_lock:
        rule = normalize_rule({
            "id": rule_next_id,
            "name": data.get("name") or default_rule_name(condition, value, command),
            "condition": condition,
            "value": value,
            "command": command,
            "enabled": bool(data.get("enabled", True)),
            "cooldown_minutes": cooldown_minutes,
            "created_at": now_iso(),
        })
        rule_next_id += 1
        automation_rules.append(rule)
        save_rules_unlocked()
        rule_copy = dict(rule)
    add_log("自动化", f"新增规则: {rule_copy['name']}", {
        "command": rule_copy["command_label"],
    })
    return rule_copy


def create_rule_from_preset(preset_id):
    preset = next((item for item in RULE_PRESETS if item["id"] == preset_id), None)
    if not preset:
        raise ValueError("未找到推荐规则")
    return create_rule(dict(preset))


def set_rule_enabled(rule_id, enabled):
    updated = None
    with rule_lock:
        for rule in automation_rules:
            if rule["id"] == rule_id:
                rule["enabled"] = enabled
                rule["message"] = "已启用" if enabled else "已停用"
                updated = dict(rule)
                save_rules_unlocked()
                break
    if updated:
        add_log("自动化", f"{'启用' if enabled else '停用'}规则: {updated['name']}")
    return updated


def delete_rule(rule_id):
    deleted = None
    with rule_lock:
        for index, rule in enumerate(automation_rules):
            if rule["id"] == rule_id:
                deleted = automation_rules.pop(index)
                save_rules_unlocked()
                break
    if deleted:
        add_log("自动化", f"删除规则: {deleted['name']}")
    return deleted


def rule_target_satisfied(command):
    if command == "ac_on":
        return current_state["power"]
    if command == "light_on":
        return current_state["light"]
    if command == "light_off":
        return not current_state["light"]
    if command == "ac_off":
        return not current_state["power"]
    if command.startswith("mode_"):
        mode = command.replace("mode_", "", 1)
        return current_state["power"] and current_state["mode"] == mode
    if command.startswith("fan_"):
        fan = command.replace("fan_", "", 1)
        return current_state["power"] and current_state["fan"] == fan
    return False


def rule_on_cooldown(rule, now):
    if rule["condition"] == "time_at" and rule.get("last_triggered_date") == now.strftime("%Y-%m-%d"):
        return True
    if not rule.get("last_triggered_at"):
        return False
    try:
        last = datetime.strptime(rule["last_triggered_at"], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return now - last < timedelta(minutes=rule["cooldown_minutes"])


def rule_condition_matched(rule, environment, now):
    condition = rule["condition"]
    value = rule["value"]
    if condition.startswith(("temperature_", "humidity_")) and not environment.get("sensor_valid", True):
        return False
    if condition == "temperature_gt":
        return float(environment["room_temperature"]) > float(value)
    if condition == "temperature_lt":
        return float(environment["room_temperature"]) < float(value)
    if condition == "humidity_gt":
        return float(environment["humidity"]) > float(value)
    if condition == "humidity_lt":
        return float(environment["humidity"]) < float(value)
    if condition == "time_at":
        return now.strftime("%H:%M") == value
    return False


def automation_worker():
    while True:
        now = datetime.now()
        environment = update_environment()
        due_rules = []

        with rule_lock:
            for rule in automation_rules:
                if not rule["enabled"]:
                    continue
                if (
                    rule["condition"].startswith(("temperature_", "humidity_"))
                    and not environment.get("sensor_valid", True)
                ):
                    rule["message"] = "等待有效的 DHT11 数据"
                    continue
                if rule_target_satisfied(rule["command"]):
                    rule["message"] = "目标状态已满足"
                    continue
                if rule_on_cooldown(rule, now):
                    continue
                if rule_condition_matched(rule, environment, now):
                    rule["last_triggered_at"] = now_iso()
                    rule["last_triggered_date"] = now.strftime("%Y-%m-%d")
                    rule["trigger_count"] += 1
                    rule["message"] = "正在执行"
                    due_rules.append(dict(rule))
            if due_rules:
                save_rules_unlocked()

        for rule in due_rules:
            success, message, state_code = run_command(rule["command"])
            with rule_lock:
                for stored_rule in automation_rules:
                    if stored_rule["id"] == rule["id"]:
                        stored_rule["message"] = message
                        break
                save_rules_unlocked()
            add_log("自动化", f"规则触发{'成功' if success else '失败'}: {rule['name']}", {
                "command": rule["command_label"],
                "state_code": state_code,
                "message": message,
            }, success=success)

        time.sleep(10)


def initialize_runtime():
    load_event_logs()
    load_timers_from_disk()
    load_rules_from_disk()
    add_log("系统", "服务启动", {
        "pending_timers": len([task for task in timer_tasks if task["status"] == "pending"]),
        "enabled_rules": len([rule for rule in automation_rules if rule["enabled"]]),
    })


def start_workers():
    threading.Thread(target=timer_worker, daemon=True).start()
    threading.Thread(target=automation_worker, daemon=True).start()
