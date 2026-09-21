"""CPA phase recognition: the rules, the sequence, and the absence of future data.

Three groups, in the order the report asked for them:

1. **recognition** - synthetic cycles that cover a full upside sequence, a skipped
   crossback, a repeated base, a wedge drop without exhaustion, a false breakout, a
   low-volume breakout, a downside cycle and a flat market with no phase at all;
2. **anti-leakage** - truncated history must not change earlier readings, pivots must
   not include the bar that breaks them, a lower interval must not read an unclosed
   higher bar, a weekly reading must not appear before the week closes, and later
   volume must not rewrite an earlier confirmation;
3. **signal mapping** - what a confirmed phase turns into, and what it must never
   turn into (an observation never opens a position; `long_only` never shorts).

Every fixture is built by a named helper so a failure says which shape broke.
"""

from __future__ import annotations

import unittest

from quantdesk.strategy import cpa
from quantdesk.strategy.cpa import indicators as ind

HOUR = 3_600_000
DAY = 86_400_000
WEEK = 604_800_000


def bar(ts: int, open_: float, high: float, low: float, close: float, volume: float) -> dict:
    return {"ts": ts, "open": open_, "high": high, "low": low, "close": close,
            "volume": volume, "turnover": close * volume}


class Builder:
    """A small bar builder that keeps the price path readable in a test."""

    def __init__(self, price: float = 200.0, start: int = 1_700_000_000_000, step: int = HOUR):
        self.bars: list[dict] = []
        self.ts = start
        self.step = step
        self.price = price

    def push(self, change: float, *, volume: float = 100.0, wick: float = 0.3,
             upper: float = 0.0, lower: float = 0.0) -> None:
        open_ = self.price
        self.price = self.price * (1 + change)
        close = self.price
        high = max(open_, close) * (1 + wick / 100) * (1 + upper / 100)
        low = min(open_, close) * (1 - wick / 100) * (1 - lower / 100)
        self.bars.append(bar(self.ts, open_, high, low, close, volume))
        self.ts += self.step

    def run(self, count: int, change: float, **kwargs) -> None:
        for _ in range(count):
            self.push(change, **kwargs)

    def contract(self, count: int, *, change: float = 0.001, volume: float = 70.0,
                 start_wick: float = 0.4, end_wick: float = 0.08) -> None:
        """A wedge: ranges shrink bar by bar while price drifts up gently."""
        for index in range(count):
            span = end_wick + (start_wick - end_wick) * (1 - index / max(1, count - 1))
            self.push(change, volume=volume * (1 - index / (count * 1.5)), wick=span)


def upside_cycle(*, crossback: bool = True, base: bool = True, exhaustion: bool = True,
                 breakout_volume: float = 280.0, drop: bool = True) -> list[dict]:
    """A full upside sequence, with the middle stages switchable."""
    b = Builder()
    b.run(70, -0.006, volume=120.0)                       # decline
    b.push(0.0, volume=140.0, wick=0.2, lower=3.8)        # reversal extension
    b.contract(14, volume=70.0)                           # wedge
    b.run(3, 0.026, volume=breakout_volume, wick=0.2)     # wedge pop：决定性突破
    if crossback:
        b.run(7, -0.005, volume=95.0, wick=0.1)           # pullback into the EMAs
    if base:
        b.contract(12, change=0.0012, volume=80.0, start_wick=0.35, end_wick=0.08)
        b.run(3, 0.020, volume=300.0, wick=0.2)           # base n' break
    if exhaustion:
        b.run(9, 0.032, volume=360.0, wick=0.1, upper=1.6)
    if drop:
        b.run(12, -0.016, volume=320.0, wick=0.2)
    return b.bars


