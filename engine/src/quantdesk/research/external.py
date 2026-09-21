"""The evidence standardisation layer: fetch, gate, cache, standardise.

External research (OpenBB) is optional enrichment. Three rules shape this module:

1. **A reading is only usable if we know when it was published.** A financial
   statement's period end is not its publication date, and a macro figure has a
   first release and later revisions. Historical analysis must not see anything
   that did not exist on the date being judged.
2. **Nothing is invented.** A provider that has no data produces an `unavailable`
   record naming the reason; it never produces an empty or substituted value. The
   real provider is always recorded - never the word "openbb".
3. **A failure is contained.** A provider that errors is reported, the configured
   fallback is tried, and the research run continues with less evidence rather
   than failing. The caller learns it is degraded.

The fetcher is injected, so this layer never talks to a plugin or the network
itself and can be tested without either.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# Where an evidence record stands. `unavailable` is a first-class outcome: "the
# provider had nothing for this instrument" is information, not an error.
STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"
STATUS_REJECTED = "rejected"
STATUS_ERROR = "error"

# How a macro figure came to exist. A revision published later must not be
# visible to a judgement made earlier.
RELEASE_FIRST = "first"
RELEASE_REVISION = "revision"
RELEASE_UNKNOWN = "unknown"

# Topic -> the key in `[openbb.providers]`. They are not always the same word: the
# report names the topic `company_profile` and configures it as `profile`, and
# assuming one implied the other meant that topic was never fetched at all.
PROVIDER_CONFIG_KEYS = {
    "company_profile": "profile",
}


def provider_config_key(topic: str) -> str:
    return PROVIDER_CONFIG_KEYS.get(topic, topic)


# Topics whose readings describe a future event and therefore have no publication
# time of their own (an economic calendar entry for next Friday).
TOPICS_WITHOUT_PUBLICATION_TIME = frozenset({"macro_calendar"})

EVIDENCE_VERSION = "evidence/1"

# Default cache windows, in minutes, used when the operator has not configured a
# topic. Config wins; this is only a floor so a missing key cannot mean "cache
# forever".
DEFAULT_TTL_MINUTES = {
    "company_profile": 7 * 24 * 60,
    "fundamentals": 24 * 60,
    "financial_growth": 24 * 60,
    "earnings": 12 * 60,
    "filings": 3 * 24 * 60,
    "company_events": 12 * 60,
    "news": 30,
    "short_interest": 24 * 60,
    "institutional_ownership": 24 * 60,
    "macro": 12 * 60,
    "macro_calendar": 3 * 60,
}
DEFAULT_TTL_FALLBACK_MINUTES = 60


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def content_hash(payload: Any) -> str:
    """A stable hash of a reading, so an unchanged document is reused."""
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def parameters_hash(parameters: dict | None) -> str:
    return hashlib.sha256(_canonical(parameters or {}).encode("utf-8")).hexdigest()[:16]


def cache_key_for(
    *, provider: str, endpoint: str, symbol: str, as_of: str | None, parameters: dict | None = None
) -> str:
    """provider + endpoint + symbol + as_of + parameters, hashed.

    The parameters are part of the key on purpose: two calls for the same symbol
    with different limits or windows are different questions.
    """
    material = "|".join(
        [
            EVIDENCE_VERSION,
            provider.strip().lower(),
            endpoint.strip().lower(),
            symbol.strip().upper(),
            (as_of or "").strip(),
            parameters_hash(parameters),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def ttl_minutes(topic: str, external_config: dict | None = None) -> int:
    """The cache window for a topic, from config when present."""
    configured = ((external_config or {}).get("cache_ttl_minutes") or {})
    value = configured.get(topic)
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    return DEFAULT_TTL_MINUTES.get(topic, DEFAULT_TTL_FALLBACK_MINUTES)


def _parse_instant(raw: str | None) -> dt.datetime | None:
    """Parse an ISO-8601 instant or date. Returns None when it is not one."""
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _parse_date(raw: str | None) -> dt.date | None:
    instant = _parse_instant(raw)
    return instant.date() if instant else None


def macro_release_kind(payload: Any) -> str:
    """First release or a revision, when the payload says so.

    Providers differ in how they mark this, so the shapes we know are recognised
    and anything else is `unknown` - which the point-in-time gate then treats as
    unusable for a historical judgement rather than assuming it is a first print.
    """
    if not isinstance(payload, dict):
        return RELEASE_UNKNOWN
    for key in ("release", "releaseKind", "release_kind"):
        value = str(payload.get(key) or "").strip().lower()
        if value in {RELEASE_FIRST, RELEASE_REVISION}:
            return value
    if payload.get("isRevision") is True or payload.get("is_revision") is True:
        return RELEASE_REVISION
    if payload.get("isRevision") is False or payload.get("is_revision") is False:
        return RELEASE_FIRST
    for key in ("vintage", "vintageDate"):
        if payload.get(key):
            return RELEASE_REVISION
    return RELEASE_UNKNOWN


@dataclass(frozen=True)
class PointInTimeDecision:
    """Whether a reading may be used for the date being judged, and why."""

    accepted: bool
    reason: str = ""
    basis: str = ""

    def as_dict(self) -> dict:
        return {"accepted": self.accepted, "reason": self.reason, "basis": self.basis}


def check_point_in_time(
    *,
    topic: str,
    trade_date: str,
    as_of: str | None,
    published_at: str | None,
    observed_at: str | None,
    payload: Any = None,
    required: bool = True,
) -> PointInTimeDecision:
    """Gate one reading against the date it would be used to judge.

    `trade_date` is the day the analysis speaks about. Anything published after
    the end of that day did not exist yet, and using it would be look-ahead.
    """
    if topic in TOPICS_WITHOUT_PUBLICATION_TIME:
        # A calendar entry is a scheduled future event; its own "publication" is
        # the schedule itself, so the trade date is the only sensible bound.
        return PointInTimeDecision(True, basis="calendar", reason="未来日历事件，无发布时间")

    judged = _parse_date(trade_date)
    if judged is None:
        return PointInTimeDecision(False, reason=f"研判日期无法解析：{trade_date!r}", basis="tradeDate")

    published = _parse_instant(published_at)
    if published is None:
        if required:
            # "不确定公布时间的数据不得进入历史策略验证" - an undated reading
            # cannot be shown to predate the judgement, so it cannot be evidence.
            return PointInTimeDecision(
                False,
                reason="缺少可解析的发布时间，无法确认数据早于研判日期",
                basis="publishedAt",
            )
        return PointInTimeDecision(True, basis="publishedAt", reason="未要求时点校验")

    period_end = _parse_date(as_of)
    if period_end is not None and published.date() == period_end:
        # A filing's period end is not its publication date. Accepting it would
        # make a quarter's results visible on the last day of that quarter.
        return PointInTimeDecision(
            False,
            reason=f"发布时间与财务期末同一天（{period_end.isoformat()}），无法区分公布时间与期末日期",
            basis="publishedAt=asOf",
        )

    observed = _parse_instant(observed_at)

    # The substantive check comes first: "this was not public yet" is the reason
    # the reader needs, and a reading fetched for a past date will often trip the
    # consistency check below as a side effect of the same fact.
    if published.date() > judged:
        return PointInTimeDecision(
            False,
            reason=f"该数据发布于研判日期之后（{published.date().isoformat()} > {judged.isoformat()}）",
            basis="publishedAt>tradeDate",
        )

    if observed is not None and observed < published:
        return PointInTimeDecision(
            False,
            reason="观测时间早于发布时间，时间戳自相矛盾",
            basis="observedAt<publishedAt",
        )

    if topic == "macro":
        release = macro_release_kind(payload)
        if release == RELEASE_REVISION and published.date() > judged:
            return PointInTimeDecision(
                False, reason="修订数据晚于研判日期", basis="macroRevision"
            )
        return PointInTimeDecision(True, basis=f"macro:{release}", reason="")

    return PointInTimeDecision(True, basis="publishedAt", reason="")


@dataclass
class EvidenceRecord:
    """One standardised reading, ready to store or to cite."""

    key: str
    label: str
    value: Any
    source: str
    provider: str
    endpoint: str
    symbol: str
    topic: str
    observed_at: str
    as_of: str = ""
    published_at: str = ""
    expires_at: str = ""
    point_in_time: bool = False
    content_hash: str = ""
    status: str = STATUS_OK
    warnings: list[str] = field(default_factory=list)
    cache_key: str = ""
    # The key of the request this reading came from. A request yields several
    # readings, so the cache is looked up by this rather than by the per-reading
    # key - which is exactly the bug that made every run refetch.
    request_key: str = ""
    cached: bool = False

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "source": self.source,
            "provider": self.provider,
            "endpoint": self.endpoint,
            "asOf": self.as_of,
            "publishedAt": self.published_at,
            "observedAt": self.observed_at,
            "expiresAt": self.expires_at,
            "contentHash": self.content_hash,
            "pointInTime": self.point_in_time,
            "warnings": list(self.warnings),
        }

    def as_store_row(self) -> dict:
        return {
            "cache_key": self.cache_key,
            "request_key": self.request_key,
            "provider": self.provider,
            "endpoint": self.endpoint,
            "symbol": self.symbol,
            "topic": self.topic,
            "as_of": self.as_of or None,
            "published_at": self.published_at or None,
            "observed_at": self.observed_at,
            "expires_at": self.expires_at or None,
            "source_url": self.source,
            # An absent reading stores no payload at all: an empty object would
            # read as "the provider said {}" instead of "the provider had nothing".
            "payload_json": None if self.value is None else _canonical(self.value),
            "content_hash": self.content_hash,
            "point_in_time": self.point_in_time,
            "status": self.status,
            "warning": "; ".join(self.warnings) or None,
        }


@dataclass
class EvidenceBundle:
    """What the research layer receives: usable readings plus what is missing."""

    symbol: str
    trade_date: str
    # Whether external research was switched on for this run. A feature the
    # operator turned off is not a degradation - flagging it as one would teach
    # readers to ignore the label that matters.
    external_enabled: bool = True
    evidence: list[EvidenceRecord] = field(default_factory=list)
    unavailable: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    cache_hits: int = 0
    calls: int = 0

    @property
    def usable(self) -> list[EvidenceRecord]:
        return [item for item in self.evidence if item.status == STATUS_OK]

    @property
    def degraded(self) -> bool:
        """True when an enabled run is missing something it asked for.

        Every consumer of this bundle must say so in its output: research that
        silently loses its fundamentals is research that overstates its basis.
        """
        if not self.external_enabled:
            return False
        return bool(self.unavailable or self.rejected or self.errors)

    def topics_present(self) -> list[str]:
        return sorted({item.topic for item in self.usable})

    def summary(self) -> dict:
        return {
            "symbol": self.symbol,
            "tradeDate": self.trade_date,
            "usable": len(self.usable),
            "topics": self.topics_present(),
            "unavailable": self.unavailable,
            "rejected": self.rejected,
            "errors": self.errors,
            "cacheHits": self.cache_hits,
            "calls": self.calls,
            "enabled": self.external_enabled,
            "degraded": self.degraded,
        }


def _unavailable_reason(payload: dict | None) -> str:
    """Read an unavailable answer, whichever shape the adapter phrased it in."""
    if not payload:
        return "Provider 未返回该标的的数据"
    summary = payload.get("unavailableReason")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    raw = payload.get("unavailable")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if isinstance(raw, list):
        parts = [
            f"{item.get('provider') or '未知 Provider'}：{item.get('reason') or '无原因'}"
            for item in raw
            if isinstance(item, dict)
        ]
        if parts:
            return "；".join(parts)
    return "Provider 未返回该标的的数据"


def provider_plan(topic: str, openbb_config: dict | None = None) -> list[str]:
    """The providers to try for a topic, primary first.

    The operator names the provider per interface. When an interface has no
    configured provider the plan is empty and the caller reports that rather than
    silently reaching for a default: an unknown provider is a policy decision, not
    a fallback.
    """
    config = openbb_config or {}
    providers = config.get("providers") or {}
    fallbacks = config.get("fallbacks") or {}
    plan: list[str] = []
    key = provider_config_key(topic)
    primary = str(providers.get(key) or "").strip()
    if primary:
        plan.append(primary)
    for item in fallbacks.get(key) or []:
        name = str(item).strip()
        if name and name not in plan:
            plan.append(name)
    return plan


def point_in_time_required(topic: str, openbb_config: dict | None = None) -> bool:
    policy = ((openbb_config or {}).get("point_in_time") or {})
    if topic in (policy.get("exempt") or []):
        return False
    return bool(policy.get("default", True))


class ExternalEvidenceService:
    """Cache and point-in-time gate in front of an injected evidence fetcher."""

    def __init__(self, db, external_config: dict | None = None, openbb_config: dict | None = None, *, now=None):
        self.db = db
        self.external_config = external_config or {}
        self.openbb_config = openbb_config or {}
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))

    # -- cache -----------------------------------------------------------
    def _is_fresh(self, row: dict, ttl: int) -> bool:
        if ttl <= 0:
            return False
        updated = int(row.get("updated_ts") or 0)
        age_minutes = (self._now().timestamp() * 1000 - updated) / 60_000
        return age_minutes < ttl

    def _record_from_row(self, row: dict) -> EvidenceRecord:
        try:
            payload = json.loads(row.get("payload_json") or "null")
        except (TypeError, ValueError):
            payload = None
        return EvidenceRecord(
            key=row.get("cache_key") or "",
            label=row.get("topic") or "",
            value=payload,
            source=row.get("source_url") or "",
            provider=row.get("provider") or "",
            endpoint=row.get("endpoint") or "",
            symbol=row.get("symbol") or "",
            topic=row.get("topic") or "",
            observed_at=row.get("observed_at") or "",
            as_of=row.get("as_of") or "",
            published_at=row.get("published_at") or "",
            expires_at=row.get("expires_at") or "",
            point_in_time=bool(row.get("point_in_time")),
            content_hash=row.get("content_hash") or "",
            status=row.get("status") or STATUS_OK,
            warnings=[item for item in str(row.get("warning") or "").split("; ") if item],
            cache_key=row.get("cache_key") or "",
            cached=True,
        )

    def request_key(self, *, provider: str, endpoint: str, symbol: str, as_of: str | None) -> str:
        """The key for one (provider, endpoint, symbol, as-of) request.

        Readings produced by that request carry it alongside their own unique key,
        which is what makes a cache lookup possible without knowing in advance
        which readings the provider is going to return.
        """
        return cache_key_for(provider=provider, endpoint=endpoint, symbol=symbol, as_of=as_of)

    def cached(
        self, *, provider: str, endpoint: str, symbol: str, topic: str, as_of: str | None
    ) -> list[EvidenceRecord] | None:
        """Fresh readings already stored for this request, if any."""
        rows = self.db.list_external_evidence_by_request(
            self.request_key(provider=provider, endpoint=endpoint, symbol=symbol, as_of=as_of)
        )
        if not rows:
            return None
        ttl = ttl_minutes(topic, self.external_config)
        # A negative result is cached too, but only for the topic's window: a name
        # that had no filings yesterday may have them today.
        fresh = [row for row in rows if self._is_fresh(row, ttl)]
        if not fresh:
            return None
        return [self._record_from_row(row) for row in fresh]

    def store(self, record: EvidenceRecord) -> None:
        """Write one reading, timestamped by this service's clock.

        The freshness check compares against `updated_ts`, so the write and the
        check have to use the same clock: a store that stamps wall time while the
        service reasons about a supplied `now` would never expire anything.
        """
        row = record.as_store_row()
        row["updated_ts"] = int(self._now().timestamp() * 1000)
        self.db.upsert_external_evidence(row)

    # -- collection ------------------------------------------------------
    def collect(
        self,
        *,
        symbol: str,
        trade_date: str,
        topics: Iterable[str],
        fetcher: Callable[[str, str, str], dict],
    ) -> EvidenceBundle:
        """Fetch, gate, standardise and cache each topic for one contract.

        `fetcher(provider, topic, symbol)` returns either
        `{"evidence": [...]}` with provider-shaped readings, `{"unavailable": reason}`
        or raises. Whatever happens, the bundle says which topics are missing and
        why - a research run continues on the evidence it has.
        """
        bundle = EvidenceBundle(symbol=symbol, trade_date=trade_date)
        for topic in topics:
            plan = provider_plan(topic, self.openbb_config)
            if not plan:
                bundle.unavailable.append(
                    {"topic": topic, "provider": "", "reason": "该接口未配置 Provider，未发起调用"}
                )
                continue
            resolved = False
            for index, provider in enumerate(plan):
                endpoint = f"{topic}:{provider}"
                request_key = self.request_key(
                    provider=provider, endpoint=endpoint, symbol=symbol, as_of=trade_date
                )
                hit = self.cached(
                    provider=provider, endpoint=endpoint, symbol=symbol, topic=topic, as_of=trade_date
                )
                if hit is not None:
                    bundle.cache_hits += 1
                    usable = [record for record in hit if record.status == STATUS_OK]
                    bundle.evidence.extend(usable)
                    if not usable:
                        bundle.unavailable.append(
                            {
                                "topic": topic,
                                "provider": provider,
                                "reason": hit[0].warnings[0] if hit[0].warnings else "缓存记录为不可用",
                                "cached": True,
                            }
                        )
                    resolved = True
                    break
                bundle.calls += 1
                try:
                    payload = fetcher(provider, topic, symbol)
                except Exception as exc:  # noqa: BLE001 - a provider failure is contained
                    reason = f"{type(exc).__name__}: {exc}"
                    bundle.errors.append({"topic": topic, "provider": provider, "reason": reason})
                    self.store(
                        EvidenceRecord(
                            key=f"{symbol}.{topic}.{provider}", label=topic, value=None, source="",
                            provider=provider, endpoint=endpoint, symbol=symbol, topic=topic,
                            observed_at=self._now().isoformat().replace("+00:00", "Z"),
                            status=STATUS_ERROR, cache_key=cache_key_for(
                                provider=provider, endpoint=endpoint, symbol=symbol, as_of=trade_date
                            ), request_key=request_key, warnings=[reason],
                        )
                    )
                    continue
                if not payload or payload.get("unavailable"):
                    reason = _unavailable_reason(payload)
                    bundle.unavailable.append({"topic": topic, "provider": provider, "reason": reason})
                    self.store(
                        EvidenceRecord(
                            key=f"{symbol}.{topic}.{provider}", label=topic, value=None, source="",
                            provider=provider, endpoint=endpoint, symbol=symbol, topic=topic,
                            observed_at=self._now().isoformat().replace("+00:00", "Z"),
                            status=STATUS_UNAVAILABLE, cache_key=cache_key_for(
                                provider=provider, endpoint=endpoint, symbol=symbol, as_of=trade_date
                            ), request_key=request_key, warnings=[reason],
                        )
                    )
                    resolved = True
                    break
                records, rejections = self.standardise(
                    payload=payload, provider=provider, endpoint=endpoint, symbol=symbol,
                    topic=topic, trade_date=trade_date, request_key=request_key,
                )
                for record in records:
                    bundle.evidence.append(record)
                bundle.rejected.extend(rejections)
                resolved = True
                break
            if not resolved:
                bundle.unavailable.append(
                    {
                        "topic": topic,
                        "provider": plan[0] if plan else "",
                        "reason": f"全部 Provider 均失败或不可用（尝试：{', '.join(plan)}）",
                    }
                )
        return bundle

    # -- standardisation -------------------------------------------------
    def standardise(
        self,
        *,
        payload: dict,
        provider: str,
        endpoint: str,
        symbol: str,
        topic: str,
        trade_date: str,
        request_key: str = "",
    ) -> tuple[list[EvidenceRecord], list[dict]]:
        """Turn provider readings into standardised, point-in-time-checked records."""
        required = point_in_time_required(topic, self.openbb_config)
        observed_at = str(payload.get("observedAt") or self._now().isoformat().replace("+00:00", "Z"))
        ttl = ttl_minutes(topic, self.external_config)
        expires_at = str(
            payload.get("expiresAt")
            or (self._now() + dt.timedelta(minutes=ttl)).isoformat().replace("+00:00", "Z")
        )
        accepted: list[EvidenceRecord] = []
        rejected: list[dict] = []
        for index, item in enumerate(payload.get("evidence") or []):
            if not isinstance(item, dict):
                continue
            item_topic = str(item.get("topic") or topic)
            value = item.get("value")
            published_at = str(item.get("publishedAt") or "")
            as_of = str(item.get("asOf") or "")
            decision = check_point_in_time(
                topic=item_topic,
                trade_date=trade_date,
                as_of=as_of or None,
                published_at=published_at or None,
                observed_at=observed_at,
                payload=value,
                required=required,
            )
            if not decision.accepted:
                rejected.append(
                    {
                        "topic": item_topic,
                        "provider": provider,
                        "key": item.get("key") or f"{symbol}.{item_topic}.{index}",
                        "reason": decision.reason,
                        "basis": decision.basis,
                        "publishedAt": published_at,
                    }
                )
                continue
            record = EvidenceRecord(
                key=str(item.get("key") or f"{symbol}.{item_topic}.{index}"),
                label=str(item.get("label") or item_topic),
                value=value,
                source=str(item.get("source") or payload.get("source") or ""),
                provider=provider,
                endpoint=str(item.get("endpoint") or endpoint),
                symbol=symbol,
                topic=item_topic,
                observed_at=observed_at,
                as_of=as_of,
                published_at=published_at,
                expires_at=str(item.get("expiresAt") or expires_at),
                point_in_time=bool(item.get("pointInTime", required)),
                content_hash=content_hash(value),
                status=STATUS_OK,
                warnings=[str(entry) for entry in item.get("warnings") or []],
                cache_key=cache_key_for(
                    provider=provider, endpoint=endpoint, symbol=symbol, as_of=trade_date,
                    parameters={"key": item.get("key"), "topic": item_topic},
                ),
                request_key=request_key or self.request_key(
                    provider=provider, endpoint=endpoint, symbol=symbol, as_of=trade_date
                ),
            )
            self.store(record)
            accepted.append(record)
        return accepted, rejected

    def prune(self) -> dict:
        retention = (self.external_config.get("retention_days") or {})
        evidence_days = int(retention.get("evidence") or 400)
        now_ms = int(self._now().timestamp() * 1000)
        return {
            "evidence": self.db.prune_external_evidence(max_age_days=evidence_days, now_ms=now_ms)
        }
