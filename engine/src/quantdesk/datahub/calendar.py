"""Trading calendar for tokenised equities, and what a missing bar means.

Stock perpetuals on Bybit trade around the clock, but the contract that they track
does not: outside the underlying session the perp prints on negligible notional,
and on a market holiday it prints on almost nothing. That distinction decides how
a gap in local data should be read. A gap inside the session is missing data that
must be repaired; the same gap at 03:00 New York on a Sunday is the market being
closed, and repairing it would invent bars.

The session window is derived, not assumed: `observed_session` measures when a
symbol actually traded from the bars on hand, and `DEFAULT_EQUITY_SESSION` is the
US cash session it should line up with. Holidays are the NYSE calendar, including
the observed dates for the fixed-date ones, because a holiday the venue does not
trade is exactly the day that would otherwise look like a data outage.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc

# The US cash session the single-name and ETF perps track, in New York time.
US_SESSION_OPEN = dt.time(9, 30)
US_SESSION_CLOSE = dt.time(16, 0)
US_HALF_DAY_CLOSE = dt.time(13, 0)


@dataclass(frozen=True)
class Session:
    """One trading window for one calendar day, in UTC."""

    date: str
    open_ts: int
    close_ts: int
    half_day: bool = False

    def contains(self, stamp: int) -> bool:
        return self.open_ts <= stamp < self.close_ts

    def as_dict(self) -> dict:
        return {
            "date": self.date,
            "openTs": self.open_ts,
            "closeTs": self.close_ts,
            "halfDay": self.half_day,
        }


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """The nth given weekday of a month (weekday: Monday=0)."""
    day = dt.date(year, month, 1)
    shift = (weekday - day.weekday()) % 7
    return day + dt.timedelta(days=shift + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    if month == 12:
        day = dt.date(year, 12, 31)
    else:
        day = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    return day - dt.timedelta(days=(day.weekday() - weekday) % 7)


def _observed(day: dt.date) -> dt.date:
    """A fixed-date holiday lands on the nearest weekday when it hits a weekend."""
    if day.weekday() == 5:
        return day - dt.timedelta(days=1)
    if day.weekday() == 6:
        return day + dt.timedelta(days=1)
    return day


def us_market_holidays(year: int) -> dict[dt.date, str]:
    """NYSE holidays for one year, with the days actually observed."""
    easter = _easter_sunday(year)
    holidays = {
        _observed(dt.date(year, 1, 1)): "元旦",
        _nth_weekday(year, 1, 0, 3): "马丁·路德·金日",
        _nth_weekday(year, 2, 0, 3): "总统日",
        easter - dt.timedelta(days=2): "耶稣受难日",
        _last_weekday(year, 5, 0): "阵亡将士纪念日",
        _observed(dt.date(year, 7, 4)): "独立日",
        _nth_weekday(year, 9, 0, 1): "劳动节",
        _nth_weekday(year, 11, 3, 4): "感恩节",
        _observed(dt.date(year, 12, 25)): "圣诞节",
    }
    # A New Year's Day that falls on a Saturday closes the exchange on the Friday
    # before it, which is 31 December of the previous year. Both directions are
    # needed: 2021 opens with the 2020 observance, and 2021 ends with the 2022 one.
    if dt.date(year, 1, 1).weekday() == 5:
        holidays[dt.date(year - 1, 12, 31)] = "元旦（顺延）"
    if dt.date(year + 1, 1, 1).weekday() == 5:
        holidays[dt.date(year, 12, 31)] = "元旦前夕休市"
    return holidays


def _easter_sunday(year: int) -> dt.date:
    """Anonymous Gregorian algorithm; Good Friday is the Friday before it."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def half_days(year: int) -> dict[dt.date, str]:
    """Early closes: the day after Thanksgiving, Christmas Eve, Independence Eve.

    A half day is only a half day if the exchange is open at all: in a year when
    3 July is the observed Independence Day holiday, or 24 December is the
    observed Christmas holiday, the market is shut rather than closing early.
    """
    holidays = us_market_holidays(year)
    out: dict[dt.date, str] = {}
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    after = thanksgiving + dt.timedelta(days=1)
    if after not in holidays:
        out[after] = "感恩节次日"
    christmas_eve = dt.date(year, 12, 24)
    if christmas_eve.weekday() < 5 and christmas_eve not in holidays:
        out[christmas_eve] = "平安夜"
    july_third = dt.date(year, 7, 3)
    if july_third.weekday() < 5 and july_third not in holidays:
        out[july_third] = "独立日前夕"
    return out


def _to_utc(day: dt.date, moment: dt.time) -> int:
    local = dt.datetime.combine(day, moment, tzinfo=ET)
    return int(local.astimezone(UTC).timestamp() * 1000)