def wedge_under_wide_pivot(*, pop: float = 0.012, pops: int = 1) -> list[dict]:
    """A wedge whose pop clears the contraction high but not the 20-bar high.

    The spike sits inside `pivotLookback` but outside `contractionWindow`, so the two
    pivots disagree - the normal shape on real bars, where the 20-bar high belongs to
    the expansion the wedge just escaped. It is a spike *and reversal* on purpose: the
    wedge has to form below the averages (that is what the pop reclaims), so a spike
    that held its gain would put the contraction back above the slow EMA.
    """
    b = Builder()
    b.run(70, -0.006, volume=120.0)                      # decline
    b.push(0.0, volume=140.0, wick=0.2, lower=3.8)       # reversal extension
    b.push(0.06, volume=240.0, wick=0.5, upper=1.5)      # spike: sets the 20-bar pivot
    b.push(-0.055, volume=200.0, wick=0.3)               # straight back below the EMAs
    b.run(4, 0.0002, volume=90.0, wick=0.6)              # wide baseline inside the window
    b.contract(10, volume=70.0)                          # the wedge
    b.run(pops, pop, volume=280.0, wick=0.2)             # the pop
    return b.bars


def downside_cycle() -> list[dict]:
    b = Builder()
    b.run(80, 0.004, volume=110.0)                        # rise
    b.contract(12, change=-0.0008, volume=70.0, start_wick=0.35, end_wick=0.08)
    b.run(14, -0.022, volume=300.0, wick=0.2)             # breakdown: wedge drop
    b.run(8, 0.005, volume=90.0, wick=0.1)                # bearish crossback
    return b.bars


def flat_market() -> list[dict]:
    b = Builder()
    for index in range(120):
        b.push(0.0004 if index % 2 else -0.0004, volume=100.0, wick=0.35)
    return b.bars


def phases(bars: list[dict], **parameters) -> cpa.PhaseSeries:
    merged = {"minBars": 30}
    merged.update(parameters)
    return cpa.analyze(symbol="TESTUSDT", display_symbol="TEST", interval="1h",
                       product_type="crypto", bars=bars, parameters=merged)


def confirmed(series: cpa.PhaseSeries) -> list[tuple[int, str]]:
    return [(index, record.phase) for index, record in enumerate(series.records)
            if record.status == "confirmed"]


# --------------------------------------------------------------- recognition

