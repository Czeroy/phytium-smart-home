import glob
import json
import os
import shutil
import socket
import stat
import subprocess
import threading
import time
from pathlib import Path

from storage import BASE_DIR


REALTIME_BACKEND = os.getenv("REALTIME_BACKEND", "auto").strip().lower()
OPENAMP_ENDPOINT = os.getenv("OPENAMP_ENDPOINT", "").strip()
JAILHOUSE_CELL_NAME = os.getenv("JAILHOUSE_CELL_NAME", "smart_home_rt").strip()
JAILHOUSE_SOURCE_DIR = Path(os.getenv(
    "JAILHOUSE_SOURCE_DIR",
    os.path.expanduser("~/openamp-jailhouse/phytium-jailhouse/src/jailhouse"),
))
JAILHOUSE_TOOL = os.getenv("JAILHOUSE_TOOL", "").strip()
JAILHOUSE_CONFIG_DIR = Path(os.getenv(
    "JAILHOUSE_CONFIG_DIR",
    str(JAILHOUSE_SOURCE_DIR / "configs" / "arm64"),
))
REALTIME_COMMAND_TIMEOUT = float(os.getenv("REALTIME_COMMAND_TIMEOUT", "1.5"))
SAFE_OPENAMP_REMOTEPROC = os.getenv("SAFE_OPENAMP_REMOTEPROC", "0").strip().lower() in {"1", "true", "yes"}
REALTIME_BRIDGE_SOCKET = os.getenv("REALTIME_BRIDGE_SOCKET", "/run/smart-home-rt/bridge.sock")
REALTIME_BRIDGE_KIND = os.getenv("REALTIME_BRIDGE_KIND", "auto").strip().lower()

_METRICS_LOCK = threading.Lock()
_COMMAND_METRICS = {
    "total": 0,
    "success": 0,
    "failed": 0,
    "latency_total_ms": 0.0,
    "max_latency_ms": 0.0,
    "last_latency_ms": 0.0,
    "last_command": "",
    "last_transport": "",
    "last_timestamp": None,
}


def realtime_metrics():
    with _METRICS_LOCK:
        data = dict(_COMMAND_METRICS)
    total = data["total"]
    data["success_rate"] = round(data["success"] / total, 4) if total else None
    data["average_latency_ms"] = round(data.pop("latency_total_ms") / total, 3) if total else None
    data["max_latency_ms"] = round(data["max_latency_ms"], 3)
    data["last_latency_ms"] = round(data["last_latency_ms"], 3)
    return data


def _finish_command(started_at, command, transport, success, result):
    latency_ms = (time.perf_counter() - started_at) * 1000
    with _METRICS_LOCK:
        _COMMAND_METRICS["total"] += 1
        _COMMAND_METRICS["success" if success else "failed"] += 1
        _COMMAND_METRICS["latency_total_ms"] += latency_ms
        _COMMAND_METRICS["max_latency_ms"] = max(_COMMAND_METRICS["max_latency_ms"], latency_ms)
        _COMMAND_METRICS["last_latency_ms"] = latency_ms
        _COMMAND_METRICS["last_command"] = command
        _COMMAND_METRICS["last_transport"] = transport
        _COMMAND_METRICS["last_timestamp"] = time.time()
    result.setdefault("transport", transport)
    result.setdefault("latency_ms", round(latency_ms, 3))
    return success, result


def _glob(paths):
    found = []
    for pattern in paths:
        found.extend(glob.glob(pattern))
    return sorted(set(found))


def _path_exists(path):
    return bool(path) and Path(path).exists()


def _read_text(path, limit=4096):
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")[:limit].strip()
    except OSError:
        return ""


def _module_loaded(name):
    modules = _read_text("/proc/modules")
    return any(line.split(" ", 1)[0] == name for line in modules.splitlines())


def _list_dir(path):
    try:
        return sorted(item.name for item in Path(path).iterdir())
    except OSError:
        return []


