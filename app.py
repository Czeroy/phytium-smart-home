import hashlib
import hmac
import os
import secrets
import traceback
import time

from flask import Flask, Response, jsonify, make_response, request

from feedback_store import (
    add_feedback,
    deduplicate_feedback,
    feedback_summary,
    generate_training_data,
    recent_feedback,
    update_feedback,
)
from health_service import build_health
from hardware_layer import build_hardware_profile
from device_control import (
    COMMAND_LABELS,
    IR_BACKEND,
    LIGHT_OFF_CMD,
    LIGHT_ON_CMD,
    IR_RECEIVE_DEVICE,
    IR_SEND_DEVICE,
    MOCK_IR,
    MOCK_LIGHT,
    REMOTE_NAME,
    TEMP_ADJUST_MODES,
    VALID_COMMANDS,
    build_ac_state_code,
    capture_ir_raw,
    clear_ir_store,
    create_device,
    current_state,
    delete_device,
    forwarder_backend_enabled,
    get_device,
    get_selected_device_id,
    ir_backend_status,
    ir_profiles,
    is_valid_state_code,
    learn_forwarder_code,
    load_forwarder_map,
    list_devices,
    save_ir_store,
    save_forwarder_map,
    save_lirc_config,
    select_device,
    send_ir,
    state_code_label,
    update_environment,
    load_ir_store,
)
from intent_service import corrected_intent_result
from runtime_store import (
    RULE_COMMANDS,
    RULE_CONDITIONS,
    add_log,
    cancel_timer_task,
    clear_logs,
    create_rule,
    delete_rule,
    create_rule_from_preset,
    initialize_runtime,
    recent_logs,
    schedule_timer,
    set_rule_enabled,
    start_workers,
    visible_rule_presets,
    visible_rules,
    visible_timers,
)
from project_overview import build_project_overview
from storage import BASE_DIR, now_iso


app = Flask(__name__)
response_cache = {}
AUTH_PASSWORD = ""
AUTH_SECRET = os.getenv("SMART_HOME_SECRET_KEY", "").strip() or AUTH_PASSWORD or secrets.token_hex(32)
AUTH_COOKIE_NAME = "smart_home_auth"
AUTH_TTL_SECONDS = int(os.getenv("SMART_HOME_AUTH_TTL_SECONDS", "86400"))
AUTH_COOKIE_SECURE = os.getenv("SMART_HOME_COOKIE_SECURE", "0").lower() in {"1", "true", "yes", "on"}
LOGIN_ATTEMPTS = {}
LOGIN_WINDOW_SECONDS = 300
LOGIN_MAX_ATTEMPTS = 8


def auth_enabled():
    return False


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.remote_addr or "unknown"


def _sign_auth_payload(payload):
    return hmac.new(AUTH_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def make_auth_token():
    issued_at = str(int(time.time()))
    nonce = secrets.token_hex(8)
    payload = f"{issued_at}:{nonce}"
    return f"{payload}:{_sign_auth_payload(payload)}"


def check_auth_token(token):
    if not auth_enabled() or not token:
        return not auth_enabled()
    parts = str(token).split(":")
    if len(parts) != 3:
        return False
    issued_at, nonce, signature = parts
    payload = f"{issued_at}:{nonce}"
    expected = _sign_auth_payload(payload)
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        age = time.time() - int(issued_at)
    except ValueError:
        return False
    return 0 <= age <= AUTH_TTL_SECONDS


def is_authenticated():
    return check_auth_token(request.cookies.get(AUTH_COOKIE_NAME))


def too_many_login_attempts(ip):
    now = time.monotonic()
    attempts = [stamp for stamp in LOGIN_ATTEMPTS.get(ip, []) if now - stamp < LOGIN_WINDOW_SECONDS]
    LOGIN_ATTEMPTS[ip] = attempts
    return len(attempts) >= LOGIN_MAX_ATTEMPTS


def record_login_failure(ip):
    now = time.monotonic()
    attempts = [stamp for stamp in LOGIN_ATTEMPTS.get(ip, []) if now - stamp < LOGIN_WINDOW_SECONDS]
    attempts.append(now)
    LOGIN_ATTEMPTS[ip] = attempts


@app.before_request
def require_auth_for_api():
    if request.method == "OPTIONS":
        return None
    if not auth_enabled():
        return None
    if request.path == "/" or request.path.startswith("/api/auth/"):
        return None
    if request.path.startswith("/api/") and not is_authenticated():
        return jsonify({
            "success": False,
            "auth_required": True,
            "message": "请先登录后再控制设备",
        }), 401
    return None


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), camera=()")
    if request.path == "/" or request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


