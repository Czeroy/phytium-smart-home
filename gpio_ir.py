import errno
import mmap
import os
import re
import struct
import subprocess
import time
from pathlib import Path


EDGE_RE = re.compile(
    r"event:\s+(FALLING|RISING)\s+EDGE.*?timestamp:\s+\[\s*([0-9]+(?:\.[0-9]+)?)\s*\]",
    re.IGNORECASE,
)


def parse_gpiomon_edges(output):
    edges = []
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="ignore")

    for line in str(output or "").splitlines():
        match = EDGE_RE.search(line)
        if not match:
            continue
        edges.append({
            "edge": match.group(1).upper(),
            "timestamp": float(match.group(2)),
        })
    return edges


def edges_to_raw(edges, frame_gap_us=30000, min_duration_us=80, max_duration_us=200000):
    if len(edges) < 2:
        return []

    start = 0
    while start < len(edges) and edges[start]["edge"] != "FALLING":
        start += 1
    edges = edges[start:]
    if len(edges) < 2:
        return []

    raw = []
    for current, following in zip(edges, edges[1:]):
        duration_us = int(round((following["timestamp"] - current["timestamp"]) * 1_000_000))
        if duration_us <= 0:
            continue
        if duration_us > frame_gap_us and raw:
            break
        if duration_us < min_duration_us or duration_us > max_duration_us:
            continue
        raw.append(duration_us)

    if len(raw) >= 2 and len(raw) % 2 == 1:
        raw.pop()
    return raw


def capture_raw_gpio(chip="gpiochip2", line=10, timeout=8, max_edges=700, frame_gap_us=30000):
    command = ["gpiomon", "-n", str(max_edges), "-r", "-f", chip, str(line)]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = completed.stdout
        error_output = completed.stderr
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        error_output = exc.stderr or ""
    except FileNotFoundError:
        return False, [], "未找到 gpiomon，请先安装 gpiod: sudo apt install -y gpiod"

    edges = parse_gpiomon_edges(output)
    raw = edges_to_raw(edges, frame_gap_us=frame_gap_us)

    if len(raw) < 12:
        detail = str(error_output or "").strip()
        suffix = f"；gpiomon: {detail}" if detail else ""
        return False, raw, f"没有收到完整红外信号，请确认接收头接线、电源和按键时机{suffix}"

    return True, raw, f"GPIO 学习成功，共收到 {len(raw)} 个红外脉冲"


def _write_sysfs(path, value):
    path = Path(path)
    try:
        path.write_text(str(value), encoding="ascii")
    except OSError as exc:
        detail = f"{path} <- {value}"
        raise OSError(exc.errno, f"{exc.strerror}: {detail}") from exc


def _export_pwm(pwm_chip_path, channel):
    pwm_path = pwm_chip_path / f"pwm{channel}"
    if pwm_path.exists():
        return pwm_path

    _write_sysfs(pwm_chip_path / "export", channel)
    deadline = time.time() + 1.0
    while time.time() < deadline:
        if pwm_path.exists():
            return pwm_path
        time.sleep(0.02)
    raise RuntimeError(f"PWM channel did not appear: {pwm_path}")


def _set_pwm_enabled(pwm_path, enabled):
    try:
        _write_sysfs(Path(pwm_path) / "enable", 1 if enabled else 0)
    except OSError as exc:
        if not enabled and exc.errno == errno.EINVAL:
            return
        raise


def _set_pwm_polarity(pwm_path, polarity):
    if not polarity:
        return
    polarity = str(polarity).strip().lower()
    if polarity not in {"normal", "inversed"}:
        raise RuntimeError(f"Unsupported PWM polarity: {polarity}")
    polarity_path = Path(pwm_path) / "polarity"
    if not polarity_path.exists():
        return
    try:
        _write_sysfs(polarity_path, polarity)
    except OSError as exc:
        if polarity == "normal" and exc.errno == errno.EINVAL:
            return
        raise


def _parse_int_env(name, default):
    raw = os.getenv(name)
    if raw in {None, ""}:
        return default
    try:
        return int(str(raw), 0)
    except ValueError:
        return default


def _sleep_us(duration_us):
    duration_us = max(0, int(duration_us))
    if duration_us <= 0:
        return

    deadline = time.perf_counter_ns() + (duration_us * 1000)
    if duration_us > 2000:
        time.sleep((duration_us - 1000) / 1_000_000)
    while time.perf_counter_ns() < deadline:
        pass


