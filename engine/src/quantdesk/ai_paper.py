"""Model-directed paper trading with a deterministic local risk gate.

The model can inspect an allow-listed evidence bundle and return one structured
paper action.  It never receives exchange credentials and it cannot call an
execution route: all actions terminate in :class:`PaperEngine` backed by an
isolated SQLite file.
"""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .backtest import DEFAULT_SLIPPAGE_BPS
from .config.instruments import INTERVAL_MS, VENUE_SYMBOLS, require_instrument
from .config.settings import load_app_config, load_llm_settings
from .datahub.db import Database
from .features.indicators import add_indicators
from .features.resonance import resonance, stance_for_interval
from .llm import ChatMessage, DriverConfig, build_driver, credential_issue, resolve_api_key
from .paper import PaperConfig, PaperEngine, PaperError
from .paper.engine import DEFAULT_MAINTENANCE_MARGIN_RATE
from .risk import RiskBook
from .strategy import cpa
from .studies import instrument_meta, paper_defaults

PROFILE_ID = "default"
ROLE = "ai_paper_trader"
ALLOWED_ACTIONS = {"hold", "open", "close"}
ALLOWED_HORIZONS = {"short", "swing"}
ALLOWED_STYLES = {"conservative", "aggressive", "gambler"}
MARK_MAX_AGE_MS = 120_000
EVALUATION_LEASE_MS = 15 * 60 * 1000
GATE_VIP0_TAKER_FEE_BPS = 5.0
GATE_FEE_EFFECTIVE_DATE = "2026-09-01"


@dataclass(frozen=True)
class StylePolicy:
    name: str
    label: str
    max_leverage: float
    risk_fraction: float
    max_notional_fraction: float
    max_open_positions: int
    min_confidence: float
    temperature: float


STYLE_POLICIES = {
    "conservative": StylePolicy("conservative", "稳妥", 3, 0.005, 0.25, 2, 0.72, 0.10),
    "aggressive": StylePolicy("aggressive", "激进", 10, 0.015, 0.60, 4, 0.58, 0.25),
    "gambler": StylePolicy("gambler", "赌徒", 200, 0.040, 1.00, 6, 0.45, 0.45),
}

HORIZON_FRAMES = {
    "short": ("15m", "1h", "4h"),
    "swing": ("4h", "1d"),
}


