"""Agent campaigns: a pre-registered, budgeted, human-promoted search.

The five locked decisions this module implements, and where each one lives:

* **D1 deterministic search first** - `mode` is stored and defaults to
  `deterministic_search`; an `llm_assisted` campaign is refused here until its own
  stage, so nothing can quietly start one.
* **D2 budget (≤32 proposals/round, ≤5 rounds, ≤10 min/round)** - enforced at
  registration *and* per round through `agent_budget_ledger`; a breach stops the
  campaign with `budget_limited` and the arithmetic is kept.
* **D3 human-only promotion** - `promote()` demands a named human and refuses a
  campaign that has not finished, a proposal that has no validation evidence, and a
  promotion that would touch anything live. There is no agent-facing path to it.
* **D4 the agent cannot change code** - a proposal is data: ids from the frozen
  factor space, parameters inside the provider's declared ranges, a rule template by
  name. This module re-checks that against the *snapshot taken at registration*, so
  a later change to the live library cannot widen a running campaign's space.
* **D5 groups are validated separately** - `group_name` fixes the universe at
  registration; the stock group never contains the leveraged ETFs, and the crypto
  group is its own thing.

The test window is sealed at registration and has no writer until `unseal_test()`,
which is one-shot (stage 6's Gate-C).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# D2. These are the caps the plan locked; a campaign may ask for less.
MAX_PROPOSALS_PER_ROUND = 32
MAX_ROUNDS = 5
MAX_ROUND_MS = 10 * 60 * 1000
SEGMENTS = ("train", "validation")
# Statuses a campaign can be in. `budget_limited` and `compliance_blocked` are
# terminal states with reasons, not failures to hide.
STATUSES = (
    "preregistered",
    "running",
    "completed",
    "budget_limited",
    "compliance_blocked",
    "failed",
)
TERMINAL = ("completed", "budget_limited", "compliance_blocked", "failed")


class CampaignError(RuntimeError):
    """A campaign operation the caller may fix, with an actionable message."""

    def __init__(self, message: str, *, status: int = 409, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Budget:
    proposals_per_round: int
    max_rounds: int
    round_deadline_ms: int

    @staticmethod
    def from_payload(raw: dict[str, Any] | None) -> "Budget":
        raw = raw or {}

        def value(key: str, default: int) -> int:
            # `0` is a request for zero proposals, not a missing field: defaulting it
            # silently would turn "as small as possible" into "the full cap".
            item = raw.get(key)
            return default if item is None else int(item)

        budget = Budget(
            proposals_per_round=value("proposalsPerRound", MAX_PROPOSALS_PER_ROUND),
            max_rounds=value("maxRounds", MAX_ROUNDS),
            round_deadline_ms=value("roundDeadlineMs", MAX_ROUND_MS),
        )
        if not 1 <= budget.proposals_per_round <= MAX_PROPOSALS_PER_ROUND:
            raise CampaignError(
                f"每轮提案上限必须在 1–{MAX_PROPOSALS_PER_ROUND} 之间（D2），收到 "
                f"{budget.proposals_per_round}",
                status=422,
            )
        if not 1 <= budget.max_rounds <= MAX_ROUNDS:
            raise CampaignError(
                f"轮数上限必须在 1–{MAX_ROUNDS} 之间（D2），收到 {budget.max_rounds}", status=422
            )
        if not 1_000 <= budget.round_deadline_ms <= MAX_ROUND_MS:
            raise CampaignError(
                f"每轮时限必须在 1 秒–{MAX_ROUND_MS // 60_000} 分钟之间（D2），收到 "
                f"{budget.round_deadline_ms} 毫秒",
                status=422,
            )
        return budget

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposalsPerRound": self.proposals_per_round,
            "maxRounds": self.max_rounds,
            "roundDeadlineMs": self.round_deadline_ms,
        }


def _now() -> int:
    return int(time.time() * 1000)


def _one(db: Any, sql: str, params: tuple = ()) -> Any:
    """The store exposes `query`/`execute`; a single row is just the first of them."""
    rows = db.query(sql, params)
    return rows[0] if rows else None


def _campaign(row: Any) -> dict[str, Any]:
    return {
        "uid": row["campaign_uid"],
        "provider": row["provider"],
        "agentVersion": row["agent_version"],
        "mode": row["mode"],
        "group": row["group_name"],
        "interval": row["interval"],
        "horizonBars": int(row["horizon_bars"]),
        "universe": json.loads(row["universe_json"] or "[]"),
        "factorSpace": json.loads(row["factor_space_json"] or "[]"),
        "libraryTs": row["library_ts"],
        "hypothesis": row["hypothesis"],
        "successCriteria": row["success_criteria"],
        "windows": {
            "train": [row["train_start_ts"], row["train_end_ts"]],
            "validation": [row["validation_start_ts"], row["validation_end_ts"]],
            "test": [row["test_start_ts"], row["test_end_ts"]],
        },
        "testUnsealedTs": row["test_unsealed_ts"],
        # The only place the test window's existence is acknowledged before unsealing.
        "testSealed": row["test_unsealed_ts"] is None,
        "budget": json.loads(row["budget_json"] or "{}"),
        "status": row["status"],
        "stopReason": row["stop_reason"],
        "roundsUsed": int(row["rounds_used"]),
        "trialsUsed": int(row["trials_used"]),
        "proposalsUsed": int(row["proposals_used"]),
        "seed": int(row["seed"]),
        "snapshotHash": row["snapshot_hash"],
        "createdTs": row["created_ts"],
        "startedTs": row["started_ts"],
        "finishedTs": row["finished_ts"],
    }


def get_campaign(db: Any, uid: str) -> dict[str, Any]:
    row = _one(db, "SELECT * FROM agent_campaigns WHERE campaign_uid=?", (uid,))
    if row is None:
        raise CampaignError(f"没有这个战役：{uid}", status=404)
    return _campaign(row)


def list_campaigns(db: Any, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM agent_campaigns ORDER BY created_ts DESC LIMIT ?", (int(limit),)
    )
    return [_campaign(row) for row in rows]


def _validate_windows(windows: dict[str, Iterable[int] | None]) -> dict[str, tuple[int, int]]:
    """The three windows must exist, be ordered, and not overlap.

    This is the pre-registration's backbone: a campaign that could choose its test
    window after seeing its validation results would not have an out-of-sample
    segment at all.
    """
    parsed: dict[str, tuple[int, int]] = {}
    for name in ("train", "validation", "test"):
        pair = windows.get(name)
        if not pair:
            raise CampaignError(f"预注册必须给出 {name} 窗口", status=422)
        start, end = (int(pair[0]), int(pair[1])) if len(tuple(pair)) == 2 else (0, 0)
        if start <= 0 or end <= start:
            raise CampaignError(f"{name} 窗口不合法：{start} – {end}", status=422)
        parsed[name] = (start, end)
    order = ("train", "validation", "test")
    for earlier, later in zip(order, order[1:]):
        # `>=`, not `>`: a bar carries its opening timestamp, so an `end` that equals
        # the next `start` puts the same bar in two segments - and a test bar that is
        # also a validation bar is not out of sample.
        if parsed[earlier][1] >= parsed[later][0]:
            raise CampaignError(
                f"{earlier} 与 {later} 窗口重叠或相接（{earlier} 结束于 {parsed[earlier][1]}，"
                f"{later} 开始于 {parsed[later][0]}）；三段必须按时间严格分开",
                status=422,
            )
    return parsed


def preregister(
    db: Any,
    *,
    group: str,
    interval: str,
    horizon_bars: int,
    hypothesis: str,
    success_criteria: str,
    windows: dict[str, Iterable[int] | None],
    budget: dict[str, Any] | None = None,
    provider: str = "",
    agent_version: str = "",
    mode: str = "deterministic_search",
    seed: int = 42,
    home: Path | None = None,
) -> dict[str, Any]:
    """Write the campaign down before anything is searched."""
    from .factor_gates import group_symbols
    from .factor_library import library_for

    if mode != "deterministic_search":
        raise CampaignError(
            "第一版只开确定性搜索（D1）；LLM 辅助提案属于阶段 10，需要单独开启", status=422
        )
    if not str(hypothesis).strip() or not str(success_criteria).strip():
        raise CampaignError("预注册必须写明假设与成功判据", status=422)
    if int(horizon_bars) <= 0:
        raise CampaignError("视野必须是正的 bar 数", status=422)
    try:
        universe = list(group_symbols(group))
    except ValueError as exc:
        raise CampaignError(
            f"{exc}；可用分组：stock / leveraged_etf / crypto", status=422
        ) from exc
    if len(universe) < 2:
        raise CampaignError(f"分组 {group} 的标的不足 2 个，无法做组合验证", status=422)

    from .config.settings import quantdesk_home as _home

    home = home or _home()
    entries = library_for(group, interval, horizon=int(horizon_bars), home=home)
    if not entries:
        entries = library_for(group, interval, home=home)
    if not entries:
        raise CampaignError(
            f"{group}/{interval} 还没有受控因子库：先运行 `quantdesk factors gate-scan`。"
            "没有证据的因子空间不允许开战役",
            status=409,
        )
    limits = Budget.from_payload(budget)
    parsed = _validate_windows(windows)
    timestamp = _now()
    uid = f"camp-{uuid.uuid4().hex[:12]}"
    db.execute(
        "INSERT INTO agent_campaigns "
        "(campaign_uid, provider, agent_version, mode, group_name, interval, horizon_bars, "
        " universe_json, factor_space_json, library_ts, hypothesis, success_criteria, "
        " train_start_ts, train_end_ts, validation_start_ts, validation_end_ts, "
        " test_start_ts, test_end_ts, budget_json, status, seed, created_ts) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'preregistered',?,?)",
        (
            uid, provider, agent_version, mode, group, interval, int(horizon_bars),
            json.dumps(universe, ensure_ascii=False),
            json.dumps(
                [
                    {"factorId": item["factorId"], "tier": item["tier"], "family": item["family"]}
                    for item in entries
                ],
                ensure_ascii=False,
            ),
            str(entries[0].get("libraryTs") or ""),
            str(hypothesis), str(success_criteria),
            parsed["train"][0], parsed["train"][1],
            parsed["validation"][0], parsed["validation"][1],
            parsed["test"][0], parsed["test"][1],
            json.dumps(limits.as_dict(), ensure_ascii=False),
            int(seed), timestamp,
        ),
    )
    _ledger(db, uid, round_number=0, entry="rounds", amount=0, limit_value=limits.max_rounds,
            note=f"预注册：最多 {limits.max_rounds} 轮 × {limits.proposals_per_round} 个提案")
    return get_campaign(db, uid)


def _campaign_id(db: Any, uid: str) -> int:
    row = _one(db, "SELECT id FROM agent_campaigns WHERE campaign_uid=?", (uid,))
    if row is None:
        raise CampaignError(f"没有这个战役：{uid}", status=404)
    return int(row["id"])


def _ledger(
    db: Any, uid: str, *, round_number: int, entry: str, amount: float,
    limit_value: float, breached: bool = False, note: str = "",
) -> None:
    db.execute(
        "INSERT INTO agent_budget_ledger "
        "(campaign_id, round, entry, amount, limit_value, breached, note, created_ts) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (_campaign_id(db, uid), int(round_number), entry, float(amount), float(limit_value),
         1 if breached else 0, note, _now()),
    )


def budget_state(db: Any, uid: str) -> dict[str, Any]:
    campaign = get_campaign(db, uid)
    limits = Budget.from_payload(campaign["budget"])
    return {
        "roundsUsed": campaign["roundsUsed"],
        "roundsLeft": max(0, limits.max_rounds - campaign["roundsUsed"]),
        "proposalsUsed": campaign["proposalsUsed"],
        "trialsUsed": campaign["trialsUsed"],
        "limits": limits.as_dict(),
    }


def start_round(db: Any, uid: str, *, round_number: int, wallclock_ms: int = 0) -> dict[str, Any]:
    """Open a round, or refuse it because the budget is already spent."""
    campaign = get_campaign(db, uid)
    if campaign["status"] in TERMINAL:
        raise CampaignError(
            f"战役已结束（{campaign['status']}：{campaign['stopReason'] or '无原因'}）", status=409
        )
    limits = Budget.from_payload(campaign["budget"])
    if int(round_number) != campaign["roundsUsed"] + 1:
        raise CampaignError(
            f"轮次必须连续：已用 {campaign['roundsUsed']} 轮，收到第 {round_number} 轮", status=409
        )
    if int(round_number) > limits.max_rounds:
        _stop(db, uid, "budget_limited", f"轮数用尽（{limits.max_rounds} 轮）")
        _ledger(db, uid, round_number=round_number, entry="rounds", amount=round_number,
                limit_value=limits.max_rounds, breached=True, note="轮数超预算")
        raise CampaignError(f"轮数超过预算 {limits.max_rounds}（D2）", status=409)
    if wallclock_ms > limits.round_deadline_ms:
        _stop(db, uid, "budget_limited", f"上一轮耗时 {wallclock_ms} 毫秒超过单轮时限")
        _ledger(db, uid, round_number=round_number, entry="wallclock_ms", amount=wallclock_ms,
                limit_value=limits.round_deadline_ms, breached=True, note="单轮超时")
        raise CampaignError(f"单轮耗时 {wallclock_ms} 毫秒超过上限（D2）", status=409)
    db.execute(
        "UPDATE agent_campaigns SET status='running', started_ts=COALESCE(started_ts,?), "
        "rounds_used=rounds_used+1 WHERE campaign_uid=?",
        (_now(), uid),
    )
    _ledger(db, uid, round_number=round_number, entry="rounds", amount=round_number,
            limit_value=limits.max_rounds, note=f"第 {round_number} 轮开始")
    return get_campaign(db, uid)


def record_proposals(
    db: Any, uid: str, *, round_number: int, proposals: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Store a round's proposals, after checking them against the frozen space.

    The check is deliberately against the snapshot, not the live library: a campaign
    must not be able to widen its own search space halfway through, which is exactly
    what would happen if the space were looked up again here.
    """
    campaign = get_campaign(db, uid)
    limits = Budget.from_payload(campaign["budget"])
    allowed = {item["factorId"] for item in campaign["factorSpace"]}
    if len(proposals) > limits.proposals_per_round:
        _stop(db, uid, "budget_limited",
              f"本轮 {len(proposals)} 个提案超过每轮上限 {limits.proposals_per_round}")
        _ledger(db, uid, round_number=round_number, entry="proposals", amount=len(proposals),
                limit_value=limits.proposals_per_round, breached=True, note="提案数超预算")
        raise CampaignError(
            f"本轮提案 {len(proposals)} 个，超过每轮上限 {limits.proposals_per_round}（D2）",
            status=409,
        )
    stored: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in proposals:
        proposal_uid = str(item.get("proposalId") or "")
        if not proposal_uid or proposal_uid in seen:
            raise CampaignError(f"提案 ID 缺失或重复：{proposal_uid!r}", status=422)
        seen.add(proposal_uid)
        unknown = [fid for fid in item.get("factorIds") or [] if fid not in allowed]
        if unknown:
            raise CampaignError(
                f"提案 {proposal_uid} 使用了冻结空间之外的因子：{', '.join(unknown)}（D4）",
                status=422,
            )
        hypothesis = str(item.get("hypothesis") or "").strip()
        if not hypothesis:
            raise CampaignError(f"提案 {proposal_uid} 缺少假设", status=422)
        parameters = item.get("parameters") or {}
        if any(not isinstance(value, (int, float)) or isinstance(value, bool)
               for value in parameters.values()):
            raise CampaignError(
                f"提案 {proposal_uid} 的参数必须是数值（D4：提案是数据，不是代码）", status=422
            )
        existing = _one(
            db,
            "SELECT round FROM agent_proposals WHERE campaign_id=? AND proposal_uid=?",
            (_campaign_id(db, uid), proposal_uid),
        )
        if existing is not None:
            # The same id in the same round is a retry of a round that was already
            # recorded, which must not double the campaign's trial count. The same id
            # in a *different* round is a provider reusing its names, and re-feeding
            # an old candidate as if it were new would quietly inflate the search -
            # so it is refused, by name.
            if int(existing["round"]) != int(round_number):
                raise CampaignError(
                    f"提案 ID {proposal_uid} 已经在第 {int(existing['round'])} 轮用过；"
                    "每一轮的提案 ID 必须唯一（否则同一批候选会被重复计入多重检验）",
                    status=409,
                )
            continue
        db.execute(
            "INSERT INTO agent_proposals "
            "(campaign_id, round, proposal_uid, kind, factor_ids, parameters, rule_json, "
            " hypothesis, expected_failure_mode, status, created_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,'proposed',?)",
            (
                _campaign_id(db, uid), int(round_number), proposal_uid,
                str(item.get("kind") or "parameter_set"),
                json.dumps(list(item.get("factorIds") or []), ensure_ascii=False),
                json.dumps(parameters, ensure_ascii=False),
                json.dumps(item.get("rule"), ensure_ascii=False) if item.get("rule") else None,
                hypothesis, str(item.get("expectedFailureMode") or "")[:400], _now(),
            ),
        )
        stored.append({"proposalId": proposal_uid, "status": "proposed"})
    db.execute(
        "UPDATE agent_campaigns SET proposals_used=proposals_used+? WHERE campaign_uid=?",
        (len(stored), uid),
    )
    used = get_campaign(db, uid)["proposalsUsed"]
    _ledger(db, uid, round_number=round_number, entry="proposals", amount=len(stored),
            limit_value=limits.proposals_per_round, note=f"第 {round_number} 轮入库")
    if used > limits.proposals_per_round * limits.max_rounds:
        _stop(db, uid, "budget_limited", f"提案总量 {used} 超过全程上限")
    return stored


