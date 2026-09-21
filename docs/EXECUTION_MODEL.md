# 执行模型（Gate-B：事件驱动执行层）

这份文档说明 QuantDesk 回测"一个信号是怎么变成一笔成交的"，以及每一个开关的含义、
默认值、它带来的保守假设和已知缺口。数值结果里带的 `execution_model` 块是这份文档的
机器可读版本；两者必须一致，改代码就要改这里。

适用范围：`engine/src/quantdesk/backtest/engine.py`。回测只读本地历史，不联网、不下单、
不持有任何密钥（Gate-A 约束），这一层也没有引入任何下单路径。

---

## 1. 默认模型（不打开任何新开关时）

| 环节 | 默认行为 |
| --- | --- |
| 信号 | 只用**已收盘**的K线：第 i 根开盘成交时，信号取自第 `i-1-latency_bars` 根 |
| 成交价 | 成交那根K线的**开盘价**，按 `slippage_bps` 加一次不利滑点，再按 `tick_size` 取整 |
| 数量 | 当前净值 × `allocation_pct` × `leverage` / 开盘价，按 `qty_step` 向下取整 |
| 最小下单 | 名义额低于 `min_order_notional` 的信号直接不下单（留一条 `rejected` 订单） |
| 保证金 | 逐仓；维持保证金率与杠杆上限按交易所风险档位逐笔取值 |
| 资金费 | 按交易所实际结算时间、用**当时**标记价计收 |
| 强平 | 逐仓，逐根按标记价的不利极值判定，损失不超过保证金 |
| 休市空 bar | `fill_on_thin="skip"`：不建仓，等有深度的K线 |
| 平仓 | 整笔成交，不建模退出时的部分成交 |
| 保护性委托 | **完全不模拟**（`stop_loss_pct` / `take_profit_pct` / `trailing_stop_pct` 都是 `None`） |
| 挂单 | 不存在：`maker_fill="never"`，一切成交都吃价差 |
| 清算费 | `liquidation_fee_bps = 0.0`：只收交易手续费 |

默认组合下，引擎的数值结果与加这一层之前**逐位一致**。回归证据见
`engine/tests/test_execution_lifecycle.py::DefaultParityTests`（基线数字写死在测试里）。

---

## 2. 订单生命周期（第 1、2 项）

每次开仓/平仓都会生成一个 `Order`，随结果一起返回（`result.orders` /
`result.as_dict()["orders"]`）：

```
order_id, created_index, created_time, side(buy/sell), purpose(entry/exit),
order_type(market/limit/stop/take_profit/trailing_stop/liquidation),
quantity, reason(signal/stop_loss/take_profit/trailing_stop/liquidation/end_of_data),
limit_price, status, filled_quantity, unfilled_quantity, avg_fill_price,
filled_index, filled_time, bars_to_fill, fee_kind(taker/maker),
rejected_reason, note, fills[{index,time,quantity,price,fee,feeKind,slippageBps}]
```

状态流转：`created → accepted → partially_filled → filled`，
终止态另有 `cancelled`（信号失效 / 挂单没被触及 / 数据结束）与 `rejected`（低于最小
下单量、可用保证金不足）。`data_quality["orders"]` 给出汇总（各状态计数、fills 数、
maker/taker 成交数、最重成交延迟、未成交数量）。

### 跨K线部分成交（`partial_fill="cap"`）

`max_participation` 限制单根K线能吃掉的成交量比例。默认 `partial_fill="ignore"`
保持历史行为（按截断后的量成交，剩下的计入未成交）。打开 `partial_fill="cap"` 后：

* 当根只能成交 `max_participation × volume`，剩余部分**跨K线继续成交**，前提是信号
  仍然支持这个方向；
* 信号反向/离场、被保护性委托或强平终止、或数据结束时，剩余挂单被撤销，并如实记入
  `unfilledOrders` / `unfilledNotional` / `unfilledQuantity`；
* 每笔开仓记录成交笔数 `entry_fills` 与最重延迟 `entry_delay_bars`，
  `data_quality["maxFillDelayBars"]` 是全局最慢的一笔。

保守性：跨K线累计只会让持仓**更小**（每根K线仍然受参与率上限约束），不会比"一次吃满"
更好；测试断言 `capped.trades[0].quantity < filled.trades[0].quantity`。

---

## 3. 保护性委托（第 3、4 项）

