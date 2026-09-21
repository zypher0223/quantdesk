"""OS process isolation for external plugin commands."""

from __future__ import annotations

import json
import os
from functools import lru_cache
import platform
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path


# Paths a plugin process must be able to read for an interpreter to start:
# the OS runtime plus QuantDesk's own venv. Kept as one list so the probe and the
# profile a plugin actually runs under cannot drift apart.
BASE_READABLE: tuple[str, ...] = (
    "/System",
    "/usr",
    "/bin",
    "/sbin",
    "/Library",
    "/private/etc",
    "/private/var/db",
    "/dev",
)


def _signal_name(returncode: int) -> str | None:
    """Map a negative return code to its signal name.

    A process killed by a signal carries no stderr, so without this the reason
    for a failed sandbox probe is lost entirely.
    """
    if returncode >= 0:
        return None
    try:
        return signal.Signals(-returncode).name
    except ValueError:
        return f"signal {-returncode}"


@dataclass(frozen=True)
class SandboxStatus:
    policy: str
    backend: str | None
    available: bool
    enforced: bool
    detail: str
    signal: str | None = None
    probe_command: tuple[str, ...] = ()
    diagnostic: str = ""

    def as_dict(self) -> dict:
        return {
            "policy": self.policy,
            "backend": self.backend,
            "available": self.available,
            "enforced": self.enforced,
            "detail": self.detail,
            # Why the probe failed, in the terms a user can act on.
            "signal": self.signal,
            "probeCommand": list(self.probe_command),
            "diagnostic": self.diagnostic,
        }


def _probe_profile() -> str:
    """The read-only profile used to decide whether the backend is usable.

    Deliberately the same shape as what `wrap()` hands a plugin, so a probe
    failure means enforcement would fail too.
    """
    reads = " ".join(f'(subpath {_quoted(path)})' for path in BASE_READABLE)
    return f'(version 1) (deny default) (allow process*) (allow signal) (allow sysctl-read) (allow mach-lookup) (allow file-read-metadata) (allow file-read* {reads})'


def _probe_backend() -> tuple[str | None, bool, str, str | None, tuple[str, ...]]:
    """Return (backend, available, error, signal, probe command)."""
    backend = None
    candidate: list[str] | None = None
    if platform.system() == "Darwin" and Path("/usr/bin/sandbox-exec").is_file():
        backend = "sandbox-exec"
        candidate = ["/usr/bin/sandbox-exec", "-p", _probe_profile(), "/usr/bin/true"]
    elif platform.system() == "Linux" and shutil.which("bwrap"):
        backend = "bubblewrap"
        probe_fn = shutil.which("bwrap") or "bwrap"
        candidate = [probe_fn, "--ro-bind", "/", "/", "--unshare-all", "--", "/bin/true"]
    available = False
    probe_error = ""
    probe_signal: str | None = None
    if candidate:
        try:
            probe = subprocess.run(candidate, capture_output=True, text=True, timeout=5, check=False)
            available = probe.returncode == 0
            if not available:
                probe_signal = _signal_name(probe.returncode)
                probe_error = (probe.stderr or probe.stdout).strip()[-300:]
                if not probe_error and probe_signal:
                    # Terminated by a signal: the kernel produced no message, so
                    # name the signal instead of leaving the reason blank.
                    probe_error = f"探针被 {probe_signal} 终止（无 stderr 输出）"
        except (OSError, subprocess.TimeoutExpired) as exc:
            probe_error = str(exc)
    return backend, available, probe_error, probe_signal, tuple(candidate or ())


def diagnose() -> SandboxStatus:
    """Full sandbox report for a human: probed backend plus what failed and how."""
    base = status()
    if base.available:
        diagnostic = "沙箱可用；插件会在受限配置下运行。"
    elif base.signal:
        diagnostic = (
            f"探针进程被 {base.signal} 终止，说明该主机拒绝启动受限沙箱配置；"
            "sandbox-exec 在部分 macOS 版本上对受限 subpath 读权限会直接 abort。"
            "可复现：见 probeCommand。"
        )
    elif base.backend:
        diagnostic = "探针返回非零退出码；stderr 见 detail。"
    else:
        diagnostic = "未检测到 sandbox-exec 或 bwrap。"
    return replace(base, diagnostic=diagnostic)


def configured_policy() -> str:
    """The sandbox policy: the environment wins, then config.toml, then preferred.

    An operator who wants isolation enforced should not have to edit a launchd
    plist or a compose file to say so - but the environment still overrides the
    file, because that is how a container image pins it.
    """
    policy = os.environ.get("QUANTDESK_PLUGIN_SANDBOX", "").strip().lower()
    if policy not in {"required", "preferred", "off"}:
        policy = ""
    if not policy:
        try:
            from ..config.settings import load_app_config

            section = getattr(load_app_config(), "plugins", None) or {}
            candidate = str(section.get("sandbox") or "").strip().lower()
            policy = candidate if candidate in {"required", "preferred", "off"} else ""
        except Exception:  # noqa: BLE001 - a broken config must not break plugin loading
            policy = ""
    return policy or "preferred"


@lru_cache(maxsize=1)
def status_cached() -> SandboxStatus:
    """The sandbox state, probed once per process.

    `status()` forks a probe process, which is the right cost for a settings page
    and the wrong one for every factor run's provenance label. The state cannot
    change inside a running engine without a restart: the policy comes from the
    environment or config.toml, and the backend's availability comes from the host.
    """
    return status()