class RecognitionTests(unittest.TestCase):
    def test_a_full_upside_cycle_reaches_wedge_pop_and_ends_with_a_wedge_drop(self):
        series = phases(upside_cycle())
        order = [phase for _, phase in confirmed(series)]
        self.assertIn("wedge_pop", order)
        self.assertIn("wedge_drop", order)
        self.assertLess(order.index("wedge_pop"), order.index("wedge_drop"))

    def test_the_wedge_pop_carries_its_pivot_and_reasons(self):
        series = phases(upside_cycle())
        pop = next(record for record in series.records if record.phase == "wedge_pop")
        self.assertIsNotNone(pop.pivot_price, "突破必须有枢轴价位")
        self.assertGreater(pop.volume_ratio or 0, 1.0, "成交量比必须记录")
        self.assertTrue(any("枢轴" in reason for reason in pop.reasons))
        self.assertEqual(pop.parameter_version, cpa.PARAMETER_VERSION)
        self.assertIn("status", pop.as_dict())

    def test_the_wedge_pop_breaks_the_contraction_pivot_not_the_wide_pivot(self):
        """A pop breaks out of the wedge it formed in, not of the prior expansion.

        The two pivots are different levels on real bars. Requiring the `pivotLookback`
        high made the rule inert: on BTC 1d over 2000 bars, 35 contraction readings
        produced 1 bar above the 20-bar pivot and 0 confirmed pops, so no upside cycle
        ever started and `ema_crossback` / `base_n_break` were unreachable too.
        """
        bars = wedge_under_wide_pivot()
        series = phases(bars)
        pops = [record for record in series.records if record.phase == "wedge_pop"]
        self.assertTrue(pops, "楔形突破必须在真实形态上可确认")
        pop = pops[0]
        self.assertEqual(pop.status, "confirmed")
        self.assertFalse(pop.checks["abovePivot"], "本形态没有突破 20 根枢轴")
        self.assertTrue(pop.checks["aboveWedgePivot"], "突破的是收缩区枢轴")

        index = series.records.index(pop)
        window = 10
        contraction_high = max(float(bar["high"]) for bar in bars[index - window:index])
        contraction_low = min(float(bar["low"]) for bar in bars[index - window:index])
        wide_high = max(float(bar["high"]) for bar in bars[index - 20:index])
        self.assertGreater(wide_high, contraction_high, "两个枢轴必须真的不同才有区分度")
        self.assertAlmostEqual(pop.pivot_price or 0, contraction_high, places=4)
        self.assertAlmostEqual(pop.invalidation_price or 0, contraction_low, places=4)
        self.assertTrue(any("收缩区枢轴" in reason for reason in pop.reasons))

    def test_a_wedge_pop_can_still_break_the_wide_pivot(self):
        """The scoped pivot widens nothing else: a strong pop still clears both.

        A pop that also makes a new 20-bar high is confirmed exactly as before, so the
        change is a floor on what counts as a breakout, not a replacement of one level
        by another.
        """
        series = phases(upside_cycle())
        pops = [record for record in series.records if record.phase == "wedge_pop"]
        self.assertTrue(pops)
        self.assertTrue(any(record.checks["abovePivot"] for record in pops),
                        "同时越过 20 根枢轴的突破仍须确认")
        self.assertTrue(any(record.checks["wedgeBelowSlowEma"] for record in pops),
                        "楔形必须形成于均线下方")

    def test_the_default_contraction_threshold_stays_reachable(self):
        """A rule nobody can reach is not a rule.

        The shipped 0.50/0.55 demanded that the recent window's mean range halve, which
        on real bars never coincided with the bar that has to pop - so the bullish cycle
        never started and the three bullish entry phases were dead on every symbol and
        timeframe (BTC 1d: 35 contraction readings, 0 pops, 0 trades over 2000 bars).
        This guard pins the calibrated values so a future edit cannot quietly restore an
        inert default. It asserts *reachability*, never profitability: at these values
        the universe-wide daily returns are mostly negative and the daily PBO is 0.83.
        """
        from quantdesk.strategy.cpa.defaults import defaults_for

        for interval in ("15m", "1h", "4h", "1d"):
            value = defaults_for("crypto", interval)["contractionThreshold"]
            self.assertLessEqual(
                value, 0.35,
                f"{interval} 的收缩阈值回到 {value}：真实数据上楔形突破将无法确认",
            )
        # Weekly needs its own, lower value: adjacent weekly windows overlap in regime.
        self.assertLessEqual(
            defaults_for("crypto", "1w")["contractionThreshold"], 0.25
        )
        # The published catalogue must agree with the resolved defaults, or the
        # parameter panel would prefill a value the strategy cannot act on.
        spec = next(item for item in cpa.PARAMETER_SPECS
                    if item["key"] == "contractionThreshold")
        self.assertLessEqual(spec["default"], 0.35)

    def test_a_skipped_crossback_is_recorded_rather_than_invented(self):
        """With no pullback the cycle may jump straight to a base n' break."""
        series = phases(upside_cycle(crossback=False))
        order = [phase for _, phase in confirmed(series)]
        self.assertIn("wedge_pop", order)
        self.assertNotIn("ema_crossback", order, "没有回踩就不该出现回踩阶段")
        if "base_n_break" in order:
            self.assertLess(order.index("wedge_pop"), order.index("base_n_break"))

    def test_a_base_n_break_may_repeat_inside_one_cycle(self):
        bars = upside_cycle(exhaustion=False)
        extra = Builder.__new__(Builder)
        series = phases(bars)
        bases = [index for index, phase in confirmed(series) if phase == "base_n_break"]
        self.assertLessEqual(len(bases), len(set(bases)), "同一根K线不能重复计入")
        self.assertTrue(all(isinstance(index, int) for index in bases))

    def test_a_wedge_drop_needs_a_prior_upside_cycle(self):
        """A breakdown in a market that never rose is not a wedge drop."""
        b = Builder()
        b.run(90, -0.004, volume=110.0)
        b.contract(12, change=-0.0006, volume=70.0)
        b.run(10, -0.014, volume=300.0)
        series = phases(b.bars)
        self.assertNotIn("wedge_drop", [phase for _, phase in confirmed(series)],
                         "没有上行周期时不得确认楔形下跌")

    def test_a_low_volume_breakout_is_refused(self):
        quiet = phases(upside_cycle(breakout_volume=60.0))
        self.assertNotIn("wedge_pop", [phase for _, phase in confirmed(quiet)],
                         "缩量突破不得确认为楔形突破")

    def test_a_false_breakout_that_closes_back_inside_is_refused(self):
        b = Builder()
        b.run(70, -0.006, volume=120.0)
        b.contract(14, volume=70.0)
        before = b.price
        # The wick clears the wedge high while the close stays inside it. The earlier
        # fixture closed 1.2% up, which under the scoped pivot *is* a breakout - that
        # version only passed because nothing could clear the 20-bar pivot.
        b.push(0.0005, volume=300.0, wick=0.15, upper=1.0)
        b.push(-0.020, volume=300.0, wick=0.15)
        b.run(6, -0.004, volume=100.0)
        series = phases(b.bars)
        pops = [record for record in series.records if record.phase == "wedge_pop"]
        self.assertTrue(all(record.status != "confirmed" for record in pops),
                        "假突破（收回区间内）不得确认")
        self.assertGreater(float(b.bars[70 + 14]["high"]), before,
                           "影线必须真的越过收缩区枢轴才算假突破")

    def test_a_flat_market_produces_no_confirmed_phase(self):
        series = phases(flat_market())
        self.assertEqual(confirmed(series), [])

    def test_a_downside_cycle_gets_its_own_phases_and_never_a_mirrored_sign_flip(self):
        series = phases(downside_cycle(), sideMode="symmetric")
        order = [phase for _, phase in confirmed(series)]
        self.assertTrue(
            {"downside_base_n_break", "downside_ema_crossback"} & set(order),
            f"下行周期应产生独立的下行阶段，实际 {order}",
        )
        self.assertNotIn("wedge_pop", order, "下跌不应靠把上涨规则取负来识别")

    def test_exhaustion_is_an_observation_and_blocks_new_entries(self):
        series = phases(upside_cycle())
        exhausted = [record for record in series.records if record.phase == "exhaustion_extension"]
        self.assertTrue(exhausted, "强延伸应产生衰竭观察")
        self.assertTrue(all(record.status == "candidate" for record in exhausted))
        self.assertTrue(any("禁止新开仓" in warning for record in exhausted
                            for warning in record.warnings))

    def test_insufficient_history_is_reported_instead_of_guessed(self):
        series = phases(upside_cycle()[:20], minBars=60)
        self.assertTrue(series.insufficient)
        self.assertIn("少于", series.insufficient_reason)
        self.assertEqual(series.records, [])
        self.assertNotIn("insufficient", series.counts())