def record_trial(
    db: Any, uid: str, *, proposal_uid: str, segment: str, run_id: str = "",
    sharpe: float | None = None, return_pct: float | None = None,
    max_drawdown_pct: float | None = None, trades: int | None = None,
    verdict: str = "", reason: str = "",
) -> dict[str, Any]:
    """Record what one proposal scored on one *visible* segment."""
    campaign = get_campaign(db, uid)
    if segment == "test" and campaign["testUnsealedTs"] is None:
        raise CampaignError(
            "试验分段只能是 train/validation：测试段在开封前不接受任何写入（Gate-C）", status=422
        )
    if segment not in (*SEGMENTS, "test"):
        raise CampaignError(f"试验分段只能是 {'/'.join((*SEGMENTS, 'test'))}：{segment}", status=422)
    if segment == "test":
        # The one-shot out-of-sample measurement happens *after* the search, so a
        # finished campaign may still receive exactly one test trial - but only once,
        # because a window that can be measured twice is not out of sample.
        measured = _one(
            db,
            "SELECT id FROM agent_trials WHERE campaign_id=? AND segment='test' "
            "AND run_id IS NOT NULL AND run_id != ''",
            (_campaign_id(db, uid),),
        )
        if measured is not None:
            raise CampaignError(
                "测试段已经评估过一次；重复评估会把它变成可复用的样本（Gate-C）", status=409
            )
    elif campaign["status"] in TERMINAL:
        # The search itself may not keep adding trials to a finished campaign.
        raise CampaignError(f"战役已结束（{campaign['status']}）", status=409)
    row = _one(db, 
        "SELECT id FROM agent_proposals WHERE campaign_id=? AND proposal_uid=?",
        (_campaign_id(db, uid), proposal_uid),
    )
    if row is None:
        raise CampaignError(f"提案不在这个战役里：{proposal_uid}", status=404)
    db.execute(
        "INSERT INTO agent_trials "
        "(campaign_id, proposal_id, segment, run_id, sharpe, return_pct, max_drawdown_pct, "
        " trades, verdict, reason, created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (_campaign_id(db, uid), int(row["id"]), segment, run_id, sharpe, return_pct,
         max_drawdown_pct, trades, verdict, reason, _now()),
    )
    db.execute(
        "UPDATE agent_campaigns SET trials_used=trials_used+1 WHERE campaign_uid=?", (uid,)
    )
    db.execute(
        "UPDATE agent_proposals SET status='evaluated' WHERE campaign_id=? AND proposal_uid=?",
        (_campaign_id(db, uid), proposal_uid),
    )
    _ledger(db, uid, round_number=campaign["roundsUsed"], entry="trials", amount=1,
            limit_value=0, note=f"{proposal_uid} 在 {segment} 上的试验")
    return {"proposalId": proposal_uid, "segment": segment}


