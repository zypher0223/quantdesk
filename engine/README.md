# QuantDesk 引擎

Python 3.12 引擎：数据接入、指标与共振、回测、模拟盘、可审计的大模型研判，并对外提供本地行情网关。

## 安装

```bash
uv sync          # 或 python -m venv .venv && .venv/bin/pip install -e .
```

`.venv` 里是 `-e` 可编辑安装，指向 `src/quantdesk`。**如果这个目录是拷贝过来的，venv 里的路径仍然指向原项目**，需要用 `.venv/bin/python -c "import quantdesk; print(quantdesk.__file__)"` 确认，否则你跑的是另一份代码。修复方式见 `.venv/lib/python3.12/site-packages/_editable_impl_quantdesk.pth`。

## 启动网关

```bash
QUANTDESK_PROXY=http://127.0.0.1:12003 .venv/bin/python -m quantdesk.cli serve   # 127.0.0.1:8765
```

仅绑定本机、只读、无需交易所密钥。

## 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 存活、代理是否已配置、周期与合约数 |
| GET | `/api/instruments` | 固定合约池：分组、产品类型、周期、共振门槛 |
| GET | `/api/instruments-info` | 交易所实时合约状态、tickSize、minOrderQty（**仅限固定池**） |
| GET | `/api/resonance` | 多周期共振，引擎计算 |
| GET | `/api/llm/settings` | profile 列表、能力映射、**Key 是否已配置**（永不返回值） |
| GET | `/api/llm/status` | 各能力的就绪状态与图表识别可用性 |
| POST | `/api/llm/keys` | 写入 `keys.env`（chmod 600） |
| POST | `/api/llm/roles` | 把能力切到另一个 profile（换供应商的入口） |
| POST | `/api/llm/profiles/{name}/test` | 最小往返，返回命名的失败原因 |
| POST | `/api/llm/profiles/{name}/models` | 拉取该账号实际可用的模型列表 |
| POST | `/api/llm/analyze-chart` | K线截图识别（base64 JSON），结果落 `chart_analyses` |
| GET | `/api/tradingagents/readiness` | 真正的 TradingAgents 运行时、profile、凭据与标的映射检查 |
| POST | `/api/tradingagents/run` | 运行官方多智能体图并归档报告与辩论状态 |
| POST | `/api/tradingagents/jobs` | 提交持久化异步任务，立即返回 jobId |
| GET | `/api/tradingagents/jobs` | 查询排队、运行、完成、失败和取消的任务 |
| POST | `/api/tradingagents/jobs/{id}/cancel` | 取消排队任务；运行中任务完成当前子进程后丢弃结果 |
| GET | `/api/tradingagents/runs` | 查询多智能体历史运行 |
| GET | `/api/plugins` | 发现插件、清单错误、启停状态与能力声明 |
| GET | `/api/plugins/registry` | 已启用插件的能力注册中心与外部策略目录 |
| POST | `/api/plugins/install` | 从 GitHub HTTPS 仓库拉取并校验插件，默认禁用 |
| POST | `/api/plugins/{id}/update` | 原子更新托管插件并自动停用，失败恢复旧版本 |
| GET | `/api/plugins/{id}/dependencies` | 检查锁定依赖、系统命令和沙箱状态 |
| POST | `/api/plugins/{id}/dependencies/install` | 从带哈希的 wheel 锁文件建立独立运行时 |
| PUT | `/api/plugins/{id}/enabled` | 启用或禁用插件 |
| POST | `/api/plugins/{id}/health` | 在隔离子进程中执行插件健康检查 |
| DELETE | `/api/plugins/{id}` | 卸载已停用插件；默认保留私有数据 |
| GET | `/api/strategies` | 内置与外部插件策略的统一注册表 |
| POST | `/api/backtest` | 引擎回测：资金费、杠杆、强平、步长取整、休市空 bar |
| GET | `/api/scheduler/status` | 后台行情轮转与每日任务状态 |
| PUT | `/api/scheduler/settings` | 保存轮转间隔、缓存深度和每日研判设置 |
| POST | `/api/scheduler/market/run` | 立即采集一个指定或轮转中的合约 |
| GET | `/api/scheduler/runs` | 查询后台任务运行记录 |
| GET | `/api/paper/account` | 模拟账户：标记价估值、未实现盈亏、保证金率、强平距离 |
| POST | `/api/paper/positions` | 模拟开仓（市价 + 滑点 + 步长取整 + 最小名义额校验） |
| POST | `/api/paper/positions/{id}/close` | 模拟平仓，并在同一事务写入日志 |
| PUT | `/api/paper/positions/{id}/note` | 修改开仓理由（日志条目保持不可改） |
| POST | `/api/paper/reset` | 重置模拟账户（有未平仓持仓时拒绝） |
| GET | `/api/journal` | 日志条目 + 哈希完整性报告 |
| GET | `/api/journal/export` | 导出 JSON / CSV（含完整性报告） |
| GET | `/bybit/v5/market/kline` | K线 |
| GET | `/bybit/v5/market/tickers` | 行情快照 |
| GET | `/bybit/v5/market/funding/history` | 资金费率历史 |
| GET | `/bybit/v5/market/open-interest` | 持仓量历史 |

