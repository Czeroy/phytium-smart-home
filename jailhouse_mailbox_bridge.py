import json
import mmap
import os
import socket
import signal
import sys
import struct
import time
from pathlib import Path


SOCKET_PATH = os.getenv("REALTIME_BRIDGE_SOCKET", "/run/smart-home-rt/bridge.sock")
MEM_DEVICE = os.getenv("REALTIME_MAILBOX_MEM", "/dev/mem")
MAILBOX_PHYS = int(os.getenv("REALTIME_MAILBOX_PHYS", "0xb1001000"), 0)
MAILBOX_SIZE = int(os.getenv("REALTIME_MAILBOX_SIZE", "0x1000"), 0)

def _sigbus_handler(signum, frame):
    print("ERROR: SIGBUS while accessing mailbox memory. /dev/mem cannot safely map this Jailhouse reserved region.", flush=True)
    sys.exit(135)

signal.signal(signal.SIGBUS, _sigbus_handler)
REQUEST_TIMEOUT = float(os.getenv("REALTIME_COMMAND_TIMEOUT", "1.5"))

MAGIC = 0x53485254  # SHRT
VERSION = 1
FLAG_REQUEST = 1
FLAG_ACK = 2
PAYLOAD_OFFSET = 64
PAYLOAD_SIZE = 3072
ACK_OFFSET = PAYLOAD_OFFSET + PAYLOAD_SIZE
ACK_SIZE = 512

COMMAND_IDS = {
    "ping": 1,
    "status": 2,
    "heartbeat": 3,
    "ac_command": 10,
    "ir_send": 11,
    "light_command": 20,
    "sensor_sample": 30,
}


def u32_get(buf, offset):
    return struct.unpack_from("<I", buf, offset)[0]


def u32_set(buf, offset, value):
    struct.pack_into("<I", buf, offset, value & 0xFFFFFFFF)


def read_c_string(buf, offset, limit):
    raw = bytes(buf[offset:offset + limit])
    return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")


def send_mailbox_command(command, payload):
    """
    Real Jailhouse heterogeneous communication path.

    Linux userspace -> bridge socket -> /dev/smart_home_mailbox kernel driver
    -> Jailhouse shared memory 0xb1001000 -> CPU3 inmate -> ACK back.
    """
    dev = os.getenv("REALTIME_MAILBOX_DEV", "/dev/smart_home_mailbox")

    req = json.dumps({
        "command": command,
        "payload": payload,
    }, ensure_ascii=False).encode("utf-8")
    command_id = COMMAND_IDS.get(command, 0)
    packet = struct.pack("<II", command_id, len(req)) + req

    try:
        fd = os.open(dev, os.O_RDWR)
        try:
            os.write(fd, packet)
            ack = os.read(fd, 1024).decode("utf-8", errors="replace").strip("\x00\r\n ")
        finally:
            os.close(fd)

        return True, {
            "success": True,
            "message": "mailbox command accepted by Jailhouse CPU3 realtime inmate",
            "bridge": "mailbox-kernel-driver",
            "command": command,
            "payload": payload,
            "mailbox_phys": hex(MAILBOX_PHYS),
            "ack": ack,
        }

    except OSError as e:
        return False, {
            "success": False,
            "message": f"mailbox command failed through kernel driver: {e}",
            "bridge": "mailbox-kernel-driver",
            "command": command,
            "payload": payload,
            "mailbox_phys": hex(MAILBOX_PHYS),
        }


def handle_request(line):
    try:
        request = json.loads(line)
        command = str(request.get("command") or "ping")
        payload = request.get("payload") or {}
        ok, result = send_mailbox_command(command, payload)
        return {"success": ok, **result}
    except Exception as exc:
        return {
            "success": False,
            "message": f"{type(exc).__name__}: {exc}",
            "bridge": "mailbox",
        }


def serve():
    socket_path = Path(SOCKET_PATH)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    os.chmod(socket_path, 0o666)
    server.listen(8)
    print(f"smart-home mailbox bridge listening on {socket_path} -> {hex(MAILBOX_PHYS)}", flush=True)

    while True:
        conn, _ = server.accept()
        with conn:
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
                if len(data) > 131072:
                    break
            response = handle_request(data.decode("utf-8", errors="ignore"))
            conn.sendall((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))


if __name__ == "__main__":
    serve()