def cached_json(key, ttl_seconds, builder):
    now = time.monotonic()
    cached = response_cache.get(key)
    if cached and now - cached["time"] < ttl_seconds:
        return jsonify(cached["data"])

    data = builder()
    response_cache[key] = {"time": now, "data": data}
    return jsonify(data)


def request_device_id():
    if request.method == "GET":
        value = request.args.get("device_id", "")
    else:
        data = request.get_json(silent=True) or {}
        value = data.get("device_id", "")
    value = str(value or "").strip()
    return value or None


def device_payload(device_id=None):
    selected_id = get_selected_device_id()
    active = get_device(device_id or selected_id)
    return {
        "devices": list_devices(),
        "selected_device_id": selected_id,
        "device": {
            "id": active["id"],
            "name": active["name"],
            "location": active.get("location", ""),
            "kind": active.get("kind", "ac"),
        } if active else None,
    }


@app.route("/api/auth/status")
def auth_status():
    return jsonify({
        "success": True,
        "auth_required": auth_enabled(),
        "authenticated": is_authenticated(),
        "https": request.is_secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https",
        "message": "已启用访问密码" if auth_enabled() else "未设置访问密码，公网暴露时不安全",
    })


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    if not auth_enabled():
        return jsonify({
            "success": True,
            "auth_required": False,
            "message": "未启用访问密码",
        })

    ip = _client_ip()
    if too_many_login_attempts(ip):
        return jsonify({
            "success": False,
            "message": "登录失败次数过多，请几分钟后再试",
        }), 429

    data = request.get_json(silent=True) or {}
    password = str(data.get("password", ""))
    if not hmac.compare_digest(password, AUTH_PASSWORD):
        record_login_failure(ip)
        add_log("安全", f"登录失败: {ip}", {"ip": ip}, success=False)
        return jsonify({
            "success": False,
            "message": "密码不正确",
        }), 401

    LOGIN_ATTEMPTS.pop(ip, None)
    response = make_response(jsonify({
        "success": True,
        "auth_required": True,
        "message": "登录成功",
    }))
    response.set_cookie(
        AUTH_COOKIE_NAME,
        make_auth_token(),
        max_age=AUTH_TTL_SECONDS,
        httponly=True,
        secure=AUTH_COOKIE_SECURE,
        samesite="Lax",
    )
    add_log("安全", f"登录成功: {ip}", {"ip": ip})
    return response


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    response = make_response(jsonify({"success": True, "message": "已退出登录"}))
    response.delete_cookie(AUTH_COOKIE_NAME)
    return response


@app.route("/")
def index():
    return (BASE_DIR / "index.html").read_text(encoding="utf-8")


@app.route("/api/devices", methods=["GET", "POST"])
def devices_api():
    if request.method == "GET":
        return jsonify(device_payload())

    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"success": False, "message": "请输入设备名称"}), 400
    location = str(data.get("location", "")).strip()
    created = create_device(name, location=location)
    add_log("设备", f"新增设备: {created['name']}", {
        "device_id": created["id"],
        "location": created.get("location", ""),
    })
    return jsonify({
        "success": True,
        "message": f"已新增设备: {created['name']}",
        **device_payload(created["id"]),
    })


@app.route("/api/devices/select", methods=["POST"])
def select_device_api():
    data = request.get_json(silent=True) or {}
    device_id = str(data.get("device_id", "")).strip()
    if not device_id:
        return jsonify({"success": False, "message": "缺少 device_id"}), 400
    selected = select_device(device_id)
    if not selected:
        return jsonify({"success": False, "message": "未找到设备"}), 404
    add_log("设备", f"切换设备: {selected['name']}", {"device_id": selected["id"]})
    return jsonify({
        "success": True,
        "message": f"当前控制设备已切换为 {selected['name']}",
        **device_payload(selected["id"]),
    })


