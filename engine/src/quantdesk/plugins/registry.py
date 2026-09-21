"""Capability registry that turns enabled plugins into typed business adapters."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .manager import PluginError, PluginManager, PluginRecord
from .protocol import (
    AgentManifestResult,
    AgentProposeRequest,
    AgentProposeResult,
    AgentReflectRequest,
    AgentReflectResult,
    AnalyticsPortfolioRequest,
    FactorCatalogResult,
    FactorComputeRequest,
    FactorComputeResult,
    ValidationAnalyzeRequest,
    ValidationAnalyzeResult,
    AnalyticsPortfolioResult,
    AnalyticsScenarioRequest,
    AnalyticsScenarioResult,
    DataCandlesRequest,
    DataCandlesResult,
    NotificationEvent,
    NotifyResult,
    ResearchCollectRequest,
    ResearchCollectResult,
    StrategyDescribeResult,
    StrategyGenerateRequest,
    StrategyGenerateResult,
)


def check_proposals(proposals: list, manifest: "AgentManifestResult", budget) -> None:
    """Refuse a proposal that steps outside what the provider declared.

    Raises `PluginError` with the offending proposal id and the rule it broke, so
    an operator reading the log knows exactly which proposal was rejected and why.
    """
    space = manifest.proposalSpace
    allowed_factors = set(space.factorIds)
    if len(proposals) > int(budget.proposals):
        raise PluginError(
            f"提案数量 {len(proposals)} 超过本轮预算 {int(budget.proposals)}"
        )
    if len(proposals) > int(space.maxProposalsPerRound):
        raise PluginError(
            f"提案数量 {len(proposals)} 超过 manifest 声明的每轮上限 {space.maxProposalsPerRound}"
        )
    seen: set[str] = set()
    for proposal in proposals:
        if proposal.proposalId in seen:
            raise PluginError(f"提案 ID 重复：{proposal.proposalId}")
        seen.add(proposal.proposalId)
        unknown = [item for item in proposal.factorIds if item not in allowed_factors]
        if unknown:
            raise PluginError(
                f"提案 {proposal.proposalId} 使用了 manifest 未声明的因子：{', '.join(unknown)}"
            )
        for name, value in (proposal.parameters or {}).items():
            bounds = space.parameters.get(name)
            if bounds is None:
                raise PluginError(f"提案 {proposal.proposalId} 使用了未声明的参数：{name}")
            low, high = float(bounds[0]), float(bounds[-1])
            if not (low <= float(value) <= high):
                raise PluginError(
                    f"提案 {proposal.proposalId} 的参数 {name}={value} 超出声明范围 [{low:g}, {high:g}]"
                )
        if proposal.rule is not None:
            template = str((proposal.rule or {}).get("type") or "")
            if not template:
                raise PluginError(f"提案 {proposal.proposalId} 的规则缺少 type")
            if template not in set(space.ruleTemplates):
                raise PluginError(
                    f"提案 {proposal.proposalId} 使用了 manifest 未声明的规则模板：{template}"
                )
        if not proposal.hypothesis.strip():
            raise PluginError(f"提案 {proposal.proposalId} 缺少假设（hypothesis）")


class PluginRegistry:
    """Discover enabled adapters and validate every business message."""

    def __init__(self, manager: PluginManager):
        self.manager = manager

    def enabled(self, capability: str | None = None) -> list[PluginRecord]:
        records, _ = self.manager.discover()
        return [
            record for record in records
            if record.enabled and (capability is None or capability in record.manifest.capabilities)
        ]

    @staticmethod
    def _validate(model, payload: dict[str, Any], plugin_id: str):
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            raise PluginError(f"插件 {plugin_id} 返回的数据不符合业务协议：{exc.errors(include_url=False)}") from exc

    def data_candles(self, plugin_id: str, request: DataCandlesRequest) -> DataCandlesResult:
        response = self.manager.invoke(
            plugin_id, "data.candles", request.model_dump(), capability="data_provider"
        )
        return self._validate(DataCandlesResult, response["result"], plugin_id)

    def describe_strategies(self, plugin: PluginRecord) -> StrategyDescribeResult:
        response = self.manager.invoke(
            plugin.manifest.id, "strategy.describe", {}, capability="strategy"
        )
        return self._validate(StrategyDescribeResult, response["result"], plugin.manifest.id)

    def generate_signals(
        self, plugin_id: str, request: StrategyGenerateRequest
    ) -> StrategyGenerateResult:
        response = self.manager.invoke(
            plugin_id, "strategy.generate", request.model_dump(), capability="strategy"
        )
        return self._validate(StrategyGenerateResult, response["result"], plugin_id)

    def collect_research(
        self, plugin_id: str, request: ResearchCollectRequest
    ) -> ResearchCollectResult:
        response = self.manager.invoke(
            plugin_id, "research.collect", request.model_dump(), capability="research_tool"
        )
        return self._validate(ResearchCollectResult, response["result"], plugin_id)

    # -- v2: portfolio analytics ----------------------------------------
    def portfolio_analytics(
        self, plugin_id: str, request: AnalyticsPortfolioRequest
    ) -> AnalyticsPortfolioResult:
        """VaR, volatility, correlation, risk contribution and optimization.

        The request always carries QuantDesk's own contract symbols and returns,
        and the market-data version it was computed from: the provider is a
        calculator, never a source of market data.
        """
        response = self.manager.invoke(
            plugin_id, "analytics.portfolio", request.model_dump(by_alias=True), capability="analytics"
        )
        return self._validate(AnalyticsPortfolioResult, response["result"], plugin_id)

    def scenario_analytics(
        self, plugin_id: str, request: AnalyticsScenarioRequest
    ) -> AnalyticsScenarioResult:
        response = self.manager.invoke(
            plugin_id, "analytics.scenario", request.model_dump(), capability="analytics"
        )
        return self._validate(AnalyticsScenarioResult, response["result"], plugin_id)

    # -- v3: factor research and validation ------------------------------
    def factor_catalog(self, plugin_id: str) -> FactorCatalogResult:
        """The factors this provider is willing to compute, already filtered."""
        response = self.manager.invoke(
            plugin_id, "factor.catalog", {}, capability="factor_provider"
        )
        return self._validate(FactorCatalogResult, response["result"], plugin_id)

    def compute_factors(
        self, plugin_id: str, request: FactorComputeRequest
    ) -> FactorComputeResult:
        """Compute factors over the engine's closed candles.

        The candles, funding and auxiliary readings all come from QuantDesk, so a
        provider has nothing to fetch and no way to substitute another venue's
        prices for Bybit's.
        """
        response = self.manager.invoke(
            plugin_id, "factor.compute", request.model_dump(), capability="factor_provider"
        )
        return self._validate(FactorComputeResult, response["result"], plugin_id)

    def analyze_validation(
        self, plugin_id: str, request: ValidationAnalyzeRequest
    ) -> ValidationAnalyzeResult:
        response = self.manager.invoke(
            plugin_id, "validation.analyze", request.model_dump(), capability="backtest_validator"
        )
        return self._validate(ValidationAnalyzeResult, response["result"], plugin_id)

    # -- v4: the strategy improvement agent ------------------------------
    def agent_manifest(self, plugin_id: str) -> AgentManifestResult:
        """What this provider is willing to propose, and inside which bounds."""
        response = self.manager.invoke(
            plugin_id, "agent.manifest", {}, capability="strategy_agent"
        )
        return self._validate(AgentManifestResult, response["result"], plugin_id)

    def agent_propose(
        self, plugin_id: str, request: AgentProposeRequest, manifest: AgentManifestResult
    ) -> AgentProposeResult:
        """One round of proposals, checked against the provider's own bounds.

        The check is the point: a provider that proposes a factor outside the space
        it declared, a parameter outside its ranges, or a rule template it never
        listed is refused, and the refusal names which proposal and why. Without it
        "allowlist" would be a promise in a README rather than a property of the
        system.
        """
        response = self.manager.invoke(
            plugin_id, "agent.propose", request.model_dump(), capability="strategy_agent"
        )
        result = self._validate(AgentProposeResult, response["result"], plugin_id)
        check_proposals(result.proposals, manifest, request.budget)
        return result

    def agent_reflect(
        self, plugin_id: str, request: AgentReflectRequest, manifest: AgentManifestResult
    ) -> AgentReflectResult:
        response = self.manager.invoke(
            plugin_id, "agent.reflect", request.model_dump(), capability="strategy_agent"
        )
        result = self._validate(AgentReflectResult, response["result"], plugin_id)
        check_proposals(result.proposals, manifest, request.budget)
        return result

    def notify(self, plugin_id: str, event: NotificationEvent) -> NotifyResult:
        response = self.manager.invoke(
            plugin_id, "notify.send", event.model_dump(), capability="notifier"
        )
        return self._validate(NotifyResult, response["result"], plugin_id)

    def notify_all(self, event: NotificationEvent) -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []
        for plugin in self.enabled("notifier"):
            try:
                result = self.notify(plugin.manifest.id, event)
                outcomes.append({"pluginId": plugin.manifest.id, "ok": True, **result.model_dump()})
            except PluginError as exc:
                outcomes.append({"pluginId": plugin.manifest.id, "ok": False, "error": str(exc)})
        return outcomes