def _send_raw_pwm_sysfs(raw, pwm_chip="pwmchip2", channel=0, carrier_hz=38000, duty_cycle=0.33, polarity=None):
    if not raw:
        return False, "红外码为空，无法发送"

    pwm_chip_path = Path("/sys/class/pwm") / pwm_chip
    if not pwm_chip_path.exists():
        return False, f"未找到 PWM 设备: {pwm_chip_path}"

    try:
        pwm_path = _export_pwm(pwm_chip_path, int(channel))
        period_ns = max(1, int(round(1_000_000_000 / int(carrier_hz))))
        duty_ns = max(1, min(period_ns - 1, int(round(period_ns * float(duty_cycle)))))
        polarity = polarity or os.getenv("IR_PWM_POLARITY", "normal")

        _set_pwm_enabled(pwm_path, False)
        _set_pwm_polarity(pwm_path, polarity)
        _write_sysfs(pwm_path / "period", period_ns)
        _write_sysfs(pwm_path / "duty_cycle", duty_ns)

        try:
            for index, duration in enumerate(raw):
                _set_pwm_enabled(pwm_path, index % 2 == 0)
                _sleep_us(duration)
        finally:
            _set_pwm_enabled(pwm_path, False)
        return True, f"GPIO/PWM(sysfs) 红外发送完成，共 {len(raw)} 段"
    except PermissionError:
        return False, "没有 PWM 写入权限，请用 root 运行服务，或给 /sys/class/pwm 配置权限"
    except OSError as exc:
        return False, f"GPIO/PWM(sysfs) 红外发送失败: {exc}"
    except RuntimeError as exc:
        return False, str(exc)


_PAGE_SIZE = mmap.PAGESIZE
_PWM_REG_TCTRL = 0x04
_PWM_REG_TPERIOD = 0x0C
_PWM_REG_PWMCTRL = 0x10
_PWM_REG_PWMCCR = 0x14
_PWM_CHANNEL_BASE_OFFSET = 0x400
_PWM_CHANNEL_STRIDE = 0x400
_PWM_ENABLE_MASK = 0x2


def _page_base(addr):
    return addr & ~(_PAGE_SIZE - 1)


def _page_offset(addr):
    return addr - _page_base(addr)


def _read_reg(mm, offset):
    value, = struct.unpack_from("<I", mm, offset)
    return value


def _write_reg(mm, offset, value):
    struct.pack_into("<I", mm, offset, value & 0xFFFFFFFF)


def _direct_pwm_config(channel):
    return {
        "base_addr": _parse_int_env("IR_PWM_MMIO_BASE", 0x2804B000),
        "iopad_base_addr": _parse_int_env("IR_IOPAD_BASE", 0x32B30000),
        "iopad_reg_offset": _parse_int_env("IR_IOPAD_REG_OFFSET", 0x0060),
        "iopad_func": _parse_int_env("IR_IOPAD_FUNC", 0x1),
        "active_pwmctrl": _parse_int_env("IR_PWM_ACTIVE_CTRL", 0x00C4),
        "idle_pwmctrl": _parse_int_env("IR_PWM_IDLE_CTRL", 0x0014),
        "base_clock_hz": _parse_int_env("IR_PWM_BASE_CLOCK_HZ", 50_000_000),
        "fixed_div": _parse_int_env("IR_PWM_FIXED_DIV", -1),
        "pre_idle_us": _parse_int_env("IR_PWM_PRE_IDLE_US", 10_000),
        "post_idle_us": _parse_int_env("IR_PWM_POST_IDLE_US", 10_000),
        "channel": int(channel),
    }


def _compute_direct_pwm_counts(base_clock_hz, carrier_hz, duty_cycle, fixed_div=-1):
    carrier_hz = max(1, int(carrier_hz))
    duty_cycle = max(0.01, min(0.99, float(duty_cycle)))

    if int(fixed_div) >= 0:
        div = int(fixed_div)
        cycles = int(round(base_clock_hz / ((div + 1) * carrier_hz)))
        if not (2 <= cycles <= 0x10000):
            raise RuntimeError(
                f"Fixed div {div} produced invalid cycle count {cycles} for {carrier_hz}Hz @ {base_clock_hz}Hz"
            )
        period = cycles - 1
        duty = max(1, min(period, int(round(cycles * duty_cycle)) - 1))
        return div, period, duty

    for div in range(0, 4096):
        cycles = int(round(base_clock_hz / ((div + 1) * carrier_hz)))
        if 2 <= cycles <= 0x10000:
            period = cycles - 1
            duty = max(1, min(period, int(round(cycles * duty_cycle)) - 1))
            return div, period, duty

    raise RuntimeError(
        f"Unable to derive direct PWM counters for {carrier_hz}Hz @ {base_clock_hz}Hz base clock"
    )


