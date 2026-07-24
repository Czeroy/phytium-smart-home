from feedback_store import feedback_summary
from hardware_layer import build_hardware_profile
from realtime_bridge import realtime_status
from runtime_store import visible_rules, visible_timers, recent_logs


def build_project_overview():
    hardware = build_hardware_profile()
    rules = visible_rules()
    timers = visible_timers()
    logs = recent_logs(20)
    feedback = feedback_summary()
    realtime = realtime_status()

    modules = [
        {"name": "手机网页", "detail": "控制空调、灯光、定时、红外学习和健康检查"},
        {"name": "Flask API", "detail": "提供状态、控制、AI意图、日志、规则和红外接口"},
        {"name": "AI意图识别", "detail": "模型识别 + 规则纠偏 + 低置信度确认"},
        {"name": "运行时服务", "detail": "定时任务、自动化规则、事件日志后台执行"},
        {"name": "设备抽象层", "detail": "空调走红外状态码，灯光走GPIO/继电器命令"},
        {"name": "Jailhouse异构实时层", "detail": realtime["message"]},
        {"name": "纠错闭环", "detail": "把真实识别错误整理成后续训练样本"},
    ]

    highlights = [
        "本地AI意图识别，不依赖云端大模型也能执行常用家居指令",
        "空调红外按状态码学习，适合真实空调这种一次发送完整状态的设备",
        "灯光和空调分离控制，避免把灯误做成红外设备",
        "网页内置系统健康检查，方便开发板部署和现场排错",
        "AI纠错反馈能沉淀真实语音样本，后续可重新训练提升准确率",
        "自动化规则和定时任务在后端运行，网页关闭后也能继续执行",
        "Jailhouse 隔离 CPU3，并通过 shared-memory mailbox 完成 Linux 与实时 inmate 的业务通信",
        "实时通道不可用时自动降级到 Linux 控制层，避免单点故障导致家居控制完全中断",
    ]

    return {
        "modules": modules,
        "highlights": highlights,
        "hardware": hardware,
        "realtime": realtime,
        "metrics": {
            "rules": len(rules),
            "timers": len(timers),
            "recent_logs": len(logs),
            "feedback_total": feedback["total"],
            "feedback_training_ready": feedback["training_ready"],
            "realtime_commands": realtime.get("metrics", {}).get("total", 0),
            "realtime_success_rate": realtime.get("metrics", {}).get("success_rate"),
            "realtime_average_latency_ms": realtime.get("metrics", {}).get("average_latency_ms"),
            "realtime_max_latency_ms": realtime.get("metrics", {}).get("max_latency_ms"),
        },
    }
