import ctypes
import os
import select
import threading
import time
from datetime import datetime
from pathlib import Path


def _env_bool(name, default="0"):
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


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


DHT11_ENABLED = _env_bool("DHT11_ENABLED")
DHT11_CHIP = os.getenv("DHT11_CHIP", "/dev/gpiochip3")
DHT11_LINE = _env_int("DHT11_LINE", 1)
DHT11_BOARD_PIN = _env_int("DHT11_BOARD_PIN", 11)
DHT11_MIN_INTERVAL_SECONDS = max(1.0, _env_float("DHT11_MIN_INTERVAL_SECONDS", 2.0))
DHT11_MAX_STALE_SECONDS = max(
    DHT11_MIN_INTERVAL_SECONDS,
    _env_float("DHT11_MAX_STALE_SECONDS", 30.0),
)
DHT11_RETRIES = max(1, min(_env_int("DHT11_RETRIES", 2), 3))

GPIO_V2_LINES_MAX = 64
GPIO_V2_LINE_NUM_ATTRS_MAX = 10

GPIO_V2_LINE_FLAG_INPUT = 1 << 2
GPIO_V2_LINE_FLAG_OUTPUT = 1 << 3
GPIO_V2_LINE_FLAG_EDGE_RISING = 1 << 4
GPIO_V2_LINE_FLAG_EDGE_FALLING = 1 << 5
GPIO_V2_LINE_FLAG_OPEN_DRAIN = 1 << 6

GPIO_V2_LINE_ATTR_ID_OUTPUT_VALUES = 2
GPIO_V2_LINE_EVENT_RISING_EDGE = 1
GPIO_V2_LINE_EVENT_FALLING_EDGE = 2


class GpioV2LineAttributeData(ctypes.Union):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("values", ctypes.c_uint64),
        ("debounce_period_us", ctypes.c_uint32),
    ]


class GpioV2LineAttribute(ctypes.Structure):
    _fields_ = [
        ("id", ctypes.c_uint32),
        ("padding", ctypes.c_uint32),
        ("data", GpioV2LineAttributeData),
    ]


class GpioV2LineConfigAttribute(ctypes.Structure):
    _fields_ = [
        ("attr", GpioV2LineAttribute),
        ("mask", ctypes.c_uint64),
    ]


class GpioV2LineConfig(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("num_attrs", ctypes.c_uint32),
        ("padding", ctypes.c_uint32 * 5),
        ("attrs", GpioV2LineConfigAttribute * GPIO_V2_LINE_NUM_ATTRS_MAX),
    ]


class GpioV2LineRequest(ctypes.Structure):
    _fields_ = [
        ("offsets", ctypes.c_uint32 * GPIO_V2_LINES_MAX),
        ("consumer", ctypes.c_char * 32),
        ("config", GpioV2LineConfig),
        ("num_lines", ctypes.c_uint32),
        ("event_buffer_size", ctypes.c_uint32),
        ("padding", ctypes.c_uint32 * 5),
        ("fd", ctypes.c_int32),
    ]


class GpioV2LineValues(ctypes.Structure):
    _fields_ = [
        ("bits", ctypes.c_uint64),
        ("mask", ctypes.c_uint64),
    ]


class GpioV2LineEvent(ctypes.Structure):
    _fields_ = [
        ("timestamp_ns", ctypes.c_uint64),
        ("id", ctypes.c_uint32),
        ("offset", ctypes.c_uint32),
        ("seqno", ctypes.c_uint32),
        ("line_seqno", ctypes.c_uint32),
        ("padding", ctypes.c_uint32 * 6),
    ]


def _iowr(type_value, number, struct_type):
    return (
        (3 << 30)
        | (type_value << 8)
        | number
        | (ctypes.sizeof(struct_type) << 16)
    )


GPIO_V2_GET_LINE_IOCTL = _iowr(0xB4, 0x07, GpioV2LineRequest)
GPIO_V2_LINE_SET_CONFIG_IOCTL = _iowr(0xB4, 0x0D, GpioV2LineConfig)
GPIO_V2_LINE_SET_VALUES_IOCTL = _iowr(0xB4, 0x0F, GpioV2LineValues)