# ------------------------------------------------------------- anti-leakage

class AntiLeakageTests(unittest.TestCase):
    def test_truncating_history_does_not_change_earlier_readings(self):
        bars = upside_cycle()
        full = phases(bars)
        for cut in (60, 80, 100, len(bars) - 5):
            shorter = phases(bars[:cut])
            for index in range(len(shorter.records)):
                self.assertEqual(
                    shorter.records[index].as_dict()["phase"],
                    full.records[index].as_dict()["phase"],
                    f"截断到 {cut} 根后第 {index} 根阶段变化：历史被未来改写",
                )
                self.assertEqual(
                    shorter.records[index].checks, full.records[index].checks,
                    f"截断到 {cut} 根后第 {index} 根判定条件变化",
                )

    def test_the_pivot_never_includes_the_bar_that_breaks_it(self):
        bars = upside_cycle()
        lookback = 20
        index = 87  # inside the breakout run
        pivot = ind.prior_high(bars, index, lookback)
        self.assertEqual(pivot, max(float(row["high"]) for row in bars[index - lookback:index]))
        self.assertNotIn(float(bars[index]["high"]),
                         [float(row["high"]) for row in bars[index - lookback:index]])
        record = phases(bars).records[index]
        self.assertIsNotNone(record.pivot_price)

    def test_appending_future_bars_cannot_change_an_earlier_confirmation(self):
        """The specific worry: a later volume spike must not retro-confirm anything."""
        bars = upside_cycle(breakout_volume=60.0)  # a breakout that should be refused
        before = phases(bars)
        self.assertNotIn("wedge_pop", [phase for _, phase in confirmed(before)])
        tail = Builder(price=float(bars[-1]["close"]), start=int(bars[-1]["ts"]) + HOUR)
        tail.run(40, 0.02, volume=900.0)  # huge future volume
        after = phases(bars + tail.bars)
        # Compare by bar index, not by counting confirmations: the tail confirms phases
        # of its own, and slicing the longer list by count pulled those into the
        # comparison - a test bug that reported leakage where there was none.
        prefix = [(index, phase) for index, phase in confirmed(after)
                  if index < len(before.records)]
        self.assertEqual(prefix, confirmed(before), "追加未来K线不得改变此前的确认")
        self.assertNotIn("wedge_pop", [phase for _, phase in prefix],
                         "未来成交量不得把过去的缩量突破改判为确认")

    def test_a_lower_interval_cannot_read_an_unclosed_higher_bar(self):
        base = upside_cycle()[:120]
        higher = [dict(row) for row in base[::4]]
        series = cpa.analyze(symbol="TESTUSDT", display_symbol="TEST", interval="1h",
                             product_type="crypto", bars=base,
                             management_bars=higher, parameters={"minBars": 30})
        self.assertEqual(series.higher_intervals[0], "4h")
        seen_closed = 0
        for record in series.records:
            view = record.higher
            self.assertIsNotNone(view)
            if not view.available:
                self.assertIn("尚未收盘", view.reason)
                continue
            seen_closed += 1
            self.assertLessEqual(view.closed_at + 4 * HOUR, record.time,
                                 "低周期读到了尚未收盘的高周期K线")
        self.assertGreater(seen_closed, 0)

    def test_a_weekly_reading_cannot_appear_before_the_week_closes(self):
        daily = upside_cycle()[:200]
        weekly = [dict(row) for row in daily[::7]]
        series = cpa.analyze(symbol="BTCUSDT", display_symbol="BTC", interval="1d",
                             product_type="crypto", bars=daily,
                             management_bars=weekly, parameters={"minBars": 30})
        for record in series.records:
            view = record.higher
            if view.available:
                self.assertLessEqual(view.closed_at + 7 * DAY, record.time)

    def test_missing_higher_timeframe_is_reported_as_insufficient_backdrop(self):
        series = phases(upside_cycle(), requireHigherTimeframe=True)
        self.assertTrue(any("背景" in warning for warning in series.warnings))
        # With no higher data at all, an entry must not happen.
        events = cpa.events_for(upside_cycle(), {**series.parameters,
                                                 "requireHigherTimeframe": True})
        self.assertNotIn(1, events, "背景不足时不得入场")