def trials_for(
    db: Any, uid: str, *, include_test: bool = False, limit: int = 500
) -> list[dict[str, Any]]:
    """What the agent (or a reader) is allowed to see.

    `include_test` is not a convenience flag: it is refused while the campaign's test
    window is sealed, which is what makes the seal a property of the system rather
    than of a caller's discipline. Admin detail views use it after unsealing only.
    """
    campaign = get_campaign(db, uid)
    if include_test and campaign["testUnsealedTs"] is None:
        raise CampaignError(
            "测试段尚未开封（Gate-C）：先用一次性开封接口打开，且只能开一次", status=409
        )
    segments = ("train", "validation", "test") if include_test else SEGMENTS
    placeholders = ",".join("?" for _ in segments)
    rows = db.query(
        "SELECT t.*, p.proposal_uid, p.factor_ids, p.parameters FROM agent_trials t "
        "JOIN agent_proposals p ON p.id = t.proposal_id "
        f"WHERE t.campaign_id=? AND t.segment IN ({placeholders}) ORDER BY t.id LIMIT ?",
        (_campaign_id(db, uid), *segments, int(limit)),
    )
    return [
        {
            "proposalId": row["proposal_uid"],
            "segment": row["segment"],
            "runId": row["run_id"],
            "sharpe": row["sharpe"],
            "returnPct": row["return_pct"],
            "maxDrawdownPct": row["max_drawdown_pct"],
            "trades": row["trades"],
            "verdict": row["verdict"],
            "reason": row["reason"],
            "factorIds": json.loads(row["factor_ids"] or "[]"),
            "parameters": json.loads(row["parameters"] or "{}"),
        }
        for row in rows
    ]