所有行情端点强制 `category=linear`，且 symbol 必须先通过固定合约池白名单（`AMD` 会映射为 `AMDSTOCKUSDT`）。原始上游读数有 30 秒 TTL 缓存。

## 大模型适配层

`llm/base.py` 只有一个接口，DeepSeek 与 OpenAI 共用 OpenAI 兼容实现，差异在 `base_url` 与是否走代理。失败原因是被分类的，不是被猜的：

| 分类 | 触发 |
| --- | --- |
| `no_key` | 该 profile 没有可用凭据 |
| `invalid_key` | 401 / 403 或 key 被拒 |
| `quota` | 402 或 `Insufficient Balance` |
| `model_not_found` | 404 或模型名不存在 |
| `rate_limited` | 429 |
| `network` | 连接失败；若 provider 为 openai 且未配 proxy 会额外提示 |
| `timeout` / `server` / `bad_request` | 其余对应状态 |

要点：

- **代理进 profile**。本机实测 `api.deepseek.com` 可直连、`api.openai.com` 必须走代理，所以 `proxy` 是 profile 字段而非全局。
- **视觉路由有守卫**。只有声明 `supports_vision = true` 的 profile 才会收到图片；若 `chart_analysis` 指向纯文本模型，请求以 409 拒绝，**不会**静默改发别的供应商。
- **Key 只进不出**。`/api/llm/settings` 只报告 `hasKey` 与变量名；值只写入 `keys.env`。
- **凭据形态会被检查**。值为空、或像占位符（`sk-...`、`your-api-key`、`changeme`）、含省略号、
  首尾空白、含非 ASCII 字符时，`hasKey` 判为 false 并返回 `credentialIssue` 说明原因。
  这一层是为了避免把"文档里的示例占位符被原样粘贴"误报成"密钥错误"——
  上游只会回一个 `Authentication Fails`，让人找错方向。长度不检查：各供应商格式差异太大。
- `keys.env` 写入时按大小写不敏感去重，同名变量不会残留两行不同值。
- 环境变量优先于文件，而 `keys.env` 只在网关启动时加载：**在设置页保存密钥后，
  当前进程立即生效，但重启网关会回到文件里的值**。若两者不一致，以环境变量为准。
- **配置目录不可写时不崩**。`~/.quantdesk` 只读时回落到内存默认值，并在 `/api/llm/settings` 用 `homeWritable: false` 告知界面。

## 回测、模拟盘与日志

回测引擎在 `backtest/engine.py`，策略注册表提供双均线交叉、价格通道突破、RSI 反转，并合并已启用插件的 `strategy.describe` 目录。所有策略统一生成按K线时间对齐的 long / short / flat 事件，再由同一撮合引擎计算费用、资金费和强平。

建模的成本与约束：

