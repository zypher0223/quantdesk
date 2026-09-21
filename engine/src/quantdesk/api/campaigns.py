"""Campaign endpoints: pre-registration, rounds, the ledger, unsealing, promotion.

Two boundaries this surface keeps deliberately narrow:

* **Nothing here runs a search.** The endpoints record a campaign's state; the
  orchestration that asks a provider for proposals and hands them to the study queue
  lives in `quantdesk.agent_campaign` and is driven by the run worker, so a page load
  can never start a search.
* **Promotion is an operator action.** `POST /{uid}/promote` requires `approvedBy` and
  has no plugin-facing counterpart; a provider process speaks JSON-RPC to the engine
  and has no route to this router at all.

The Gate-C pair is the same shape: `GET /{uid}/stats` is read-only and answers before
and after the seal is broken (never with a test-segment value while it is sealed), and
`POST /{uid}/verdict` is the one-shot unsealing - it needs an approver, it writes at
most one verdict per (campaign, proposal, segment), and calling it twice returns the
row the first call wrote instead of running the test segment again.
"""

from __future__ import annotations

import inspect

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from .. import campaigns, campaign_stats
from ..campaigns import CampaignError
from ..config.settings import quantdesk_home
from ..datahub.db import Database

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


def _db() -> Database:
    return Database(quantdesk_home() / "quantdesk.db")


class WindowBody(BaseModel):
    train: list[int] = Field(..., min_length=2, max_length=2)
    validation: list[int] = Field(..., min_length=2, max_length=2)
    test: list[int] = Field(..., min_length=2, max_length=2)


class BudgetBody(BaseModel):
    proposalsPerRound: int = campaigns.MAX_PROPOSALS_PER_ROUND
    maxRounds: int = campaigns.MAX_ROUNDS
    roundDeadlineMs: int = campaigns.MAX_ROUND_MS


class CampaignBody(BaseModel):
    group: str = Field(..., min_length=1, max_length=32)
    interval: str = Field("1h", max_length=8)
    horizonBars: int = Field(24, ge=1, le=5000)
    hypothesis: str = Field(..., min_length=1, max_length=2000)
    successCriteria: str = Field(..., min_length=1, max_length=2000)
    windows: WindowBody
    budget: BudgetBody = Field(default_factory=BudgetBody)
    provider: str = Field("", max_length=64)
    agentVersion: str = Field("", max_length=120)
    mode: str = Field("deterministic_search", max_length=40)
    seed: int = Field(42, ge=0)


class ProposalBody(BaseModel):
    proposalId: str = Field(..., min_length=1, max_length=64)
    kind: str = Field("parameter_set", max_length=40)
    factorIds: list[str] = Field(default_factory=list, max_length=32)
    parameters: dict[str, float] = Field(default_factory=dict)
    rule: dict | None = None
    hypothesis: str = Field(..., min_length=1, max_length=400)
    expectedFailureMode: str = Field("", max_length=400)


class ProposalsBody(BaseModel):
    round: int = Field(..., ge=1, le=campaigns.MAX_ROUNDS)
    proposals: list[ProposalBody] = Field(..., min_length=1, max_length=campaigns.MAX_PROPOSALS_PER_ROUND)


class TrialBody(BaseModel):
    proposalId: str = Field(..., min_length=1, max_length=64)
    segment: str = Field("validation", max_length=16)
    runId: str = Field("", max_length=120)
    sharpe: float | None = None
    returnPct: float | None = None
    maxDrawdownPct: float | None = None
    trades: int | None = Field(None, ge=0)
    verdict: str = Field("", max_length=40)
    reason: str = Field("", max_length=400)


class RoundBody(BaseModel):
    round: int = Field(..., ge=1, le=campaigns.MAX_ROUNDS)
    wallclockMs: int = Field(0, ge=0)


class FinishBody(BaseModel):
    status: str = Field("completed", max_length=32)
    reason: str = Field("", max_length=400)


class UnsealBody(BaseModel):
    approvedBy: str = Field(..., min_length=1, max_length=120)


class PromoteBody(BaseModel):
    proposalId: str = Field(..., min_length=1, max_length=64)
    approvedBy: str = Field(..., min_length=1, max_length=120)
    note: str = Field("", max_length=400)