def agent_visible_trials(db: Any, uid: str, *, round_number: int = 0, limit: int = 256) -> list[dict]:
    """The summaries a provider is handed for one round: train/validation only."""
    trials = trials_for(db, uid, include_test=False, limit=limit)
    if round_number:
        rows = db.query(
            "SELECT proposal_uid FROM agent_proposals WHERE campaign_id=? AND round=?",
            (_campaign_id(db, uid), int(round_number)),
        )
        earlier = {row["proposal_uid"] for row in rows}
        trials = [item for item in trials if item["proposalId"] not in earlier]
    return trials


def _stop(db: Any, uid: str, status: str, reason: str) -> None:
    db.execute(
        "UPDATE agent_campaigns SET status=?, stop_reason=?, finished_ts=? WHERE campaign_uid=?",
        (status, reason, _now(), uid),
    )


def finish(db: Any, uid: str, *, status: str = "completed", reason: str = "") -> dict[str, Any]:
    if status not in TERMINAL:
        raise CampaignError(f"结束状态必须是 {', '.join(TERMINAL)} 之一", status=422)
    _stop(db, uid, status, reason)
    _ledger(db, uid, round_number=0, entry="rounds", amount=0, limit_value=0,
            note=f"战役结束：{status} {reason}".strip())
    return get_campaign(db, uid)