| 项 | 处理方式 |
| --- | --- |
| 成交假设 | 收盘确认交叉，下一根K线**开盘**成交（固定，写在 `assumptions` 里） |
| 手续费 | 双边按 bps 计收，进场的部分在开仓时即从权益扣除 |
| 滑点 | 开平各计一次，方向相反 |
| 资金费 | 每 8 小时结算，以当时K线收盘价近似标记价；多头付、空头收 |
| 杠杆 | 逐仓，放大名义敞口；亏损以已投入保证金为上限 |
| 强平 | 用每根K线的**不利极值**（多单看 low、空单看 high）判定，强平价按 `p(1-1/L)/(1-mmr)` |
| 交易所约束 | tickSize / qtyStep 取整、最小下单名义额，均取自实时合约目录 |
| 休市空 bar | 默认不建仓（`fill_on_thin: skip`）；允许时会在结果里给出警告 |
| 杠杆 ETF | 声明标的自带的每日再平衡复利衰减未被建模 |

本地没有资金费历史时**不会假装计入 0**，而是在 `warnings` 里说明；先跑 `fetch derivatives` 即可。

模拟盘（`paper/engine.py`）用交易所标记价估值，暴露未实现盈亏、保证金率、距强平价百分比；资金费区分**付出**与**收取**（多头付 0.5、空头收 0.5 不等于零成本）。

日志表由数据库触发器强制只可追加：`UPDATE` / `DELETE` 直接报错，不依赖调用方自觉。每条记录带内容哈希，`/api/journal` 会重新计算并报告是否被改动——包括绕过应用直接改库的情况。

## 口径要点

- 股票类合约在交易所分为 `symbolType=stock`（个股）与 `symbolType=ETF`（SOXL/SOXS）。只查 `stock` 会把两个杠杆 ETF 误报为不可交易。
- Bybit 返回的 `openInterest` 是张数/币数，`openInterestValue` 才是 USDT 名义额；展示与风控一律用后者。
- 股票永续在美股休市时段仍会打印K线，但成交额可能只有活跃时段的千分之一（实测 AAPL 15m：6.45 vs 5,485）。`features/resonance.py` 的 `is_thin_session` 会识别这种空 bar 并停用其量能投票，避免在休市尾巴上读出"放量"。

## CLI

```bash
.venv/bin/python -m quantdesk.cli universe              # 校验 17 个合约在交易所的实时状态
.venv/bin/python -m quantdesk.cli resonance AAPLUSDT --venue bybit
.venv/bin/python -m quantdesk.cli derivs AAPLUSDT --venue bybit
.venv/bin/python -m quantdesk.cli sepa AAPLUSDT --venue bybit
.venv/bin/python -m quantdesk.cli fetch derivatives AAPLUSDT --venue bybit   # 拉资金费/持仓量历史
.venv/bin/python -m quantdesk.cli backtest AAPL --interval 1h --leverage 2 --bars 400
.venv/bin/python -m quantdesk.cli info
.venv/bin/python -m quantdesk.cli plugins list
.venv/bin/python -m quantdesk.cli plugins check example-strategy
```

`backtest` 与网页「策略回测」使用同一个引擎实现，输出应完全一致。参数覆盖快慢线、方向、资金、仓位比例、手续费、滑点、杠杆，以及 `--no-funding` / `--no-liquidation` / `--fill-on-thin allow` 三个开关。

## 测试

```bash
.venv/bin/python -m pytest tests -q
```

覆盖：合约池分组与 ETF 类型、未知合约拒绝、共振端点（含K线不足时的降级）、NaN 不进入 JSON、休市空 bar 判定、网关超时与交易所错误处理、LLM 失败分类、`keys.env` 权限与清除语义、视觉路由守卫、截图识别落库、回测成本与强平不变量、日志不可改不可删与哈希校验。

## 大模型研判与 TradingAgents

系统保留两条明确区分的研判路径：

- `POST /api/research` 执行一次证据约束的快速模型研判，使用当前图表K线并由 QuantDesk 校验计划点位。
- `POST /api/tradingagents/jobs` 把官方 `TradingAgentsGraph.propagate()` 多智能体流程放入持久化单工队列，包含分析师、
  多空研究员辩论、交易员、风险辩论与最终决策；`GET /api/tradingagents/readiness` 检查运行时、模型凭据和代码映射。