class VerdictBody(BaseModel):
    """Asking for the Gate-C judgement.

    `approvedBy` is the same human approval `unseal` takes, because judging the test
    segment *is* the one-shot unsealing; there is no anonymous way to open it.
    """

    approvedBy: str = Field(..., min_length=1, max_length=120)
    proposalId: str = Field("", max_length=64)
    runId: str = Field("", max_length=120)
    segment: str = Field("test", max_length=16)


async def _guard(action):
    """Await the work and turn a campaign refusal into the status it deserves.

    The thread-pool calls return awaitables, and a guard that forgets to await them
    hands FastAPI a coroutine to serialise - which fails as a 500 with a message
    about encoders rather than about the campaign.
    """
    try:
        result = action()
        if inspect.isawaitable(result):
            result = await result
        return result
    except CampaignError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.get("")
async def list_campaigns(limit: int = Query(50, ge=1, le=200)):
    return {"campaigns": await run_in_threadpool(campaigns.list_campaigns, _db(), limit=limit)}


@router.post("")
async def create_campaign(body: CampaignBody):
    """Pre-register a campaign: hypothesis, windows, frozen space and budget."""
    return await _guard(
        lambda: campaigns.preregister(
            _db(),
            group=body.group,
            interval=body.interval,
            horizon_bars=body.horizonBars,
            hypothesis=body.hypothesis,
            success_criteria=body.successCriteria,
            windows={
                "train": body.windows.train,
                "validation": body.windows.validation,
                "test": body.windows.test,
            },
            budget=body.budget.model_dump(),
            provider=body.provider,
            agent_version=body.agentVersion,
            mode=body.mode,
            seed=body.seed,
            home=quantdesk_home(),
        )
    )


@router.get("/{uid}")
async def campaign_detail(uid: str, include_test: bool = Query(False, alias="includeTest")):
    return await _guard(
        lambda: run_in_threadpool(
            campaigns.detail, _db(), uid, include_test=include_test
        )
    )


@router.get("/{uid}/trials")
async def campaign_trials(
    uid: str,
    include_test: bool = Query(False, alias="includeTest"),
    limit: int = Query(500, ge=1, le=2000),
):
    return {
        "trials": await _guard(
            lambda: run_in_threadpool(
                campaigns.trials_for, _db(), uid, include_test=include_test, limit=limit
            )
        )
    }


@router.get("/{uid}/ledger")
async def campaign_ledger(uid: str, limit: int = Query(200, ge=1, le=2000)):
    return {
        "ledger": await _guard(lambda: run_in_threadpool(campaigns.ledger_for, _db(), uid, limit=limit)),
        "budget": await _guard(lambda: run_in_threadpool(campaigns.budget_state, _db(), uid)),
    }


@router.post("/{uid}/rounds")
async def open_round(uid: str, body: RoundBody):
    return await _guard(
        lambda: run_in_threadpool(
            campaigns.start_round, _db(), uid,
            round_number=body.round, wallclock_ms=body.wallclockMs,
        )
    )


@router.post("/{uid}/proposals")
async def record_proposals(uid: str, body: ProposalsBody):
    return {
        "proposals": await _guard(
            lambda: run_in_threadpool(
                campaigns.record_proposals, _db(), uid,
                round_number=body.round,
                proposals=[item.model_dump() for item in body.proposals],
            )
        )
    }


@router.post("/{uid}/trials")
async def record_trial(uid: str, body: TrialBody):
    return await _guard(
        lambda: run_in_threadpool(
            campaigns.record_trial, _db(), uid,
            proposal_uid=body.proposalId, segment=body.segment, run_id=body.runId,
            sharpe=body.sharpe, return_pct=body.returnPct,
            max_drawdown_pct=body.maxDrawdownPct, trades=body.trades,
            verdict=body.verdict, reason=body.reason,
        )
    )


@router.post("/{uid}/run", status_code=202)
async def run_campaign_round(uid: str):
    """Queue the campaign's next round.

    Queued rather than run here: a round is a dozen backtests, and an HTTP handler
    that blocks for ten minutes is not an interface. The worker executes it as the
    `campaign` study kind and reports progress on the run row.
    """
    campaign = await _guard(lambda: run_in_threadpool(campaigns.get_campaign, _db(), uid))

    def submit():
        from .runs import get_run_worker

        worker = get_run_worker()
        queued = worker.queue.submit("campaign", {"campaign": uid})
        worker.wake()
        return queued

    try:
        queued = await run_in_threadpool(submit)
    except Exception as exc:  # StudyError and friends, with the queue's own message
        raise HTTPException(409, f"{type(exc).__name__}: {exc}") from exc
    return {"queued": True, "run": queued, "campaign": campaign["uid"],
            "status": campaign["status"], "roundsUsed": campaign["roundsUsed"]}


