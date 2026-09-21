# Cycle of Price Action — QuantDesk 规则化适配版

规则定义、默认参数与校准记录。当前规则版本 **`cpa-qd/1.3.0`**。

> 这是 QuantDesk 自己的阈值套在一个公开概念上的实现，**不是** Oliver Kell 原版策略，
> 也不声称与其一致。识别层只用 OHLCV，确定性、离线、不调用模型。

## 1. 引擎契约

- 识别层输出**每根 K 线一条**记录，`status` 为 `confirmed` / `candidate` / `none`。
  观察阶段（`reversal_extension`、`exhaustion_extension`）永远是 `candidate`，
  **候选与观察都不下单**。
- 信号层把已确认阶段映射为事件：`1` 多 / `-1` 空 / `0` 平 / `None` 观望。
  引擎在第 `i` 根依据 `signal_events[i - 1 - latency_bars]` 行动，并在**下一根开盘**成交。
- 顺序语义是规则的一部分：**结构性确认先于观察**。急跌既是"偏离均线"也是"楔形下跌"，
  先判定的那条赢得该根，结构证据优先。
- 枢轴只取当前 K 线**之前**的数据；收缩、均线距离同样只看之前。
- 分批建仓/减仓由 `positionModel=intent` 表达（`PositionIntent`），默认为 `single`
  （单仓位事件模型，与阶段 B 逐位一致）。

## 2. 八个阶段

| 阶段 | 类型 | 方向 | 确认条件（要点） |
| --- | --- | --- | --- |
| `reversal_extension` | 观察 | 多 | 收盘低于两均线 ≥ `extensionAtr`，且放量/长下影/收盘反转 |
| `wedge_pop` | 确认 | 多 | 收缩 ∧ 均线间距收窄 ∧ 收盘站上两均线 ∧ **突破收缩区枢轴** ∧ 放量 ∧ **楔形形成于慢线下方** ∧ 当前周期为 `downside`/`none` |
| `ema_crossback` | 确认 | 多 | 已确认上行周期 ∧ 快线在慢线上方 ∧ 回踩 EMA 区 ≤ `crossbackToleranceAtr` ∧ 结构低点未破 |
| `base_n_break` | 确认 | 多 | 已确认上行周期 ∧ 收缩 ∧ 收盘突破 20 根枢轴 ∧ 站上慢线 |
| `exhaustion_extension` | 观察 | 多 | 已确认上行周期 ∧ 高于快线 ≥ `exhaustionAtr` ∧ 长上影/滞涨 |
| `wedge_drop` | 确认 | 空 | 上行周期或此前处于均线上方 ∧ 破结构 ∧ 快线转弱 ∧ 放量 |
| `downside_ema_crossback` | 确认 | 空 | 已确认下行周期 ∧ 价格在双均线下方 ∧ 反抽 EMA 区 |
| `downside_base_n_break` | 确认 | 空 | 已确认下行周期 ∧ 收缩 ∧ 收盘跌破枢轴低点 |

周期状态机：上行周期**只能由 `wedge_pop` 开启**；`ema_crossback` / `base_n_break`
只能出现在已确认的上行周期内，不能自行启动周期；`wedge_drop` 结束上行周期。
`entryStages` 只约束**首个入场**能否来自某阶段，不影响离场与下行阶段。

## 3. 本轮的两处校准（`cpa-qd/1.2.0` → `1.3.0`）

### 3.1 楔形突破的枢轴改为收缩窗口的高点

**问题**：`wedge_pop` 原先要求收盘突破 `pivotLookback`（20 根）的高点。该窗口同时包含
收缩窗口与**收缩之前的那段扩张**，所以那个价位在下跌之后远在价格上方——"突破"不再是
楔形突破。在真实数据上该规则**从不触发**：

BTC 1d、2000 根，逐条门槛的通过根数（收缩读数共 35 根）：

| 条件 | 通过根数 |
| --- | --- |
| 收缩（`contractionScore ≥ 0.5`） | 35 |
| 收盘站上双均线 | 2 |
| 均线间距收窄 | 2 |
| 放量 | 4 |
| 突破 20 根枢轴 | 1 |
| **五条同时成立** | **0** |