_cache_lock = threading.Lock()
_last_good = None
_last_attempt_monotonic = 0.0
_last_attempt_success = False
_last_attempt_error = ""
_libc = None


class DHT11ReadError(RuntimeError):
    pass


def _load_libc():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
        _libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
        _libc.ioctl.restype = ctypes.c_int
    return _libc


def _ioctl(fd, request, data):
    result = _load_libc().ioctl(fd, request, ctypes.byref(data))
    if result < 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _busy_wait_microseconds(duration_us):
    deadline = time.perf_counter_ns() + int(duration_us * 1000)
    while time.perf_counter_ns() < deadline:
        pass


def _request_output_line(chip_fd, open_drain=True):
    request = GpioV2LineRequest()
    request.offsets[0] = DHT11_LINE
    request.consumer = b"smart-home-dht11"
    request.num_lines = 1
    request.event_buffer_size = 128
    request.config.flags = GPIO_V2_LINE_FLAG_OUTPUT
    if open_drain:
        request.config.flags |= GPIO_V2_LINE_FLAG_OPEN_DRAIN
    request.config.num_attrs = 1
    request.config.attrs[0].attr.id = GPIO_V2_LINE_ATTR_ID_OUTPUT_VALUES
    request.config.attrs[0].attr.data.values = 0
    request.config.attrs[0].mask = 1
    _ioctl(chip_fd, GPIO_V2_GET_LINE_IOCTL, request)
    return request.fd


def _capture_edges():
    if os.name == "nt":
        raise DHT11ReadError("DHT11 GPIO reading is only available on Linux")
    if not Path(DHT11_CHIP).exists():
        raise DHT11ReadError(f"GPIO device does not exist: {DHT11_CHIP}")

    open_flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        open_flags |= os.O_CLOEXEC
    chip_fd = os.open(DHT11_CHIP, open_flags)
    line_fd = -1
    try:
        try:
            line_fd = _request_output_line(chip_fd, open_drain=True)
        except OSError:
            line_fd = _request_output_line(chip_fd, open_drain=False)
    finally:
        os.close(chip_fd)

    try:
        time.sleep(0.020)

        release = GpioV2LineValues()
        release.bits = 1
        release.mask = 1
        _ioctl(line_fd, GPIO_V2_LINE_SET_VALUES_IOCTL, release)
        _busy_wait_microseconds(25)

        input_config = GpioV2LineConfig()
        input_config.flags = (
            GPIO_V2_LINE_FLAG_INPUT
            | GPIO_V2_LINE_FLAG_EDGE_RISING
            | GPIO_V2_LINE_FLAG_EDGE_FALLING
        )
        _ioctl(line_fd, GPIO_V2_LINE_SET_CONFIG_IOCTL, input_config)
        os.set_blocking(line_fd, False)

        events = []
        event_size = ctypes.sizeof(GpioV2LineEvent)
        deadline = time.monotonic() + 0.012
        while time.monotonic() < deadline and len(events) < 100:
            remaining = max(0.0, deadline - time.monotonic())
            readable, _, _ = select.select([line_fd], [], [], remaining)
            if not readable:
                break
            try:
                payload = os.read(line_fd, event_size * 64)
            except BlockingIOError:
                continue
            for offset in range(0, len(payload) - event_size + 1, event_size):
                event = GpioV2LineEvent.from_buffer_copy(payload[offset:offset + event_size])
                events.append((event.timestamp_ns, event.id))

        high_pulses = []
        rising_at = None
        for timestamp_ns, event_id in events:
            if event_id == GPIO_V2_LINE_EVENT_RISING_EDGE:
                rising_at = timestamp_ns
            elif event_id == GPIO_V2_LINE_EVENT_FALLING_EDGE and rising_at is not None:
                high_pulses.append((timestamp_ns - rising_at) / 1000.0)
                rising_at = None
        return high_pulses
    finally:
        if line_fd >= 0:
            os.close(line_fd)