@app.route("/api/devices/<device_id>", methods=["DELETE"])
def delete_device_api(device_id):
    target = get_device(device_id)
    if not target:
        return jsonify({"success": False, "message": "未找到设备"}), 404
    try:
        removed = delete_device(device_id)
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    if not removed:
        return jsonify({"success": False, "message": "未找到设备"}), 404
    add_log("设备", f"删除设备: {target['name']}", {"device_id": device_id})
    return jsonify({
        "success": True,
        "message": f"已删除设备: {target['name']}",
        **device_payload(),
    })


@app.route("/api/command", methods=["POST"])
def handle_command():
    from device_control import run_command
    from realtime_bridge import realtime_status, send_realtime_command

    data = request.get_json(silent=True) or {}
    command = data.get("command")
    if not command:
        return jsonify({"success": False, "message": "缺少 command 参数"}), 400

    device_id = str(data.get("device_id", "")).strip() or None
    if device_id:
        selected = select_device(device_id)
        if not selected:
            return jsonify({"success": False, "message": "未找到设备"}), 404

    realtime_result = None
    try:
        realtime = realtime_status()
        if realtime.get("command_ready"):
            rt_command = "light_command" if str(command).startswith("light_") else "ac_command"
            rt_success, realtime_result = send_realtime_command(rt_command, {
                "ui_command": command,
                "label": COMMAND_LABELS.get(command, command),
                "device_id": get_selected_device_id(),
            })
            realtime_result["success"] = rt_success
            realtime_result["dispatch_mode"] = "realtime_forwarded"
        else:
            realtime_result = {
                "success": None,
                "dispatch_mode": "linux_fallback",
                "message": "实时命令通道未就绪，已由 Linux 控制层直接执行",
                "status": realtime,
            }
    except Exception as exc:
        realtime_result = {
            "success": False,
            "dispatch_mode": "linux_fallback_after_error",
            "message": f"实时桥接异常，已降级到 Linux: {type(exc).__name__}: {exc}",
        }

    success, message, state_code = run_command(command)
    add_log("控制", f"{COMMAND_LABELS.get(command, command)}: {message}", {
        "command": command,
        "state_code": state_code,
        "device_id": get_selected_device_id(),
        "realtime": realtime_result,
    }, success=success)
    status_code = 200 if success else 400
    return jsonify({
        "success": success,
        "message": message,
        "state_code": state_code,
        "state": dict(current_state),
        "realtime": realtime_result,
        **device_payload(),
    }), status_code


@app.route("/api/intent", methods=["POST"])
def get_intent():
    data = request.get_json(silent=True) or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"success": False, "message": "请输入文本"}), 400

    try:
        result = corrected_intent_result(text)
        add_log("AI", f"识别文本: {text}", {
            "intents": result.get("intents", []),
            "commands": result.get("command_labels", []),
            "timer": result.get("timer"),
        }, success=result.get("success", False))
        return jsonify(result)
    except Exception as exc:
        traceback.print_exc()
        add_log("AI", f"意图识别出错: {text}", {
            "error": f"{type(exc).__name__}: {exc}",
        }, success=False)
        return jsonify({
            "success": False,
            "message": f"AI意图识别出错: {type(exc).__name__}: {exc}",
        }), 500


@app.route("/api/environment")
def get_environment():
    return jsonify(update_environment())


@app.route("/api/health")
def get_health():
    return cached_json("health", 8, build_health)


@app.route("/api/hardware")
def get_hardware():
    return jsonify(build_hardware_profile())


@app.route("/api/realtime/status")
def get_realtime_status():
    from realtime_bridge import realtime_status

    return jsonify(realtime_status())


@app.route("/api/realtime/command", methods=["POST"])
def post_realtime_command():
    from realtime_bridge import send_realtime_command

    data = request.get_json(silent=True) or {}
    command = data.get("command", "ping")
    payload = data.get("payload", {})
    success, result = send_realtime_command(command, payload)
    add_log("Realtime", result.get("message", command), {
        "command": command,
        "payload": payload,
        "backend": result.get("status", {}).get("backend"),
    }, success=success)
    status_code = 200 if success else 400
    return jsonify({"success": success, **result}), status_code


