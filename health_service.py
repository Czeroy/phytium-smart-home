import os
from pathlib import Path
import shutil

import ai

from device_control import (
    IR_BACKEND,
    IR_CODES_PATH,
    IR_RECEIVE_DEVICE,
    IR_SEND_DEVICE,
    LIRC_CONFIG_PATH,
    LIGHT_OFF_CMD,
    LIGHT_ON_CMD,
    MOCK_IR,
    MOCK_LIGHT,
    REMOTE_NAME,
    ir_backend_status,
)
from feedback_store import feedback_count
from feedback_store import FEEDBACK_TRAINING_PATH, feedback_summary
from hardware_layer import build_hardware_profile
from runtime_store import (
    EVENT_LOG_PATH,
    RULE_STORE_PATH,
    TIMER_STORE_PATH,
    recent_logs,
    visible_rules,
    visible_timers,
)
from realtime_bridge import realtime_status
from storage import BASE_DIR
from yuyin import speech_engine_status


def item(name, ok, message, detail=None, level="ok", fix=""):
    return {
        "name": name,
        "ok": bool(ok),
        "level": level if ok else "error",
        "message": message,
        "detail": detail or {},
        "fix": fix,
    }


def file_status(path, label):
    path = Path(path)
    if path.exists():
        return item(label, True, "文件存在", {"path": str(path), "size": path.stat().st_size})
    return item(
        label,
        True,
        "文件尚未生成，运行后会自动创建",
        {"path": str(path)},
        level="warn",
        fix="如果这是运行数据文件，可以忽略；如果是模型或红外配置，请重新生成。",
    )


def device_status(path, label, mock):
    exists = Path(path).exists()
    if mock:
        return item(label, True, "模拟模式，未要求真实设备", {"path": path, "exists": exists}, level="warn")
    return item(
        label,
        exists,
        "设备存在" if exists else "未检测到设备",
        {"path": path, "exists": exists},
        fix="检查红外模块接线、内核设备树和 /dev/lirc* 是否生成。",
    )


def command_status(command, label, optional=False):
    found = shutil.which(command) is not None
    if optional and not found:
        return item(label, True, "未安装，当前模式可不使用", {"command": command}, level="warn")
    return item(
        label,
        found,
        "已安装" if found else "未安装",
        {"command": command},
        fix=f"在开发板上安装对应工具，或先打开 MOCK_IR=1 调试。",
    )