def decode_high_pulses(high_pulses_us):
    pulses = [float(value) for value in high_pulses_us if 10.0 <= float(value) <= 120.0]
    if len(pulses) < 40:
        raise DHT11ReadError(f"incomplete DHT11 frame: only {len(pulses)} data pulses")

    checksum_error = ""
    for start in reversed(range(0, len(pulses) - 39)):
        frame = pulses[start:start + 40]
        bits = [1 if width > 50.0 else 0 for width in frame]
        values = []
        for byte_index in range(5):
            value = 0
            for bit in bits[byte_index * 8:(byte_index + 1) * 8]:
                value = (value << 1) | bit
            values.append(value)

        expected_checksum = sum(values[:4]) & 0xFF
        if expected_checksum != values[4]:
            checksum_error = (
                f"checksum mismatch: got {values[4]}, expected {expected_checksum}"
            )
            continue

        humidity = values[0] + values[1] / 10.0
        temperature = (values[2] & 0x7F) + values[3] / 10.0
        if values[2] & 0x80:
            temperature = -temperature
        if not 0.0 <= humidity <= 100.0 or not -20.0 <= temperature <= 80.0:
            raise DHT11ReadError(
                f"out-of-range DHT11 reading: {temperature} C, {humidity}% RH"
            )
        return round(temperature, 1), round(humidity, 1)

    raise DHT11ReadError(checksum_error or "no valid DHT11 frame found")


def _read_once():
    return decode_high_pulses(_capture_edges())


def _reading_payload(success, error="", cached=False, stale=False):
    reading = {
        "success": bool(success),
        "enabled": DHT11_ENABLED,
        "source": "dht11",
        "chip": DHT11_CHIP,
        "line": DHT11_LINE,
        "board_pin": DHT11_BOARD_PIN,
        "temperature": None,
        "humidity": None,
        "updated_at": "",
        "age_seconds": None,
        "cached": bool(cached),
        "stale": bool(stale),
        "error": str(error or ""),
    }
    if _last_good:
        age = max(0.0, time.monotonic() - _last_good["monotonic"])
        reading.update({
            "updated_at": _last_good["updated_at"],
            "age_seconds": round(age, 1),
        })
        if success or age <= DHT11_MAX_STALE_SECONDS:
            reading["temperature"] = _last_good["temperature"]
            reading["humidity"] = _last_good["humidity"]
    return reading


def read_dht11(force=False):
    global _last_attempt_error, _last_attempt_monotonic, _last_attempt_success, _last_good

    if not DHT11_ENABLED:
        return _reading_payload(False, "DHT11 is disabled; set DHT11_ENABLED=1")

    with _cache_lock:
        now_monotonic = time.monotonic()
        if not force and now_monotonic - _last_attempt_monotonic < DHT11_MIN_INTERVAL_SECONDS:
            if _last_attempt_success:
                return _reading_payload(True, cached=True)
            return _reading_payload(
                False,
                _last_attempt_error or "DHT11 read failed",
                cached=bool(_last_good),
                stale=bool(_last_good),
            )

        _last_attempt_monotonic = now_monotonic
        errors = []
        for attempt in range(DHT11_RETRIES):
            try:
                temperature, humidity = _read_once()
                _last_good = {
                    "temperature": temperature,
                    "humidity": humidity,
                    "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "monotonic": time.monotonic(),
                }
                _last_attempt_success = True
                _last_attempt_error = ""
                return _reading_payload(True)
            except (DHT11ReadError, OSError) as exc:
                errors.append(str(exc))
                if attempt + 1 < DHT11_RETRIES:
                    time.sleep(1.05)

        _last_attempt_success = False
        _last_attempt_error = "; ".join(errors[-2:]) or "DHT11 read failed"
        stale = bool(_last_good)
        return _reading_payload(
            False,
            _last_attempt_error,
            cached=bool(_last_good),
            stale=stale,
        )


def sensor_status(read=True):
    if read and DHT11_ENABLED:
        return read_dht11()
    return _reading_payload(False, "DHT11 is disabled" if not DHT11_ENABLED else "")