@app.route("/api/overview")
def get_overview():
    return cached_json("overview", 8, build_project_overview)


@app.route("/api/feedback", methods=["GET", "POST"])
def feedback():
    if request.method == "GET":
        try:
            limit = int(request.args.get("limit", 50))
        except ValueError:
            limit = 50
        return jsonify({
            "items": recent_feedback(limit),
            "summary": feedback_summary(),
        })

    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    if not str(text).strip():
        return jsonify({"success": False, "message": "缺少语音文本"}), 400

    verdict = data.get("verdict", "wrong")
    note = data.get("note", "")
    result = data.get("result", {})
    item = add_feedback(
        text,
        result=result,
        verdict=verdict,
        note=note,
        expected_intents=data.get("expected_intents"),
        expected_commands=data.get("expected_commands"),
    )
    add_log("AI反馈", f"{'识别正确' if verdict == 'correct' else '识别错误'}: {text}", {
        "note": note,
        "intents": result.get("intents", []) if isinstance(result, dict) else [],
    }, success=verdict == "correct")
    return jsonify({"success": True, "feedback": item})


@app.route("/api/feedback/<int:feedback_id>", methods=["PATCH", "PUT"])
def patch_feedback(feedback_id):
    data = request.get_json(silent=True) or {}
    item = update_feedback(feedback_id, data)
    if not item:
        return jsonify({"success": False, "message": "未找到反馈记录"}), 404
    add_log("AI反馈", f"更新纠错样本: #{feedback_id}", {
        "expected_intents": item.get("expected_intents", []),
        "expected_commands": item.get("expected_commands", []),
    })
    return jsonify({
        "success": True,
        "feedback": item,
        "summary": feedback_summary(),
        "items": recent_feedback(50),
    })


@app.route("/api/feedback/training-data", methods=["POST"])
def make_feedback_training_data():
    payload = generate_training_data()
    add_log("AI反馈", "生成纠错训练数据", {
        "count": payload["count"],
    })
    return jsonify({"success": True, **payload})


@app.route("/api/feedback/deduplicate", methods=["POST"])
def dedupe_feedback():
    payload = deduplicate_feedback()
    add_log("AI反馈", "清理重复纠错样本", {
        "removed": payload["removed"],
    })
    return jsonify({"success": True, **payload})


@app.route("/api/timers", methods=["GET", "POST"])
def timers():
    if request.method == "GET":
        return jsonify({"timers": visible_timers()})

    data = request.get_json(silent=True) or {}
    commands = data.get("commands")
    command = data.get("command")
    if not commands and command:
        commands = [command]
    if not commands:
        return jsonify({"success": False, "message": "缺少定时命令"}), 400
    commands = [command for command in commands if command in VALID_COMMANDS]
    if not commands:
        return jsonify({"success": False, "message": "没有可执行的定时命令"}), 400

    delay_seconds = data.get("delay_seconds")
    if delay_seconds is None:
        delay_minutes = float(data.get("delay_minutes", 0))
        delay_seconds = int(delay_minutes * 60)
    delay_seconds = int(delay_seconds)
    if delay_seconds <= 0:
        return jsonify({"success": False, "message": "定时时间必须大于 0"}), 400

    task = schedule_timer(commands, delay_seconds, data.get("label", "手动定时"))
    return jsonify({"success": True, "timer": task, "timers": visible_timers()})


@app.route("/api/timers/<int:timer_id>", methods=["DELETE"])
def cancel_timer(timer_id):
    if cancel_timer_task(timer_id):
        return jsonify({"success": True, "timers": visible_timers()})
    return jsonify({"success": False, "message": "未找到可取消的定时任务"}), 404


@app.route("/api/logs", methods=["GET", "DELETE"])
def logs():
    if request.method == "DELETE":
        clear_logs()
        return jsonify({"success": True, "logs": []})

    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 200))
    return jsonify({"logs": recent_logs(limit)})


