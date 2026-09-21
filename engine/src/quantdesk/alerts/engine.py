"""Evaluate persistent alert rules against closed data stored in SQLite."""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from datetime import datetime, time as clock_time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

from ..config.instruments import (
    CORE_TIMEFRAMES,
    RESONANCE_MIN_BARS,
    TIMEFRAMES,
    require_instrument,
)
from ..datahub.db import Database
from ..features.indicators import add_indicators
from ..features.resonance import DEFAULT_WEIGHTS, resonance, stance_for_interval
from ..monitoring import DataQualityMonitor
from ..plugins import NotificationEvent, PluginManager, PluginRegistry
from ..strategy.registry import StrategyRegistry


class AlertRuleError(ValueError):
    """A user supplied alert rule is invalid."""


CONDITION_CATALOG: dict[str, dict[str, Any]] = {
    "price_above": {"label": "收盘价高于", "unit": "price", "operator": "above", "timeframe": True},
    "price_below": {"label": "收盘价低于", "unit": "price", "operator": "below", "timeframe": True},
    "price_cross_above": {"label": "收盘价上穿", "unit": "price", "operator": "cross_above", "timeframe": True},
    "price_cross_below": {"label": "收盘价下穿", "unit": "price", "operator": "cross_below", "timeframe": True},
    "funding_above": {"label": "资金费率高于", "unit": "%", "operator": "above", "timeframe": False},
    "funding_below": {"label": "资金费率低于", "unit": "%", "operator": "below", "timeframe": False},
    "oi_change_above": {"label": "24H持仓量增幅高于", "unit": "%", "operator": "above", "timeframe": False},
    "oi_change_below": {"label": "24H持仓量变化低于", "unit": "%", "operator": "below", "timeframe": False},
    "volume_ratio_above": {"label": "成交量倍数高于", "unit": "x", "operator": "above", "timeframe": True},
    "resonance_above": {"label": "多周期共振高于", "unit": "/100", "operator": "above", "timeframe": False},
    "resonance_below": {"label": "多周期共振低于", "unit": "/100", "operator": "below", "timeframe": False},
    "strategy_signal": {"label": "策略产生新信号", "unit": "signal", "operator": "signal", "timeframe": True, "strategy": True},
}

SEVERITIES = {"info", "warning", "critical"}
SIGNAL_DIRECTIONS = {"any", "long", "short"}


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[Path, threading.Lock] = {}


