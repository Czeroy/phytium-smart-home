"""Safe fault-injection controller used by the project FIT tests.

The controller changes process environment flags only. Production modules read
those flags at command time, so tests can exercise failure and recovery paths
without disconnecting hardware, exhausting memory, or stopping Jailhouse.
"""

import os
import threading
import time
from collections import defaultdict
from enum import Enum


class FaultType(str, Enum):
    GPIO_FAILURE = "gpio_failure"
    IR_SEND_FAILURE = "ir_send_failure"
    IR_RECEIVE_TIMEOUT = "ir_receive_timeout"
    NETWORK_DISCONNECT = "network_disconnect"
    VOSK_MODEL_MISSING = "vosk_model_missing"
    STORAGE_CORRUPTION = "storage_corruption"
    MULTI_CORE_RACE = "multi_core_race"
    JAILHOUSE_CELL_FAILURE = "jailhouse_cell_failure"


FAULT_DESCRIPTIONS = {
    FaultType.GPIO_FAILURE: {
        "name": "GPIO 引脚故障",
        "severity": "high",
        "risk": "红外或继电器无法访问",
        "control": "切换模拟后端并返回明确告警",
    },
    FaultType.IR_SEND_FAILURE: {
        "name": "红外发送失败",
        "severity": "high",
        "risk": "命令成功返回但家电未动作",
        "control": "发送函数返回失败，不更新设备状态并记录日志",
    },
    FaultType.IR_RECEIVE_TIMEOUT: {
        "name": "红外接收超时",
        "severity": "medium",
        "risk": "学习操作永久阻塞",
        "control": "有限等待后失败，允许用户重试",
    },
    FaultType.NETWORK_DISCONNECT: {
        "name": "外部网络断开",
        "severity": "medium",
        "risk": "云端能力不可用",
        "control": "核心意图识别和设备控制保持本地运行",
    },
    FaultType.VOSK_MODEL_MISSING: {
        "name": "语音模型缺失",
        "severity": "medium",
        "risk": "语音识别不可用",
        "control": "状态接口报告未就绪，网页文字控制仍可用",
    },
    FaultType.STORAGE_CORRUPTION: {
        "name": "JSON 存储损坏",
        "severity": "high",
        "risk": "启动失败或运行数据丢失",
        "control": "结构化读取失败时返回安全默认值",
    },
    FaultType.MULTI_CORE_RACE: {
        "name": "多核并发竞争",
        "severity": "high",
        "risk": "统计或 mailbox 序号错乱",
        "control": "共享状态使用锁保护，通信设置超时",
    },
    FaultType.JAILHOUSE_CELL_FAILURE: {
        "name": "Jailhouse inmate 失联",
        "severity": "critical",
        "risk": "实时业务命令不可达",
        "control": "检测通道不可用并自动降级到 Linux 控制层",
    },
}


FAULT_ENV = {
    FaultType.GPIO_FAILURE: {"FIT_GPIO_FAILURE": "1"},
    FaultType.IR_SEND_FAILURE: {"IR_SEND_FAILURE": "1"},
    FaultType.IR_RECEIVE_TIMEOUT: {"IR_RECEIVE_TIMEOUT": "1"},
    FaultType.NETWORK_DISCONNECT: {
        "HTTP_PROXY": "http://127.0.0.1:9/",
        "HTTPS_PROXY": "http://127.0.0.1:9/",
    },
    FaultType.VOSK_MODEL_MISSING: {"VOSK_MODEL_PATH": "/nonexistent/vosk-model"},
    FaultType.STORAGE_CORRUPTION: {"FIT_STORAGE_CORRUPTION": "1"},
    FaultType.MULTI_CORE_RACE: {"FIT_MULTI_CORE_RACE": "1"},
    FaultType.JAILHOUSE_CELL_FAILURE: {"JAILHOUSE_CELL_FAILURE": "1"},
}


class FaultInjector:
    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    @classmethod
    def get_instance(cls):
        return cls()

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._lock = threading.RLock()
        self._active = {}
        self._backups = defaultdict(dict)
        self._history = []

    def inject(self, fault_type, **params):
        fault_type = FaultType(fault_type)
        with self._lock:
            if fault_type in self._active:
                return False, f"故障已激活: {FAULT_DESCRIPTIONS[fault_type]['name']}"
            values = dict(FAULT_ENV[fault_type])
            if fault_type == FaultType.IR_RECEIVE_TIMEOUT:
                values["IR_RECEIVE_TIMEOUT"] = str(params.get("timeout", 0.05))
            for key, value in values.items():
                self._backups[fault_type][key] = os.environ.get(key)
                os.environ[key] = value
            now = time.time()
            self._active[fault_type] = {"injected_at": now, "params": params}
            self._history.append({"time": now, "fault": fault_type.value, "action": "inject"})
            return True, f"已注入: {FAULT_DESCRIPTIONS[fault_type]['name']}"

    def recover(self, fault_type):
        fault_type = FaultType(fault_type)
        with self._lock:
            if fault_type not in self._active:
                return False, f"故障未激活: {FAULT_DESCRIPTIONS[fault_type]['name']}"
            for key, old_value in self._backups.pop(fault_type, {}).items():
                if old_value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old_value
            del self._active[fault_type]
            self._history.append({"time": time.time(), "fault": fault_type.value, "action": "recover"})
            return True, f"已恢复: {FAULT_DESCRIPTIONS[fault_type]['name']}"

    def recover_all(self):
        recovered = 0
        for fault_type in list(self._active):
            success, _ = self.recover(fault_type)
            recovered += int(success)
        return recovered

    def is_active(self, fault_type):
        with self._lock:
            return FaultType(fault_type) in self._active

    def get_status(self):
        with self._lock:
            return {
                "active_faults": [fault.value for fault in self._active],
                "available_faults": [
                    {"type": fault.value, **FAULT_DESCRIPTIONS[fault]}
                    for fault in FaultType
                ],
                "history": list(self._history[-50:]),
            }

    def run_test_suite(self):
        results = []
        self.recover_all()
        for fault_type in FaultType:
            injected, inject_message = self.inject(fault_type)
            active = self.is_active(fault_type)
            recovered, recover_message = self.recover(fault_type)
            inactive = not self.is_active(fault_type)
            passed = injected and active and recovered and inactive
            results.append({
                "fault": fault_type.value,
                "name": FAULT_DESCRIPTIONS[fault_type]["name"],
                "passed": passed,
                "inject_message": inject_message,
                "recover_message": recover_message,
            })
        return {
            "total": len(results),
            "passed": sum(item["passed"] for item in results),
            "failed": sum(not item["passed"] for item in results),
            "results": results,
        }


def inject_fault(fault_type, **params):
    return FaultInjector.get_instance().inject(fault_type, **params)


def recover_fault(fault_type):
    return FaultInjector.get_instance().recover(fault_type)


def run_fault_test_suite():
    return FaultInjector.get_instance().run_test_suite()