@app.route("/api/rules", methods=["GET", "POST"])
def rules():
    if request.method == "GET":
        return jsonify({
            "rules": visible_rules(),
            "conditions": RULE_CONDITIONS,
            "commands": [
                {"command": command, "label": COMMAND_LABELS.get(command, command)}
                for command in sorted(RULE_COMMANDS)
            ],
        })

    data = request.get_json(silent=True) or {}
    try:
        rule = create_rule(data)
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    return jsonify({"success": True, "rule": rule, "rules": visible_rules()})


@app.route("/api/rules/presets")
def rule_presets():
    return jsonify({"presets": visible_rule_presets()})


@app.route("/api/rules/presets/<preset_id>", methods=["POST"])
def create_preset_rule(preset_id):
    try:
        rule = create_rule_from_preset(preset_id)
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    return jsonify({"success": True, "rule": rule, "rules": visible_rules()})


@app.route("/api/rules/<int:rule_id>/toggle", methods=["POST"])
def toggle_rule(rule_id):
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", True))
    updated = set_rule_enabled(rule_id, enabled)
    if not updated:
        return jsonify({"success": False, "message": "未找到规则"}), 404
    return jsonify({"success": True, "rule": updated, "rules": visible_rules()})


@app.route("/api/rules/<int:rule_id>", methods=["DELETE"])
def remove_rule(rule_id):
    if not delete_rule(rule_id):
        return jsonify({"success": False, "message": "未找到规则"}), 404
    return jsonify({"success": True, "rules": visible_rules()})


@app.route("/api/ir/profiles")
def get_ir_profiles():
    device_id = request_device_id()
    if device_id:
        selected = select_device(device_id)
        if not selected:
            return jsonify({"success": False, "message": "未找到设备"}), 404
    active_state = dict(current_state)
    current_ac_code = build_ac_state_code(active_state)
    profiles = ir_profiles()
    learned_count = len([profile for profile in profiles if profile.get("learned")])
    return jsonify({
        "mock": MOCK_IR,
        "backend": IR_BACKEND,
        "backend_status": ir_backend_status(),
        "receive_device": IR_RECEIVE_DEVICE,
        "send_device": IR_SEND_DEVICE,
        "remote_name": REMOTE_NAME,
        "light_control": {
            "mock": MOCK_LIGHT,
            "configured": bool(LIGHT_ON_CMD and LIGHT_OFF_CMD),
        },
        "current_ac_code": current_ac_code,
        "current_ac_label": state_code_label(current_ac_code),
        "summary": {
            "total": len(profiles),
            "learned": learned_count,
            "missing": len(profiles) - learned_count,
        },
        "profiles": profiles,
        **device_payload(),
    })