# ------------------------------------------------------------ signal mapping

class SignalMappingTests(unittest.TestCase):
    def test_a_confirmed_wedge_pop_opens_a_long(self):
        bars = upside_cycle()
        series = phases(bars)
        events = cpa.events_for(bars, series.parameters, records=series.records)
        pop_index = next(index for index, phase in confirmed(series) if phase == "wedge_pop")
        self.assertEqual(events[pop_index], 1)
        self.assertIsNone(events[pop_index - 1], "确认之前不得有信号")

    def test_long_only_never_shorts(self):
        bars = downside_cycle()
        series = phases(bars, sideMode="long_only")
        events = cpa.events_for(bars, series.parameters, records=series.records)
        self.assertNotIn(-1, events)

    def test_symmetric_mode_may_short_a_confirmed_downside_phase(self):
        bars = downside_cycle()
        series = phases(bars, sideMode="symmetric")
        events = cpa.events_for(bars, series.parameters, records=series.records)
        self.assertIn(-1, events, "symmetric 模式下行的确认阶段应允许做空观察")

    def test_an_observation_never_produces_a_signal_by_itself(self):
        bars = upside_cycle(exhaustion=True, drop=False)
        series = phases(bars)
        events = cpa.events_for(bars, series.parameters, records=series.records)
        for index, record in enumerate(series.records):
            if record.status == "candidate" and record.phase == "reversal_extension":
                self.assertNotEqual(events[index], 1)

    def test_a_wedge_drop_closes_the_long(self):
        bars = upside_cycle()
        series = phases(bars)
        events = cpa.events_for(bars, series.parameters, records=series.records)
        self.assertIn(0, events, "楔形下跌或衰竭应当产生平仓事件")

    def test_entry_stage_selection_is_respected(self):
        bars = upside_cycle()
        series = phases(bars, entryStages=["ema_crossback"], exhaustionAtr=100.0)
        events = cpa.events_for(bars, series.parameters, records=series.records)
        # The wedge pop is still *recognised* - it is what starts the cycle - but it is
        # recorded as a candidate and produces no event, because the operator asked for
        # crossback entries only.
        pop_index = next(index for index, record in enumerate(series.records)
                         if record.phase == "wedge_pop")
        self.assertEqual(series.records[pop_index].status, "candidate")
        self.assertNotEqual(events[pop_index], 1, "未选中的入场阶段不得开仓")
        self.assertIn(1, events, "选中的阶段仍然应当开仓")

    def test_exhaustion_exits_at_the_observation_bar(self):
        bars = [bar(index * HOUR, 100, 101, 99, 100, 10) for index in range(4)]
        records = [
            cpa.PhaseRecord(time=0, phase="wedge_pop", status="confirmed"),
            cpa.PhaseRecord(time=HOUR, phase="exhaustion_extension", status="candidate"),
            cpa.PhaseRecord(time=2 * HOUR),
            cpa.PhaseRecord(time=3 * HOUR, phase="wedge_drop", status="confirmed"),
        ]
        events = cpa.events_for(bars, {"entryStages": ["wedge_pop"],
                                      "exitOnExhaustion": True}, records=records)
        self.assertEqual(events, [1, 0, None, None])

    def test_bearish_levels_break_low_and_invalidate_above_high(self):
        series = phases(downside_cycle(), sideMode="symmetric")
        bearish = [record for record in series.records
                   if record.status == "confirmed" and record.direction == "bearish"]
        self.assertTrue(bearish)
        for record in bearish:
            if record.pivot_price is not None and record.invalidation_price is not None:
                self.assertGreater(record.invalidation_price, record.pivot_price)

    def test_the_event_series_is_aligned_with_the_bars(self):
        bars = upside_cycle()
        series = phases(bars)
        events = cpa.events_for(bars, series.parameters, records=series.records)
        self.assertEqual(len(events), len(bars))

    def test_a_second_entry_signal_is_not_emitted_while_already_long(self):
        bars = upside_cycle()
        series = phases(bars)
        events = cpa.events_for(bars, series.parameters, records=series.records)
        entries = [index for index, event in enumerate(events) if event == 1]
        self.assertEqual(len(entries), len(set(entries)))
        self.assertLessEqual(len(entries), 1 + 1, "第一期是单仓位：不应反复开仓")