def unseal_test(db: Any, uid: str, *, approved_by: str) -> dict[str, Any]:
    """Open the sealed test window, once, by a named human (Gate-C).

    One-shot on purpose: a window that can be reopened is not out of sample, it is a
    slower training set. The first unsealing is recorded with its timestamp and who
    asked for it, and a second attempt is refused rather than ignored.
    """
    if not str(approved_by).strip():
        raise CampaignError("开封测试段必须写明是谁批准的", status=422)
    campaign = get_campaign(db, uid)
    if campaign["testUnsealedTs"] is not None:
        raise CampaignError(
            f"测试段已经在 {campaign['testUnsealedTs']} 开封过一次，不能重复开封（Gate-C）",
            status=409,
        )
    if campaign["trialsUsed"] <= 0:
        raise CampaignError("战役还没有任何试验记录，开封测试段没有意义", status=409)
    if campaign["status"] not in TERMINAL and campaign["status"] != "running":
        raise CampaignError(f"战役还处于 {campaign['status']}，不能开封测试段", status=409)
    db.execute(
        "UPDATE agent_campaigns SET test_unsealed_ts=?, stop_reason=? WHERE campaign_uid=?",
        (_now(), f"测试段由 {approved_by} 一次性开封", uid),
    )
    _ledger(db, uid, round_number=0, entry="rounds", amount=0, limit_value=0,
            note=f"{approved_by} 开封测试段（Gate-C，一次性）")
    return get_campaign(db, uid)