def equity_session(day: dt.date) -> Session | None:
    """The US cash session for one calendar day, or None when the market is shut."""
    if day.weekday() >= 5:
        return None
    if day in us_market_holidays(day.year):
        return None
    early = half_days(day.year).get(day)
    close = US_HALF_DAY_CLOSE if early else US_SESSION_CLOSE
    return Session(
        date=day.isoformat(),
        open_ts=_to_utc(day, US_SESSION_OPEN),
        close_ts=_to_utc(day, close),
        half_day=bool(early),
    )


def sessions_between(start_ts: int, end_ts: int) -> list[Session]:
    """Every session that overlaps a window, oldest first."""
    if end_ts < start_ts:
        start_ts, end_ts = end_ts, start_ts
    first = dt.datetime.fromtimestamp(start_ts / 1000, UTC).astimezone(ET).date() - dt.timedelta(days=1)
    last = dt.datetime.fromtimestamp(end_ts / 1000, UTC).astimezone(ET).date() + dt.timedelta(days=1)
    out: list[Session] = []
    day = first
    while day <= last:
        session = equity_session(day)
        if session and session.close_ts > start_ts and session.open_ts < end_ts:
            out.append(session)
        day += dt.timedelta(days=1)
    return out


def is_session_open(stamp: int) -> bool:
    moment = dt.datetime.fromtimestamp(stamp / 1000, UTC).astimezone(ET)
    session = equity_session(moment.date())
    return bool(session and session.contains(stamp))


def classify_timestamp(stamp: int) -> tuple[str, str]:
    """Why a bar may legitimately be absent at this moment.

    Returns `(kind, reason)` where kind is `open`, `off_hours`, `weekend` or
    `holiday`. Only `open` means a missing bar is a data defect.
    """
    moment = dt.datetime.fromtimestamp(stamp / 1000, UTC).astimezone(ET)
    day = moment.date()
    holiday = us_market_holidays(day.year).get(day)
    if holiday:
        return "holiday", f"{day.isoformat()} 美股休市（{holiday}）"
    if day.weekday() >= 5:
        return "weekend", f"{day.isoformat()} 周末"
    session = equity_session(day)
    if session and session.contains(stamp):
        return "open", ""
    if session:
        if stamp < session.open_ts:
            return "off_hours", "开盘前"
        return "off_hours", "收盘后"
    return "off_hours", "非交易时段"


@dataclass
class ObservedSession:
    """When a symbol actually traded, measured from its own bars."""

    symbol: str
    bars: int
    by_hour_utc: dict[int, float]
    peak_hour_utc: int | None
    active_hours_utc: list[int]
    thin_hours_utc: list[int]
    measured: bool

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "bars": self.bars,
            "peakHourUtc": self.peak_hour_utc,
            "activeHoursUtc": self.active_hours_utc,
            "thinHoursUtc": self.thin_hours_utc,
            "measured": self.measured,
        }


def observed_session(
    candles: list[dict],
    symbol: str,
    *,
    thin_ratio: float = 0.10,
    min_bars: int = 12,
) -> ObservedSession:
    """Measure the trading window from the bars, instead of assuming one.

    A stock perp prints around the clock, so "is the market open" cannot be read
    from the presence of a bar. It can be read from where the notional is: hours
    that trade under a tenth of the symbol's own peak are off-hours, whatever the
    calendar says.

    The measurement needs several bars per hour bucket to mean anything - a single
    day of hourly bars gives one observation per bucket, where every hour looks
    equally important. Too short a sample is reported as unmeasured rather than
    returned as a confident answer.
    """
    buckets: dict[int, list[float]] = {}
    for row in candles:
        moment = dt.datetime.fromtimestamp(int(row["ts"]) / 1000, UTC)
        notional = abs(float(row.get("volume") or 0)) * float(row.get("close") or 0)
        buckets.setdefault(moment.hour, []).append(notional)
    if len(candles) < min_bars or not buckets:
        return ObservedSession(symbol, len(candles), {}, None, [], [], measured=False)
    # The busiest bar of each hour is the robust statistic here: with a handful of
    # bars per bucket a median can land on the quiet side of an active hour, while
    # the maximum stays on the side that actually traded.
    busiest = {hour: max(values) for hour, values in buckets.items()}
    peak_hour = max(busiest, key=lambda hour: busiest[hour])
    peak = busiest[peak_hour]
    active = sorted(hour for hour, value in busiest.items() if value >= peak * thin_ratio)
    thin = sorted(hour for hour, value in busiest.items() if value < peak * thin_ratio)
    return ObservedSession(
        symbol=symbol,
        bars=len(candles),
        by_hour_utc={hour: round(value, 4) for hour, value in busiest.items()},
        peak_hour_utc=peak_hour,
        active_hours_utc=active,
        thin_hours_utc=thin,
        measured=True,
    )