@app.route("/api/ir/learn", methods=["POST"])
def learn_ir_code():
    data = request.get_json(silent=True) or {}
    device_id = str(data.get("device_id", "")).strip() or None
    if device_id:
        selected = select_device(device_id)
        if not selected:
            return jsonify({"success": False, "message": "未找到设备"}), 404
    code = str(data.get("code", "")).strip().upper()
    if not is_valid_state_code(code):
        return jsonify({"success": False, "message": "无效状态码"}), 400

    label = str(data.get("label") or state_code_label(code)).strip()
    try:
        timeout = int(data.get("timeout", 18))
    except (TypeError, ValueError):
        timeout = 18
    try:
        frame_gap_us = int(data.get("frame_gap_us", 120000))
    except (TypeError, ValueError):
        frame_gap_us = 120000
    try:
        max_edges = int(data.get("max_edges", 1200))
    except (TypeError, ValueError):
        max_edges = 1200

    if forwarder_backend_enabled():
        preferred_slot = (
            data.get("forwarder_id")
            or data.get("slot")
            or data.get("ir_number")
            or data.get("code_id")
        )
        success, message, forwarder_id = learn_forwarder_code(
            code,
            preferred_slot=preferred_slot,
            timeout=max(3, min(timeout, 60)),
        )
        if not success:
            add_log("红外", f"转发模块学习失败 {code}: {message}", {
                "code": code,
                "forwarder_id": forwarder_id,
                "device_id": get_selected_device_id(),
            }, success=False)
            return jsonify({
                "success": False,
                "message": message,
                "code": code,
                "forwarder_id": forwarder_id,
                "raw_length": 0,
            }), 400

        store = load_ir_store()
        store["codes"][code] = {
            "label": label,
            "raw": [],
            "raw_length": 0,
            "forwarder_id": forwarder_id,
            "updated_at": now_iso(),
        }
        save_ir_store(store)
        add_log("红外", f"转发模块学习成功 {code}: {label}", {
            "code": code,
            "forwarder_id": forwarder_id,
            "device_id": get_selected_device_id(),
        })
        return jsonify({
            "success": True,
            "message": message,
            "code": code,
            "label": label,
            "forwarder_id": forwarder_id,
            "raw_length": 0,
            "profiles": ir_profiles(),
            **device_payload(),
        })

    success, raw, message = capture_ir_raw(
        timeout=max(3, min(timeout, 30)),
        frame_gap_us=max(30000, min(frame_gap_us, 200000)),
        max_edges=max(300, min(max_edges, 2000)),
    )
    if not success:
        add_log("红外", f"学习失败 {code}: {message}", {
            "code": code,
            "raw_length": len(raw),
            "device_id": get_selected_device_id(),
        }, success=False)
        return jsonify({"success": False, "message": message, "raw_length": len(raw)}), 400

    store = load_ir_store()
    store["codes"][code] = {
        "label": label,
        "raw": raw,
        "raw_length": len(raw),
        "updated_at": now_iso(),
    }
    save_ir_store(store)
    save_lirc_config()
    add_log("红外", f"学习成功 {code}: {label}", {
        "code": code,
        "raw_length": len(raw),
        "device_id": get_selected_device_id(),
    })

    return jsonify({
        "success": True,
        "message": message,
        "code": code,
        "label": label,
        "raw_length": len(raw),
        "profiles": ir_profiles(),
        **device_payload(),
    })


@app.route("/api/ir/test", methods=["POST"])
def test_ir_code():
    data = request.get_json(silent=True) or {}
    device_id = str(data.get("device_id", "")).strip() or None
    if device_id:
        selected = select_device(device_id)
        if not selected:
            return jsonify({"success": False, "message": "未找到设备"}), 404
    code = str(data.get("code", "")).strip().upper()
    if not is_valid_state_code(code):
        return jsonify({"success": False, "message": "无效状态码"}), 400

    success, message = send_ir(code)
    add_log("红外", f"测试发送 {code}: {message}", {
        "code": code,
        "device_id": get_selected_device_id(),
    }, success=success)
    return jsonify({
        "success": success,
        "message": message,
        "code": code,
        **device_payload(),
    }), 200 if success else 400


@app.route("/api/ir/delete", methods=["POST"])
def delete_ir_code():
    data = request.get_json(silent=True) or {}
    device_id = str(data.get("device_id", "")).strip() or None
    if device_id:
        selected = select_device(device_id)
        if not selected:
            return jsonify({"success": False, "message": "未找到设备"}), 404
    code = str(data.get("code", "")).strip().upper()
    if not is_valid_state_code(code):
        return jsonify({"success": False, "message": "无效状态码"}), 400

    store = load_ir_store()
    if code not in store.get("codes", {}):
        return jsonify({"success": False, "message": "这个状态码还没有学习"}), 404
    store["codes"].pop(code, None)
    save_ir_store(store)
    if forwarder_backend_enabled():
        mapping = load_forwarder_map()
        mapping.pop(code, None)
        save_forwarder_map(mapping)
    save_lirc_config()
    add_log("红外", f"删除红外状态码: {code}", {"code": code, "device_id": get_selected_device_id()})
    profiles = ir_profiles()
    return jsonify({
        "success": True,
        "message": f"{code} 已删除，可重新学习",
        "profiles": profiles,
        "summary": {
            "total": len(profiles),
            "learned": len([profile for profile in profiles if profile.get("learned")]),
            "missing": len([profile for profile in profiles if not profile.get("learned")]),
        },
        **device_payload(),
    })