class AiPaperError(RuntimeError):
    """A configuration, evidence, or risk-gate refusal."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _finite_number(
    value: Any,
    label: str,
    *,
    default: float | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if value is None or value == "":
        if default is None:
            raise AiPaperError(f"{label} 必须是有限数字")
        number = float(default)
    else:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise AiPaperError(f"{label} 必须是有限数字") from exc
    if not math.isfinite(number):
        raise AiPaperError(f"{label} 必须是有限数字")
    if minimum is not None and number < minimum:
        raise AiPaperError(f"{label} 不能小于 {minimum:g}")
    if maximum is not None and number > maximum:
        raise AiPaperError(f"{label} 不能大于 {maximum:g}")
    return number


def _optional_finite_number(value: Any, label: str) -> float | None:
    if value is None or value == "":
        return None
    return _finite_number(value, label)


def _minimum_executable_notional(
    mark: float,
    qty_step: float,
    min_order_notional: float,
    *,
    slippage_bps: float = 0.0,
) -> float:
    """Smallest requested notional that survives venue quantity rounding.

    The paper engine converts requested notional to quantity and floors it to
    ``qty_step`` before applying simulated slippage.  A plain min-notional check
    therefore is not enough for high-priced contracts such as ETH: 5 USDT can
    pass the venue minimum while still buying less than one 0.01-ETH step.
    Use the lower possible simulated fill so the returned amount works for
    either long or short market fills.
    """
    mark = float(mark)
    qty_step = float(qty_step)
    min_order_notional = max(0.0, float(min_order_notional))
    if not math.isfinite(mark) or mark <= 0 or not math.isfinite(qty_step) or qty_step <= 0:
        return float("inf")
    conservative_fill = mark * max(1e-9, 1 - abs(float(slippage_bps)) / 10_000)
    steps = max(1, math.ceil(min_order_notional / (conservative_fill * qty_step) - 1e-12))
    return steps * qty_step * mark


def _symbol_set(value: Any) -> set[str]:
    """A symbol collection as a set, from JSON text or a list."""
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except json.JSONDecodeError:
            value = []
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(item) for item in value}


def _config_changed(current: dict, updates: dict) -> bool:
    """Whether a validated update actually changes the stored rules.

    The page sends the whole draft on every save, so comparing first keeps
    `config_revision` meaning "the rules changed" rather than "somebody pressed save".
    Symbols compare as sets: the stored order is not part of the rule set.
    """
    for key, value in updates.items():
        if key in ("updated_ts", "config_revision"):
            continue
        if key == "symbols_json":
            if _symbol_set(current.get("symbols_json")) != _symbol_set(value):
                return True
            continue
        if key == "fib_only":
            if bool(current.get(key)) != bool(value):
                return True
            continue
        if key in ("initial_cash", "max_leverage"):
            try:
                if float(current.get(key) or 0) != float(value):
                    return True
            except (TypeError, ValueError):
                return True
            continue
        if str(current.get(key)) != str(value):
            return True
    return False


def parse_decision(text: str) -> dict:
    """Parse a provider response without accepting prose around the JSON object."""
    cleaned = (text or "").strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.I | re.S)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        value = json.loads(
            cleaned,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"不允许的非有限数字 {token}")
            ),
        )
    except (json.JSONDecodeError, ValueError) as exc:
        detail = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
        raise AiPaperError(f"模型没有返回有效 JSON：{detail}") from exc
    if not isinstance(value, dict):
        raise AiPaperError("模型输出必须是一个 JSON 对象")
    action = str(value.get("action") or "").lower()
    if action not in ALLOWED_ACTIONS:
        raise AiPaperError("模型 action 只能是 hold、open 或 close")
    value["action"] = action
    return value


class AiPaperService:
    """Own one isolated AI paper account and its model-independent memory."""

    def __init__(
        self, home: Path | str, *, profile_id: str = PROFILE_ID,
        create_if_missing: bool = True, profile_name: str | None = None,
    ):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", profile_id):
            raise AiPaperError("AI 模拟实例 ID 不合法")
        self.profile_id = profile_id
        self.db = Database(self.home / "quantdesk.db")
        if create_if_missing:
            self._ensure_profile(profile_name)
        elif not self.db.query("SELECT 1 FROM ai_paper_profiles WHERE id=?", (self.profile_id,)):
            self.db.close()
            raise AiPaperError("AI 模拟实例不存在")

    def close(self) -> None:
        self.db.close()

    @property
    def account_path(self) -> Path:
        return self.home / f"ai-paper-{self.profile_id}.db"

    @property
    def memory_path(self) -> Path:
        return self.home / "ai-paper-memory" / f"{self.profile_id}.md"

    def _ensure_profile(self, profile_name: str | None = None) -> None:
        now = _now_ms()
        name = (profile_name or ("主模拟" if self.profile_id == PROFILE_ID else "新模拟")).strip()
        if not name or len(name) > 40:
            raise AiPaperError("模拟名称必须为 1–40 个字符")
        self.db.execute(
            "INSERT OR IGNORE INTO ai_paper_profiles "
            "(id,name,enabled,initial_cash,max_leverage,horizon,style,symbols_json,model_role,created_ts,updated_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (self.profile_id, name, 0, 100_000.0, 3.0, "short", "conservative", _json(list(VENUE_SYMBOLS)), ROLE, now, now),
        )
        if not self.memory_path.exists():
            self.write_memory()

    def profile(self) -> dict:
        rows = self.db.query("SELECT * FROM ai_paper_profiles WHERE id=?", (self.profile_id,))
        if not rows:
            raise AiPaperError("AI 模拟账户配置不存在")
        row = rows[0]
        try:
            symbols = json.loads(row.get("symbols_json") or "[]")
        except json.JSONDecodeError:
            symbols = []
        row["symbols"] = [symbol for symbol in symbols if symbol in VENUE_SYMBOLS]
        row["enabled"] = bool(row["enabled"])
        row["fib_only"] = bool(row.get("fib_only"))
        return row

    def update_profile(self, **updates: Any) -> dict:
        return self._update_profile(bump_revision=True, **updates)

    def _update_profile(self, *, bump_revision: bool, **updates: Any) -> dict:
        current = self.profile()
        allowed = {"name", "initial_cash", "max_leverage", "horizon", "style", "fib_only", "symbols"}
        unknown = set(updates) - allowed
        if unknown:
            raise AiPaperError(f"未知配置字段：{', '.join(sorted(unknown))}")
        if current["enabled"] and (set(updates) - {"name"}):
            raise AiPaperError("运行中的模拟不能修改交易条件；请新建模拟或先停止当前实例")
        if "horizon" in updates and updates["horizon"] not in ALLOWED_HORIZONS:
            raise AiPaperError("交易周期只能是 short 或 swing")
        if "style" in updates and updates["style"] not in ALLOWED_STYLES:
            raise AiPaperError("交易风格只能是 conservative、aggressive 或 gambler")
        if "name" in updates:
            updates["name"] = str(updates["name"]).strip()
            if not updates["name"] or len(updates["name"]) > 40:
                raise AiPaperError("模拟名称必须为 1–40 个字符")
        if "fib_only" in updates:
            updates["fib_only"] = 1 if bool(updates["fib_only"]) else 0
        if "max_leverage" in updates:
            updates["max_leverage"] = _finite_number(
                updates["max_leverage"], "最大杠杆", minimum=1, maximum=200
            )
        if "initial_cash" in updates:
            updates["initial_cash"] = _finite_number(
                updates["initial_cash"], "初始本金", minimum=100, maximum=1_000_000_000
            )
        if "symbols" in updates:
            symbols = list(dict.fromkeys(str(item).upper() for item in updates["symbols"]))
            invalid = [item for item in symbols if item not in VENUE_SYMBOLS]
            if invalid or not symbols:
                raise AiPaperError("标的范围必须是固定合约池中至少一个合约")
            engine, account_db = self._engine(current)
            try:
                open_symbols = {row["symbol"] for row in engine.open_positions()}
            finally:
                account_db.close()
            removed_open = sorted(open_symbols - set(symbols))
            if removed_open:
                raise AiPaperError(
                    f"不能移除仍有 AI 模拟持仓的合约：{', '.join(removed_open)}；请先平仓"
                )
            updates["symbols_json"] = _json(symbols)
            updates.pop("symbols")
        if "initial_cash" in updates and float(updates["initial_cash"]) != float(current["initial_cash"]):
            engine, account_db = self._engine(current)
            try:
                if engine.open_positions() or account_db.journal_entries(limit=1):
                    raise AiPaperError("账户已有交易记录；修改本金前请先停止并重置 AI 模拟账户")
            finally:
                account_db.close()
        updates["updated_ts"] = _now_ms()
        if bump_revision and _config_changed(current, updates):
            # One extra revision per real change, so a decision can be traced back to
            # the exact rule set that produced it. A save that changes nothing - the
            # page sends the whole draft every time - does not inflate the counter.
            updates["config_revision"] = int(current.get("config_revision") or 1) + 1
        columns = ",".join(f"{key}=?" for key in updates)
        self.db.execute(
            f"UPDATE ai_paper_profiles SET {columns} WHERE id=?",
            (*updates.values(), self.profile_id),
        )
        self.write_memory()
        return self.profile()

    @classmethod
    def create(cls, home: Path | str, *, name: str, **config: Any) -> "AiPaperService":
        profile_id = f"sim-{uuid.uuid4().hex[:12]}"
        service = cls(home, profile_id=profile_id, profile_name=name)
        try:
            if config:
                # A brand-new instance is revision 1: that *is* its first rule set,
                # not a change to one.
                service._update_profile(bump_revision=False, **config)
                service.db.execute(
                    "UPDATE ai_paper_profiles SET config_revision=1 WHERE id=?",
                    (profile_id,),
                )
            return service
        except Exception:
            service.delete(force=True)
            service.close()
            raise

    @classmethod
    def summaries(cls, home: Path | str) -> list[dict]:
        bootstrap = cls(home)
        try:
            ids = [row["id"] for row in bootstrap.db.query(
                "SELECT id FROM ai_paper_profiles ORDER BY created_ts,id"
            )]
        finally:
            bootstrap.close()
        result = []
        for profile_id in ids:
            service = cls(home, profile_id=profile_id, create_if_missing=False)
            try:
                snapshot = service.snapshot(decision_limit=1)
                result.append({
                    "profile": snapshot["profile"],
                    "metrics": snapshot["metrics"],
                    "equity": snapshot["account"]["equity"],
                    "openPositions": len(snapshot["account"]["positions"]),
                })
            finally:
                service.close()
        return result

    def delete(self, *, force: bool = False) -> None:
        profile = self.profile()
        if profile["enabled"] and not force:
            raise AiPaperError("请先停止该 AI 模拟实例再删除")
        owner = f"delete-{uuid.uuid4().hex}"
        if not force and not self.db.acquire_ai_paper_lease(
            self.profile_id, owner, now_ms=_now_ms(), ttl_ms=EVALUATION_LEASE_MS
        ):
            raise AiPaperError("该实例仍有一轮评估正在运行，暂时不能删除")
        try:
            engine, account_db = self._engine(profile)
            try:
                if engine.open_positions() and not force:
                    raise AiPaperError("该实例仍有未平仓持仓，不能删除")
            finally:
                account_db.close()
            self.db.execute("DELETE FROM ai_paper_leases WHERE profile_id=?", (self.profile_id,))
            self.db.execute("DELETE FROM ai_paper_decisions WHERE profile_id=?", (self.profile_id,))
            self.db.execute("DELETE FROM ai_paper_profiles WHERE id=?", (self.profile_id,))
            for path in (self.account_path, self.memory_path):
                for suffix in ("", "-wal", "-shm") if path == self.account_path else ("",):
                    candidate = Path(str(path) + suffix)
                    if candidate.exists():
                        candidate.unlink()
        finally:
            if not force:
                self.db.release_ai_paper_lease(self.profile_id, owner)

    def set_enabled(self, enabled: bool) -> dict:
        if enabled:
            profile = self.profile()
            try:
                llm_profile = load_llm_settings(self.home).profile_for(profile["model_role"])
            except KeyError as exc:
                raise AiPaperError("没有可用于 AI 模拟交易的大模型 profile，请先到设置页配置") from exc
            key, candidates = resolve_api_key(llm_profile.provider, llm_profile.api_key_env)
            issue = credential_issue(key)
            if not key or issue:
                env = candidates[0] if candidates else llm_profile.api_key_env
                raise AiPaperError(f"启动前请为模型 profile「{llm_profile.name}」配置 {env}" + (f"（{issue}）" if issue else ""))
        self.db.execute(
            "UPDATE ai_paper_profiles SET enabled=?,last_error=NULL,updated_ts=? WHERE id=?",
            (1 if enabled else 0, _now_ms(), self.profile_id),
        )
        return self.profile()

    def start_with_config(self, **config: Any) -> dict:
        """Save the rules the page is showing, then start - as one logical transaction.

        The bug this closes: the start button used to enable whatever was already in the
        database, so a user who picked "short/aggressive, no SOXL/SOXS" and pressed
        start ran the previous rules instead, with nothing in the logs saying so.

        Order, and why each step is where it is:

        1. the instance must be stopped (``update_profile`` refuses condition changes
           while running, and starting over a running instance would silently swap the
           rules under an open book);
        2. the full configuration is validated by ``update_profile`` - unknown fields,
           enum values, ranges, and the rule that a symbol with an open position cannot
           be dropped;
        3. it is persisted in a single ``UPDATE``, so a rejected configuration cannot
           leave a partial one behind;
        4. only then is ``enabled`` set.

        If validation fails, nothing is written and the instance stays stopped. If the
        configuration saves but the start itself fails (no model credentials, say), the
        saved rules stand and the instance stays stopped - the error says so, because
        "saved but not running" and "not saved" are different things to a user.
        """
        current = self.profile()
        if current["enabled"]:
            raise AiPaperError("该模拟正在运行；请先停止，再修改规则并启动")
        if config:
            self._update_profile(bump_revision=True, **config)
        try:
            self.set_enabled(True)
        except AiPaperError as exc:
            raise AiPaperError(f"规则已保存，但未能启动：{exc}") from exc
        return self.snapshot()

    def _engine(self, profile: dict | None = None) -> tuple[PaperEngine, Database]:
        profile = profile or self.profile()
        account_db = Database(self.account_path)
        app = load_app_config(self.home)
        paper = app.paper or {}
        config = PaperConfig(
            initial_cash=float(profile["initial_cash"]),
            taker_fee_bps=self._ai_taker_fee_bps(paper),
            slippage_bps=float(paper.get("slippage_bps", DEFAULT_SLIPPAGE_BPS)),
            maintenance_margin_rate=float(paper.get("maintenance_margin_rate", DEFAULT_MAINTENANCE_MARGIN_RATE)),
            max_leverage=float(profile["max_leverage"]),
        )
        return PaperEngine(account_db, config, RiskBook(self.db)), account_db

    @staticmethod
    def _ai_taker_fee_bps(paper: dict | None = None) -> float:
        """Return the AI simulator's Gate-benchmarked market-fill fee.

        AI paper orders are immediate simulated fills, so both entry and exit are
        taker executions.  This is intentionally separate from the manual-paper and
        backtest defaults: changing the AI benchmark must not rewrite another
        subsystem's cost assumptions.
        """
        value = (paper or {}).get("ai_paper_taker_fee_bps", GATE_VIP0_TAKER_FEE_BPS)
        try:
            fee = float(value)
        except (TypeError, ValueError) as exc:
            raise AiPaperError("AI 模拟手续费必须是有效的 bps 数值") from exc
        if not math.isfinite(fee) or fee < 0 or fee > 100:
            raise AiPaperError("AI 模拟手续费必须在 0–100 bps 之间")
        return fee

    def _fee_policy(self) -> dict:
        fee = self._ai_taker_fee_bps((load_app_config(self.home).paper or {}))
        gate_standard = math.isclose(fee, GATE_VIP0_TAKER_FEE_BPS, rel_tol=0, abs_tol=1e-9)
        return {
            "benchmark": "Gate",
            "tier": "VIP 0" if gate_standard else "自定义",
            "product": "USDT 永续合约",
            "fillType": "taker",
            "takerFeeBps": fee,
            "feeRatePct": fee / 100,
            "chargedOn": ["open", "close"],
            "formula": f"成交名义价值 × {fee / 100:.4f}%",
            "effectiveDate": GATE_FEE_EFFECTIVE_DATE if gate_standard else None,
        }

    def _execution_costs(self, spec) -> tuple[float, float]:
        slippage, _shared_fee = paper_defaults(spec, home=self.home)
        return slippage, self._ai_taker_fee_bps((load_app_config(self.home).paper or {}))

    def _marks(self, symbols: list[str], *, now: int | None = None) -> dict[str, float]:
        marks: dict[str, float] = {}
        current = _now_ms() if now is None else int(now)
        for symbol in symbols:
            snapshot = self.db.load_market_snapshot("bybit", symbol)
            received = (snapshot or {}).get("received_ts")
            if not received or current - int(received) > MARK_MAX_AGE_MS:
                continue
            raw = (snapshot or {}).get("mark_price") or (snapshot or {}).get("last_price")
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0:
                marks[symbol] = value
        return marks

    def _metrics(self, account: dict, journal: list[dict]) -> dict:
        chronological = sorted(journal, key=lambda row: (row["closed_ts"], row["id"]))
        equity = float(account["initial_cash"])
        peak = equity
        max_drawdown = 0.0
        for row in chronological:
            equity += float(row["net_pnl"])
            peak = max(peak, equity)
            if peak:
                max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)
        wins = sum(1 for row in journal if float(row["net_pnl"]) > 0)
        losses = sum(1 for row in journal if float(row["net_pnl"]) < 0)
        closed = len(journal)
        initial = float(account["initial_cash"])
        return {
            "closedTrades": closed,
            "wins": wins,
            "losses": losses,
            "winRate": round(wins / closed * 100, 2) if closed else 0.0,
            "returnPct": round((float(account["equity"]) - initial) / initial * 100, 4) if initial else 0.0,
            "netPnl": round(float(account["equity"]) - initial, 6),
            "realizedNetPnl": round(sum(float(row["net_pnl"]) for row in journal), 6),
            "maxDrawdownPct": round(max_drawdown, 4),
        }

    def snapshot(self, *, decision_limit: int = 100) -> dict:
        profile = self.profile()
        engine, account_db = self._engine(profile)
        try:
            rows = engine.open_positions()
            marks = self._marks([row["symbol"] for row in rows])
            account = engine.account(marks).as_dict()
            journal = account_db.journal_entries(limit=500)
            decisions = self.db.query(
                "SELECT * FROM ai_paper_decisions WHERE profile_id=? ORDER BY cycle_ts DESC,id DESC LIMIT ?",
                (self.profile_id, int(decision_limit)),
            )
        finally:
            account_db.close()
        for row in decisions:
            try:
                row["evidence"] = json.loads(row.pop("evidence_json") or "{}")
            except json.JSONDecodeError:
                row["evidence"] = {}
        position_conditions: dict[int, dict] = {}
        for stored_row in self.db.query(
            "SELECT position_id,evidence_json FROM ai_paper_decisions "
            "WHERE profile_id=? AND action='open' AND position_id IS NOT NULL ORDER BY id DESC",
            (self.profile_id,),
        ):
            position_id = int(stored_row["position_id"])
            if position_id in position_conditions:
                continue
            try:
                stored_evidence = json.loads(stored_row.get("evidence_json") or "{}")
            except json.JSONDecodeError:
                stored_evidence = {}
            position_conditions[position_id] = stored_evidence.get("simulation") or {}
        current_conditions = self._condition_snapshot(profile)
        for position in account.get("positions") or []:
            position["simulation"] = position_conditions.get(int(position["id"])) or current_conditions
        memory = self.memory_path.read_text(encoding="utf-8") if self.memory_path.exists() else ""
        return {
            "simulationOnly": True,
            "profile": profile,
            "policy": asdict(STYLE_POLICIES[profile["style"]]),
            "feePolicy": self._fee_policy(),
            "account": account,
            "metrics": self._metrics(account, journal),
            "journal": journal[:200],
            "decisions": decisions,
            "memory": {
                "path": str(self.memory_path),
                "updatedAt": int(self.memory_path.stat().st_mtime * 1000) if self.memory_path.exists() else None,
                "content": memory,
                "modelIndependent": True,
            },
        }

    def _condition_snapshot(self, profile: dict | None = None) -> dict:
        profile = profile or self.profile()
        policy = STYLE_POLICIES[profile["style"]]
        return {
            "id": self.profile_id,
            "profileId": self.profile_id,
            # Which version of the rules this call ran under. Stored on every decision
            # and every position's condition block, so a later edit cannot rewrite the
            # history of what was actually in force.
            "configRevision": int(profile.get("config_revision") or 1),
            "name": profile["name"],
            "style": profile["style"],
            "styleLabel": policy.label,
            "horizon": profile["horizon"],
            "horizonLabel": "短线" if profile["horizon"] == "short" else "中长线",
            "fibOnly": bool(profile["fib_only"]),
            "entryLabel": "Fib 0.618–0.786" if profile["fib_only"] else "多 Agent 综合",
            "maxLeverage": float(profile["max_leverage"]),
            "symbols": list(profile["symbols"]),
        }

    def write_memory(self) -> Path:
        profile = self.profile()
        engine, account_db = self._engine(profile)
        try:
            rows = engine.open_positions()
            account = engine.account(self._marks([row["symbol"] for row in rows])).as_dict()
            journal = account_db.journal_entries(limit=200)
        finally:
            account_db.close()
        metrics = self._metrics(account, journal)
        fee_policy = self._fee_policy()
        decisions = self.db.query(
            "SELECT cycle_ts,action,symbol,side,confidence,reason,lesson_applied,status,error "
            "FROM ai_paper_decisions WHERE profile_id=? ORDER BY cycle_ts DESC,id DESC LIMIT 30",
            (self.profile_id,),
        )
        style = STYLE_POLICIES[profile["style"]]
        lines = [
            "# QuantDesk AI 模拟交易记忆",
            "",
            "> 此文件由 QuantDesk 自动维护，不含 API Key。它与模型供应商无关，切换大模型后仍会作为历史经验提供给新模型。",
            "> 这些记录只用于模拟盘的上下文学习，不会训练或微调模型权重，也不会连接实盘。",
            "",
            "## 当前规则",
            "",
            f"- 模拟实例：{profile['name']}（{self.profile_id}）",
            f"- 模式：{style.label}（{profile['style']}）",
            f"- 周期：{'短线 15m/1h/4h' if profile['horizon'] == 'short' else '中长线 4h/日线'}",
            f"- 入场规则：{'仅斐波那契 0.618–0.786 回调区；Agent 只制定止盈止损' if profile['fib_only'] else 'AI 综合多 Agent 证据并经本地风控'}",
            f"- 初始本金：{float(profile['initial_cash']):,.2f} USDT",
            f"- 用户最大杠杆：{float(profile['max_leverage']):g}x；本模式有效上限：{min(float(profile['max_leverage']), style.max_leverage):g}x",
            f"- 手续费基准：{fee_policy['benchmark']} {fee_policy['tier']} {fee_policy['product']} "
            f"{fee_policy['fillType'].upper()} {fee_policy['feeRatePct']:.4f}%/次；开仓、平仓均收取",
            f"- 标的：{', '.join(profile['symbols'])}",
            "",
            "## 累计表现",
            "",
            f"- 权益：{float(account['equity']):,.2f} USDT",
            f"- 收益率：{metrics['returnPct']:+.4f}%",
            f"- 总盈亏：{metrics['netPnl']:+,.2f} USDT；已实现净盈亏：{metrics['realizedNetPnl']:+,.2f} USDT",
            f"- 累计手续费：{float(account['fees_paid']):,.4f} USDT（已计入净盈亏）",
            f"- 已平仓：{metrics['closedTrades']}；盈利：{metrics['wins']}；亏损：{metrics['losses']}；正确率：{metrics['winRate']:.2f}%",
            f"- 最大已实现回撤：{metrics['maxDrawdownPct']:.4f}%",
            "",
            "## 盈亏记录（最新在前）",
            "",
        ]
        if journal:
            for row in journal[:50]:
                outcome = "盈利" if float(row["net_pnl"]) > 0 else ("亏损" if float(row["net_pnl"]) < 0 else "持平")
                lines.append(
                    f"- {row['closed_ts']} · {row['symbol']} {row['side']} {row['leverage']:g}x · "
                    f"{outcome} {float(row['net_pnl']):+.2f} USDT · 手续费 {float(row['fees']):.4f} USDT · "
                    f"原因：{row.get('rationale') or '未记录'} · 退出：{row['exit_reason']}"
                )
        else:
            lines.append("- 尚无已平仓交易。")
        lines.extend(["", "## 最近决策与采用的经验", ""])
        if decisions:
            for row in decisions:
                note = row.get("error") or row.get("reason") or "无"
                lesson = row.get("lesson_applied") or "未声明"
                lines.append(f"- {row['cycle_ts']} · {row['action']} {row.get('symbol') or ''} · {row['status']} · {note} · 采用经验：{lesson}")
        else:
            lines.append("- 尚无 AI 决策。")
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.memory_path.with_suffix(".md.tmp")
        temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temp.replace(self.memory_path)
        return self.memory_path

    def _closed_bar_ts(self, symbol: str, interval: str, now: int) -> int | None:
        rows = self.db.load_candles("bybit", symbol, interval, limit=3)
        closed = [int(row["ts"]) for row in rows if int(row["ts"]) + INTERVAL_MS[interval] <= now]
        return max(closed) if closed else None

    def _fibonacci_setup(
        self, rows: list[dict], *, mark: float | None, interval: str
    ) -> dict:
        """Build one confirmed swing retracement without using future bars.

        A three-bar confirmation span means an anchor is only published after
        three later bars exist.  Entry is eligible solely while the live mark is
        inside the 0.618–0.786 retracement band.  Exit levels are produced by
        separate structure, volatility, and risk roles so the decision model
        cannot move them in Fibonacci-only mode.
        """
        ordered = sorted(rows, key=lambda row: int(row["ts"]))[-180:]
        if len(ordered) < 40:
            return {"available": False, "eligible": False, "reason": "至少需要 40 根已收盘K线"}
        if mark is None or not math.isfinite(float(mark)) or float(mark) <= 0:
            return {"available": False, "eligible": False, "reason": "缺少新鲜标记价"}

        span = 3
        pivots: list[dict] = []
        for index in range(span, len(ordered) - span):
            window = ordered[index - span:index + span + 1]
            row = ordered[index]
            high = float(row["high"])
            low = float(row["low"])
            if high == max(float(item["high"]) for item in window):
                pivots.append({"kind": "high", "price": high, "ts": int(row["ts"])})
            if low == min(float(item["low"]) for item in window):
                pivots.append({"kind": "low", "price": low, "ts": int(row["ts"])})
        pivots.sort(key=lambda item: (item["ts"], item["kind"]))
        if len(pivots) < 2:
            return {"available": False, "eligible": False, "reason": "没有确认的相反摆动高低点"}

        end = pivots[-1]
        start = next((item for item in reversed(pivots[:-1]) if item["kind"] != end["kind"]), None)
        if start is None:
            return {"available": False, "eligible": False, "reason": "没有确认的相反摆动起点"}
        if end["kind"] == "high" and float(end["price"]) <= float(start["price"]):
            return {"available": False, "eligible": False, "reason": "最近上涨摆动没有形成有效价格区间"}
        if end["kind"] == "low" and float(end["price"]) >= float(start["price"]):
            return {"available": False, "eligible": False, "reason": "最近下跌摆动没有形成有效价格区间"}

        true_ranges = []
        for previous, current in zip(ordered[-16:-1], ordered[-15:]):
            true_ranges.append(max(
                float(current["high"]) - float(current["low"]),
                abs(float(current["high"]) - float(previous["close"])),
                abs(float(current["low"]) - float(previous["close"])),
            ))
        atr = sum(true_ranges) / len(true_ranges) if true_ranges else 0.0
        swing = abs(float(end["price"]) - float(start["price"]))
        if swing < max(float(mark) * 0.003, atr * 2):
            return {"available": False, "eligible": False, "reason": "确认摆动相对波动率过小"}

        bullish = end["kind"] == "high"
        if bullish:
            high, low = float(end["price"]), float(start["price"])
            levels = {str(level): high - swing * level for level in (0.382, 0.5, 0.618, 0.786)}
            zone_low, zone_high = levels["0.786"], levels["0.618"]
            stop = max(low, zone_low - atr * 0.5, float(mark) * 0.70)
            take_1, take_2 = levels["0.382"], high
            side = "long"
        else:
            high, low = float(start["price"]), float(end["price"])
            levels = {str(level): low + swing * level for level in (0.382, 0.5, 0.618, 0.786)}
            zone_low, zone_high = levels["0.618"], levels["0.786"]
            stop = min(high, zone_high + atr * 0.5, float(mark) * 1.30)
            take_1, take_2 = levels["0.382"], low
            side = "short"
        in_zone = zone_low <= float(mark) <= zone_high
        rounded_levels = {key: round(value, 8) for key, value in levels.items()}
        return {
            "available": True,
            "eligible": in_zone,
            "interval": interval,
            "side": side,
            "anchorStart": start,
            "anchorEnd": end,
            "levels": rounded_levels,
            "zoneLow": round(zone_low, 8),
            "zoneHigh": round(zone_high, 8),
            "markPrice": round(float(mark), 8),
            "inEntryZone": in_zone,
            "entryRule": "标记价必须位于斐波那契 0.618–0.786 回调区",
            "exitPlan": {
                "stopLoss": round(stop, 8),
                "takeProfit1": round(take_1, 8),
                "takeProfit2": round(take_2, 8),
                "agents": ["structure_agent", "volatility_agent", "risk_agent"],
                "method": "确认摆动结构 + ATR14 波动缓冲 + 两级结构目标",
            },
        }

    def _symbol_evidence(self, symbol: str, frames: tuple[str, ...], now: int) -> dict | None:
        result: dict[str, Any] = {"symbol": symbol, "timeframes": {}}
        stances = []
        primary_rows: list[dict] = []
        for frame in frames:
            rows = self.db.load_candles("bybit", symbol, frame, limit=240)
            rows = [row for row in rows if int(row["ts"]) + INTERVAL_MS[frame] <= now]
            if frame == frames[0]:
                primary_rows = rows
            if len(rows) < 30:
                continue
            enriched = add_indicators(pd.DataFrame(rows))
            enriched.attrs["interval"] = frame
            last = enriched.iloc[-1]
            def number(name: str) -> float | None:
                raw = last.get(name)
                return None if raw is None or not math.isfinite(float(raw)) else round(float(raw), 6)
            result["timeframes"][frame] = {
                "barTs": int(last["ts"]), "close": number("close"), "ema10": number("ema10"),
                "ema50": number("ema50"), "ema200": number("ema200"), "rsi14": number("rsi14"),
                "macd": number("macd"), "macdSignal": number("macds"), "macdHistogram": number("macdh"),
                "atr14": number("atr14"), "adx14": number("adx14"), "volumeRatio": number("vol_ratio"),
                "returnPct": number("ret_1"),
            }
            try:
                stances.append(stance_for_interval(enriched))
            except Exception:
                pass
        if not result["timeframes"]:
            return None
        if stances:
            result["resonance"] = resonance(stances)
        snapshot = self.db.load_market_snapshot("bybit", symbol) or {}
        received = snapshot.get("received_ts")
        mark_raw = snapshot.get("mark_price") or snapshot.get("last_price")
        mark = None
        try:
            candidate_mark = float(mark_raw)
            if received and now - int(received) <= MARK_MAX_AGE_MS and math.isfinite(candidate_mark) and candidate_mark > 0:
                mark = candidate_mark
        except (TypeError, ValueError):
            pass
        result["derivatives"] = {
            "markPrice": snapshot.get("mark_price"), "fundingRate": snapshot.get("funding_rate"),
            "openInterest": snapshot.get("open_interest"), "openInterestValue": snapshot.get("open_interest_value"),
            "price24hPct": snapshot.get("price_24h_pct"), "receivedTs": snapshot.get("received_ts"),
        }
        result["fibonacci"] = self._fibonacci_setup(primary_rows, mark=mark, interval=frames[0])
        return result

    def _agent_council(self, item: dict, *, fib_only: bool) -> dict:
        """Expose bounded read-only specialist outputs to the decision model."""
        primary = next(iter(item.get("timeframes") or {}), "")
        technical = (item.get("timeframes") or {}).get(primary) or {}
        agents = {
            "technical_agent": {
                "available": bool(technical),
                "role": "趋势、动量与波动结构",
                "output": technical,
            },
            "derivatives_agent": {
                "available": bool(item.get("derivatives")),
                "role": "资金费率、持仓量与标记价风险",
                "output": item.get("derivatives") or {},
            },
            "price_action_agent": {
                "available": bool(item.get("cpa")),
                "role": "Cycle of Price Action 阶段与结构失效",
                "output": item.get("cpa") or {"unavailable": "尚无读数"},
            },
            "tradingagents": {
                "available": bool(item.get("tradingAgents")),
                "role": "多智能体研究、辩论与风险复核",
                "output": item.get("tradingAgents") or {"unavailable": "尚无已完成研判"},
            },
            "backtest_agent": {
                "available": bool(item.get("latestBacktest")),
                "role": "最近规则回测与历史约束",
                "output": item.get("latestBacktest") or {"unavailable": "尚无已完成回测"},
            },
            "factor_agent": {
                "available": bool(item.get("latestFactorRun")),
                "role": "插件因子覆盖与计算结果",
                "output": item.get("latestFactorRun") or {"unavailable": "尚无已完成因子运行"},
            },
        }
        fib = item.get("fibonacci") or {}
        return {
            "protocol": "ai-paper-agent-council/v1",
            "entryAuthority": "fibonacci_0618_0786_only" if fib_only else "ai_multi_agent_synthesis",
            "otherAgentsEntryUseAllowed": not fib_only,
            "otherAgentsExitPlanningOnly": fib_only,
            "exitPlan": fib.get("exitPlan") if fib_only and fib.get("eligible") else None,
            "agents": agents,
        }

    def build_evidence(self, profile: dict | None = None) -> dict:
        profile = profile or self.profile()
        now = _now_ms()
        frames = HORIZON_FRAMES[profile["horizon"]]
        engine, account_db = self._engine(profile)
        try:
            open_rows = engine.open_positions()
            marks = self._marks([row["symbol"] for row in open_rows])
            account = engine.account(marks).as_dict()
        finally:
            account_db.close()
        candidates = []
        for symbol in profile["symbols"]:
            item = self._symbol_evidence(symbol, frames, now)
            if item:
                score = abs(float((item.get("resonance") or {}).get("score") or 0))
                candidates.append((score, item))
        open_symbols = {row["symbol"] for row in open_rows}
        policy = STYLE_POLICIES[profile["style"]]
        current_notional = sum(float(position.get("notional") or 0) for position in account.get("positions") or [])
        if profile["fib_only"]:
            def fib_rank(pair: tuple[float, dict]) -> tuple[int, float]:
                fib = pair[1].get("fibonacci") or {}
                eligible = bool(fib.get("eligible"))
                mark = float(fib.get("markPrice") or 0)
                middle = (float(fib.get("zoneLow") or 0) + float(fib.get("zoneHigh") or 0)) / 2
                distance = abs(mark - middle) / middle if middle else float("inf")
                return (0 if eligible else 1, distance)
            selected = [item for _, item in sorted(candidates, key=fib_rank)[:5]]
        else:
            selected = [item for _, item in sorted(candidates, key=lambda pair: pair[0], reverse=True)[:5]]
        known = {item["symbol"] for item in selected}
        selected.extend(item for _, item in candidates if item["symbol"] in open_symbols and item["symbol"] not in known)
        # Read the other built-in research surfaces through a fixed, read-only
        # capability gateway. A model never receives a database handle or an
        # arbitrary tool name, and missing evidence remains explicitly missing.
        for item in selected:
            symbol = item["symbol"]
            spec = require_instrument(symbol)
            primary_rows = self.db.load_candles("bybit", symbol, frames[0], limit=600)
            primary_rows = [row for row in primary_rows if int(row["ts"]) + INTERVAL_MS[frames[0]] <= now]
            try:
                phase = cpa.analyze(
                    symbol=symbol, display_symbol=spec.display_symbol, interval=frames[0],
                    product_type=spec.product_type, bars=primary_rows,
                )
                phase_payload = phase.as_dict(limit=1)
                item["cpa"] = {
                    "current": phase_payload.get("current"), "insufficient": phase_payload.get("insufficient"),
                    "reason": phase_payload.get("insufficientReason"), "parameterVersion": phase_payload.get("parameterVersion"),
                }
            except Exception as exc:  # one unavailable study must not suppress the other evidence
                item["cpa"] = {"unavailable": str(exc)}
            ta_rows = self.db.query(
                "SELECT trade_date,rating,reports,meta,created_ts FROM tradingagents_runs "
                "WHERE venue_symbol=? AND error IS NULL ORDER BY created_ts DESC LIMIT 1", (symbol,),
            )
            if ta_rows:
                ta = ta_rows[0]
                try:
                    reports = json.loads(ta.get("reports") or "{}")
                except json.JSONDecodeError:
                    reports = {}
                item["tradingAgents"] = {
                    "tradeDate": ta["trade_date"], "rating": ta.get("rating"), "createdTs": ta["created_ts"],
                    "reports": {key: str(value)[:1800] for key, value in reports.items()},
                }
            bt_rows = self.db.query(
                "SELECT id,kind,strategy_id,strategy_version,summary_json,finished_ts FROM backtest_runs "
                "WHERE status='done' AND (symbol=? OR symbol LIKE ?) ORDER BY finished_ts DESC,id DESC LIMIT 1",
                (symbol, f"%{symbol}%"),
            )
            if bt_rows:
                backtest = bt_rows[0]
                try:
                    summary = json.loads(backtest.get("summary_json") or "{}")
                except json.JSONDecodeError:
                    summary = {}
                item["latestBacktest"] = {
                    "runId": backtest["id"], "kind": backtest["kind"], "strategy": backtest["strategy_id"],
                    "strategyVersion": backtest["strategy_version"], "finishedTs": backtest["finished_ts"], "summary": summary,
                }
            factor_rows = self.db.query(
                "SELECT id,summary_json,finished_ts FROM backtest_runs "
                "WHERE status='done' AND kind='factor' AND (symbol=? OR symbol LIKE ?) "
                "ORDER BY finished_ts DESC,id DESC LIMIT 1",
                (symbol, f"%{symbol}%"),
            )
            if factor_rows:
                factor = factor_rows[0]
                try:
                    factor_summary = json.loads(factor.get("summary_json") or "{}")
                except json.JSONDecodeError:
                    factor_summary = {}
                item["latestFactorRun"] = {
                    "runId": factor["id"], "finishedTs": factor["finished_ts"], "summary": factor_summary,
                }
            item["agentCouncil"] = self._agent_council(item, fib_only=profile["fib_only"])
            mark = self._marks([symbol], now=now).get(symbol)
            meta = instrument_meta(self.db, spec)
            slippage, fee = self._execution_costs(spec)
            expected_fee_rate = (1 + slippage / 10_000) * fee / 10_000
            remaining = max(
                0.0,
                (float(account["equity"]) * policy.max_notional_fraction - current_notional)
                / (1 + policy.max_notional_fraction * expected_fee_rate),
            )
            qty_step = float(meta.get("qtyStep") or 0)
            venue_minimum = float(meta.get("minNotionalValue") or 5)
            minimum_executable = (
                _minimum_executable_notional(mark, qty_step, venue_minimum, slippage_bps=slippage)
                if mark else float("inf")
            )
            can_open = True
            reason = "组合敞口与交易所数量步长允许开仓"
            if symbol in open_symbols:
                can_open, reason = False, "该合约已有持仓，禁止重复加仓"
            elif len(open_rows) >= policy.max_open_positions:
                can_open, reason = False, f"{policy.label}模式最多同时持有 {policy.max_open_positions} 个仓位"
            elif not mark or meta.get("status") != "Trading" or qty_step <= 0:
                can_open, reason = False, "缺少新鲜标记价或有效交易所数量步长"
            elif remaining + 1e-9 < minimum_executable:
                can_open = False
                reason = (
                    f"{policy.label}模式组合敞口仅剩 {remaining:.2f} USDT；"
                    f"{symbol} 按数量步长 {qty_step:g} 最少需要约 {minimum_executable:.2f} USDT"
                )
            item["executionConstraint"] = {
                "canOpen": can_open,
                "reason": reason,
                "remainingExposureNotional": round(remaining, 2),
                "minimumExecutableNotional": None if not math.isfinite(minimum_executable) else round(minimum_executable, 2),
                "qtyStep": qty_step or None,
                "venueMinNotional": venue_minimum,
            }
        memory = self.memory_path.read_text(encoding="utf-8")[-14_000:] if self.memory_path.exists() else ""
        return {
            "asOf": now,
            "simulationOnly": True,
            "simulation": self._condition_snapshot(profile),
            "executionCosts": self._fee_policy(),
            "horizon": profile["horizon"],
            "style": profile["style"],
            "fibOnly": profile["fib_only"],
            "account": account,
            "openPositions": open_rows,
            "candidates": selected,
            "persistentMemory": memory,
            "capabilityGateway": {
                "mode": "read-only-analysis-plus-paper-orders",
                "available": [
                    "market.closed_candles", "technical.indicators", "multitimeframe.resonance",
                    "derivatives.funding_and_open_interest", "cpa.current_phase",
                    "tradingagents.latest_completed_run", "backtest.latest_completed_summary",
                    "paper.account_and_positions", "memory.trade_outcomes",
                    "agent_council.read_only_collaboration", "fibonacci.confirmed_swing_retracement",
                ],
                "writeActions": ["paper.open", "paper.close", "paper.hold"],
                "liveTrading": False,
            },
        }

    def _model_decision(self, profile: dict, evidence: dict) -> tuple[dict, str, str, str]:
        settings = load_llm_settings(self.home)
        llm_profile = settings.profile_for(profile["model_role"])
        key, candidates = resolve_api_key(llm_profile.provider, llm_profile.api_key_env)
        issue = credential_issue(key)
        if not key or issue:
            env = candidates[0] if candidates else llm_profile.api_key_env
            raise AiPaperError(f"AI 模拟交易模型未就绪：请为 {llm_profile.name} 配置 {env}" + (f"（{issue}）" if issue else ""))
        driver = build_driver(DriverConfig(
            profile=llm_profile.name, provider=llm_profile.provider, base_url=llm_profile.base_url,
            api_key=key, proxy=llm_profile.proxy or None, supports_json_mode=llm_profile.supports_json_mode,
        ))
        policy = STYLE_POLICIES[profile["style"]]
        fib_instruction = (
            "当前开启斐波那契专用模式：开仓唯一依据是 candidates 中 fibonacci.eligible=true，"
            "且标记价位于确认摆动的 0.618–0.786 回调区。其他 Agent 输出只能用于解释并核对"
            "agentCouncil.exitPlan 的止损止盈，禁止用于改变入场方向或在区间外开仓；"
            "平仓只由本地止盈、止损、强平或人工紧急平仓完成。\n"
            if profile["fib_only"] else
            "请对 agentCouncil 内可用的技术、衍生品、价格行为、TradingAgents、回测和因子角色进行交叉核对。\n"
        )
        system = (
            "你是 QuantDesk 的 AI 模拟交易决策器。只操作模拟账户，绝不声称已下实盘订单。"
            "你只能从证据中的固定合约选择，每轮最多给一个动作。历史记忆是经验上下文，不是保证。"
            "必须尊重止损和本地风控；信息不足时 hold。只输出一个 JSON 对象，不要 Markdown。\n"
            f"风格={policy.label}，周期={profile['horizon']}，最低置信度={policy.min_confidence}。\n"
            "开仓前必须检查候选的 executionConstraint.canOpen；为 false 时禁止选择 open，"
            "应选择 hold 或在确有风险依据时 close，且在理由中说明容量限制。\n"
            + fib_instruction +
            "结构：{\"action\":\"hold|open|close\",\"symbol\":\"VENUE_SYMBOL或null\","
            "\"side\":\"long|short或null\",\"confidence\":0到1,\"leverage\":数字,"
            "\"notional_pct\":权益百分比,\"stop_loss\":价格或null,\"take_profit_1\":价格或null,"
            "\"take_profit_2\":价格或null,\"reason\":\"具体依据\",\"lesson_applied\":\"从历史采用的经验\","
            "\"evidence_used\":[\"实际引用的证据字段\"],\"agents_consulted\":[\"实际采用的Agent\"],"
            "\"agent_conflicts\":[\"Agent之间的分歧；没有则为空数组\"]}"
        )
        completion = driver.complete(
            [ChatMessage("system", system), ChatMessage("user", _json(evidence))],
            model=llm_profile.model_for(profile["model_role"]), temperature=policy.temperature,
            max_tokens=llm_profile.max_tokens or 1600, json_object=True,
        )
        return parse_decision(completion.text), completion.text, llm_profile.name, completion.model

    def _record_decision(
        self, decision: dict, *, evidence: dict, raw: str = "", model_profile: str = "",
        model: str = "", status: str, error: str | None = None, position_id: int | None = None,
        notional: float | None = None,
    ) -> int:
        now = _now_ms()
        def stored_number(value: Any) -> float | None:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None
            return number if math.isfinite(number) else None
        def stored_list(value: Any) -> list[str]:
            if not isinstance(value, list):
                return []
            return [str(item)[:200] for item in value[:30] if str(item).strip()]
        self.db.execute(
            "INSERT INTO ai_paper_decisions "
            "(profile_id,cycle_ts,model_profile,model,action,symbol,side,leverage,notional,stop_loss,take_profit_1,take_profit_2,confidence,reason,lesson_applied,evidence_json,raw_response,status,error,position_id,created_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.profile_id, now, model_profile or None, model or None, decision.get("action", "hold"),
             decision.get("symbol"), decision.get("side"), stored_number(decision.get("leverage")), stored_number(notional),
             stored_number(decision.get("stop_loss")), stored_number(decision.get("take_profit_1")), stored_number(decision.get("take_profit_2")),
             stored_number(decision.get("confidence")), str(decision.get("reason") or ""), str(decision.get("lesson_applied") or ""),
             _json({
                 "asOf": evidence.get("asOf"),
                 "fibOnly": evidence.get("fibOnly", False),
                 "simulation": evidence.get("simulation") or self._condition_snapshot(),
                 "evidenceUsed": stored_list(decision.get("evidence_used")),
                 "agentsConsulted": stored_list(decision.get("agents_consulted")),
                 "agentConflicts": stored_list(decision.get("agent_conflicts")),
             }),
             raw or None, status, error, position_id, now),
        )
        return int(self.db.query("SELECT last_insert_rowid() AS id")[0]["id"])

    def _execute(self, profile: dict, decision: dict, evidence: dict, *, raw: str, model_profile: str, model: str) -> dict:
        action = decision["action"]
        confidence = _finite_number(
            decision.get("confidence"), "置信度", default=0, minimum=0, maximum=1
        )
        decision["confidence"] = confidence
        policy = STYLE_POLICIES[profile["style"]]
        if action == "hold":
            self._record_decision(decision, evidence=evidence, raw=raw, model_profile=model_profile, model=model, status="held")
            return {"status": "held", "decision": decision}
        symbol = str(decision.get("symbol") or "").upper()
        if symbol not in profile["symbols"] or symbol not in VENUE_SYMBOLS:
            raise AiPaperError("模型选择了配置范围之外的标的")
        if profile["fib_only"] and action == "close":
            raise AiPaperError("斐波那契专用模式不接受模型主观平仓，只执行 Agent 制定的止盈止损、强平或人工紧急平仓")
        if action == "open" and not profile["fib_only"] and confidence < policy.min_confidence:
            raise AiPaperError(f"置信度 {confidence:.2f} 低于{policy.label}模式门槛 {policy.min_confidence:.2f}")
        candidates = {
            str(item.get("symbol") or "").upper(): item
            for item in evidence.get("candidates") or []
        }
        if action == "open":
            candidate = candidates.get(symbol)
            if not candidate:
                raise AiPaperError(f"{symbol} 不在本轮证据候选中，拒绝开仓")
            primary = HORIZON_FRAMES[profile["horizon"]][0]
            bar = (candidate.get("timeframes") or {}).get(primary) or {}
            bar_ts = bar.get("barTs")
            as_of = int(evidence.get("asOf") or _now_ms())
            if bar_ts is None or as_of - (int(bar_ts) + INTERVAL_MS[primary]) > 2 * INTERVAL_MS[primary]:
                raise AiPaperError(f"{symbol} 的 {primary} 已收盘 K 线过旧，拒绝开仓")
            if profile["fib_only"]:
                fib = candidate.get("fibonacci") or {}
                if not fib.get("eligible") or not fib.get("inEntryZone"):
                    raise AiPaperError(f"{symbol} 当前不在斐波那契 0.618–0.786 回调区，拒绝开仓")
                expected_side = str(fib.get("side") or "")
                if str(decision.get("side") or "") != expected_side:
                    raise AiPaperError(f"斐波那契确认方向为 {expected_side}，模型方向不一致")
                exit_plan = ((candidate.get("agentCouncil") or {}).get("exitPlan") or {})
                if not exit_plan:
                    raise AiPaperError("其他 Agent 没有形成完整止盈止损方案，拒绝开仓")
                decision["stop_loss"] = exit_plan.get("stopLoss")
                decision["take_profit_1"] = exit_plan.get("takeProfit1")
                decision["take_profit_2"] = exit_plan.get("takeProfit2")
                decision["agents_consulted"] = exit_plan.get("agents") or []
        engine, account_db = self._engine(profile)
        try:
            rows = engine.open_positions()
            marks = self._marks(list({symbol, *(row["symbol"] for row in rows)}))
            mark = marks.get(symbol)
            if not mark:
                raise AiPaperError(f"{symbol} 没有可用标记价，拒绝交易")
            meta = instrument_meta(self.db, require_instrument(symbol))
            if meta.get("status") != "Trading" or not meta.get("qtyStep") or not meta.get("maxLeverage"):
                raise AiPaperError(f"无法确认 {symbol} 的交易状态、数量步长或杠杆上限")
            slippage, fee = self._execution_costs(require_instrument(symbol))
            engine.config.slippage_bps = slippage
            engine.config.taker_fee_bps = fee
            if action == "close":
                matches = [row for row in rows if row["symbol"] == symbol]
                if not matches:
                    raise AiPaperError(f"AI 模拟账户没有 {symbol} 未平仓持仓")
                outcomes = [engine.close_position(int(row["id"]), mark_price=mark, tick_size=meta.get("tickSize"), exit_reason="ai_signal") for row in matches]
                self._record_decision(decision, evidence=evidence, raw=raw, model_profile=model_profile, model=model,
                                      status="executed", position_id=int(matches[0]["id"]))
                return {"status": "executed", "action": "close", "outcomes": outcomes}
            if len(rows) >= policy.max_open_positions:
                raise AiPaperError(f"{policy.label}模式最多同时持有 {policy.max_open_positions} 个仓位")
            if any(row["symbol"] == symbol for row in rows):
                raise AiPaperError(f"{symbol} 已有 AI 模拟持仓，拒绝重复加仓")
            side = str(decision.get("side") or "")
            if side not in {"long", "short"}:
                raise AiPaperError("开仓方向必须是 long 或 short")
            stop = _finite_number(decision.get("stop_loss"), "止损价", minimum=0.00000001)
            if stop <= 0 or (side == "long" and stop >= mark) or (side == "short" and stop <= mark):
                raise AiPaperError("开仓必须提供位于正确方向的有效止损")
            stop_distance = abs(mark - stop) / mark
            if stop_distance < 0.001 or stop_distance > 0.30:
                raise AiPaperError("止损距离必须在价格的 0.1% 到 30% 之间")
            account = engine.account(marks)
            requested_pct = _finite_number(
                decision.get("notional_pct"), "开仓名义额比例", minimum=0.000001, maximum=100
            ) / 100
            risk_notional = account.equity * policy.risk_fraction / stop_distance
            existing_notional = sum(float(position.notional) for position in account.positions)
            # Entry fees immediately reduce equity, so reserve their effect on
            # the portfolio cap instead of appearing slightly over-limit after
            # a successful fill.
            expected_fee_rate = (1 + slippage / 10_000) * fee / 10_000
            exposure_notional = max(
                0.0,
                (account.equity * policy.max_notional_fraction - existing_notional)
                / (1 + policy.max_notional_fraction * expected_fee_rate),
            )
            model_notional = account.equity * requested_pct
            notional = min(risk_notional, exposure_notional, model_notional)
            minimum_notional = float(meta.get("minNotionalValue") or 5)
            qty_step = float(meta.get("qtyStep") or 0)
            minimum_executable = _minimum_executable_notional(
                mark, qty_step, minimum_notional, slippage_bps=slippage
            )
            if notional + 1e-9 < minimum_executable:
                if exposure_notional + 1e-9 < minimum_executable:
                    raise AiPaperError(
                        f"{policy.label}模式组合敞口上限仅剩 {exposure_notional:.2f} USDT；"
                        f"{symbol} 按交易所数量步长 {qty_step:g} 最少需要约 "
                        f"{minimum_executable:.2f} USDT，本轮未下单"
                    )
                raise AiPaperError(
                    f"本轮风险预算仅允许 {notional:.2f} USDT；{symbol} 按交易所数量步长 "
                    f"{qty_step:g} 最少需要约 {minimum_executable:.2f} USDT，本轮未下单"
                )
            venue_max = float(meta["maxLeverage"])
            requested_leverage = _finite_number(
                decision.get("leverage"), "杠杆", default=1, minimum=1
            )
            leverage = min(requested_leverage, float(profile["max_leverage"]), policy.max_leverage, venue_max)
            take_profit_1 = _optional_finite_number(decision.get("take_profit_1"), "第一止盈价")
            take_profit_2 = _optional_finite_number(decision.get("take_profit_2"), "第二止盈价")
            view = engine.open_position(
                symbol, side, notional=notional, leverage=leverage,
                rationale=str(decision.get("reason") or "AI 模拟决策"), mark_price=mark, mark_prices=marks,
                tick_size=meta.get("tickSize"), qty_step=meta.get("qtyStep"),
                min_order_notional=minimum_notional, max_leverage=venue_max,
                stop_loss=stop, take_profit_1=take_profit_1, take_profit_2=take_profit_2,
            )
            self._record_decision(decision, evidence=evidence, raw=raw, model_profile=model_profile, model=model,
                                  status="executed", position_id=view.id, notional=notional)
            return {"status": "executed", "action": "open", "position": asdict(view)}
        finally:
            account_db.close()

    def run_once(
        self, *, force: bool = False,
        decision_provider: Callable[[dict], dict] | None = None,
    ) -> dict:
        owner = uuid.uuid4().hex
        acquired_at = _now_ms()
        if not self.db.acquire_ai_paper_lease(
            self.profile_id, owner, now_ms=acquired_at, ttl_ms=EVALUATION_LEASE_MS
        ):
            return {"status": "busy", "message": "AI 模拟交易已有一轮评估正在运行"}
        try:
            profile = self.profile()
            now = _now_ms()
            primary = HORIZON_FRAMES[profile["horizon"]][0]
            bars = [self._closed_bar_ts(symbol, primary, now) for symbol in profile["symbols"]]
            latest_bar = max((value for value in bars if value is not None), default=None)
            if latest_bar is None:
                raise AiPaperError(f"没有已收盘的 {primary} K 线，无法进行 AI 评估")
            if not force and profile.get("last_bar_ts") and latest_bar <= int(profile["last_bar_ts"]):
                return {"status": "not_due", "lastBarTs": latest_bar}
            evidence = self.build_evidence(profile)
            raw = model_profile = model = ""
            try:
                if profile["fib_only"] and not any(
                    bool((item.get("fibonacci") or {}).get("eligible"))
                    for item in evidence.get("candidates") or []
                ):
                    decision = {
                        "action": "hold",
                        "confidence": 1.0,
                        "reason": "所选合约均未进入确认摆动的斐波那契 0.618–0.786 回调区",
                        "lesson_applied": "区间外不交易",
                        "evidence_used": ["candidates.*.fibonacci.inEntryZone"],
                        "agents_consulted": [],
                        "agent_conflicts": [],
                    }
                    raw, model_profile, model = _json(decision), "local/fibonacci-gate", "deterministic"
                elif decision_provider:
                    decision = parse_decision(_json(decision_provider(evidence)))
                    raw, model_profile, model = _json(decision), "test/injected", "injected"
                else:
                    decision, raw, model_profile, model = self._model_decision(profile, evidence)
                if not force and not self.profile()["enabled"]:
                    decision = {
                        "action": "hold", "confidence": 1.0,
                        "reason": "模型评估期间该模拟已被停止，本轮结果不执行",
                        "lesson_applied": "停止指令优先于待执行决策",
                        "evidence_used": [], "agents_consulted": [], "agent_conflicts": [],
                    }
                    raw, model_profile, model = _json(decision), "local/stop-gate", "deterministic"
                result = self._execute(profile, decision, evidence, raw=raw, model_profile=model_profile, model=model)
                self.db.execute(
                    "UPDATE ai_paper_profiles SET last_cycle_ts=?,last_bar_ts=?,last_error=NULL,updated_ts=? WHERE id=?",
                    (now, latest_bar, now, self.profile_id),
                )
                self.write_memory()
                return result
            except Exception as exc:
                decision = locals().get("decision", {"action": "hold", "reason": "模型调用或风控失败"})
                self._record_decision(decision, evidence=evidence, raw=raw, model_profile=model_profile, model=model,
                                      status="rejected" if decision_provider or raw else "failed", error=str(exc))
                self.db.execute(
                    "UPDATE ai_paper_profiles SET last_cycle_ts=?,last_bar_ts=?,last_error=?,updated_ts=? WHERE id=?",
                    (now, latest_bar, str(exc), now, self.profile_id),
                )
                self.write_memory()
                raise
        finally:
            self.db.release_ai_paper_lease(self.profile_id, owner)

    def reconcile(self) -> list[dict]:
        owner = f"reconcile-{uuid.uuid4().hex}"
        if not self.db.acquire_ai_paper_lease(
            self.profile_id, owner, now_ms=_now_ms(), ttl_ms=EVALUATION_LEASE_MS
        ):
            return []
        profile = self.profile()
        try:
            engine, account_db = self._engine(profile)
            try:
                rows = engine.open_positions()
                if not rows:
                    return []
                symbols = sorted({row["symbol"] for row in rows})
                marks = self._marks(symbols)
                funding = {
                    symbol: self.db.load_funding("bybit", symbol, start_ts=min(int(row["updated_ts"]) for row in rows if row["symbol"] == symbol))
                    for symbol in symbols
                }
                ticks = {symbol: instrument_meta(self.db, require_instrument(symbol)).get("tickSize") for symbol in symbols}
                before = len(account_db.journal_entries(limit=10_000))
                events = engine.reconcile(marks, funding_by_symbol=funding, tick_sizes=ticks)
                after = len(account_db.journal_entries(limit=10_000))
            finally:
                account_db.close()
            if after != before:
                self.write_memory()
            return events
        finally:
            self.db.release_ai_paper_lease(self.profile_id, owner)

    def close_position(self, position_id: int, reason: str = "manual_override") -> dict:
        """Emergency close inside the AI simulation; still writes its journal."""
        owner = f"close-{uuid.uuid4().hex}"
        if not self.db.acquire_ai_paper_lease(
            self.profile_id, owner, now_ms=_now_ms(), ttl_ms=EVALUATION_LEASE_MS
        ):
            raise AiPaperError("该实例正在评估或结算，暂时不能人工平仓")
        profile = self.profile()
        try:
            engine, account_db = self._engine(profile)
            try:
                rows = [row for row in engine.open_positions() if int(row["id"]) == int(position_id)]
                if not rows:
                    raise AiPaperError(f"没有找到 AI 模拟持仓 #{position_id}")
                row = rows[0]
                mark = self._marks([row["symbol"]]).get(row["symbol"])
                if not mark:
                    raise AiPaperError(f"{row['symbol']} 没有可用标记价，无法平仓")
                meta = instrument_meta(self.db, require_instrument(row["symbol"]))
                slippage, fee = self._execution_costs(require_instrument(row["symbol"]))
                engine.config.slippage_bps = slippage
                engine.config.taker_fee_bps = fee
                result = engine.close_position(
                    int(position_id), mark_price=mark, tick_size=meta.get("tickSize"), exit_reason=reason,
                )
            finally:
                account_db.close()
            self.write_memory()
            return result
        finally:
            self.db.release_ai_paper_lease(self.profile_id, owner)

    def reset(self) -> None:
        profile = self.profile()
        if profile["enabled"]:
            raise AiPaperError("请先停止 AI 自动模拟，再重置账户")
        owner = f"reset-{uuid.uuid4().hex}"
        if not self.db.acquire_ai_paper_lease(
            self.profile_id, owner, now_ms=_now_ms(), ttl_ms=EVALUATION_LEASE_MS
        ):
            raise AiPaperError("该实例仍有一轮评估正在运行，暂时不能重置")
        try:
            engine, account_db = self._engine(profile)
            try:
                if engine.open_positions():
                    raise AiPaperError("还有未平仓的 AI 模拟持仓，必须先平仓")
            finally:
                account_db.close()
            for suffix in ("", "-wal", "-shm"):
                path = Path(str(self.account_path) + suffix)
                if path.exists():
                    path.unlink()
            self.db.execute("DELETE FROM ai_paper_decisions WHERE profile_id=?", (self.profile_id,))
            self.db.execute(
                "UPDATE ai_paper_profiles SET last_cycle_ts=NULL,last_bar_ts=NULL,last_error=NULL,updated_ts=? WHERE id=?",
                (_now_ms(), self.profile_id),
            )
            self.write_memory()
        finally:
            self.db.release_ai_paper_lease(self.profile_id, owner)


def run_ai_paper_cycle(home: Path | str) -> dict:
    """Reconcile every isolated instance and evaluate each enabled one when due."""
    bootstrap = AiPaperService(home)
    try:
        profile_ids = [row["id"] for row in bootstrap.db.query(
            "SELECT id FROM ai_paper_profiles ORDER BY created_ts,id"
        )]
    finally:
        bootstrap.close()
    outcomes = []
    for profile_id in profile_ids:
        service = AiPaperService(home, profile_id=profile_id, create_if_missing=False)
        try:
            profile = service.profile()
            events = service.reconcile()
            decision = service.run_once(force=False) if profile["enabled"] else {"status": "stopped"}
            outcomes.append({
                "profileId": profile_id, "name": profile["name"],
                "events": events, "decision": decision, "error": None,
            })
        except Exception as exc:  # noqa: BLE001 - one instance must not stop the others
            outcomes.append({"profileId": profile_id, "events": [], "decision": None, "error": str(exc)})
        finally:
            service.close()
    return {"profiles": outcomes}