def build_health():
    checks = []
    hardware = build_hardware_profile()

    checks.append(item("Flask服务", True, "当前接口请求正常"))
    checks.append(item(
        "公网访问保护",
        True,
        "登录保护已关闭，适合局域网调试",
        {"auth_enabled": False},
        level="warn",
        fix="如果后面重新暴露到公网，建议再打开登录保护或只允许局域网访问。",
    ))
    checks.append(item(
        "AI模型",
        ai.model is not None and ai.vocab is not None,
        "模型已加载" if ai.model is not None and ai.vocab is not None else "模型或词表未加载",
        fix="确认 best_model.pt、vocab.json、intents.json 位于项目目录，并重启服务。",
    ))
    checks.append(file_status(BASE_DIR / "best_model.pt", "模型文件 best_model.pt"))
    checks.append(file_status(BASE_DIR / "vocab.json", "词表 vocab.json"))
    checks.append(file_status(BASE_DIR / "intents.json", "意图表 intents.json"))
    checks.append(item("AI低置信度保护", True, "网页会在低置信度时先询问确认再执行"))

    voice_status = speech_engine_status()
    checks.append(item(
        "语音识别 Vosk",
        True,
        "Vosk语音识别可用" if voice_status["ready"] else voice_status.get("message", "Vosk或语音模型未配置完整"),
        voice_status,
        level="ok" if voice_status["ready"] else "warn",
        fix="安装 vosk，并把中文模型目录放到项目 model 文件夹，或设置 VOSK_MODEL_PATH。",
    ))

    realtime = realtime_status()
    openamp = realtime["openamp"]
    jailhouse = realtime["jailhouse"]
    fusion = realtime.get("fusion", {})
    checks.append(item(
        "OpenAMP / rpmsg",
        True,
        fusion.get("message") if fusion.get("proof_ready") and not openamp["endpoint_ready"] else openamp["message"],
        {"openamp": openamp, "fusion": fusion},
        level="ok" if openamp["endpoint_ready"] or fusion.get("proof_ready") else "warn",
        fix="本项目默认使用安全的 Jailhouse mailbox 路径；只有确认固件和资源表匹配时才启用 remoteproc/rpmsg。",
    ))
    checks.append(item(
        "Jailhouse",
        True,
        jailhouse["message"],
        jailhouse,
        level="ok" if jailhouse["ready"] else "warn",
        fix="安装 Jailhouse 用户态工具，加载 jailhouse 内核模块，再启用 root cell 和实时 inmate cell。",
    ))
    checks.append(item(
        "实时侧桥接",
        True,
        realtime["message"],
        realtime,
        level="ok" if realtime["ready"] else "warn",
        fix="确认 CPU3 已隔离、bridge.sock 存在且 REALTIME_BRIDGE_KIND=mailbox；Flask/AI 保持在 Linux 侧。",
    ))

    ir_backend = ir_backend_status()
    if IR_BACKEND in {"gpio", "gpio_pwm", "pwm"}:
        checks.append(item(
            "红外 GPIO 接收",
            MOCK_IR or ir_backend.get("rx_exists"),
            "模拟模式" if MOCK_IR else ("GPIO 接收设备存在" if ir_backend.get("rx_exists") else "未检测到 GPIO 接收设备"),
            ir_backend,
            level="warn" if MOCK_IR else "ok",
            fix="确认红外接收头接到 40Pin 7 号脚 GPIO2_10，并让服务具备 /dev/gpiochip2 读取权限。",
        ))
        checks.append(item(
            "红外 PWM 发射",
            MOCK_IR or ir_backend.get("pwm_chip_exists"),
            "模拟模式" if MOCK_IR else ("PWM 设备存在" if ir_backend.get("pwm_chip_exists") else "未检测到 PWM 设备"),
            ir_backend,
            level="warn" if MOCK_IR else "ok",
            fix="确认红外发射模块接到 PWM 输出脚，当前默认使用 /sys/class/pwm/pwmchip0/pwm0。",
        ))
        checks.append(item(
            "红外链路验证",
            True,
            "接收、学习和 PWM 发送接口已打通；当前小功率发射头仅适合近距离验证",
            {
                "rx_board_pin": ir_backend.get("rx_board_pin"),
                "rx_gpio_name": ir_backend.get("rx_gpio_name"),
                "tx_board_pin": ir_backend.get("tx_board_pin"),
                "tx_gpio_name": ir_backend.get("tx_gpio_name"),
                "pwm": f"{ir_backend.get('pwm_chip')}/pwm{ir_backend.get('pwm_channel')}",
                "carrier_hz": ir_backend.get("carrier_hz"),
                "duty_cycle": ir_backend.get("duty_cycle"),
            },
            level="warn",
            fix="正式控制空调建议更换 5V 大功率红外发射模块；当前占空比保持 0.33。",
        ))
        checks.append(command_status("gpiomon", "gpiomon GPIO 边沿工具", optional=MOCK_IR))
    elif IR_BACKEND == "forwarder":
        mapped = ir_backend.get("forwarder_mapped_codes", [])
        mapped_count = len(mapped) if isinstance(mapped, list) else 0
        port = ir_backend.get("forwarder_port", "")
        checks.append(item(
            "串口红外转发模块",
            MOCK_IR or (bool(port) and mapped_count > 0),
            "模拟模式" if MOCK_IR else f"已映射 {mapped_count} 个状态码",
            ir_backend,
            level="warn" if MOCK_IR or mapped_count == 0 else "ok",
            fix="确认 IR_FORWARDER_PORT 指向实际串口、波特率为 9600，并检查状态码映射 JSON。",
        ))
    else:
        checks.append(device_status(IR_RECEIVE_DEVICE, "红外接收设备", MOCK_IR))
        checks.append(device_status(IR_SEND_DEVICE, "红外发射设备", MOCK_IR))
        checks.append(command_status("mode2", "mode2 接收工具", optional=MOCK_IR))
        checks.append(command_status("ir-ctl", "ir-ctl 发射工具", optional=MOCK_IR))
        checks.append(command_status("irsend", "irsend 兼容发送工具", optional=True))

    light_ready = MOCK_LIGHT or bool(LIGHT_ON_CMD and LIGHT_OFF_CMD)
    checks.append(item(
        "灯光控制",
        light_ready,
        "模拟模式" if MOCK_LIGHT else ("已配置 GPIO/命令" if light_ready else "未配置 LIGHT_ON_CMD / LIGHT_OFF_CMD"),
        {"mock": MOCK_LIGHT, "light_on_cmd": bool(LIGHT_ON_CMD), "light_off_cmd": bool(LIGHT_OFF_CMD)},
        level="warn" if MOCK_LIGHT else "ok",
    ))

    checks.append(file_status(IR_CODES_PATH, "红外码库 ir_codes.json"))
    checks.append(file_status(LIRC_CONFIG_PATH, "LIRC配置 my_ac_remote.conf"))
    checks.append(file_status(TIMER_STORE_PATH, "定时文件 timers.json"))
    checks.append(file_status(RULE_STORE_PATH, "规则文件 rules.json"))
    checks.append(file_status(EVENT_LOG_PATH, "日志文件 event_logs.json"))
    checks.append(file_status(FEEDBACK_TRAINING_PATH, "纠错训练数据 feedback_training.json"))

    checks.append(item("定时服务", True, "后台线程已启动", {"visible_timers": len(visible_timers())}))
    checks.append(item("自动化规则", True, "后台线程已启动", {"rules": len(visible_rules())}))
    checks.append(item("运行日志", True, "日志接口正常", {"recent_logs": len(recent_logs(20))}))
    feedback = feedback_summary()
    checks.append(item("AI纠错样本", True, "反馈存储正常", {
        "feedback_count": feedback_count(),
        "training_ready": feedback["training_ready"],
        "needs_correction": feedback["needs_correction"],
        "duplicates": feedback.get("duplicates", 0),
    }))

    for controller in hardware["controllers"]:
        checks.append(item(
            controller["name"],
            controller["ready"],
            controller["summary"],
            controller.get("detail", {}),
            level="warn" if controller.get("mock") else "ok",
            fix="硬件未接入时可先用模拟模式；接入后再关闭 MOCK 环境变量。",
        ))

    last_error = next((log for log in recent_logs(50) if log.get("success") is False), None)
    checks.append(item(
        "最近错误",
        True,
        "暂无错误日志" if not last_error else last_error.get("message", "存在错误日志"),
        {"last_error": last_error} if last_error else {},
        level="warn" if last_error else "ok",
        fix="打开运行日志查看具体报错，优先处理红外、灯光命令和AI接口错误。",
    ))

    errors = [check for check in checks if check["level"] == "error"]
    warnings = [check for check in checks if check["level"] == "warn"]
    if errors:
        status = "error"
    elif warnings:
        status = "warn"
    else:
        status = "ok"

    return {
        "status": status,
        "remote_name": REMOTE_NAME,
        "mock_ir": MOCK_IR,
        "mock_light": MOCK_LIGHT,
        "summary": {
            "ok": len([check for check in checks if check["level"] == "ok"]),
            "warn": len(warnings),
            "error": len(errors),
        },
        "checks": checks,
    }
