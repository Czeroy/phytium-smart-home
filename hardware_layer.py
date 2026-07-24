from pathlib import Path

from dht11_sensor import DHT11_ENABLED, sensor_status
from device_control import (
    IR_BACKEND,
    IR_RECEIVE_DEVICE,
    IR_SEND_DEVICE,
    LIGHT_OFF_CMD,
    LIGHT_ON_CMD,
    MOCK_IR,
    MOCK_LIGHT,
    REMOTE_NAME,
    ir_backend_status,
    ir_profiles,
)


def build_hardware_profile():
    profiles = ir_profiles()
    learned = [profile for profile in profiles if profile.get("learned")]
    missing = [profile for profile in profiles if not profile.get("learned")]

    light_ready = MOCK_LIGHT or bool(LIGHT_ON_CMD and LIGHT_OFF_CMD)
    receive_exists = Path(IR_RECEIVE_DEVICE).exists()
    send_exists = Path(IR_SEND_DEVICE).exists()
    ir_backend = ir_backend_status()
    environment_sensor = sensor_status()
    if IR_BACKEND in {"gpio", "gpio_pwm", "pwm"}:
        receive_exists = bool(ir_backend.get("rx_exists"))
        send_exists = bool(ir_backend.get("pwm_chip_exists"))

    return {
        "remote_name": REMOTE_NAME,
        "controllers": [
            {
                "name": "空调红外控制器",
                "kind": "IR state-code",
                "mock": MOCK_IR,
                "ready": MOCK_IR or bool(learned),
                "summary": f"已学习 {len(learned)}/{len(profiles)} 个状态码",
                "detail": {
                    "backend": IR_BACKEND,
                    "receive_device": IR_RECEIVE_DEVICE,
                    "receive_exists": receive_exists,
                    "send_device": IR_SEND_DEVICE,
                    "send_exists": send_exists,
                    "gpio_backend": ir_backend,
                    "learned_count": len(learned),
                    "missing_count": len(missing),
                },
            },
            {
                "name": "灯光控制器",
                "kind": "GPIO/relay command",
                "mock": MOCK_LIGHT,
                "ready": light_ready,
                "summary": "模拟灯光" if MOCK_LIGHT else ("已配置灯光命令" if light_ready else "未配置灯光命令"),
                "detail": {
                    "light_on_cmd": bool(LIGHT_ON_CMD),
                    "light_off_cmd": bool(LIGHT_OFF_CMD),
                },
            },
            {
                "name": "DHT11 温湿度传感器",
                "kind": "GPIO single-wire sensor",
                "mock": not DHT11_ENABLED,
                "ready": bool(environment_sensor.get("success")) if DHT11_ENABLED else True,
                "summary": (
                    f"{environment_sensor['temperature']}°C / {environment_sensor['humidity']}%RH"
                    if environment_sensor.get("success")
                    else (
                        f"读取失败: {environment_sensor.get('error', '未知错误')}"
                        if DHT11_ENABLED
                        else "未启用，当前使用模拟温湿度"
                    )
                ),
                "detail": environment_sensor,
            },
        ],
        "interfaces": [
            {"name": "Web API", "role": "手机网页和本机接口"},
            {"name": "AI Intent", "role": "语音文本到设备命令"},
            {"name": "Runtime Workers", "role": "定时任务和自动化规则"},
            {"name": "Device Layer", "role": "红外、灯光、传感器统一封装"},
        ],
    }
