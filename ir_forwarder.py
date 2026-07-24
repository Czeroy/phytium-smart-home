import json
import os
import time
from pathlib import Path


DEFAULT_PORT = os.getenv("IR_FORWARDER_PORT", "/dev/ttyUSB0")
DEFAULT_BAUDRATE = int(os.getenv("IR_FORWARDER_BAUDRATE", "9600"))


def _parse_code_id(value):
    if isinstance(value, bool):
        raise ValueError("code id must be an integer")
    if isinstance(value, int):
        code_id = value
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("empty code id")
        if text.lower().startswith("0x"):
            code_id = int(text, 16)
        elif all(ch in "0123456789" for ch in text):
            code_id = int(text, 10)
        else:
            code_id = int(text, 16)
    if not 0 <= code_id <= 0xFF:
        raise ValueError(f"code id out of range: {value}")
    return code_id


def load_code_map(path):
    path = Path(path)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "codes" in data and isinstance(data["codes"], dict):
        data = data["codes"]
    if not isinstance(data, dict):
        raise ValueError(f"forwarder code map must be an object: {path}")
    result = {}
    for state_code, code_id in data.items():
        result[str(state_code).upper()] = _parse_code_id(code_id)
    return result


def _serial_transaction_pyserial(port, baudrate, payload, read_size, timeout):
    import serial

    with serial.Serial(
        port=port,
        baudrate=baudrate,
        bytesize=8,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=timeout,
        write_timeout=timeout,
    ) as ser:
        ser.reset_input_buffer()
        ser.write(payload)
        ser.flush()
        return ser.read(read_size)


def _serial_transaction_posix(port, baudrate, payload, read_size, timeout):
    import select
    import termios

    speed_name = f"B{int(baudrate)}"
    if not hasattr(termios, speed_name):
        raise RuntimeError(f"unsupported baudrate without pyserial: {baudrate}")
    speed = getattr(termios, speed_name)

    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = termios.CLOCAL | termios.CREAD | termios.CS8
        attrs[3] = 0
        attrs[4] = speed
        attrs[5] = speed
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = max(1, int(timeout * 10))
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIOFLUSH)

        os.write(fd, payload)
        deadline = time.monotonic() + timeout
        chunks = []
        remaining = read_size
        while remaining > 0 and time.monotonic() < deadline:
            wait = max(0.0, deadline - time.monotonic())
            readable, _, _ = select.select([fd], [], [], wait)
            if not readable:
                break
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def serial_transaction(port, baudrate, payload, read_size=1, timeout=1.0):
    port = str(port or DEFAULT_PORT)
    baudrate = int(baudrate or DEFAULT_BAUDRATE)
    payload = bytes(payload)
    try:
        return _serial_transaction_pyserial(port, baudrate, payload, read_size, timeout)
    except ImportError:
        return _serial_transaction_posix(port, baudrate, payload, read_size, timeout)


def test_module(port=DEFAULT_PORT, baudrate=DEFAULT_BAUDRATE, timeout=1.0):
    reply = serial_transaction(port, baudrate, bytes([0xE4]), read_size=1, timeout=timeout)
    if reply == bytes([0xE4]):
        return True, "forwarder test OK"
    if reply == bytes([0xFF]):
        return False, "forwarder returned FF"
    if not reply:
        return False, "forwarder test timeout"
    return False, f"forwarder unexpected reply: {reply.hex(' ')}"


def send_stored_code(code_id, port=DEFAULT_PORT, baudrate=DEFAULT_BAUDRATE, timeout=2.0):
    code_id = _parse_code_id(code_id)
    reply = serial_transaction(port, baudrate, bytes([0xE3, code_id]), read_size=1, timeout=timeout)
    if reply == bytes([0xE3]):
        return True, f"forwarder sent stored IR code {code_id}"
    if reply == bytes([0xFF]):
        return False, f"forwarder code {code_id} is empty or invalid"
    if not reply:
        return False, f"forwarder send timeout for code {code_id}"
    return False, f"forwarder unexpected reply for code {code_id}: {reply.hex(' ')}"


def learn_to_slot(code_id, port=DEFAULT_PORT, baudrate=DEFAULT_BAUDRATE, timeout=15.0):
    code_id = _parse_code_id(code_id)
    reply = serial_transaction(port, baudrate, bytes([0xE0, code_id]), read_size=1, timeout=timeout)
    if reply == bytes([0xE0]):
        return True, f"forwarder learned IR code {code_id}"
    if reply == bytes([0xFF]):
        return False, f"forwarder learning failed for code {code_id}"
    if not reply:
        return False, f"forwarder learning timeout for code {code_id}"
    return False, f"forwarder unexpected learning reply: {reply.hex(' ')}"


def backend_status(port=DEFAULT_PORT, baudrate=DEFAULT_BAUDRATE, map_path=None):
    status = {
        "forwarder_port": str(port),
        "forwarder_baudrate": int(baudrate),
        "forwarder_protocol": "9600N81, E3 XX sends stored IR code",
    }
    if map_path:
        path = Path(map_path)
        status["forwarder_map_path"] = str(path)
        status["forwarder_map_exists"] = path.exists()
        try:
            status["forwarder_mapped_codes"] = sorted(load_code_map(path))
        except Exception as exc:
            status["forwarder_map_error"] = f"{type(exc).__name__}: {exc}"
    return status