@app.route("/api/ir/reset", methods=["POST"])
def reset_ir_codes():
    data = request.get_json(silent=True) or {}
    device_id = str(data.get("device_id", "")).strip() or None
    if device_id:
        selected = select_device(device_id)
        if not selected:
            return jsonify({"success": False, "message": "未找到设备"}), 404
    device = clear_ir_store()
    if forwarder_backend_enabled():
        save_forwarder_map({})
    save_lirc_config()
    profiles = ir_profiles()
    add_log("红外", f"清空当前设备红外码库: {device['name']}", {
        "device_id": device["id"],
    })
    return jsonify({
        "success": True,
        "message": f"已清空 {device['name']} 的红外码库",
        "profiles": profiles,
        "summary": {
            "total": len(profiles),
            "learned": 0,
            "missing": len(profiles),
        },
        **device_payload(),
    })


@app.route("/api/ir/backup")
def backup_ir_codes():
    device_id = request_device_id()
    if device_id:
        selected = select_device(device_id)
        if not selected:
            return jsonify({"success": False, "message": "未找到设备"}), 404
    payload = load_ir_store()
    payload.update(device_payload())
    return jsonify(payload)


@app.route("/api/ir/lirc-config")
def get_lirc_config():
    device_id = request_device_id()
    if device_id:
        selected = select_device(device_id)
        if not selected:
            return jsonify({"success": False, "message": "未找到设备"}), 404
    text = save_lirc_config()
    return Response(text, mimetype="text/plain; charset=utf-8")


@app.route("/api/voice", methods=["POST"])
def voice_recognize():
    audio = request.files.get("audio")
    if not audio:
        return jsonify({"success": False, "message": "缺少 audio 音频文件"}), 400

    try:
        from yuyin import VoiceRecognitionError, recognize_audio_bytes, speech_engine_status
    except ImportError as exc:
        return jsonify({
            "success": False,
            "message": f"语音识别模块加载失败: {exc}",
        }), 500

    try:
        result = recognize_audio_bytes(
            audio.read(),
            filename=audio.filename or "",
            content_type=audio.content_type or "",
        )
    except VoiceRecognitionError as exc:
        add_log("语音", f"语音识别失败: {exc}", {
            "filename": audio.filename,
            "content_type": audio.content_type,
            "status": speech_engine_status(),
        }, success=False)
        return jsonify({
            "success": False,
            "message": str(exc),
            "status": speech_engine_status(),
        }), 400
    except Exception as exc:
        traceback.print_exc()
        try:
            status = speech_engine_status()
        except Exception as status_exc:
            status = {"error": f"{type(status_exc).__name__}: {status_exc}"}
        error_message = f"{type(exc).__name__}: {exc}"
        add_log("语音", f"语音接口内部错误: {error_message}", {
            "filename": audio.filename,
            "content_type": audio.content_type,
            "status": status,
            "traceback": traceback.format_exc().splitlines()[-8:],
        }, success=False)
        return jsonify({
            "success": False,
            "message": f"语音接口内部错误: {error_message}",
            "status": status,
        }), 500

    text = result.get("text", "")
    add_log("语音", f"语音转文字: {text or '未识别到文字'}", {
        "engine": result.get("engine"),
        "converted": result.get("converted"),
        "filename": audio.filename,
        "content_type": audio.content_type,
    }, success=bool(text))
    return jsonify({
        "success": bool(text),
        "text": text,
        "message": "识别成功" if text else "没有识别到文字，请靠近麦克风再试",
        **result,
    })


@app.route("/api/voice/status")
def voice_status():
    try:
        from yuyin import speech_engine_status
    except ImportError as exc:
        return jsonify({
            "ready": False,
            "message": f"语音识别模块加载失败: {exc}",
        }), 500
    return jsonify(speech_engine_status())


@app.route("/api/state")
def get_state():
    device_id = request_device_id()
    if device_id:
        selected = select_device(device_id)
        if not selected:
            return jsonify({"success": False, "message": "未找到设备"}), 404
    state = dict(current_state)
    state["capabilities"] = {
        "temp_adjust_modes": sorted(TEMP_ADJUST_MODES),
    }
    state.update(device_payload())
    return jsonify(state)


initialize_runtime()
start_workers()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
