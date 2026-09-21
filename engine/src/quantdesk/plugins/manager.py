"""Manifest discovery and subprocess execution for QuantDesk extensions.

Plugins are never imported into the API process. A plugin is a directory with a
quantdesk-plugin.toml manifest and a command that speaks the JSON-RPC protocol
documented in PLUGIN_API.md.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import uuid
import venv
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config.settings import load_runtime_secrets, toml_dumps
from .sandbox import status as sandbox_status
from .sandbox import wrap as sandbox_wrap

# The newest protocol version the engine speaks. It is not the version every
# plugin is spoken to in: each plugin is addressed in the version its own manifest
# declares, and told the engine's newest separately.
PLUGIN_API_VERSION = "4"
# v2 adds portfolio analytics; v3 adds factor research and backtest validation;
# v4 adds the strategy improvement agent. Every version stays accepted: an older
# plugin keeps working untouched, and each new capability is gated on the version
# that defines its message shapes, so an older manifest cannot silently claim it.
PLUGIN_API_VERSIONS = ("1", "2", "3", "4")
MANIFEST_NAME = "quantdesk-plugin.toml"
SUPPORTED_CAPABILITIES = (
    "data_provider",
    "strategy",
    "research_tool",
    "notifier",
    "analytics",
    "factor_provider",
    "backtest_validator",
    "strategy_agent",
)
# The version each capability belongs to. A manifest that declares a capability
# from a newer protocol than it targets is rejected at load.
CAPABILITY_MIN_VERSION = {
    "analytics": "2",
    "factor_provider": "3",
    "backtest_validator": "3",
    "strategy_agent": "4",
}
_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_METHOD_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,80}$")
_GITHUB_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


class PluginError(RuntimeError):
    """A plugin operation failed with a user-actionable message."""


@dataclass(frozen=True)
class PluginManifest:
    id: str
    name: str
    version: str
    api_version: str
    description: str
    capabilities: tuple[str, ...]
    command: tuple[str, ...]
    timeout_seconds: float = 20.0
    homepage: str = ""
    required_env: tuple[str, ...] = ()
    # Whitelisted variables that may legitimately be absent. A research adapter
    # reaches several providers and a deployment may hold keys for only some of
    # them; requiring all of them would make the adapter unusable without buying
    # every subscription first.
    optional_env: tuple[str, ...] = ()
    network: bool = False
    requirements_file: str = ""
    required_executables: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("capabilities", "command", "required_env", "optional_env", "required_executables"):
            data[key] = list(data[key])
        return data


@dataclass(frozen=True)
class PluginRecord:
    manifest: PluginManifest
    path: Path
    enabled: bool
    origin: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.manifest.as_dict(),
            "path": str(self.path),
            "enabled": self.enabled,
            "origin": self.origin,
            "valid": True,
        }


def load_manifest(path: Path) -> PluginManifest:
    manifest_path = path / MANIFEST_NAME
    if not manifest_path.is_file():
        raise PluginError(f"缺少 {MANIFEST_NAME}")
    try:
        with manifest_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PluginError(f"无法读取插件清单：{exc}") from exc

    plugin = raw.get("plugin")
    if not isinstance(plugin, dict):
        raise PluginError("清单必须包含 [plugin]")
    plugin_id = str(plugin.get("id") or "")
    if not _ID_RE.fullmatch(plugin_id):
        raise PluginError("plugin.id 必须以小写字母开头，只能包含小写字母、数字和连字符")
    name = str(plugin.get("name") or "").strip()
    version = str(plugin.get("version") or "").strip()
    api_version = str(plugin.get("api_version") or "")
    description = str(plugin.get("description") or "").strip()
    homepage = str(plugin.get("homepage") or "").strip()
    if not name or not version:
        raise PluginError("plugin.name 和 plugin.version 不能为空")
    if api_version not in PLUGIN_API_VERSIONS:
        raise PluginError(
            f"插件 API 版本 {api_version or '未声明'} 不受支持；当前支持 {'、'.join(PLUGIN_API_VERSIONS)}"
        )

    capabilities_raw = plugin.get("capabilities")
    if not isinstance(capabilities_raw, list) or not capabilities_raw:
        raise PluginError("plugin.capabilities 必须是非空数组")
    capabilities = tuple(dict.fromkeys(str(item) for item in capabilities_raw))
    unknown = [item for item in capabilities if item not in SUPPORTED_CAPABILITIES]
    if unknown:
        raise PluginError(f"不支持的能力：{', '.join(unknown)}")
    # A capability belongs to a protocol version, so the version is part of the
    # claim: a v1 manifest declaring `analytics` would be promising a message
    # shape its own version does not define. The test is "at least", not "equals":
    # a v4 plugin still speaks every earlier business message, and requiring an
    # exact match would make it impossible for one plugin to be, say, both a
    # factor provider (v3) and a strategy agent (v4).
    too_old = [
        f"{item}（需要 API 版本 {CAPABILITY_MIN_VERSION[item]}）"
        for item in capabilities
        if item in CAPABILITY_MIN_VERSION
        and int(api_version) < int(CAPABILITY_MIN_VERSION[item])
    ]
    if too_old:
        raise PluginError(f"清单声明的 API 版本 {api_version} 不支持能力：{', '.join(too_old)}")

    command_raw = plugin.get("command")
    if (
        not isinstance(command_raw, list)
        or not command_raw
        or len(command_raw) > 16
        or any(not isinstance(item, str) or not item or len(item) > 4096 for item in command_raw)
    ):
        raise PluginError("plugin.command 必须是 1–16 个非空字符串组成的数组")

    timeout_seconds = float(plugin.get("timeout_seconds") or 20)
    if not 1 <= timeout_seconds <= 300:
        raise PluginError("plugin.timeout_seconds 必须在 1 到 300 秒之间")

    permissions = plugin.get("permissions") or {}
    if not isinstance(permissions, dict):
        raise PluginError("plugin.permissions 必须是表")
    required_env_raw = permissions.get("env") or []
    if not isinstance(required_env_raw, list):
        raise PluginError("plugin.permissions.env 必须是数组")
    required_env = tuple(dict.fromkeys(str(item) for item in required_env_raw))
    optional_env_raw = permissions.get("optional_env") or []
    if not isinstance(optional_env_raw, list):
        raise PluginError("plugin.permissions.optional_env 必须是数组")
    optional_env = tuple(dict.fromkeys(str(item) for item in optional_env_raw))
    invalid_env = [item for item in (*required_env, *optional_env) if not _ENV_RE.fullmatch(item)]
    if invalid_env:
        raise PluginError(f"环境变量名不合法：{', '.join(invalid_env)}")
    overlap = sorted(set(required_env) & set(optional_env))
    if overlap:
        raise PluginError(f"环境变量不能同时是必需和可选：{', '.join(overlap)}")

    dependencies = plugin.get("dependencies") or {}
    if not isinstance(dependencies, dict):
        raise PluginError("plugin.dependencies 必须是表")
    requirements_file = str(dependencies.get("requirements") or "").strip()
    if requirements_file:
        requirement_path = Path(requirements_file)
        if requirement_path.is_absolute() or ".." in requirement_path.parts:
            raise PluginError("plugin.dependencies.requirements 必须是插件目录内的相对路径")
    executables_raw = dependencies.get("executables") or []
    if not isinstance(executables_raw, list):
        raise PluginError("plugin.dependencies.executables 必须是数组")
    required_executables = tuple(dict.fromkeys(str(item) for item in executables_raw))
    if any(not re.fullmatch(r"[A-Za-z0-9_.+-]{1,80}", item) for item in required_executables):
        raise PluginError("plugin.dependencies.executables 含有非法命令名")

    return PluginManifest(
        id=plugin_id,
        name=name,
        version=version,
        api_version=api_version,
        description=description,
        capabilities=capabilities,
        command=tuple(command_raw),
        timeout_seconds=timeout_seconds,
        homepage=homepage,
        required_env=required_env,
        optional_env=optional_env,
        network=bool(permissions.get("network", False)),
        requirements_file=requirements_file,
        required_executables=required_executables,
    )


class PluginManager:
    """Discover, install, enable, and invoke external repository adapters."""

    def __init__(self, home: Path):
        self.home = Path(home)
        self.install_root = self.home / "plugins"
        self.data_root = self.home / "plugin-data"
        self.runtime_root = self.home / "plugin-runtimes"
        self.state_path = self.home / "plugins.toml"

    def _state(self) -> dict[str, bool]:
        if not self.state_path.exists():
            return {}
        try:
            with self.state_path.open("rb") as handle:
                raw = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise PluginError(f"无法读取 {self.state_path}：{exc}") from exc
        plugins = raw.get("plugins") or {}
        return {str(key): bool(value) for key, value in plugins.items()}

    def _write_state(self, state: dict[str, bool]) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".toml.tmp")
        try:
            tmp.write_text(
                toml_dumps({"plugins": dict(sorted(state.items()))}),
                encoding="utf-8",
            )
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.state_path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise PluginError(f"无法保存插件状态：{exc}") from exc

    def _candidate_paths(self) -> list[Path]:
        candidates: list[Path] = []
        if self.install_root.exists():
            candidates.extend(path for path in self.install_root.iterdir() if path.is_dir())
        for raw in os.environ.get("QUANTDESK_PLUGIN_PATH", "").split(os.pathsep):
            if not raw:
                continue
            path = Path(raw).expanduser()
            if (path / MANIFEST_NAME).is_file():
                candidates.append(path)
            elif path.is_dir():
                candidates.extend(child for child in path.iterdir() if child.is_dir())
        # Installed plugins win if the same id is also exposed through an extra path.
        return list(dict.fromkeys(path.resolve() for path in candidates))

    @staticmethod
    def _origin(path: Path) -> dict[str, Any]:
        source_file = path / ".quantdesk-source.json"
        if not source_file.is_file():
            return {"kind": "path"}
        try:
            value = json.loads(source_file.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"kind": "path"}
        except (OSError, ValueError):
            return {"kind": "path"}

    def discover(self) -> tuple[list[PluginRecord], list[dict[str, str]]]:
        state = self._state()
        valid: dict[str, PluginRecord] = {}
        invalid: list[dict[str, str]] = []
        for path in self._candidate_paths():
            try:
                manifest = load_manifest(path)
                if manifest.id in valid:
                    invalid.append(
                        {
                            "path": str(path),
                            "id": manifest.id,
                            "error": f"插件 ID 重复；已使用 {valid[manifest.id].path}",
                        }
                    )
                    continue
                valid[manifest.id] = PluginRecord(
                    manifest=manifest,
                    path=path,
                    enabled=state.get(manifest.id, False),
                    origin=self._origin(path),
                )
            except PluginError as exc:
                invalid.append({"path": str(path), "id": path.name, "error": str(exc)})
        return sorted(valid.values(), key=lambda item: item.manifest.id), invalid

    def get(self, plugin_id: str) -> PluginRecord:
        plugins, _ = self.discover()
        for plugin in plugins:
            if plugin.manifest.id == plugin_id:
                return plugin
        raise PluginError(f"没有找到插件 {plugin_id}")

    def set_enabled(self, plugin_id: str, enabled: bool) -> PluginRecord:
        plugin = self.get(plugin_id)  # refuse stale/invalid ids
        if enabled:
            dependencies = self.dependency_status(plugin_id)
            if not dependencies["ready"]:
                detail = "; ".join(dependencies["problems"])
                raise PluginError(f"插件依赖尚未就绪：{detail}")
            sandbox = sandbox_status()
            if sandbox.policy == "required" and not sandbox.available:
                raise PluginError(sandbox.detail)
            # Enabling new code is gated on one successful isolated execution.
            self.health(plugin_id)
        state = self._state()
        state[plugin_id] = bool(enabled)
        self._write_state(state)
        return self.get(plugin_id)

    def _requirements_path(self, plugin: PluginRecord) -> Path | None:
        if not plugin.manifest.requirements_file:
            return None
        path = (plugin.path / plugin.manifest.requirements_file).resolve()
        try:
            path.relative_to(plugin.path.resolve())
        except ValueError as exc:
            raise PluginError("依赖文件不能位于插件目录之外") from exc
        if not path.is_file() or path.is_symlink():
            raise PluginError(f"依赖锁文件不存在或不安全：{plugin.manifest.requirements_file}")
        if path.stat().st_size > 256_000:
            raise PluginError("依赖锁文件超过 256 KB")
        return path

    @staticmethod
    def _validate_requirements_lock(path: Path) -> str:
        content = path.read_text(encoding="utf-8")
        logical = content.replace("\\\n", " ").splitlines()
        packages = 0
        for raw in logical:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if (
                line.startswith(("-", ".", "/"))
                or " @ " in line
                or "://" in line
                or "\\" in line
            ):
                raise PluginError("依赖锁文件只允许固定版本的 PyPI wheel，不能使用 URL、路径或 pip 选项")
            tokens = line.split()
            hash_tokens = [item for item in tokens if item.startswith("--hash=")]
            if any(
                item.startswith("-") and item not in hash_tokens
                for item in tokens
            ):
                raise PluginError("依赖锁文件只允许 --hash 参数")
            if not hash_tokens or any(
                not re.fullmatch(r"--hash=sha256:[0-9a-fA-F]{64}", item)
                for item in hash_tokens
            ):
                raise PluginError("每个 Python 依赖必须使用 == 固定版本并提供 --hash=sha256")
            requirement = " ".join(item for item in tokens if item not in hash_tokens)
            if not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9_,.-]+\])?"
                r"==[^\s;/]+(?:\s*;\s*.+)?",
                requirement,
            ):
                raise PluginError("每个 Python 依赖必须是固定版本的 PyPI 包")
            packages += 1
        if not packages:
            raise PluginError("依赖锁文件没有可安装的软件包")
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _runtime_dir(self, plugin_id: str) -> Path:
        return self.runtime_root / plugin_id

    @staticmethod
    def _venv_python(runtime: Path) -> Path:
        return runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def dependency_status(self, plugin_id: str) -> dict[str, Any]:
        plugin = self.get(plugin_id)
        problems: list[str] = []
        missing_executables = [
            item for item in plugin.manifest.required_executables if shutil.which(item) is None
        ]
        if missing_executables:
            problems.append(f"主机缺少命令：{', '.join(missing_executables)}")
        requirements_hash = None
        requirements_error = None
        try:
            path = self._requirements_path(plugin)
            if path:
                requirements_hash = self._validate_requirements_lock(path)
        except (OSError, UnicodeError, PluginError) as exc:
            requirements_error = str(exc)
            problems.append(requirements_error)
        runtime = self._runtime_dir(plugin_id)
        if runtime.is_symlink():
            problems.append("插件独立运行时不能是符号链接")
        marker_path = runtime / ".quantdesk-runtime.json"
        marker: dict[str, Any] = {}
        if marker_path.is_file():
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                marker = {}
        needs_install = bool(requirements_hash) and (
            marker.get("requirementsSha256") != requirements_hash
            or not self._venv_python(runtime).is_file()
        )
        if needs_install:
            problems.append("Python 依赖未安装或锁文件已变化")
        sandbox = sandbox_status()
        if sandbox.policy == "required" and not sandbox.available:
            problems.append(sandbox.detail)
        return {
            "declared": bool(plugin.manifest.requirements_file or plugin.manifest.required_executables),
            "requirementsFile": plugin.manifest.requirements_file or None,
            "requirementsSha256": requirements_hash,
            "installedSha256": marker.get("requirementsSha256"),
            "runtimePath": str(runtime),
            "missingExecutables": missing_executables,
            "needsInstall": needs_install,
            "ready": not problems,
            "problems": problems,
            "sandbox": sandbox.as_dict(),
        }

    def install_dependencies(self, plugin_id: str) -> dict[str, Any]:
        plugin = self.get(plugin_id)
        if plugin.enabled:
            raise PluginError("安装依赖前必须先停用插件")
        missing = [item for item in plugin.manifest.required_executables if shutil.which(item) is None]
        if missing:
            raise PluginError(f"缺少系统命令：{', '.join(missing)}；QuantDesk 不会自动安装系统软件")
        requirements = self._requirements_path(plugin)
        if requirements is None:
            return self.dependency_status(plugin_id)
        digest = self._validate_requirements_lock(requirements)
        if self.runtime_root.is_symlink():
            raise PluginError("插件运行时目录不能是符号链接")
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        staging = self.runtime_root / f".staging-{plugin_id}-{uuid.uuid4().hex}"
        backup = self.runtime_root / f".backup-{plugin_id}-{uuid.uuid4().hex}"
        target = self._runtime_dir(plugin_id)
        try:
            venv.EnvBuilder(with_pip=True, clear=True).create(staging)
            python = self._venv_python(staging)
            env = {
                "PATH": os.environ.get("PATH", ""),
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                "HOME": str(self.data_root / plugin_id),
                "PIP_CONFIG_FILE": os.devnull,
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_NO_INPUT": "1",
            }
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
                if os.environ.get(key):
                    env[key] = os.environ[key]
            result = subprocess.run(
                [
                    str(python), "-m", "pip", "install", "--require-hashes",
                    "--only-binary=:all:", "--no-input", "-r", str(requirements),
                ],
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
                env=env,
            )
            if result.returncode:
                detail = (result.stderr or result.stdout).strip()[-3000:]
                raise PluginError(f"插件依赖安装失败：{detail}")
            (staging / ".quantdesk-runtime.json").write_text(
                json.dumps(
                    {
                        "pluginId": plugin_id,
                        "pluginVersion": plugin.manifest.version,
                        "requirementsSha256": digest,
                        "installedAt": int(time.time() * 1000),
                    },
                    ensure_ascii=False,
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            if target.exists():
                os.replace(target, backup)
            os.replace(staging, target)
            shutil.rmtree(backup, ignore_errors=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if backup.exists() and not target.exists():
                os.replace(backup, target)
            raise PluginError(f"无法建立插件独立运行时：{exc}") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return self.dependency_status(plugin_id)

    def describe(self, plugin: PluginRecord) -> dict[str, Any]:
        return {
            **plugin.as_dict(),
            "dependencies": self.dependency_status(plugin.manifest.id),
            "sandbox": sandbox_status().as_dict(),
        }

    @staticmethod
    def validate_github_source(source: str) -> tuple[str, str]:
        match = _GITHUB_RE.fullmatch(source.strip())
        if not match:
            raise PluginError("第一版只允许 https://github.com/OWNER/REPO 形式的远程仓库")
        return match.group("owner"), match.group("repo")

    @staticmethod
    def _reject_symlinks(path: Path) -> None:
        for item in path.rglob("*"):
            if item.is_symlink():
                raise PluginError(f"插件仓库包含符号链接，已拒绝安装：{item.relative_to(path)}")

    def _stage_source(self, source: str, ref: str | None, staging: Path) -> dict[str, Any]:
        source_path = Path(source).expanduser()
        if source_path.is_dir():
            shutil.copytree(source_path, staging, symlinks=True)
            return {"kind": "local", "source": str(source_path.resolve()), "ref": ref or ""}
        self.validate_github_source(source)
        command = ["git", "clone", "--filter=blob:none", "--depth", "1"]
        if ref:
            if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", ref):
                raise PluginError("Git ref 含有不支持的字符")
            command.extend(["--branch", ref])
        command.extend([source, str(staging)])
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
        except FileNotFoundError as exc:
            raise PluginError("服务器没有安装 git，无法拉取远程插件") from exc
        except subprocess.TimeoutExpired as exc:
            raise PluginError("拉取插件仓库超时") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-1000:]
            raise PluginError(f"git clone 失败：{detail}")
        commit_result = subprocess.run(
            ["git", "-C", str(staging), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        commit = commit_result.stdout.strip() if commit_result.returncode == 0 else ""
        return {"kind": "github", "source": source, "ref": ref or "", "commit": commit}

    @staticmethod
    def _write_origin(staging: Path, origin: dict[str, Any]) -> None:
        (staging / ".quantdesk-source.json").write_text(
            json.dumps(origin, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def install(self, source: str, ref: str | None = None) -> PluginRecord:
        """Copy a local adapter or clone a GitHub repository without executing it."""
        self.install_root.mkdir(parents=True, exist_ok=True)
        staging = self.install_root / f".staging-{uuid.uuid4().hex}"
        try:
            origin = self._stage_source(source, ref, staging)
            self._reject_symlinks(staging)
            manifest = load_manifest(staging)
            target = self.install_root / manifest.id
            if target.exists():
                raise PluginError(f"插件 {manifest.id} 已安装；请使用更新操作")
            self._write_origin(staging, origin)
            os.replace(staging, target)
            return PluginRecord(manifest=manifest, path=target, enabled=False, origin=origin)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def update(self, plugin_id: str, ref: str | None = None) -> dict[str, Any]:
        """Stage and atomically replace an installed plugin; updated code stays disabled."""
        current = self.get(plugin_id)
        if current.path.parent.resolve() != self.install_root.resolve():
            raise PluginError("QUANTDESK_PLUGIN_PATH 暴露的外部插件不能由 QuantDesk 更新")
        source = str(current.origin.get("source") or "")
        kind = current.origin.get("kind")
        if kind not in {"github", "local"} or not source:
            raise PluginError("插件没有可更新的来源记录")
        selected_ref = ref if ref is not None else str(current.origin.get("ref") or "") or None
        staging = self.install_root / f".staging-{uuid.uuid4().hex}"
        backup = self.install_root / f".backup-{plugin_id}-{uuid.uuid4().hex}"
        old_version = current.manifest.version
        old_commit = current.origin.get("commit")
        previous_state = self._state()
        replaced = False
        try:
            origin = self._stage_source(source, selected_ref, staging)
            self._reject_symlinks(staging)
            manifest = load_manifest(staging)
            if manifest.id != plugin_id:
                raise PluginError(f"更新仓库的 plugin.id 为 {manifest.id}，与已安装的 {plugin_id} 不一致")
            self._write_origin(staging, origin)
            state = dict(previous_state)
            state[plugin_id] = False
            self._write_state(state)
            os.replace(current.path, backup)
            os.replace(staging, current.path)
            replaced = True
            shutil.rmtree(backup, ignore_errors=True)
        except PluginError:
            if backup.exists():
                if current.path.exists():
                    shutil.rmtree(current.path, ignore_errors=True)
                os.replace(backup, current.path)
            self._write_state(previous_state)
            raise
        except OSError as exc:
            if backup.exists():
                if current.path.exists():
                    shutil.rmtree(current.path, ignore_errors=True)
                os.replace(backup, current.path)
            self._write_state(previous_state)
            raise PluginError(f"插件更新失败，已恢复原版本：{exc}") from exc
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if backup.exists():
                if not replaced and not current.path.exists():
                    os.replace(backup, current.path)
                else:
                    shutil.rmtree(backup, ignore_errors=True)
        updated = self.get(plugin_id)
        return {
            "plugin": self.describe(updated),
            "previousVersion": old_version,
            "previousCommit": old_commit,
            "changed": old_version != updated.manifest.version or old_commit != updated.origin.get("commit"),
            "reviewRequired": True,
        }

    def uninstall(self, plugin_id: str, *, purge_data: bool = False) -> dict[str, Any]:
        plugin = self.get(plugin_id)
        if plugin.path.parent.resolve() != self.install_root.resolve():
            raise PluginError("QUANTDESK_PLUGIN_PATH 暴露的外部插件不能由 QuantDesk 卸载")
        if plugin.enabled:
            raise PluginError("卸载前必须先停用插件")
        removed = self.install_root / f".removed-{plugin_id}-{uuid.uuid4().hex}"
        try:
            os.replace(plugin.path, removed)
        except OSError as exc:
            raise PluginError(f"无法卸载插件：{exc}") from exc
        state = self._state()
        state.pop(plugin_id, None)
        try:
            self._write_state(state)
        except PluginError:
            os.replace(removed, plugin.path)
            raise
        cleanup_pending = False
        try:
            shutil.rmtree(removed)
        except OSError:
            # The directory name is hidden from discovery, so the plugin is
            # already uninstalled. A later cleanup can safely remove it.
            cleanup_pending = True
        shutil.rmtree(self._runtime_dir(plugin_id), ignore_errors=True)
        data_removed = False
        if purge_data:
            shutil.rmtree(self.data_root / plugin_id, ignore_errors=True)
            data_removed = True
        return {
            "id": plugin_id,
            "removed": True,
            "dataRemoved": data_removed,
            "cleanupPending": cleanup_pending,
        }

    def _command(self, plugin: PluginRecord) -> list[str]:
        command = list(plugin.manifest.command)
        if command[0] in {"python", "python3"}:
            runtime = self._runtime_dir(plugin.manifest.id)
            command[0] = str(self._venv_python(runtime)) if plugin.manifest.requirements_file else sys.executable
        elif "/" in command[0] or command[0].startswith("."):
            executable = (plugin.path / command[0]).resolve()
            try:
                executable.relative_to(plugin.path)
            except ValueError as exc:
                raise PluginError("插件命令不能指向插件目录之外") from exc
            command[0] = str(executable)
        return command

    def invoke(
        self,
        plugin_id: str,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        capability: str | None = None,
        allow_disabled: bool = False,
    ) -> dict[str, Any]:
        plugin = self.get(plugin_id)
        if not plugin.enabled and not allow_disabled:
            raise PluginError(f"插件 {plugin_id} 尚未启用")
        if capability and capability not in plugin.manifest.capabilities:
            raise PluginError(f"插件 {plugin_id} 未声明能力 {capability}")
        if not _METHOD_RE.fullmatch(method):
            raise PluginError("插件方法名不合法")

        dependencies = self.dependency_status(plugin_id)
        if not dependencies["ready"]:
            raise PluginError(f"插件依赖或沙箱未就绪：{'; '.join(dependencies['problems'])}")

        # The API server and CLI can invoke plugins before any LLM endpoint has
        # loaded keys.env. Make credential access deterministic for cron/jobs.
        load_runtime_secrets(self.home)
        missing_env = [name for name in plugin.manifest.required_env if not os.environ.get(name)]
        if missing_env:
            raise PluginError(f"插件缺少环境变量：{', '.join(missing_env)}")
        self.data_root.mkdir(parents=True, exist_ok=True)
        if self.data_root.is_symlink():
            raise PluginError("插件数据目录不能是符号链接")
        plugin_home = self.data_root / plugin_id
        if plugin_home.is_symlink():
            raise PluginError("插件私有数据目录不能是符号链接")
        plugin_home.mkdir(parents=True, exist_ok=True)
        os.chmod(plugin_home, 0o700)
        plugin_tmp = plugin_home / "tmp"
        if plugin_tmp.is_symlink():
            raise PluginError("插件临时目录不能是符号链接")
        plugin_tmp.mkdir(exist_ok=True)
        env = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "HOME": str(plugin_home),
            "TMPDIR": str(plugin_tmp),
            "PYTHONDONTWRITEBYTECODE": "1",
            "QUANTDESK_PLUGIN_ID": plugin_id,
            "QUANTDESK_PLUGIN_DATA": str(plugin_home),
        }
        for name in (*plugin.manifest.required_env, *plugin.manifest.optional_env):
            # Optional variables are passed through when they exist and simply
            # absent when they do not: the adapter reports per-provider readiness
            # rather than refusing to start.
            if os.environ.get(name):
                env[name] = os.environ[name]

        request_id = uuid.uuid4().hex
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
            "context": {
                "plugin_id": plugin_id,
                # The version this plugin declared, so a v1 adapter keeps getting a
                # v1 context, and a v3 adapter can tell which engine it is behind.
                "api_version": plugin.manifest.api_version,
                "engine_api_version": PLUGIN_API_VERSION,
                "supported_api_versions": list(PLUGIN_API_VERSIONS),
            },
        }
        started = time.monotonic()
        runtime = self._runtime_dir(plugin_id) if plugin.manifest.requirements_file else None
        try:
            command, isolation = sandbox_wrap(
                self._command(plugin),
                plugin_path=plugin.path,
                data_path=plugin_home,
                quantdesk_home=self.home,
                runtime_path=runtime,
                network=plugin.manifest.network,
            )
        except RuntimeError as exc:
            raise PluginError(str(exc)) from exc
        try:
            result = subprocess.run(
                command,
                input=json.dumps(request, ensure_ascii=False) + "\n",
                capture_output=True,
                text=True,
                cwd=plugin.path,
                env=env,
                timeout=plugin.manifest.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise PluginError(f"插件启动命令不存在：{plugin.manifest.command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise PluginError(f"插件执行超过 {plugin.manifest.timeout_seconds:g} 秒") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-2000:]
            raise PluginError(f"插件退出码 {result.returncode}：{detail or '没有错误输出'}")
        if len(result.stdout.encode("utf-8")) > 1_000_000:
            raise PluginError("插件输出超过 1 MB 限制")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            raise PluginError("插件必须在 stdout 输出且只输出一行 JSON-RPC 响应")
        try:
            response = json.loads(lines[0])
        except ValueError as exc:
            raise PluginError("插件返回的不是有效 JSON") from exc
        if not isinstance(response, dict) or response.get("jsonrpc") != "2.0" or response.get("id") != request_id:
            raise PluginError("插件返回的 JSON-RPC 版本或请求 ID 不匹配")
        if response.get("error"):
            error = response["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise PluginError(f"插件返回错误：{message}")
        payload = response.get("result")
        if not isinstance(payload, dict):
            raise PluginError("插件 result 必须是 JSON 对象")
        return {
            "pluginId": plugin_id,
            "method": method,
            "latencyMs": round((time.monotonic() - started) * 1000, 2),
            "sandbox": isolation.as_dict(),
            "result": payload,
        }

    def health(self, plugin_id: str) -> dict[str, Any]:
        return self.invoke(plugin_id, "health", {}, allow_disabled=True)