| 开关 | 含义 | 默认 |
| --- | --- | --- |
| `stop_loss_pct` | 相对**开仓价**的止损百分比 | `None`（关闭） |
| `take_profit_pct` | 相对**开仓价**的止盈百分比 | `None`（关闭） |
| `trailing_stop_pct` | 相对**入场以来最好价**的移动止损百分比 | `None`（关闭） |
| `bar_path` | 同一根K线内止损与止盈都被触及时先算哪一个 | `"conservative"` |

判定用的是当根K线（有标记价序列时用标记价）的高低价，与强平同源。三条硬规则：

1. **不利方向优先**（`bar_path="conservative"`）：一根K线同时穿过止损和止盈时，按
   **止损**成交。没有逐笔数据就无法知道先后，只有这一种假定不会美化结果。
   `bar_path="optimistic"` 只用于敏感性分析，不能用来出结论。
2. **跳空按开盘价成交**：止损价被跳空穿过时成交价是 `min(开盘价, 止损价)`（多头），
   即比止损价更差；止盈跳空则取 `max(开盘价, 止盈价)`，再按 `slippage_bps` 打一次
   不利滑点。
3. **移动止损只用"进入当根之前"的最好价**：最好价在每根K线的保护性判定**之后**更新，
   否则移动止损会用同一根K线的高点触发自己，等于偷看未来。

强平与止损的先后：谁离市场更近谁先成交。止损比强平价更紧时，强平分支不会触发
（测试 `test_a_stop_tighter_than_the_liquidation_price_fills_before_liquidation`）；
止损比强平价更远时，价格先到强平价，由强平分支出场。

新增的退出原因：`stop_loss` / `take_profit` / `trailing_stop`（原有 `signal` /
`liquidation` / `end_of_data` 不变）。

---

## 4. 手续费与挂单（第 5 项）

* `maker_fee_bps`（默认 `None` = 用 `fee_bps`）：挂单成交时使用的手续费率，默认**不假设**
  任何 maker 折扣。
* `maker_fill`：
  * `never`（默认）：一切成交都吃价差；
  * `passive_only`：入场是挂在**信号K线收盘价**上的限价单，只有当K线真的回踩到该价位
    （多头看 `low <= limit`，空头看 `high >= limit`）才算成交，成交价就是挂单价、
    不含滑点也不含冲击成本；出场仍然是吃单。价格没有回来 → 不成交，记为 `cancelled`。
* `maker_order_bars`（默认 `1`）：挂单存活多少根K线，超时撤销。`maker_order_bars=1`
  表示只有信号之后那根K线有机会成交。

这套建模刻意不模拟排队位置、撤单竞争与部分成交的挂单，所以 `maker_fill="passive_only"`
的结果应当读作"最乐观的挂单假设"（一定能成交在限价上），而不是"更真实"。

---

## 5. 清算费（第 6 项）

`liquidation_fee_bps`（默认 `0.0`）在强平时按 `数量 × 成交价 × bps` 额外计收，记在
`BacktestTrade.liquidation_fee`、`BacktestResult.total_liquidation_fees`、
`risk["liquidations"][i]["liquidationFee"]` 与 `data_quality["liquidationFees"]`。
它**不在** `fees` 里，也不在 `total_fees` 里：清算费与交易手续费是两件事，混在一起就
再也拆不开。它会计入 `net_pnl`（损失合计仍不超过该仓位的保证金）。

---

## 6. 高周期与严格 as-of（第 7、8 项）

`align_higher_timeframe(base, higher, higher_interval_ms)` 把高周期K线对齐到基础K线：
一根 08:00 开盘的 4h K线在 12:00 之前不可知，所以**第一根能看到它的 1h K线是 12:00
开盘那根**。规则就是 `base.ts >= higher.ts + 高周期长度`，没有任何前视。

`run_backtest(..., higher_timeframes={"4h": bars}, signal_source=callback)` 会把这些
对齐后的视图交给 `signal_source(ordered, parameters, aligned)`，多周期策略拿不到未收盘
的高周期K线。`data_quality["higherTimeframes"]` 记录每个周期可见K线数与首次可见位置。

严格 as-of 的完整含义（测试 `AsOfTests`）：

