from __future__ import annotations

import ctypes
import platform
import subprocess  # nosec B404
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProcessBirthIdentity:
    """Stable process identity that remains safe across PID reuse."""

    platform: str
    boot_identity: str
    pid: int
    start_value: str

    def payload(self) -> dict[str, object]:
        return asdict(self)


def _linux_birth_identity(pid: int) -> ProcessBirthIdentity:
    boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    close = stat.rfind(")")
    fields_after_comm = stat[close + 2 :].split()
    if close < 0 or len(fields_after_comm) < 20:
        raise RuntimeError("process_birth_identity_unavailable")
    # field 22 is index 19 after removing pid and comm.
    start_ticks = fields_after_comm[19]
    return ProcessBirthIdentity("linux", boot, pid, start_ticks)


def _darwin_birth_identity(pid: int) -> ProcessBirthIdentity:
    boot = subprocess.run(  # noqa: S603  # nosec B603
        ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    buffer = ctypes.create_string_buffer(136)
    # PROC_PIDTBSDINFO = 3; start sec/usec are the final two uint64 fields.
    observed = libproc.proc_pidinfo(pid, 3, 0, buffer, ctypes.sizeof(buffer))
    if observed < 136:
        raise RuntimeError("process_birth_identity_unavailable")
    seconds = int.from_bytes(buffer.raw[120:128], "little")
    microseconds = int.from_bytes(buffer.raw[128:136], "little")
    return ProcessBirthIdentity("darwin", boot, pid, f"{seconds}.{microseconds:06d}")


def process_birth_identity(pid: int) -> ProcessBirthIdentity:
    """Resolve a process identity or fail closed when the platform cannot prove it."""

    if pid <= 1:
        raise RuntimeError("process_birth_identity_invalid_pid")
    system = platform.system().lower()
    try:
        if system == "linux":
            return _linux_birth_identity(pid)
        if system == "darwin":
            return _darwin_birth_identity(pid)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RuntimeError("process_birth_identity_unavailable") from exc
    raise RuntimeError("process_birth_identity_platform_unsupported")


def identity_matches(identity: ProcessBirthIdentity) -> bool:
    """Return whether the live PID still has the exact recorded birth identity."""

    try:
        return process_birth_identity(identity.pid) == identity
    except RuntimeError:
        return False