class AlertEngine:
    """Rule registry, evaluator, event journal, and notifier bridge."""

    def __init__(self, home: Path):
        self.home = Path(home)
        self.db = Database(self.home / "quantdesk.db")
        key = self.home.resolve()
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(key, threading.Lock())

    @staticmethod
    def _decode_condition(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "conditionType": row["condition_type"],
            "timeframe": row.get("timeframe"),
            "threshold": row.get("threshold"),
            "strategyId": row.get("strategy_id"),
            "strategyParameters": json.loads(row.get("strategy_parameters") or "{}"),
            "signalDirection": row.get("signal_direction") or "any",
            "lastMetric": row.get("last_metric"),
            "lastObservedAt": row.get("last_observed_ts"),
            "lastMet": bool(row.get("last_met")),
        }

    def _decode_rule(self, row: dict[str, Any]) -> dict[str, Any]:
        conditions = [
            self._decode_condition(item)
            for item in self.db.query(
                "SELECT * FROM alert_rule_conditions WHERE rule_id=? ORDER BY position", (row["id"],)
            )
        ]
        return {
            "id": row["id"],
            "name": row["name"],
            "venueSymbol": row["venue_symbol"],
            "conditionType": row["condition_type"],
            "timeframe": row.get("timeframe"),
            "threshold": row["threshold"],
            "conditions": conditions,
            "cooldownSeconds": row["cooldown_seconds"],
            "enabled": bool(row["enabled"]),
            "severity": row.get("severity") or "warning",
            "quietStart": row.get("quiet_start"),
            "quietEnd": row.get("quiet_end"),
            "timezone": row.get("timezone") or "Asia/Shanghai",
            "dailyLimit": row.get("daily_limit") or 10,
            "confirmationCount": row.get("confirmation_count") or 1,
            "consecutiveCount": row.get("consecutive_count") or 0,
            "hysteresis": row.get("hysteresis") or 0,
            "armed": bool(row.get("armed", 1)),
            "lastCondition": bool(row["last_condition"]),
            "lastMetric": row.get("last_metric"),
            "lastObservedAt": row.get("last_observed_ts"),
            "lastEvaluatedAt": row.get("last_evaluated_ts"),
            "lastTriggeredAt": row.get("last_triggered_ts"),
            "createdAt": row["created_ts"],
            "updatedAt": row["updated_ts"],
        }

    @staticmethod
    def _decode_event(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["notificationResults"] = json.loads(result.pop("notification_results") or "[]")
        for old, new in (
            ("rule_id", "ruleId"), ("venue_symbol", "venueSymbol"),
            ("condition_type", "conditionType"), ("observed_ts", "observedAt"),
            ("triggered_ts", "triggeredAt"),
        ):
            result[new] = result.pop(old)
        return result

    def list_rules(self) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM alert_rules ORDER BY created_ts DESC")
        return [self._decode_rule(row) for row in rows]

    def list_events(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM alert_events ORDER BY triggered_ts DESC LIMIT ?", (int(limit),)
        )
        return [self._decode_event(row) for row in rows]

    @staticmethod
    def _clock(value: Any) -> str | None:
        if value in (None, ""):
            return None
        text = str(value)
        try:
            clock_time.fromisoformat(text)
        except ValueError as exc:
            raise AlertRuleError("静默时间必须使用 HH:MM") from exc
        return text[:5]

    @classmethod
    def validate(cls, values: dict[str, Any]) -> dict[str, Any]:
        try:
            symbol = require_instrument(str(values["venue_symbol"])).venue_symbol
        except (KeyError, ValueError) as exc:
            raise AlertRuleError(str(exc)) from exc
        raw_conditions = values.get("conditions") or [{
            "condition_type": values.get("condition_type"),
            "timeframe": values.get("timeframe"),
            "threshold": values.get("threshold"),
        }]
        if not 1 <= len(raw_conditions) <= 5:
            raise AlertRuleError("每条规则必须包含 1–5 个条件")
        conditions = []
        for raw in raw_conditions:
            condition_type = str(raw.get("condition_type") or raw.get("conditionType") or "")
            catalog = CONDITION_CATALOG.get(condition_type)
            if not catalog:
                raise AlertRuleError("不支持的告警条件")
            timeframe = raw.get("timeframe")
            if catalog["timeframe"] and timeframe not in TIMEFRAMES:
                raise AlertRuleError("该条件必须选择 15m、1h、4h 或 1d")
            if not catalog["timeframe"]:
                timeframe = None
            if catalog.get("strategy"):
                strategy_id = str(raw.get("strategy_id") or raw.get("strategyId") or "").strip()
                if not strategy_id:
                    raise AlertRuleError("策略信号条件必须选择策略")
                parameters = raw.get("strategy_parameters") or raw.get("strategyParameters") or {}
                if not isinstance(parameters, dict):
                    raise AlertRuleError("策略参数必须是 JSON 对象")
                direction = str(raw.get("signal_direction") or raw.get("signalDirection") or "any")
                if direction not in SIGNAL_DIRECTIONS:
                    raise AlertRuleError("策略信号方向无效")
                threshold = None
            else:
                strategy_id, parameters, direction = None, {}, "any"
                try:
                    threshold = float(raw.get("threshold"))
                except (TypeError, ValueError) as exc:
                    raise AlertRuleError("阈值必须是有限数字") from exc
                if not math.isfinite(threshold):
                    raise AlertRuleError("阈值必须是有限数字")
            conditions.append({
                "condition_type": condition_type,
                "timeframe": timeframe,
                "threshold": threshold,
                "strategy_id": strategy_id,
                "strategy_parameters": parameters,
                "signal_direction": direction,
            })
        cooldown = int(values.get("cooldown_seconds", 3600))
        if not 60 <= cooldown <= 604_800:
            raise AlertRuleError("冷却时间必须在 60 秒到 7 天之间")
        name = str(values.get("name") or "").strip()
        if not name or len(name) > 80:
            raise AlertRuleError("规则名称长度必须为 1–80 个字符")
        severity = str(values.get("severity") or "warning")
        if severity not in SEVERITIES:
            raise AlertRuleError("告警等级无效")
        quiet_start = cls._clock(values.get("quiet_start"))
        quiet_end = cls._clock(values.get("quiet_end"))
        if bool(quiet_start) != bool(quiet_end):
            raise AlertRuleError("静默开始和结束时间必须同时填写")
        timezone_name = str(values.get("timezone") or "Asia/Shanghai")
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise AlertRuleError("时区无效") from exc
        daily_limit = int(values.get("daily_limit", 10))
        confirmation_count = int(values.get("confirmation_count", 1))
        hysteresis = float(values.get("hysteresis", 0))
        if not 1 <= daily_limit <= 1000:
            raise AlertRuleError("每日上限必须在 1–1000 之间")
        if not 1 <= confirmation_count <= 10:
            raise AlertRuleError("连续确认次数必须在 1–10 之间")
        if confirmation_count > 1 and any(
            CONDITION_CATALOG[item["condition_type"]]["operator"] in {"cross_above", "cross_below", "signal"}
            for item in conditions
        ):
            raise AlertRuleError("上穿、下穿或策略新信号只能使用 1 次确认")
        if not math.isfinite(hysteresis) or hysteresis < 0:
            raise AlertRuleError("迟滞值必须是非负有限数字")
        return {
            "name": name,
            "venue_symbol": symbol,
            "conditions": conditions,
            "cooldown_seconds": cooldown,
            "enabled": bool(values.get("enabled", True)),
            "severity": severity,
            "quiet_start": quiet_start,
            "quiet_end": quiet_end,
            "timezone": timezone_name,
            "daily_limit": daily_limit,
            "confirmation_count": confirmation_count,
            "hysteresis": hysteresis,
        }

    @staticmethod
    def _condition_rows(rule_id: str, conditions: list[dict[str, Any]]) -> list[tuple]:
        return [
            (
                str(uuid.uuid4()), rule_id, position, item["condition_type"], item["timeframe"], item["threshold"],
                item["strategy_id"], json.dumps(item["strategy_parameters"], ensure_ascii=False), item["signal_direction"],
            )
            for position, item in enumerate(conditions)
        ]

    def _validate_strategy_conditions(self, conditions: list[dict[str, Any]]) -> None:
        requested = {item["strategy_id"] for item in conditions if item["condition_type"] == "strategy_signal"}
        if not requested:
            return
        strategies, errors = StrategyRegistry(PluginManager(self.home)).catalog()
        available = {item["id"] for item in strategies}
        missing = sorted(requested - available)
        if missing:
            detail = f"；插件错误：{errors}" if errors else ""
            raise AlertRuleError(f"找不到策略：{', '.join(missing)}{detail}")

    def create_rule(self, values: dict[str, Any]) -> dict[str, Any]:
        rule = self.validate(values)
        self._validate_strategy_conditions(rule["conditions"])
        now = int(time.time() * 1000)
        rule_id = str(uuid.uuid4())
        first = rule["conditions"][0]
        self.db.create_alert_rule(
            parent=(
                rule_id, rule["name"], rule["venue_symbol"], first["condition_type"], first["timeframe"],
                first["threshold"] or 0, rule["cooldown_seconds"], int(rule["enabled"]), rule["severity"],
                rule["quiet_start"], rule["quiet_end"], rule["timezone"], rule["daily_limit"],
                rule["confirmation_count"], rule["hysteresis"], now, now,
            ),
            conditions=self._condition_rows(rule_id, rule["conditions"]),
        )
        return self.get_rule(rule_id)

    def get_rule(self, rule_id: str) -> dict[str, Any]:
        rows = self.db.query("SELECT * FROM alert_rules WHERE id=?", (rule_id,))
        if not rows:
            raise KeyError(rule_id)
        return self._decode_rule(rows[0])

    def update_rule(self, rule_id: str, values: dict[str, Any]) -> dict[str, Any]:
        self.get_rule(rule_id)
        rule = self.validate(values)
        self._validate_strategy_conditions(rule["conditions"])
        first = rule["conditions"][0]
        self.db.replace_alert_rule(
            rule_id=rule_id,
            parent=(
                rule["name"], rule["venue_symbol"], first["condition_type"], first["timeframe"],
                first["threshold"] or 0, rule["cooldown_seconds"], int(rule["enabled"]), rule["severity"],
                rule["quiet_start"], rule["quiet_end"], rule["timezone"], rule["daily_limit"],
                rule["confirmation_count"], rule["hysteresis"], int(time.time() * 1000),
            ),
            conditions=self._condition_rows(rule_id, rule["conditions"]),
        )
        return self.get_rule(rule_id)

    def delete_rule(self, rule_id: str) -> None:
        self.get_rule(rule_id)
        self.db.execute("DELETE FROM alert_rule_conditions WHERE rule_id=?", (rule_id,))
        self.db.execute("DELETE FROM alert_rules WHERE id=?", (rule_id,))

    def history_provenance(self, symbol: str) -> dict[str, Any]:
        """Pin the version of the bars the rules are about to read.

        Reading the frames is what fixes the version, so this returns the same
        answer the evaluation used rather than a second, independent read.
        """
        from ..datahub.view import read_history
        from ..config.instruments import require_instrument

        spec = require_instrument(symbol)
        tries = [spec.venue_symbol, symbol]
        last_error: Exception | None = None
        for candidate in dict.fromkeys(tries):
            try:
                for timeframe in ("15m", "1h"):
                    history = read_history(
                        self.db,
                        symbol=candidate,
                        interval=timeframe,
                        bars=240,
                        display_symbol=spec.display_symbol,
                        product_type=spec.product_type,
                        with_funding=False,
                    )
                    return history.provenance()
            except Exception as exc:  # noqa: BLE001 - provenance is best effort
                last_error = exc
        return {"version": None, "error": str(last_error) if last_error else "无法确定数据版本"}

    def _snapshot(self, symbol: str) -> dict[str, Any]:
        snapshots: dict[str, Any] = {}
        frames: dict[str, pd.DataFrame] = {}
        rows_by_frame: dict[str, list[dict]] = {}
        for timeframe in CORE_TIMEFRAMES:
            rows = self.db.load_candles("bybit", symbol, timeframe, limit=240)
            rows_by_frame[timeframe] = rows
            if not rows:
                snapshots[f"price:{timeframe}"] = None
                snapshots[f"volume:{timeframe}"] = None
                continue
            frame = pd.DataFrame(rows)
            frames[timeframe] = frame
            observed = int(rows[-1]["ts"])
            snapshots[f"price:{timeframe}"] = (float(rows[-1]["close"]), observed)
            previous = [float(row["volume"]) for row in rows[-21:-1]]
            baseline = sum(previous) / len(previous) if previous else 0.0
            ratio = float(rows[-1]["volume"]) / baseline if baseline > 0 else None
            snapshots[f"volume:{timeframe}"] = (ratio, observed) if ratio is not None else None

        funding = self.db.load_funding("bybit", symbol, limit=1)
        snapshots["funding"] = (
            (float(funding[-1]["rate"]) * 100, int(funding[-1]["ts"])) if funding else None
        )
        oi = self.db.load_open_interest("bybit", symbol, limit=80)
        oi_change: tuple[float, int] | None = None
        if len(oi) >= 2 and float(oi[-1]["oi"]) > 0:
            target = int(oi[-1]["ts"]) - 24 * 3600 * 1000
            prior = min(oi[:-1], key=lambda row: abs(int(row["ts"]) - target))
            if abs(int(prior["ts"]) - target) <= 2 * 3600 * 1000 and float(prior["oi"]) > 0:
                oi_change = (
                    (float(oi[-1]["oi"]) / float(prior["oi"]) - 1) * 100,
                    int(oi[-1]["ts"]),
                )
        snapshots["oi_change"] = oi_change

        combined = None
        if all(len(frames.get(timeframe, ())) >= RESONANCE_MIN_BARS for timeframe in CORE_TIMEFRAMES):
            stances = []
            for timeframe in CORE_TIMEFRAMES:
                frame = frames[timeframe].copy()
                frame.attrs["interval"] = timeframe
                stances.append(stance_for_interval(add_indicators(frame)))
            score = resonance(stances, DEFAULT_WEIGHTS)["score_100"]
            observed = max(int(frames[timeframe].iloc[-1]["ts"]) for timeframe in CORE_TIMEFRAMES)
            combined = (float(score), observed)
        snapshots["resonance"] = combined
        snapshots["rows"] = rows_by_frame
        snapshots["symbol"] = symbol
        return snapshots

    def _reading(self, condition: dict[str, Any], snapshot: dict[str, Any]) -> tuple[float, int] | None:
        condition_type = condition["conditionType"]
        if condition_type.startswith("price_"):
            return snapshot.get(f"price:{condition['timeframe']}")
        if condition_type.startswith("funding_"):
            return snapshot.get("funding")
        if condition_type.startswith("oi_change_"):
            return snapshot.get("oi_change")
        if condition_type == "volume_ratio_above":
            return snapshot.get(f"volume:{condition['timeframe']}")
        if condition_type.startswith("resonance_"):
            return snapshot.get("resonance")
        rows = snapshot["rows"].get(condition["timeframe"], [])
        if not rows:
            return None
        events, _ = StrategyRegistry(PluginManager(self.home)).generate(
            condition["strategyId"],
            rows,
            symbol=snapshot["symbol"],
            timeframe=condition["timeframe"],
            parameters=condition["strategyParameters"],
        )
        return float(events[-1] or 0), int(rows[-1]["ts"])

    @staticmethod
    def _met(condition: dict[str, Any], metric: float) -> bool:
        operator = CONDITION_CATALOG[condition["conditionType"]]["operator"]
        threshold = condition["threshold"]
        if operator == "above":
            return metric > threshold
        if operator == "below":
            return metric < threshold
        if operator == "cross_above":
            return condition["lastMetric"] is not None and condition["lastMetric"] <= threshold < metric
        if operator == "cross_below":
            return condition["lastMetric"] is not None and condition["lastMetric"] >= threshold > metric
        direction = condition.get("signalDirection") or "any"
        wanted = {1.0, -1.0} if direction == "any" else {1.0 if direction == "long" else -1.0}
        return metric in wanted

    @staticmethod
    def _rearm_ready(condition: dict[str, Any], metric: float, hysteresis: float) -> bool:
        operator = CONDITION_CATALOG[condition["conditionType"]]["operator"]
        threshold = condition.get("threshold")
        if operator in {"above", "cross_above"}:
            return metric <= threshold - hysteresis
        if operator in {"below", "cross_below"}:
            return metric >= threshold + hysteresis
        return metric == 0

    @staticmethod
    def _quiet(rule: dict[str, Any], now: int) -> bool:
        if not rule["quietStart"]:
            return False
        current = datetime.fromtimestamp(now / 1000, ZoneInfo(rule["timezone"])).strftime("%H:%M")
        start, end = rule["quietStart"], rule["quietEnd"]
        return start <= current < end if start < end else current >= start or current < end

    def _daily_count(self, rule: dict[str, Any], now: int) -> int:
        zone = ZoneInfo(rule["timezone"])
        local = datetime.fromtimestamp(now / 1000, zone)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = self.db.query(
            "SELECT COUNT(*) AS count FROM alert_events WHERE rule_id=? AND triggered_ts>=?",
            (rule["id"], int(start.timestamp() * 1000)),
        )
        return int(rows[0]["count"])

    @staticmethod
    def _required_frames(rule: dict[str, Any]) -> set[str]:
        frames = {condition["timeframe"] for condition in rule["conditions"] if condition.get("timeframe")}
        if any(condition["conditionType"].startswith("resonance_") for condition in rule["conditions"]):
            frames.update(CORE_TIMEFRAMES)
        if not frames:
            frames.add("1h")
        return frames

    def evaluate_symbol(self, symbol: str) -> dict[str, Any]:
        venue_symbol = require_instrument(symbol).venue_symbol
        rules = [rule for rule in self.list_rules() if rule["enabled"] and rule["venueSymbol"] == venue_symbol]
        if not rules:
            return {
                "symbol": venue_symbol,
                "evaluated": 0,
                "triggered": [],
                "unavailable": [],
                "blocked": [],
                "history": self.history_provenance(venue_symbol),
            }
        snapshot = self._snapshot(venue_symbol)
        quality = DataQualityMonitor(self.home).check_symbol(venue_symbol)
        quality_by_frame = {frame["timeframe"]: frame for frame in quality["frames"]}
        now = int(time.time() * 1000)
        triggered: list[dict[str, Any]] = []
        unavailable: list[str] = []
        blocked: list[dict[str, Any]] = []
        with self._lock:
            for original in rules:
                # API calls create a fresh engine/database connection. Refresh
                # mutable trigger state only after acquiring the shared home
                # lock so simultaneous manual and scheduled evaluations cannot
                # emit the same observation twice.
                try:
                    rule = self.get_rule(original["id"])
                except KeyError:
                    continue
                if not rule["enabled"]:
                    continue
                bad_frames = [
                    quality_by_frame[frame]
                    for frame in self._required_frames(rule)
                    if not quality_by_frame[frame]["signalEligible"]
                ]
                if bad_frames:
                    blocked.append({
                        "ruleId": rule["id"],
                        "reasons": [f"{item['timeframe']}: {item['statusLabel']}" for item in bad_frames],
                    })
                    continue
                readings: list[tuple[dict[str, Any], float, int, bool]] = []
                for condition in rule["conditions"]:
                    try:
                        reading = self._reading(condition, snapshot)
                    except Exception as exc:
                        blocked.append({"ruleId": rule["id"], "reasons": [f"策略计算失败：{exc}"]})
                        readings = []
                        break
                    if reading is None:
                        unavailable.append(condition["id"])
                        readings = []
                        break
                    metric, observed = reading
                    readings.append((condition, metric, observed, self._met(condition, metric)))
                if not readings:
                    continue
                signature = json.dumps([observed for _, _, observed, _ in readings])
                previous_signature = self.db.query(
                    "SELECT last_observation_key FROM alert_rules WHERE id=?", (rule["id"],)
                )[0]["last_observation_key"]
                is_new = signature != previous_signature
                all_met = all(met for _, _, _, met in readings)
                consecutive = rule["consecutiveCount"]
                if is_new:
                    consecutive = consecutive + 1 if all_met else 0
                armed = rule["armed"]
                if not armed and all(
                    self._rearm_ready(condition, metric, rule["hysteresis"])
                    for condition, metric, _, _ in readings
                ):
                    armed = True
                    consecutive = 0
                for condition, metric, observed, met in readings:
                    self.db.execute(
                        "UPDATE alert_rule_conditions SET last_metric=?,last_observed_ts=?,last_met=? WHERE id=?",
                        (metric, observed, int(met), condition["id"]),
                    )
                latest_observed = max(observed for _, _, observed, _ in readings)
                self.db.execute(
                    "UPDATE alert_rules SET last_condition=?,last_metric=?,last_observed_ts=?,last_evaluated_ts=?,"
                    "last_observation_key=?,consecutive_count=?,armed=? WHERE id=?",
                    (int(all_met), readings[0][1], latest_observed, now, signature, consecutive, int(armed), rule["id"]),
                )
                cooldown_ready = (
                    rule["lastTriggeredAt"] is None
                    or now - int(rule["lastTriggeredAt"]) >= int(rule["cooldownSeconds"]) * 1000
                )
                should_trigger = (
                    all_met
                    and is_new
                    and consecutive >= rule["confirmationCount"]
                    and cooldown_ready
                    and armed
                    and not self._quiet(rule, now)
                    and self._daily_count(rule, now) < rule["dailyLimit"]
                )
                if should_trigger:
                    triggered.append(self._trigger(rule, readings, latest_observed, now))
                    if rule["hysteresis"] > 0:
                        self.db.execute("UPDATE alert_rules SET armed=0 WHERE id=?", (rule["id"],))
        return {
            "symbol": venue_symbol,
            "evaluated": len(rules),
            "triggered": triggered,
            "unavailable": unavailable,
            "blocked": blocked,
            "dataQuality": quality,
            # The same version string the backtest reports: a signal and a backtest
            # that disagree can then be traced to the data rather than to the rule.
            "history": self.history_provenance(venue_symbol),
            "evaluatedAt": now,
        }

    def _trigger(
        self,
        rule: dict[str, Any],
        readings: list[tuple[dict[str, Any], float, int, bool]],
        observed: int,
        now: int,
    ) -> dict[str, Any]:
        event_id = str(uuid.uuid4())
        parts = []
        for condition, metric, _, _ in readings:
            catalog = CONDITION_CATALOG[condition["conditionType"]]
            if catalog.get("strategy"):
                direction = "做多" if metric > 0 else "做空"
                parts.append(f"{condition['strategyId']} {condition['timeframe']} {direction}")
            else:
                frame = f" {condition['timeframe']}" if condition.get("timeframe") else ""
                parts.append(
                    f"{catalog['label']}{frame}：{metric:.6g}{catalog['unit']} / "
                    f"{condition['threshold']:.6g}{catalog['unit']}"
                )
        title = f"{rule['venueSymbol']} · {rule['name']}"
        message = "；".join(parts)
        first_condition, first_metric, _, _ = readings[0]
        self.db.record_alert_trigger(
            event=(
                event_id, rule["id"], rule["venueSymbol"],
                "compound" if len(readings) > 1 else first_condition["conditionType"],
                first_condition.get("timeframe"), first_metric, first_condition.get("threshold") or 0,
                observed, now, title, message, "[]",
            ),
            rule_id=rule["id"],
            triggered_ts=now,
        )
        notification = NotificationEvent(
            id=f"alert-{event_id}",
            type="alert.triggered",
            severity=rule["severity"],
            title=title,
            message=message,
            occurredAt=datetime.now(timezone.utc).isoformat(),
            symbol=rule["venueSymbol"],
            data={"eventId": event_id, "ruleId": rule["id"], "conditions": len(readings)},
        )
        results = PluginRegistry(PluginManager(self.home)).notify_all(notification)
        self.db.execute(
            "UPDATE alert_events SET notification_results=? WHERE id=?",
            (json.dumps(results, ensure_ascii=False), event_id),
        )
        return self._decode_event(self.db.query("SELECT * FROM alert_events WHERE id=?", (event_id,))[0])