* 第 i 根的决策只依赖 `ts <= t_i` 的信息：信号来自已收盘K线，当根的高低极值属于当根；
* 把后面的K线截掉，前面每一根的净值与已平仓交易必须逐位不变；
* 结算时间晚于最后一根K线的资金费永远不计收；
* 估值只用 `ts <= 当根` 的最新标记价；
* 高周期K线收盘前不可见。

---

## 7. K线上限（第 9 项）

单次回测/验证/组合请求的K线上限来自配置文件，而不是代码常量：

```toml
[backtest]
max_bars = 20000    # 超过就拒绝，绝不静默截断
```

* 读取时机是**请求校验时**（`studies.max_backtest_bars()`），改配置后下一次请求生效；
* 超限的请求会被拒绝，信息形如
  `请求 25000 根K线，超过当前上限 20000 根；可在 config.toml 的 [backtest] max_bars 调整（硬顶 100000 根）…`；
* 配置坏了（缺失/非数字）回落到随包默认值 `1000`；
* 硬顶 `ABSOLUTE_MAX_CANDLES = 100000`，本地配置无法突破；
* 引擎本身从不截断输入：`data_quality["bars"]` 永远等于调用方给的长度。

---

## 8. 代理数据标注（第 10 项）

`BacktestResult.data_proxies`（= `data_quality["dataProxies"]`，`[{field, kind, note}]`）
把"这份结果建立在替代数据上"写成机器可读字段，同时给出人话警告。触发规则：

| kind | 触发条件 |
| --- | --- |
| `declared` / 调用方声明的 kind | `instrument["dataProxy"]`（或 `data_proxies`）声明了代理 |
| `non_venue_source` | K线 `source` 不是交易所原始成交（`imported` / `local_derived` / `upload` / `synthetic` / `spot_history` / 非 `venue*` 前缀） |
| `derived_close_times_volume` | 用参与率模型但K线没有成交额字段，冲击成本按 收盘价×成交量 推算 |
| `bar_close_fallback` | 打开强平/资金费但没有标记价序列，用K线收盘价近似 |
| `leveraged_etf_underlying` | 标的是三倍杠杆 ETF（SOXL/SOXS 这类），存在每日再平衡的复利衰减，不能当作指数的线性代理 |

`data_quality["asOf"]` 用一句话写明 as-of 规则，`execution_model["asOfRule"]` 是它的
英文/技术版本。

---

## 9. 配置项一览（与请求字段对应）

| 引擎字段（`BacktestConfig`） | 请求字段（`studies.py`） | 默认 |
| --- | --- | --- |
| `partial_fill` | `partialFill` | `"ignore"` |
| `max_participation` | `maxParticipation` | `1.0` |
| `latency_bars` | `latencyBars` | `0` |
| `maker_fee_bps` | `makerFeeBps` | `None`（= `fee_bps`） |
| `maker_fill` | `makerFill` | `"never"` |
| `maker_order_bars` | `makerOrderBars` | `1` |
| `stop_loss_pct` | `stopLossPct` | `None` |
| `take_profit_pct` | `takeProfitPct` | `None` |
| `trailing_stop_pct` | `trailingStopPct` | `None` |
| `bar_path` | `barPath` | `"conservative"` |
| `liquidation_fee_bps` | `liquidationFeeBps` | `0.0` |
| —（配置文件） | `[backtest] max_bars` | `20000` |

校验：`maker_fill ∈ {never, passive_only}`、`maker_order_bars ∈ [1,100]`、
三个百分比 `∈ (0,100)` 或 `None`、`bar_path ∈ {conservative, optimistic}`、
费率类不得为负。写错在开跑之前就报错，不会跑出一个没人看得懂的结果。

---

## 10. 已知缺口（明确不模拟的东西）

* 平仓仍然整笔成交，没有退出侧的部分成交；
* 没有订单簿、排队位置、盘口深度、撤单竞争，拒单原因只有"低于最小下单量/保证金不足"；
* 一根K线内部没有成交顺序：保护性委托靠 `bar_path` 假定，强平与退出都只看极值；
* `maker_fill="passive_only"` 假定挂单一定能在限价上成交，是乐观假设；
* 资金费按标记价近似，标记价缺失时用K线收盘价，已在 `dataProxies` 里标注；
* 三倍杠杆 ETF 的每日再平衡衰减不建模（只有警告与标注）；
* 没有部分成交的成交明细（`fills` 最多是每根K线一笔），也没有成交回报延迟分布。