TradingAgents 在独立子进程中运行，凭据只通过环境变量传入。容器镜像固定安装官方提交
`be952b8eccb49720509af544c6675233bc1f10d0`；运行记录落 `tradingagents_runs` 表。BTC/ETH 通过只读
Hyperliquid 日线桥进入 crypto 流程。股票和 ETF 的价格、成交量、技术指标使用精确的 Bybit USDT
永续合约代码，新闻和财务工具再单独映射到公开证券代码。SPCX、SKHY 分别以 `SPCX`、`SKHY`
运行完整基本面分析师，不再被入口禁用。

快速研判 `POST /api/research` 的工作方式与普通"把数据丢给模型"不同：

1. **引擎先取证**。四周期立场（共振引擎算的）、资金费与持仓量、规则回测摘要、
   以及 `price_structure`（真实摆动高低点 + ATR14）组成证据包，每一项都有可引用的键。
2. **数据由浏览器或引擎提供，模型只做推理**。实时图表使用当前 Bybit 证据；上传历史K线时进入
   隔离的历史模式，不混入当前 ticker 与其他周期行情。演示数据不能启动研判。
3. **必须给出具体点位**。`trading_plan` 要求 `entry` / `stop_loss` / `take_profit_1` / `take_profit_2`，
   不允许"视情况而定"。
4. **引擎复核点位**，不采信模型的自述：

| 检查 | 规则 |
| --- | --- |
| 方向与顺序 | long 必须 `SL < entry < TP1 < TP2`；short 反向 |
| 盈亏比 | 引擎按点位重算，与模型自报值偏差过大时以重算值为准并提示 |
| 止损距离 | 小于 0.5 倍 ATR 判为噪音止损；单笔风险上限 20% |
| 结构位置 | 多头止损高于最近摆动低点时提示易被回踩扫掉 |
| 仓位 | 超过 20% 给出上限提示 |
| 数值溯源 | 报告里的每个数字必须能在证据包中定位，否则列入 `unsupportedNumbers` |

数值溯源允许**推导值**：情形/失效条件里的触发价（如"入场减一个 ATR"）只要落在中位价 ±20% 内
就记为 `derivedNumbers`；远离所有读数的价格（如把 333 的标的说成 640）仍会被拒绝。

报告落 `ta_reports` 表，含 profile、模型、用量、证据键清单与校验结果；`GET /api/research/reports`
可列出，`/reports/{id}` 取回 Markdown 存档。

## 模型名、推理预算与失败诊断

这台机器上踩过的三个坑，代码里都做了处理：

**1. 不存在的模型名会被静默替换。** DeepSeek 对不存在的模型名不报错，而是改用默认模型，
所以配置写错也显示"连通正常"。`/api/llm/profiles/{name}/test` 现在同时返回
`requestedModel` 与 `model`，不一致时给出 `substituted: true` 与 `warning`，界面直接标红。

**2. 推理模型的 token 预算要覆盖思维链。** `deepseek-v4-pro` 与 `deepseek-flash` 都是推理模型，
把内容放在 `reasoning_content`，`content` 可能为空。实测一次研判：
推理 3862–12820 token 波动，正文约 900–1500 token。因此：

- 驱动在 `content` 为空时回落到 `reasoning_content`，并说明原因；
- `max_tokens` 是 profile 字段（设置页可改），种子默认 `12000`，这台机器用 `20000` 才稳定；
- 预算耗尽时端点返回 `kind: truncated` 并说明"推理用了 N token、正文未产出"，
  而不是让下游拿一个解析失败的空串。

**3. 凭据形态会被检查。** 见下节。

## 配置目录必须可写

SQLite 需要在数据库旁创建 WAL 文件，所以 `QUANTDESK_HOME`（默认 `~/.quantdesk`）**必须对运行网关的账号可写**。不可写时相关端点返回 503 并说明原因，而不是抛一个裸 500：

```
QUANTDESK_HOME=/可写/目录 .venv/bin/python -m quantdesk.cli serve
```

### 离线验证视觉链路

没有真实 key 也能跑通整条链路：

```bash
.venv/bin/python scripts/stub_llm_server.py --port 8791 --verbose
```

再把某个 profile 的 `base_url` 指向 `http://127.0.0.1:8791/v1` 并设 `supports_vision = true`，即可验证路由守卫、提示词组装与 `chart_analyses` 落库。