def _remoteproc_status():
    result = []
    for entry in sorted(Path("/sys/class/remoteproc").glob("remoteproc*")):
        result.append({
            "name": entry.name,
            "state": _read_text(entry / "state"),
            "firmware": _read_text(entry / "firmware"),
        })
    return result


def _run_command(args):
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=REALTIME_COMMAND_TIMEOUT,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError) as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
        }
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def _find_openamp_endpoint():
    if OPENAMP_ENDPOINT:
        return OPENAMP_ENDPOINT if _path_exists(OPENAMP_ENDPOINT) else ""

    for candidate in _glob(["/dev/rpmsg[0-9]*", "/dev/ttyRPMSG[0-9]*"]):
        if "rpmsg_ctrl" not in candidate:
            return candidate
    return ""


def _find_jailhouse_tool():
    candidates = [
        JAILHOUSE_TOOL,
        shutil.which("jailhouse") or "",
        str(JAILHOUSE_SOURCE_DIR / "tools" / "jailhouse"),
        "/usr/local/sbin/jailhouse",
        "/usr/local/bin/jailhouse",
        "/usr/sbin/jailhouse",
        "/usr/bin/jailhouse",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return ""


def _bridge_socket_ready():
    if os.getenv("JAILHOUSE_CELL_FAILURE", "").lower() in {"1", "true", "yes", "on"}:
        return False
    if not REALTIME_BRIDGE_SOCKET:
        return False
    path = Path(REALTIME_BRIDGE_SOCKET)
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    if not stat.S_ISSOCK(mode):
        return False
    unix_sockets = _read_text("/proc/net/unix", limit=262144)
    if not unix_sockets:
        return True
    for line in unix_sockets.splitlines():
        parts = line.split()
        if parts and parts[-1] == REALTIME_BRIDGE_SOCKET:
            return True
    return False


def _ivshmem_uio_devices():
    configured = os.getenv("IVSHMEM_UIO_DEVICE", "").strip()
    candidates = [configured] if configured else []
    candidates.extend(str(path) for path in Path("/dev").glob("uio*"))
    ready = []
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            ready.append(candidate)
    return sorted(set(ready))


def _send_bridge_command(command, payload):
    packet = json.dumps({
        "command": command,
        "payload": payload,
        "timestamp": time.time(),
    }, ensure_ascii=False) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(REALTIME_COMMAND_TIMEOUT)
            client.connect(REALTIME_BRIDGE_SOCKET)
            client.sendall(packet.encode("utf-8"))
            chunks = []
            while True:
                data = client.recv(65536)
                if not data:
                    break
                chunks.append(data)
                if data.endswith(b"\n"):
                    break
    except OSError as exc:
        return False, {
            "message": f"failed to use realtime IVSHMEM bridge: {exc}",
            "bridge_socket": REALTIME_BRIDGE_SOCKET,
        }

    try:
        result = json.loads(b"".join(chunks).decode("utf-8", errors="ignore"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return False, {
            "message": f"invalid realtime bridge response: {exc}",
            "bridge_socket": REALTIME_BRIDGE_SOCKET,
        }
    return bool(result.get("success")), result


def openamp_status():
    dev_nodes = _glob(["/dev/rpmsg*", "/dev/ttyRPMSG*"])
    sys_rpmsg = _list_dir("/sys/bus/rpmsg/devices")
    remoteproc = _remoteproc_status()
    endpoint_path = _find_openamp_endpoint()
    available = bool(dev_nodes or sys_rpmsg or remoteproc)
    endpoint_ready = bool(endpoint_path and _path_exists(endpoint_path))

    if endpoint_ready:
        message = f"OpenAMP endpoint ready: {endpoint_path}"
    elif available:
        message = "OpenAMP/rpmsg traces found, but no writable endpoint is configured"
    else:
        message = "No OpenAMP/rpmsg or remoteproc device detected"

    return {
        "available": available,
        "endpoint_ready": endpoint_ready,
        "configured_endpoint": OPENAMP_ENDPOINT,
        "endpoint_path": endpoint_path,
        "dev_nodes": dev_nodes,
        "rpmsg_devices": sys_rpmsg,
        "remoteproc": remoteproc,
        "message": message,
    }


def jailhouse_status():
    tool = _find_jailhouse_tool()
    device_exists = Path("/dev/jailhouse").exists()
    module_loaded = _module_loaded("jailhouse") or Path("/sys/module/jailhouse").exists()
    config_files = sorted(str(path) for path in JAILHOUSE_CONFIG_DIR.glob("*.cell")) if JAILHOUSE_CONFIG_DIR.exists() else []
    cell_list = None
    cell_list_error = ""

    if tool and device_exists and hasattr(os, "geteuid") and os.geteuid() == 0:
        result = _run_command([tool, "cell", "list"])
        if result["ok"]:
            cell_list = result["stdout"]
        else:
            cell_list_error = result["stderr"] or result["stdout"]

    available = bool(tool or device_exists or module_loaded)
    ready = bool(tool and device_exists and module_loaded)
    if ready:
        message = "Jailhouse tool, module and device are present"
    elif available:
        missing = []
        if not tool:
            missing.append("jailhouse command")
        if not module_loaded:
            missing.append("jailhouse kernel module")
        if not device_exists:
            missing.append("/dev/jailhouse")
        message = "Jailhouse partially present, missing: " + ", ".join(missing)
    else:
        message = "Jailhouse is not installed or not enabled"

    return {
        "available": available,
        "ready": ready,
        "tool": tool or "",
        "module_loaded": module_loaded,
        "device_exists": device_exists,
        "cell_name": JAILHOUSE_CELL_NAME,
        "source_dir": str(JAILHOUSE_SOURCE_DIR),
        "config_dir": str(JAILHOUSE_CONFIG_DIR),
        "config_files": config_files,
        "cell_list": cell_list,
        "cell_list_error": cell_list_error,
        "message": message,
    }


def fusion_status(openamp, jailhouse):
    cpu_online = _read_text("/sys/devices/system/cpu/online")
    jailhouse_cpu_isolated = bool(jailhouse["ready"] and cpu_online and cpu_online != "0-3")
    ivshmem_configs = [
        path for path in jailhouse.get("config_files", [])
        if "ivshmem" in Path(path).name.lower()
    ]
    remoteproc_present = bool(openamp.get("remoteproc"))
    endpoint_ready = bool(openamp.get("endpoint_ready"))
    bridge_ready = _bridge_socket_ready()
    ivshmem_uio_devices = _ivshmem_uio_devices()
    ivshmem_command_ready = bool(bridge_ready and ivshmem_uio_devices)
    mailbox_command_ready = bool(bridge_ready and REALTIME_BRIDGE_KIND == "mailbox")
    proof_ready = bool(jailhouse_cpu_isolated and ivshmem_configs and (remoteproc_present or openamp.get("available")))
    command_ready = endpoint_ready or ivshmem_command_ready or mailbox_command_ready

    if mailbox_command_ready:
        message = "Realtime command channel is ready through Jailhouse shared-memory mailbox"
    elif command_ready:
        message = "Realtime command channel is ready through OpenAMP/rpmsg or Jailhouse IVSHMEM"
    elif bridge_ready and not ivshmem_uio_devices:
        message = "Realtime bridge socket is up, but no active command transport is configured"
    elif proof_ready:
        message = (
            "Fusion proof is ready: Jailhouse isolates the realtime CPU and "
            "ivshmem/OpenAMP transport resources are configured; remoteproc stays offline for safety"
        )
    elif jailhouse_cpu_isolated:
        message = "Jailhouse realtime CPU is isolated, but ivshmem/OpenAMP transport resources are not confirmed"
    else:
        message = "Fusion is not active yet"

    return {
        "proof_ready": proof_ready,
        "command_ready": command_ready,
        "jailhouse_cpu_isolated": jailhouse_cpu_isolated,
        "cpu_online": cpu_online,
        "ivshmem_configs": ivshmem_configs,
        "ivshmem_bridge_socket": REALTIME_BRIDGE_SOCKET,
        "ivshmem_bridge_ready": bridge_ready,
        "bridge_kind": REALTIME_BRIDGE_KIND,
        "ivshmem_uio_devices": ivshmem_uio_devices,
        "ivshmem_command_ready": ivshmem_command_ready,
        "mailbox_command_ready": mailbox_command_ready,
        "remoteproc_present": remoteproc_present,
        "remoteproc_start_allowed": SAFE_OPENAMP_REMOTEPROC,
        "message": message,
    }


def realtime_status():
    openamp = openamp_status()
    jailhouse = jailhouse_status()
    fusion = fusion_status(openamp, jailhouse)

    if REALTIME_BACKEND == "mock":
        ready = True
        message = "Realtime bridge is running in mock mode"
    elif REALTIME_BACKEND in {"openamp", "rpmsg"}:
        ready = openamp["endpoint_ready"]
        message = openamp["message"]
    elif REALTIME_BACKEND == "jailhouse":
        ready = jailhouse["ready"]
        message = jailhouse["message"]
    elif REALTIME_BACKEND in {"fusion", "openamp_jailhouse", "jailhouse_openamp"}:
        ready = fusion["proof_ready"] or fusion["command_ready"]
        message = fusion["message"]
    else:
        ready = openamp["endpoint_ready"] or fusion["proof_ready"]
        if fusion["proof_ready"] and not openamp["endpoint_ready"]:
            message = "Auto mode selected. " + fusion["message"]
        else:
            message = "Auto mode selected. " + (openamp["message"] if openamp["available"] else jailhouse["message"])

    return {
        "backend": REALTIME_BACKEND,
        "ready": ready,
        "command_ready": True if REALTIME_BACKEND == "mock" else fusion["command_ready"],
        "fusion_ready": fusion["proof_ready"],
        "message": message,
        "openamp": openamp,
        "jailhouse": jailhouse,
        "fusion": fusion,
        "metrics": realtime_metrics(),
        "role": "Linux app keeps Web/AI/logs; realtime side is reserved for IR timing, GPIO and sensor sampling.",
    }


def send_realtime_command(command, payload=None):
    started_at = time.perf_counter()
    payload = payload or {}
    status = realtime_status()
    command = str(command or "").strip()
    if not command:
        return _finish_command(started_at, command, "validation", False, {
            "message": "missing realtime command",
            "status": status,
        })

    if REALTIME_BACKEND == "mock":
        return _finish_command(started_at, command, "mock", True, {
            "message": f"mock realtime command accepted: {command}",
            "command": command,
            "payload": payload,
            "status": status,
        })

    if _bridge_socket_ready():
        bridge_success, bridge_result = _send_bridge_command(command, payload)
        bridge_result["status"] = status
        transport = "mailbox" if REALTIME_BRIDGE_KIND == "mailbox" else "ivshmem-bridge"
        return _finish_command(started_at, command, transport, bridge_success, bridge_result)

    endpoint = status["openamp"].get("endpoint_path")
    if not endpoint:
        if status.get("fusion_ready") and command in {"ping", "status", "heartbeat"}:
            return _finish_command(started_at, command, "fusion-proof", True, {
                "message": "fusion proof heartbeat accepted; command endpoint is not active yet",
                "command": command,
                "payload": payload,
                "status": status,
            })
        return _finish_command(started_at, command, "unavailable", False, {
            "message": "OpenAMP endpoint is not ready",
            "status": status,
        })

    packet = {
        "type": "smart_home_command",
        "command": command,
        "payload": payload,
        "timestamp": time.time(),
    }
    data = (json.dumps(packet, ensure_ascii=False) + "\n").encode("utf-8")

    try:
        flags = os.O_WRONLY | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(endpoint, flags)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except OSError as exc:
        return _finish_command(started_at, command, "openamp", False, {
            "message": f"failed to write realtime endpoint: {exc}",
            "endpoint": endpoint,
            "status": status,
        })

    return _finish_command(started_at, command, "openamp", True, {
        "message": f"realtime command sent: {command}",
        "endpoint": endpoint,
        "status": status,
    })