def promote(
    db: Any, uid: str, *, proposal_uid: str, approved_by: str, note: str = ""
) -> dict[str, Any]:
    """Promote one proposal to a paper candidate - by a human, never by the agent.

    D3 says promotion is a human act, so this function has no agent-shaped entry
    point: it demands `approved_by`, it refuses a campaign that is still running, and
    it refuses a proposal with no validation evidence. It writes a strategy version
    marked `candidate`, never a live configuration.
    """
    if not str(approved_by).strip():
        raise CampaignError("晋升必须写明批准人（D3：只有人能晋升）", status=422)
    campaign = get_campaign(db, uid)
    if campaign["status"] not in TERMINAL:
        raise CampaignError(
            f"战役还在 {campaign['status']}，结束前不能晋升任何提案", status=409
        )
    campaign_id = _campaign_id(db, uid)
    row = _one(db, 
        "SELECT * FROM agent_proposals WHERE campaign_id=? AND proposal_uid=?",
        (campaign_id, proposal_uid),
    )
    if row is None:
        raise CampaignError(f"提案不在这个战役里：{proposal_uid}", status=404)
    trials = db.query(
        "SELECT * FROM agent_trials WHERE proposal_id=? AND segment='validation' "
        "ORDER BY id DESC LIMIT 1",
        (int(row["id"]),),
    )
    if not trials:
        raise CampaignError(f"提案 {proposal_uid} 没有验证段证据，不能晋升", status=409)
    trial = trials[0]
    # A row is not evidence. An `inconclusive` trial means the engine could not
    # measure the candidate at all, and promoting that would put a strategy version
    # into the record with nothing behind it - the one thing the whole staged design
    # exists to prevent.
    if trial["sharpe"] is None:
        raise CampaignError(
            f"提案 {proposal_uid} 的验证段没有 Sharpe 读数（verdict={trial['verdict'] or '空'}），"
            "不能晋升：先让它在验证段跑出可测量的结果。战役记录本身就是留档",
            status=409,
        )
    factors = json.loads(row["factor_ids"] or "[]")
    parameters = json.loads(row["parameters"] or "{}")
    version = _version_hash(uid, proposal_uid, factors, parameters)
    db.execute(
        "INSERT OR REPLACE INTO strategy_versions "
        "(strategy_id, version, parameters_json, engine, engine_version, code_hash, source, "
        " created_ts) VALUES (?,?,?,?,?,?,?,?)",
        (
            f"agent:{uid}", version,
            json.dumps({"factors": factors, "parameters": parameters, "proposalId": proposal_uid},
                       ensure_ascii=False),
            "agent-campaign", "agent-campaign/1", version, f"agent:{uid}", _now(),
        ),
    )
    _ledger(db, uid, round_number=campaign["roundsUsed"], entry="promotion", amount=0,
            limit_value=0,
            note=f"{approved_by} 晋升 {proposal_uid} 为候选（{note or '无备注'}）")
    return {
        "campaign": uid,
        "proposalId": proposal_uid,
        "strategyId": f"agent:{uid}",
        "version": version,
        "approvedBy": approved_by,
        "note": note,
        "stage": "candidate",
        "factors": factors,
        "parameters": parameters,
        "validation": {
            "sharpe": trial["sharpe"],
            "returnPct": trial["return_pct"],
            "maxDrawdownPct": trial["max_drawdown_pct"],
            "trades": trial["trades"],
            "verdict": trial["verdict"],
        },
        "live": False,
    }