后果不只是少交易：上行周期无法开启 ⇒ `ema_crossback`、`base_n_break`（都要求已在上行
周期内）**同样不可达**，默认参数（`sideMode=long_only`、三个多头入场阶段）在
BTC/ETH/NVDA/AAPL 的日线上成交为 **0**。

**改法**：突破判定改用**收缩窗口**的高点，失效价与 `setupLow` 同样取收缩窗口的低点。
只作用于 `wedge_pop`；`base_n_break` 仍用 20 根枢轴（在上行趋势里创新高本来就是它的语义）。
同时新增两个判别条件：

- `wedgeBelowSlowEma`：收缩窗口的均值收盘低于同期慢线均值——楔形形成于均线**下方**，
  突破是"重新站上"；上行趋势中的平台整理属于 `base_n_break`。
- 周期限定 `cycle ∈ {downside, none}`：楔形突破是**开启**周期的反转，不是趋势中的中继。

这两条是回归测试逼出来的：只用收缩枢轴时，`upside_cycle` 夹具里上行趋势中的平台突破被
`wedge_pop` 抢走，`base_n_break` 再也拿不到该根。

### 3.2 收缩阈值 0.5/0.55 → 0.30（周线 0.20）

0.5 要求最近窗口的平均振幅相对前一窗口**减半**；真实数据上这个读数几乎从不与"随后那根
必须突破"落在同一根上。同一门槛还被 `emaGapNarrow`（均线间距收窄）共用。

改后（最终规则集）真实数据上的可达性，默认参数、`sideMode=long_only`：

| 合约 | 周期 | 根数 | 楔形突破 | 均线回踩 | 成交 |
| --- | --- | --- | --- | --- | --- |
| BTCUSDT | 15m | 4000 | 5 | 12 | 5 |
| BTCUSDT | 1h | 4000 | 5 | 10 | 5 |
| BTCUSDT | 4h | 3000 | 11 | 14 | 11 |
| BTCUSDT | 1d | 2000 | 2 | 15 | 2 |
| ETHUSDT | 1d | 2000 | 1 | 91 | 1 |
| BTCUSDT | 1w | 300 | 1 | 51 | 1 |
| NVDAUSDT | 1h | 2000 | 7 | 41 | 7 |
| AAPLUSDT | 4h | 600 | 2 | 5 | 2 |

阈值按**可达性**选定（楔形必须是罕见形态，但不能是不可能形态），**不按收益选定**。
周线另取 0.20：周线相邻窗口的制度重叠更多，同样比例更难达到（300 根周线上 0.30 → 0 个
突破，0.20 → 2 个）。

## 4. 日线验证结果（Walk-Forward + DSR/PBO）

命令见第 7 节。BTC/ETH 日线各 2000 根，6 组候选（收缩阈值 0.2/0.3/0.4 × 入场阶段两档），
4 折 Walk-Forward，训练/验证/测试 = 0.6/0.2/0.2。

| 指标 | BTCUSDT 1d | ETHUSDT 1d |
| --- | --- | --- |
| 入选收缩阈值 | 0.2 | 0.3 |
| 整段收益 | −3.30% | +80.65% |
| 整段最大回撤 | 9.52% | 25.43% |
| **PBO** | **0.857** | **0.686** |
| **DSR（入选）** | **0.387** | **0.898** |
| 参数跨窗口稳定性 | 不稳定（2 种取值） | 不稳定（2 种取值） |
| 验证段为正的窗口 | 1/4 | 3/4 |
| 留出测试段收益 | +2.35%（基准 −34.28%） | −3.20%（基准 −40.55%） |
| 测试段成交笔数 | 1 | 1 |
| 泄漏检查 | clean | clean |

**结论：未通过验证。** PBO 0.69–0.86 远高于 0.5，说明"样本内最优"在样本外多半落到中位数
以下；DSR 最高 0.898 仍低于常用的 0.95；参数在两个标的上选出不同取值、且跨窗口不稳定；
每个 Walk-Forward 窗口的验证段只有 0–1 笔成交，统计量本身没有意义（引擎也照实告警：
"训练段只有 3 笔成交，统计意义不足"）。ETH 的 +80.65% 来自极少数几笔，不能当作证据。

日线样本之所以这么薄，是形态本身的稀有度（5.5 年 1–2 次有效楔形突破），不是数据缺失。