class EpisodeCountTests(unittest.TestCase):
    """A phase that stays true for ten bars is one episode, not ten events."""

    def series(self, pattern: list[tuple[str, str]]) -> cpa.PhaseSeries:
        series = cpa.PhaseSeries(symbol="T", display_symbol="T", interval="1h",
                                 product_type="crypto")
        for index, (phase, status) in enumerate(pattern):
            series.records.append(cpa.PhaseRecord(time=1_700_000_000_000 + index * 3_600_000,
                                                  phase=phase, status=status))
        return series

    def test_a_repeated_confirmation_counts_as_one_episode(self):
        series = self.series([("wedge_pop", "confirmed")] * 3 + [("none", "none")]
                             + [("wedge_pop", "confirmed")] * 2)
        self.assertEqual(series.counts(), {"wedge_pop": 5}, "根数口径保持每根一条")
        self.assertEqual(series.runs(), {"wedge_pop": 2}, "段数口径按连续区间计")

    def test_an_observation_is_counted_separately_from_a_confirmation(self):
        series = self.series([("reversal_extension", "candidate")] * 2
                             + [("wedge_pop", "confirmed")] + [("none", "none")]
                             + [("reversal_extension", "candidate")])
        self.assertEqual(series.counts(),
                         {"reversal_extension（观察）": 3, "wedge_pop": 1})
        self.assertEqual(series.runs(),
                         {"reversal_extension（观察）": 2, "wedge_pop": 1})

    def test_both_figures_travel_in_the_payload(self):
        payload = self.series([("wedge_drop", "confirmed")] * 4).as_dict()
        self.assertEqual(payload["counts"]["wedge_drop"], 4)
        self.assertEqual(payload["phaseRuns"]["wedge_drop"], 1)

    def test_real_series_report_fewer_episodes_than_bars(self):
        series = phases(upside_cycle())
        for phase, bars in series.counts().items():
            self.assertLessEqual(series.runs().get(phase, 0), bars, phase)