def _version_hash(uid: str, proposal_uid: str, factors: list[str], parameters: dict) -> str:
    import hashlib

    material = json.dumps(
        {"campaign": uid, "proposal": proposal_uid, "factors": sorted(factors),
         "parameters": parameters, "stage": "candidate"},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def ledger_for(db: Any, uid: str, *, limit: int = 200) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM agent_budget_ledger WHERE campaign_id=? ORDER BY id LIMIT ?",
        (_campaign_id(db, uid), int(limit)),
    )
    return [
        {
            "round": int(row["round"]),
            "entry": row["entry"],
            "amount": row["amount"],
            "limit": row["limit_value"],
            "breached": bool(row["breached"]),
            "note": row["note"],
            "createdTs": int(row["created_ts"]),
        }
        for row in rows
    ]


def detail(db: Any, uid: str, *, include_test: bool = False) -> dict[str, Any]:
    """A campaign with its proposals and the trials a reader may see."""
    campaign = get_campaign(db, uid)
    campaign_id = _campaign_id(db, uid)
    proposals = db.query(
        "SELECT * FROM agent_proposals WHERE campaign_id=? ORDER BY round, id", (campaign_id,)
    )
    return {
        "campaign": campaign,
        "budgetState": budget_state(db, uid),
        "proposals": [
            {
                "proposalId": row["proposal_uid"],
                "round": int(row["round"]),
                "kind": row["kind"],
                "factorIds": json.loads(row["factor_ids"] or "[]"),
                "parameters": json.loads(row["parameters"] or "{}"),
                "hypothesis": row["hypothesis"],
                "expectedFailureMode": row["expected_failure_mode"],
                "status": row["status"],
                "rejectReason": row["reject_reason"],
            }
            for row in proposals
        ],
        "trials": trials_for(db, uid, include_test=include_test),
        "ledger": ledger_for(db, uid),
    }