@router.post("/{uid}/finish")
async def finish_campaign(uid: str, body: FinishBody):
    return await _guard(
        lambda: run_in_threadpool(
            campaigns.finish, _db(), uid, status=body.status, reason=body.reason
        )
    )


@router.post("/{uid}/unseal")
async def unseal_campaign(uid: str, body: UnsealBody):
    """Open the sealed test window. One shot: a second call is refused."""
    return await _guard(
        lambda: run_in_threadpool(campaigns.unseal_test, _db(), uid, approved_by=body.approvedBy)
    )


@router.post("/{uid}/promote")
async def promote_proposal(uid: str, body: PromoteBody):
    """Promote one proposal to a paper candidate. Humans only (D3)."""
    return await _guard(
        lambda: run_in_threadpool(
            campaigns.promote, _db(), uid,
            proposal_uid=body.proposalId, approved_by=body.approvedBy, note=body.note,
        )
    )


@router.get("/{uid}/stats")
async def campaign_statistics(
    uid: str,
    include_test: bool = Query(False, alias="includeTest"),
    blocks: int = Query(campaign_stats.DEFAULT_BLOCKS, ge=4, le=16),
):
    """DSR and proposal-dimension PBO - the same answer before and after unsealing.

    Before the seal is broken these numbers can only have been computed from
    train/validation (the search trials and their curves), so the campaign sees
    exactly what it was allowed to see; asking for `includeTest` while sealed is
    refused here, by `campaigns.trials_for`, rather than quietly ignored.
    """
    return await _guard(
        lambda: run_in_threadpool(
            campaign_stats.campaign_stats, _db(), uid, include_test=include_test, blocks=blocks
        )
    )


@router.get("/{uid}/verdict")
async def campaign_verdict(uid: str, proposalId: str = Query("", max_length=64)):
    """The verdict already on record, or a clear reason why there is none.

    Deliberately a 200 with `judged: false` when nothing has been judged: "the test
    segment is still sealed" is the normal state of a running campaign, not an error,
    and the caller needs to be able to tell the two apart without parsing a 500.
    """
    db = _db()
    campaign = await _guard(lambda: run_in_threadpool(campaigns.get_campaign, db, uid))
    stored = await _guard(
        lambda: run_in_threadpool(campaign_stats.stored_verdict, db, uid, proposal_uid=proposalId)
    )
    if stored is None:
        return {
            "judged": False,
            "campaign": uid,
            "testSealed": campaign["testSealed"],
            "reason": (
                "测试段尚未开封（Gate-C），还没有判决：开封并判决请调用 POST "
                f"/api/campaigns/{uid}/verdict，且只能开封一次"
                if campaign["testSealed"]
                else "测试段已开封但还没有写入判决：调用 POST "
                f"/api/campaigns/{uid}/verdict 完成一次性判决"
            ),
            "verdict": None,
        }
    return {"judged": True, "campaign": uid, "testSealed": campaign["testSealed"], **stored}


@router.post("/{uid}/verdict")
async def judge_campaign(uid: str, body: VerdictBody):
    """Unseal once, judge the pre-registered criteria against the test segment once.

    Idempotent: a second call returns the row the first one wrote instead of running
    the test segment again, which is the only thing that keeps it out of sample.
    """
    db = _db()
    # The out-of-sample evaluation belongs to the orchestrator, not to the statistics:
    # open the window once, run the selected proposal once, and only then judge. A
    # second call finds the stored test trial and re-judges nothing.
    if not body.runId:
        from ..agent_campaign import ensure_test_trial

        already = await _guard(
            lambda: run_in_threadpool(campaign_stats.stored_verdict, db, uid)
        )
        if already is None:
            await _guard(
                lambda: run_in_threadpool(
                    ensure_test_trial, db, uid, approved_by=body.approvedBy, home=quantdesk_home()
                )
            )
    return await _guard(
        lambda: run_in_threadpool(
            campaign_stats.adjudicate, db, uid,
            approved_by=body.approvedBy, proposal_uid=body.proposalId,
            run_id=body.runId, segment=body.segment,
        )
    )
