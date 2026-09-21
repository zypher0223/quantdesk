"""Plugin discovery, installation, state, and health endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from ..config.settings import home_writable, quantdesk_home
from ..plugins import (
    PLUGIN_API_VERSION,
    PLUGIN_API_VERSIONS,
    SUPPORTED_CAPABILITIES,
    PluginError,
    PluginManager,
    PluginRegistry,
    plugin_sandbox_status,
)
from ..strategy import StrategyRegistry

router = APIRouter(prefix="/api/plugins", tags=["plugins"])


class InstallPluginRequest(BaseModel):
    source: str = Field(..., min_length=1, max_length=500)
    ref: str | None = Field(None, max_length=200)


class PluginStateRequest(BaseModel):
    enabled: bool


class UpdatePluginRequest(BaseModel):
    ref: str | None = Field(None, max_length=200)


def _manager() -> PluginManager:
    return PluginManager(quantdesk_home())


@router.get("")
def plugins_index():
    manager = _manager()
    try:
        plugins, invalid = manager.discover()
    except PluginError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        # The newest the engine speaks, plus every version it still accepts, so a
        # reader can tell "this engine is v3" from "this engine only does v3".
        "apiVersion": PLUGIN_API_VERSION,
        "supportedApiVersions": list(PLUGIN_API_VERSIONS),
        "supportedCapabilities": list(SUPPORTED_CAPABILITIES),
        "pluginRoot": str(manager.install_root),
        "homeWritable": home_writable(manager.home),
        "sandbox": plugin_sandbox_status().as_dict(),
        "plugins": [manager.describe(plugin) for plugin in plugins],
        "invalid": invalid,
    }


@router.get("/registry")
async def plugin_registry():
    """Resolved enabled contributions used by QuantDesk business services."""
    manager = _manager()
    registry = PluginRegistry(manager)
    strategies, strategy_errors = await run_in_threadpool(StrategyRegistry(manager).catalog)
    return {
        "capabilities": {
            capability: [item.manifest.id for item in registry.enabled(capability)]
            for capability in SUPPORTED_CAPABILITIES
        },
        "strategies": [item for item in strategies if item.get("source") == "plugin"],
        "errors": strategy_errors,
    }


@router.post("/install")
async def install_plugin(request: InstallPluginRequest):
    manager = _manager()
    if not home_writable(manager.home):
        raise HTTPException(409, f"插件目录不可写：{manager.install_root}")
    try:
        # The HTTP surface is remote-facing in self-hosted deployments. Local
        # server paths remain a CLI-only development feature.
        manager.validate_github_source(request.source)
        plugin = await run_in_threadpool(manager.install, request.source, request.ref)
    except PluginError as exc:
        raise HTTPException(409, str(exc)) from exc
    return manager.describe(plugin)


@router.post("/{plugin_id}/update")
async def update_plugin(plugin_id: str, request: UpdatePluginRequest):
    manager = _manager()
    if not home_writable(manager.home):
        raise HTTPException(409, f"插件目录不可写：{manager.install_root}")
    try:
        current = manager.get(plugin_id)
        if current.origin.get("kind") != "github":
            raise PluginError("网页只能更新从 GitHub 安装的插件；本地开发插件请使用 CLI")
        return await run_in_threadpool(manager.update, plugin_id, request.ref)
    except PluginError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.delete("/{plugin_id}")
async def uninstall_plugin(plugin_id: str, purge_data: bool = Query(False, alias="purgeData")):
    try:
        return await run_in_threadpool(_manager().uninstall, plugin_id, purge_data=purge_data)
    except PluginError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/{plugin_id}/dependencies")
def plugin_dependencies(plugin_id: str):
    try:
        return _manager().dependency_status(plugin_id)
    except PluginError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{plugin_id}/dependencies/install")
async def install_plugin_dependencies(plugin_id: str):
    try:
        return await run_in_threadpool(_manager().install_dependencies, plugin_id)
    except PluginError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.put("/{plugin_id}/enabled")
def set_plugin_enabled(plugin_id: str, request: PluginStateRequest):
    try:
        plugin = _manager().set_enabled(plugin_id, request.enabled)
    except PluginError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _manager().describe(plugin)


@router.post("/{plugin_id}/health")
async def plugin_health(plugin_id: str):
    try:
        return await run_in_threadpool(_manager().health, plugin_id)
    except PluginError as exc:
        raise HTTPException(409, str(exc)) from exc