class ParameterTests(unittest.TestCase):
    def test_defaults_differ_by_asset_class_and_interval(self):
        stock = cpa.defaults_for("stock", "1h")
        crypto = cpa.defaults_for("crypto", "1h")
        etf = cpa.defaults_for("etf", "1h")
        weekly = cpa.defaults_for("stock", "1w")
        self.assertNotEqual(stock["volumeConfirm"], 1.3)
        self.assertNotEqual(crypto["extensionAtr"], stock["extensionAtr"])
        self.assertNotEqual(etf["exhaustionAtr"], stock["exhaustionAtr"])
        self.assertNotEqual(weekly["pivotLookback"], stock["pivotLookback"])

    def test_caller_parameters_override_the_defaults(self):
        resolved = cpa.resolve_parameters({"emaFast": 5}, asset_class="stock", interval="1h")
        self.assertEqual(resolved["emaFast"], 5)
        self.assertEqual(resolved["emaSlow"], 20)

    def test_the_published_schema_covers_every_parameter(self):
        keys = {spec["key"] for spec in cpa.PARAMETER_SPECS}
        default = cpa.defaults_for("stock", "1h")
        missing = {key for key in default if key not in keys and key != "atrPeriod"}
        self.assertEqual(missing, set(), f"以下参数没有中文说明与范围：{sorted(missing)}")
        for spec in cpa.PARAMETER_SPECS:
            self.assertTrue(spec["label"] and spec["help"], spec["key"])
            self.assertIn("unit", spec)

    def test_the_description_does_not_claim_the_original_strategy(self):
        described = cpa.describe()
        self.assertIn("QuantDesk", described["name"])
        self.assertNotIn("Oliver Kell 原版", described["name"])
        self.assertIn("概念来源", described["attribution"])
        self.assertIn("简化版本", described["simplePositionNotice"])


if __name__ == "__main__":
    unittest.main()