def _send_raw_pwm_devmem(raw, channel=0, carrier_hz=38000, duty_cycle=0.33):
    if os.name == "nt":
        return False, "Windows 不支持 /dev/mem 直控 PWM"
    if not raw:
        return False, "红外码为空，无法发送"

    mem_path = Path("/dev/mem")
    if not mem_path.exists():
        return False, "未找到 /dev/mem，无法使用直接寄存器 PWM 后备方案"

    config = _direct_pwm_config(channel)
    channel_base = config["base_addr"] + _PWM_CHANNEL_BASE_OFFSET + (_PWM_CHANNEL_STRIDE * config["channel"])

    try:
        div, period, duty = _compute_direct_pwm_counts(
            config["base_clock_hz"], carrier_hz, duty_cycle, fixed_div=config["fixed_div"]
        )
    except RuntimeError as exc:
        return False, str(exc)

    fd = None
    iopad_mm = None
    pwm_mm = None
    pwm_backup = None
    try:
        fd = os.open(str(mem_path), os.O_RDWR | os.O_SYNC)

        iopad_mm = mmap.mmap(
            fd,
            _PAGE_SIZE,
            mmap.MAP_SHARED,
            mmap.PROT_READ | mmap.PROT_WRITE,
            offset=_page_base(config["iopad_base_addr"] + config["iopad_reg_offset"]),
        )
        pwm_mm = mmap.mmap(
            fd,
            _PAGE_SIZE,
            mmap.MAP_SHARED,
            mmap.PROT_READ | mmap.PROT_WRITE,
            offset=_page_base(channel_base),
        )

        iopad_off = _page_offset(config["iopad_base_addr"] + config["iopad_reg_offset"])
        tctrl_off = _page_offset(channel_base + _PWM_REG_TCTRL)
        tperiod_off = _page_offset(channel_base + _PWM_REG_TPERIOD)
        pwmctrl_off = _page_offset(channel_base + _PWM_REG_PWMCTRL)
        pwmccr_off = _page_offset(channel_base + _PWM_REG_PWMCCR)

        pwm_backup = {
            "tctrl": _read_reg(pwm_mm, tctrl_off),
            "tperiod": _read_reg(pwm_mm, tperiod_off),
            "pwmctrl": _read_reg(pwm_mm, pwmctrl_off),
            "pwmccr": _read_reg(pwm_mm, pwmccr_off),
        }

        # Force pin 32 (AG57) into PWM2_OUT mode.
        iopad_reg = _read_reg(iopad_mm, iopad_off)
        iopad_reg = (iopad_reg & ~0x7) | (config["iopad_func"] & 0x7)
        _write_reg(iopad_mm, iopad_off, iopad_reg)

        # Firefly pin 32 idles high if we only disable TCTRL. Keep a dedicated
        # idle-low PWMCTRL profile so the transmitter input rests at 0V.
        #
        # Toggling TCTRL enable/disable for every mark/space transition proved
        # unstable on this board. Keep the timer enabled for the whole
        # transmission and gate the carrier by switching PWMCTRL between
        # active-carrier and idle-low profiles instead.
        _write_reg(pwm_mm, tctrl_off, 0)
        _write_reg(pwm_mm, tperiod_off, period)
        _write_reg(pwm_mm, pwmccr_off, duty)
        _write_reg(pwm_mm, pwmctrl_off, config["idle_pwmctrl"])

        enabled_tctrl = (div << 16) | _PWM_ENABLE_MASK
        _write_reg(pwm_mm, tctrl_off, enabled_tctrl)

        if config["pre_idle_us"] > 0:
            time.sleep(config["pre_idle_us"] / 1_000_000)

        for index, duration in enumerate(raw):
            if index % 2 == 0:
                _write_reg(pwm_mm, pwmctrl_off, config["active_pwmctrl"])
            else:
                _write_reg(pwm_mm, pwmctrl_off, config["idle_pwmctrl"])
            _sleep_us(duration)

        return True, f"GPIO/PWM(devmem) 红外发送完成，共 {len(raw)} 段"
    except PermissionError:
        return False, "没有 /dev/mem 写入权限，请用 root 运行服务"
    except OSError as exc:
        return False, f"GPIO/PWM(devmem) 红外发送失败: {exc}"
    finally:
        if pwm_mm is not None and pwm_backup is not None:
            try:
                _write_reg(pwm_mm, pwmctrl_off, config["idle_pwmctrl"])
                if config["post_idle_us"] > 0:
                    time.sleep(config["post_idle_us"] / 1_000_000)
                _write_reg(pwm_mm, tctrl_off, 0)
                _write_reg(pwm_mm, tperiod_off, pwm_backup["tperiod"])
                _write_reg(pwm_mm, pwmctrl_off, pwm_backup["pwmctrl"])
                _write_reg(pwm_mm, pwmccr_off, pwm_backup["pwmccr"])
                _write_reg(pwm_mm, tctrl_off, pwm_backup["tctrl"])
            except OSError:
                pass
        if pwm_mm is not None:
            try:
                pwm_mm.close()
            except OSError:
                pass
        if iopad_mm is not None:
            try:
                iopad_mm.close()
            except OSError:
                pass
        if fd is not None:
            os.close(fd)