def status() -> SandboxStatus:
    policy = configured_policy()
    backend, available, probe_error, probe_signal, probe_command = _probe_backend()
    enforced = available and policy != "off"
    if policy == "off":
        detail = "主机策略关闭了操作系统沙箱"
    elif backend and available:
        detail = f"使用 {backend} 限制文件系统、写入范围和网络"
    elif backend:
        detail = f"检测到 {backend}，但沙箱探针失败：{probe_error or '原因未知'}"
        if policy == "preferred":
            detail += "；按 preferred 策略，插件仍会运行，但没有操作系统级隔离"
        else:
            detail += "；按 required 策略，插件将被拒绝运行"
        if "subpath" in (probe_command or "") or probe_signal == "SIGABRT":
            # Measured on macOS 27 (26A428): any filtered file-read rule aborts
            # the probe, while an unfiltered `(allow file-read*)` runs. The OS
            # build cannot express a filesystem-limited profile, so the honest
            # advice is a container, not a tweaked profile.
            detail += "；该 macOS 版本对带 file-read 过滤的配置直接 abort，请在容器（bwrap）或独立用户下运行"
    elif policy == "required":
        detail = "主机要求安全沙箱，但没有找到 sandbox-exec 或 bwrap"
    else:
        detail = "没有可用的操作系统沙箱；仅保留环境变量白名单、超时和输出限制"
    return SandboxStatus(
        policy=policy,
        backend=backend,
        available=available,
        enforced=enforced,
        detail=detail,
        signal=probe_signal,
        probe_command=probe_command,
    )


def _quoted(path: Path | str) -> str:
    return json.dumps(str(path))


def _mac_profile(
    plugin_path: Path,
    data_path: Path,
    quantdesk_home: Path,
    runtime_path: Path | None,
    network: bool,
) -> str:
    readable = {
        plugin_path.resolve(),
        data_path.resolve(),
        Path(sys.prefix).resolve(),
        Path(sys.base_prefix).resolve(),
        Path(sys.executable).resolve().parent,
        Path("/System"),
        Path("/usr"),
        Path("/bin"),
        Path("/sbin"),
        Path("/Library"),
        Path("/private/etc"),
        Path("/private/var/db"),
        Path("/dev"),
    }
    if runtime_path:
        readable.add(runtime_path.resolve())
    rules = [
        "(version 1)",
        "(deny default)",
        "(allow process*)",
        "(allow signal)",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow ipc-posix*)",
        "(allow file-read-metadata)",
        f"(allow file-read* file-write* (subpath {_quoted(data_path.resolve())}))",
    ]
    for path in sorted(readable, key=str):
        rules.append(f"(allow file-read* (subpath {_quoted(path)}))")
    if network:
        rules.append("(allow network*)")
    return " ".join(rules)


def _relative_parents(home: Path, target: Path) -> list[Path]:
    try:
        relative = target.resolve().relative_to(home.resolve())
    except ValueError:
        return []
    current = home.resolve()
    parents: list[Path] = []
    for part in relative.parts[:-1]:
        current = current / part
        parents.append(current)
    return parents


def wrap(
    command: list[str],
    *,
    plugin_path: Path,
    data_path: Path,
    quantdesk_home: Path,
    runtime_path: Path | None,
    network: bool,
) -> tuple[list[str], SandboxStatus]:
    current = status()
    if current.policy == "off":
        return command, current
    if not current.available:
        if current.policy == "required":
            raise RuntimeError(current.detail)
        return command, current
    if current.backend == "sandbox-exec":
        profile = _mac_profile(plugin_path, data_path, quantdesk_home, runtime_path, network)
        return ["/usr/bin/sandbox-exec", "-p", profile, *command], current

    # Linux/Docker: make the root read-only, hide the account home and all
    # QuantDesk state, then
    # mount back only this plugin's code (read-only), data, and private runtime.
    wrapped = [
        shutil.which("bwrap") or "bwrap", "--die-with-parent", "--new-session", "--unshare-all",
        "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
    ]
    if network:
        wrapped.append("--share-net")
    home = quantdesk_home.resolve()
    account_home = Path.home().resolve()
    hidden_roots = [account_home]
    try:
        home.relative_to(account_home)
    except ValueError:
        hidden_roots.append(home)
    for hidden in sorted(hidden_roots, key=lambda item: len(item.parts)):
        wrapped.extend(["--tmpfs", str(hidden)])
    required_parents: set[Path] = set()
    for target in (plugin_path, data_path, runtime_path):
        if target is None or (target == runtime_path and not target.exists()):
            continue
        resolved = target.resolve()
        containing = [root for root in hidden_roots if resolved.is_relative_to(root)]
        if containing:
            root = max(containing, key=lambda item: len(item.parts))
            required_parents.update(_relative_parents(root, resolved))
            required_parents.add(resolved)
    for parent in sorted(required_parents, key=lambda item: len(item.parts)):
        wrapped.extend(["--dir", str(parent)])
    wrapped.extend(["--ro-bind", str(plugin_path.resolve()), str(plugin_path.resolve())])
    wrapped.extend(["--bind", str(data_path.resolve()), str(data_path.resolve())])
    if runtime_path and runtime_path.exists():
        wrapped.extend(["--ro-bind", str(runtime_path.resolve()), str(runtime_path.resolve())])
    wrapped.extend(["--tmpfs", "/tmp", "--chdir", str(plugin_path.resolve()), "--", *command])
    return wrapped, current