## 5. 默认参数（要点）

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `emaFast` / `emaSlow` | 10 / 20 | 波段均线 |
| `longSma` / `backdropSma` | 50 / 200 | 长期背景 |
| `extensionAtr` / `exhaustionAtr` | 1.5 / 3.0（加密 1.8/3.2、ETF 1.8/3.5） | 延伸与衰竭距离 |
| `contractionWindow` | 10（周线 8） | 收缩窗口 |
| `contractionThreshold` | **0.30（周线 0.20）** | 收缩比例，同时约束均线间距收窄 |
| `pivotLookback` | 20（周线 12） | 结构枢轴回看 |
| `volumeConfirm` / `downsideVolumeConfirm` | 1.3 / 1.4（加密 1.25、15m/1h +0.1） | 放量确认 |
| `crossbackToleranceAtr` | 0.6 | 回踩容差 |
| `requireHigherTimeframe` | False | 高周期过滤（默认关闭） |
| `sideMode` | `long_only` | 可切 `symmetric` |
| `entryStages` | 楔形突破/均线回踩/平台突破 | 只约束首个入场 |
| `positionModel` | `single` | 可切 `intent`（分批建仓/减仓 + 风险预算） |
| `initialExposurePct` / `addExposurePct` / `reduceExposurePct` | 50 / 25 / 50 | 意图模型仓位档位 |
| `maxPortfolioRiskPct` | 2.0（1d/1w 为 8.0） | 组合风险预算，按权益百分比 |
| `enforceOpenRisk` | True | 同一预算**同时**约束持仓浮动风险 |
| `pivotFailureExit` | True | 结构失效离场 |

高周期映射：15m→(1h,4h)、1h→(4h,1d)、4h→(1d,1w)、1d→(1w,1w)、1w→(1w,1w)；
低周期只能看到当时已收盘的高周期 K 线。

## 6. 已知限制

1. **验证路径只跑事件模型**：`run_validation` 的参数搜索与 Walk-Forward 走的是
   `generate_events`，`positionModel=intent` 在其中被忽略（实测：`single` 与 `intent` 结果
   完全相同、`dataQuality.positionIntents` 为空）。要验证意图模型的风险预算行为，目前只能
   用回测路径（`run_single` / 回测页）。把意图源接进 `search_parameters` /
   `run_walk_forward` / `evaluate_segment` 是一条独立改动，尚未做。
2. **风险预算是"下一根开盘纠正"，不是逐笔硬顶**：`maxRiskCarried` 在每根收盘标记，
   纠正单在下一根开盘成交，因此峰值 = 预算 + 最多一根的不利波动。实测 BTC 1d：
   开启约束 1,321.79（关闭 3,272.95）对预算 800；ETH 1d：1,764.70（关闭 9,876.55）。
   引擎在 `data_quality` 里写明了这一语义。
3. **股票类日线历史太短**：代币化股票永续上市于 2026-04 至 2026-07，日线只有 62–148 根，
   日线级别的 CPA 在这些标的上没有统计意义（周线、4h 更无数据）。要更长历史只能用
   带标记的代理数据（`dataProxies`，`kind=non_venue_source`），仓库里没有这样的来源。
4. **`entryStages` 在事件模型里近乎无效**：上行周期只能由楔形突破开启，所以首个入场天然
   就是楔形突破；实测 `["wedge_pop"]` 与 `["wedge_pop","ema_crossback"]` 结果逐位相同。
5. **周线样本极薄**：BTC 周线 339 根，1 次有效突破；任何周线结论都是噪声。
6. **日线验证未通过**（见第 4 节）：默认参数在日线上可用、可复现，但没有统计优势证据。

## 7. 复现

```bash
# 全量测试
engine/.venv/bin/python -m pytest engine/tests -q

# 日线验证：Walk-Forward + DSR/PBO（只读本地库，不下单、不联网）
engine/.venv/bin/python scripts/cpa_daily_validation.py \
  --symbols BTCUSDT ETHUSDT --timeframe 1d --bars 2000 \
  --sides long_only --thresholds 0.2 0.3 0.4 \
  --out .tmp/daily-validation-final.json

# 全部 17 个合约在给定阈值下的多头可达性
engine/.venv/bin/python scripts/cpa_universe_sweep.py 0.30 .tmp/universe-ct030.json
```