def send_raw_pwm(raw, pwm_chip="pwmchip2", channel=0, carrier_hz=38000, duty_cycle=0.33, polarity=None):
    driver = os.getenv("IR_PWM_DRIVER", "auto").strip().lower()
    if driver in {"devmem", "direct", "mmio"}:
        return _send_raw_pwm_devmem(
            raw,
            channel=channel,
            carrier_hz=carrier_hz,
            duty_cycle=duty_cycle,
        )

    success, message = _send_raw_pwm_sysfs(
        raw,
        pwm_chip=pwm_chip,
        channel=channel,
        carrier_hz=carrier_hz,
        duty_cycle=duty_cycle,
        polarity=polarity,
    )
    if success or driver == "sysfs":
        return success, message

    if os.name != "nt" and Path("/dev/mem").exists():
        direct_success, direct_message = _send_raw_pwm_devmem(
            raw,
            channel=channel,
            carrier_hz=carrier_hz,
            duty_cycle=duty_cycle,
        )
        if direct_success:
            return direct_success, direct_message
        return False, f"{message}; devmem fallback also failed: {direct_message}"

    return success, message


def backend_status(rx_chip="gpiochip2", rx_line=10, pwm_chip="pwmchip2", pwm_channel=0):
    rx_path = Path("/dev") / rx_chip
    pwm_chip_path = Path("/sys/class/pwm") / pwm_chip
    pwm_path = pwm_chip_path / f"pwm{pwm_channel}"
    direct = _direct_pwm_config(pwm_channel)

    return {
        "rx_chip": rx_chip,
        "rx_line": int(rx_line),
        "rx_board_pin": os.getenv("IR_RX_BOARD_PIN", "7"),
        "rx_gpio_name": os.getenv("IR_RX_GPIO_NAME", "GPIO2_10"),
        "rx_device": str(rx_path),
        "rx_exists": rx_path.exists(),
        "pwm_driver": os.getenv("IR_PWM_DRIVER", "auto").strip().lower(),
        "pwm_chip": pwm_chip,
        "pwm_channel": int(pwm_channel),
        "tx_board_pin": os.getenv("IR_TX_BOARD_PIN", "32"),
        "tx_gpio_name": os.getenv("IR_TX_GPIO_NAME", "GPIO1_1 / PWM2 / AG57"),
        "pwm_chip_path": str(pwm_chip_path),
        "pwm_chip_exists": pwm_chip_path.exists(),
        "pwm_channel_path": str(pwm_path),
        "pwm_channel_exported": pwm_path.exists(),
        "dev_mem": "/dev/mem",
        "dev_mem_exists": Path("/dev/mem").exists(),
        "direct_pwm_mmio_base": hex(direct["base_addr"]),
        "direct_iopad_base": hex(direct["iopad_base_addr"]),
        "direct_iopad_reg_offset": hex(direct["iopad_reg_offset"]),
        "direct_iopad_func": int(direct["iopad_func"]),
        "direct_active_pwmctrl": hex(direct["active_pwmctrl"]),
        "direct_idle_pwmctrl": hex(direct["idle_pwmctrl"]),
        "direct_fixed_div": int(direct["fixed_div"]),
        "direct_pre_idle_us": int(direct["pre_idle_us"]),
        "direct_post_idle_us": int(direct["post_idle_us"]),
    }
