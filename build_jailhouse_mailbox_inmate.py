#!/usr/bin/env python3
import os
import shutil
import subprocess
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
JH_DIR = Path(os.environ.get(
    "JH_DIR",
    "/home/goushi/openamp-jailhouse/phytium-jailhouse/src/jailhouse",
))
KERNEL_BUILD_DIR = Path(os.environ.get(
    "KERNEL_BUILD_DIR",
    f"/lib/modules/{os.uname().release}/build" if hasattr(os, "uname") else "",
))

SOURCE_TEMPLATE = APP_DIR / "jailhouse_smart_home_mailbox_inmate.c"
SOURCE = JH_DIR / "inmates/demos/smart-home-mailbox.c"
OBJECT = JH_DIR / "inmates/demos/smart-home-mailbox.o"
DEPENDENCY = JH_DIR / "inmates/demos/.smart-home-mailbox.o.d"
LINKED = JH_DIR / "inmates/demos/arm64/smart-home-mailbox-linked.o"
BINARY = JH_DIR / "inmates/demos/arm64/smart-home-mailbox.bin"
MARKER = "smart-home mailbox inmate ready"


def fail(message):
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def run(command, cwd):
    print("+ cd", cwd, "&&", " ".join(str(item) for item in command), flush=True)
    subprocess.run([str(item) for item in command], cwd=str(cwd), check=True)


def main():
    if not JH_DIR.is_dir():
        fail(f"Jailhouse source directory not found: {JH_DIR}")
    if not KERNEL_BUILD_DIR.is_dir():
        fail(f"kernel build directory not found: {KERNEL_BUILD_DIR}")
    if not SOURCE_TEMPLATE.is_file():
        fail(f"business inmate source not found: {SOURCE_TEMPLATE}")

    SOURCE.parent.mkdir(parents=True, exist_ok=True)
    BINARY.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE_TEMPLATE, SOURCE)
    print(f"copied {SOURCE_TEMPLATE} -> {SOURCE}")

    for path in (OBJECT, DEPENDENCY, LINKED, BINARY):
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    run([
        "gcc",
        f"-Wp,-MMD,{DEPENDENCY}",
        "-nostdinc",
        f"-I{JH_DIR}/inmates/lib/arm64",
        f"-I{JH_DIR}/inmates/../include/arch/arm64",
        f"-I{JH_DIR}/inmates/lib/include",
        f"-I{JH_DIR}/inmates/../include",
        f"-I{JH_DIR}/inmates/lib/arm64/include",
        f"-I{JH_DIR}/inmates/lib/arm64/../arm-common/include",
        "-include", "./include/linux/compiler_types.h",
        "-D__KERNEL__",
        "-mlittle-endian",
        "-DKASAN_SHADOW_SCALE_SHIFT=",
        "-fmacro-prefix-map=./=",
        "-g", "-Os", "-Werror", "-Wall", "-Wstrict-prototypes",
        "-Wtype-limits", "-Wmissing-declarations", "-Wmissing-prototypes",
        "-fno-strict-aliasing", "-fomit-frame-pointer", "-fno-pic",
        "-fno-common", "-fno-stack-protector", "-ffreestanding",
        "-ffunction-sections", "-D__LINUX_COMPILER_TYPES_H",
        f'-DKBUILD_MODFILE="{JH_DIR}/inmates/demos/arm64/smart-home-mailbox"',
        '-DKBUILD_BASENAME="smart_home_mailbox"',
        '-DKBUILD_MODNAME="smart_home_mailbox"',
        "-D__KBUILD_MODNAME=kmod_smart_home_mailbox",
        "-c", "-o", OBJECT, SOURCE,
    ], KERNEL_BUILD_DIR)

    run([
        "ld", "-EL", "-maarch64elf", "-z", "noexecstack",
        "--no-warn-rwx-segments", "--gc-sections",
        "-T", JH_DIR / "inmates/lib/arm64/inmate.lds",
        OBJECT, JH_DIR / "inmates/lib/arm64/lib.a", "-o", LINKED,
    ], JH_DIR)

    run([
        "objcopy", "-O", "binary", "--remove-section=.note.gnu.property",
        LINKED, BINARY,
    ], JH_DIR)

    strings = subprocess.run(
        ["strings", str(BINARY)], capture_output=True, text=True, check=True
    ).stdout
    if MARKER not in strings:
        fail(f"final binary does not contain marker: {MARKER}")
    print(f"OK: {BINARY} ({BINARY.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
