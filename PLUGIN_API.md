# QuantDesk 插件协议 v1 / v2 / v3

QuantDesk 插件用于把外部仓库适配成稳定的扩展能力。核心不会直接导入第三方 Python 包；每次调用都会启动插件清单声明的子进程，通过 stdin/stdout 交换一条 JSON-RPC 2.0 消息。插件异常、依赖冲突和超时不会令 API 主进程退出。

> 启用前仍须审查第三方仓库。Docker 生产镜像要求 Bubblewrap 沙箱可用，否则拒绝启用和执行插件；本地开发默认使用可用的系统沙箱，并在不可用时明确显示“隔离降级”。

## 目录

每个插件仓库根目录必须包含：

```text
quantdesk-plugin.toml
plugin.py                 # 文件名由 command 决定
```

示例见 [examples/quantdesk-plugin-template](examples/quantdesk-plugin-template)。

## 清单

```toml
[plugin]
id = "my-strategy"
name = "My Strategy"
version = "0.1.0"
api_version = "1"
description = "一句话说明"
homepage = "https://github.com/owner/repo"
capabilities = ["strategy"]
command = ["python", "plugin.py"]
timeout_seconds = 20

[plugin.permissions]
network = false
env = ["OPTIONAL_VENDOR_API_KEY"]

[plugin.dependencies]
requirements = "requirements.lock"
executables = ["ffmpeg"]
```

约束：

- `id` 只能使用小写字母、数字和连字符，且必须以字母开头。
- `api_version` 支持 `"1"`、`"2"`、`"3"`、`"4"`；v1/v2/v3 插件无需任何改动继续可用。引擎当前最新为 `"4"`。
- `capabilities` 可选值及所属版本：

  | 能力 | 需要 `api_version` |
  | --- | --- |
  | `data_provider`、`strategy`、`research_tool`、`notifier` | `"1"` |
  | `analytics` | `"2"` |
  | `factor_provider`、`backtest_validator` | `"3"` |
  | `strategy_agent` | `"4"` |

  声明了高于自身 `api_version` 的能力会在加载时被拒绝，因为该能力的消息形状不属于那个版本。
- `command` 必须是参数数组，不经过 Shell 展开。
- `permissions.env` 是明确传入插件进程的环境变量白名单。QuantDesk 不会把全部环境变量或模型密钥自动传给插件。
- `permissions.optional_env` 同样是白名单，但**允许缺失**：适合"有哪个 Provider 就用哪个"的数据适配器。
  必需变量缺失会拒绝调用，可选变量缺失只是不传入，由插件自行报告该来源不可用。
- `network = false` 时，操作系统沙箱会切断网络；声明为 `true` 才会开放网络。
- `dependencies.requirements` 必须是仓库内的相对路径。锁文件只接受 `name==version --hash=sha256:...` 形式的 PyPI 包，且只安装二进制 wheel。
- `dependencies.executables` 声明插件需要的系统命令。QuantDesk 只检查是否存在，不会自动安装系统软件。

## 调用协议

核心写入一行：

```json
{
  "jsonrpc": "2.0",
  "id": "随机请求ID",
  "method": "health",
  "params": {},
  "context": {
    "plugin_id": "my-strategy",
    "api_version": "1",
    "engine_api_version": "3",
    "supported_api_versions": ["1", "2", "3"]
  }
}
```

`context.api_version` 是**该插件自己声明的版本**：v1 适配器收到的始终是 `"1"`，v3 适配器收到 `"3"`。
引擎最新版本单独放在 `engine_api_version`。插件应据此判断自己拿到的是哪套消息形状，
而不是假设引擎版本等于自己的版本。

插件必须在 stdout 只写一行响应。日志写到 stderr。

成功：

```json
{"jsonrpc":"2.0","id":"原请求ID","result":{"ok":true,"message":"ready"}}
```

失败：

```json
{"jsonrpc":"2.0","id":"原请求ID","error":{"code":-32000,"message":"原因"}}
```

输出上限为 1 MB，默认超时 20 秒。插件独享 `HOME` 和 `QUANTDESK_PLUGIN_DATA`，位置为 QuantDesk 数据目录下的 `plugin-data/<id>`。

## 业务协议

每个插件都必须实现 `health`。QuantDesk 会先核对能力声明，再对请求与响应做严格类型校验；时间戳统一为 UTC epoch 毫秒，K线必须按时间升序且不能重复。返回结构不合规时，该插件调用失败，但不会影响 API 主进程或其他插件。

### data_provider：`data.candles`

请求：

```json
{"symbol":"BTCUSDT","timeframe":"1h","startTime":1757500000000,"endTime":1757600000000,"limit":400}
```

响应：

```json
{
  "source":"vendor-name",
  "candles":[{"time":1757500000000,"open":100,"high":105,"low":99,"close":103,"volume":42,"turnover":4326}],
  "complete":true,
  "warnings":[]
}
```

`timeframe` 只接受 `15m`、`1h`、`4h`、`1d`；价格必须大于 0，成交量不能为负。

### strategy：`strategy.describe` 与 `strategy.generate`

`strategy.describe` 请求参数为空，响应一个策略目录：

```json
{
  "strategies":[{
    "id":"momentum-cross",
    "name":"动量交叉",
    "description":"收盘确认后发出事件",
    "parameters":[
      {"key":"period","label":"周期","type":"integer","default":20,"minimum":3,"maximum":200},
      {"key":"mode","label":"方向","type":"select","default":"both","options":["both","long_only"]}
    ]
  }]
}
```

参数类型支持 `integer`、`number`、`boolean`、`select`。网页会从这个目录动态生成回测表单。

`strategy.generate` 一次接收整个已收盘K线序列，避免逐 bar 启动子进程：

```json
{
  "strategyId":"momentum-cross",
  "symbol":"BTCUSDT",
  "timeframe":"1h",
  "candles":[{"time":1757500000000,"open":100,"high":105,"low":99,"close":103,"volume":42}],
  "parameters":{"period":20,"mode":"both"}
}
```

响应：

```json
{
  "signals":[{"time":1757500000000,"direction":"long","strength":0.7,"reason":"收盘上穿均线"}],
  "warnings":[]
}
```

`direction` 可为 `long`、`short`、`flat`；没有事件的K线无需返回。核心在下一根K线开盘撮合，插件不能直接提交订单。

### research_tool：`research.collect`

请求（v1 字段仍然有效；映射与 Provider 顺序由引擎决定后下发，插件不得自行猜测标的或 Provider）：

```json
{
  "symbol": "NVDAUSDT",
  "tradeDate": "2026-09-13",
  "topics": ["fundamentals"],
  "mapping": {"venueSymbol": "NVDAUSDT", "openbbSymbol": "NVDA", "finceptSymbol": "NVDAUSDT", "leveraged": false},
  "providers": ["sec", "yfinance"],
  "openbbBaseUrl": ""
}
```

响应：

```json
{
  "provider": "sec",
  "attempts": [{"provider": "yfinance", "reason": "上游超时"}],
  "source": "https://www.sec.gov/Archives/...",
  "observedAt": "2026-09-13T08:00:00Z",
  "evidence": [{
    "key": "fundamentals.sec.nvda.income.0",
    "label": "NVDA 最近季度营收",
    "value": {"total_revenue": 46700000000},
    "source": "https://www.sec.gov/Archives/...",
    "provider": "sec",
    "endpoint": "equity.fundamental.income",
    "asOf": "2026-07-31",
    "publishedAt": "2026-08-20T20:00:00Z",
    "expiresAt": "2026-09-13T20:00:00Z",
    "contentHash": "…",
    "pointInTime": true,
    "warnings": []
  }],
  "unavailable": [],
  "warnings": []
}
```

规则：

- `observedAt` 必填（v1 起如此）；`provider` 必须是真正产出该读数的来源，不能只写平台的笼统名字。
- `publishedAt` 与 `asOf` 必须分开：财务期末日期不能当作公布日期。缺失发布时间要写进 `warnings`，
  引擎的时点校验会据此拒绝历史研判使用该条证据。
- 没有数据时返回 `unavailable`（结构化的 provider + reason），**不得**返回 0、空对象或替代代码。
- 每条事实必须携带来源和观测时间，便于后续研判引用与审计。

### analytics：`analytics.portfolio` / `analytics.scenario`（v2）

风险计算始终接收 QuantDesk 合约代码与 QuantDesk 自己算出的收益率；价格事实来源仍是 Bybit。
`analytics.portfolio` 请求（节选）：

```json
{
  "asOf": "2026-09-15T10:00:00Z",
  "baseCurrency": "USDT",
  "confidence": 0.95,
  "positions": [{"symbol": "NVDAUSDT", "group": "半导体", "side": "long", "quantity": 2, "entryPrice": 180, "markPrice": 184, "notional": 368, "margin": 73.6}],
  "returns": {"NVDAUSDT": [{"time": 1789000000000, "return": 0.012}]},
  "marketSnapshotVersion": "51df6b99a689b4a2"
}
```

响应：`provider` / `asOf` / `metrics{volatility,var,cvar,maxDrawdown}` / `riskContributions[]` /
`correlation{symbols,matrix}` / `optimization|null` / `source` / `requestId` / `warnings`。
无法计算时返回 `unavailable` 字段说明原因，而不是伪造数值。

`analytics.scenario` 请求额外携带 `scenario` 与 `shocks`（引擎已把情景规则解析为逐合约冲击百分比，
因此结果可复现）；响应包含组合权益变化、单仓损失、保证金使用率变化、是否触发账户风险限制。

**该能力不得包含任何下单、改仓或调整杠杆的路径。**

### notifier：`notify.send`

请求是一条结构化事件：

```json
{"id":"job-123","type":"tradingagents.succeeded","severity":"info","title":"BTCUSDT 研判完成","message":"Hold","occurredAt":"2026-09-13T08:00:00Z","symbol":"BTCUSDT","data":{"jobId":"123"}}
```

响应：

```json
{"delivered":true,"destination":"wechat","messageId":"wx-123","detail":""}
```

后台行情失败和 TradingAgents 完成/失败事件会广播给所有已启用的 `notifier` 插件。微信、邮件等具体渠道由插件自己实现，所需凭据必须列入 `permissions.env`。

已启用能力和插件策略可通过 `GET /api/plugins/registry` 查询。内置与插件策略的统一目录为 `GET /api/strategies`。完整可运行实现见模板中的 `plugin.py`。

### factor_provider：`factor.catalog` / `factor.compute`（v3）

因子研究插件只在 QuantDesk 给出的已收盘数据上计算，**不得**自行取行情、替换交易价格或计算收益。

`factor.catalog` 请求参数为空，返回经适配器筛选后的因子目录：

```json
{
  "providerVersion": "固定版本",
  "factors": [{
    "id": "vibe:momentum-20",
    "name": "20周期动量",
    "family": "momentum",
    "mode": "time_series",
    "requiredFields": ["close"],
    "warmupBars": 20,
    "supportedTimeframes": ["15m", "1h", "4h", "1d"],
    "implementationVersion": "0.1.15",
    "sources": ["bybit"],
    "formulaHash": "…",
    "description": ""
  }],
  "warnings": []
}
```

`factor.compute` 请求：

```json
{
  "symbol": "BTCUSDT",
  "timeframe": "1h",
  "snapshotHash": "数据哈希",
  "factorIds": ["vibe:momentum-20"],
  "candles": [{"time": 1789000000000, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
  "funding": [],
  "openInterest": [],
  "auxiliary": [],
  "parameters": {}
}
```

响应 `{snapshotHash, series:[{factorId, values:[{time, value}], implementationVersion}], warnings}`。
`value` 为 `null` 表示该点处于 warmup 或数据不足，不得用 0 填充。

### backtest_validator：`validation.analyze`（v3）

输入必须是 QuantDesk **已完成**的回测结果（`runId`、`seed`、`equityCurve`、`trades`、`benchmark`、`interval`、`tests`）。
验证器只做统计诊断，不重算盈亏、资金费或强平。

输出包括 Bootstrap Sharpe 置信区间与正 Sharpe 概率、最大回撤分布、权益路径百分位、
**路径风险模拟**结果、尾部亏损范围、样本不足警告，以及使用的随机种子与算法版本。

**命名要求**：重排交易顺序的模拟只能称为"路径风险模拟 / 最大回撤分布 / 亏损连续次数分布"，
不得表述为策略显著性检验。显著性只能来自信号随机化检验（循环位移、按日分块打乱、
在不改变持仓时长分布的前提下随机化入场位置）得到的 p 值。

### 随仓库提供的参考实现：`plugins/vibe-factors`

`plugins/vibe-factors` 是本仓库自带的 v3 参考适配器（纯标准库、`network = false`、不读写文件），
同时提供 `factor_provider` 与 `backtest_validator` 两种能力，可直接安装后启用：

```bash
quantdesk plugins install ./plugins/vibe-factors
quantdesk plugins enable vibe-factors
```

- **因子目录**：28 个白名单时序因子，覆盖 momentum / trend / volatility / distribution /
  oscillator / liquidity / structure / carry / positioning 九个家族；每个因子声明
  `requiredFields`、`warmupBars`、`formulaHash` 与 `implementationVersion`。预热期一律返回
  `null`，不用 0 填充。资金费率与持仓量因子由引擎在**同一区间**内提供结算与快照数据。
- **统计验证**：移动分块 bootstrap（块长按样本长度取 `T^(1/3)`，默认 400 次重采样）、
  信号随机化 p 值、Deflated Sharpe（Bailey & López de Prado，需要尝试次数与各次 Sharpe 离散度）、
  CSCV 回测过拟合概率 PBO（把滚动窗口分半，看样本内最优候选在另一半的排名）、
  路径风险模拟与尾部亏损。固定 `seed` 时结果可复现；样本不足时返回 `unavailable` 而不是编造数字。
- **随机化方法会自报**：`randomization.method` 为 `signal_shift`（引擎提供了基准曲线，检验
  "同样的持仓暴露若随机择时会怎样"）或 `block_permutation`（只有自身收益序列时的退化检验）。
- **单次输出上限 1 MB**：引擎按 K 线数量把因子请求分批（`POST /api/factors/compute` 的
  `batches` 字段说明分了几批），插件不需要也不应该突破运行时上限。

引擎侧接口：`GET /api/factors`（目录，来自插件或已存目录）、`POST /api/factors/compute`、
`GET /api/factors/runs`、`GET /api/factors/runs/{id}`、`POST /api/factors/validate/{run_id}`
（把统计验证结论写入 `backtest_validation_results`，在结果中心展示）。

### strategy_agent：`agent.manifest` / `agent.propose` / `agent.reflect`（v4）

自我改进的交易代理。**它只提案，QuantDesk 只裁判**：代理提出候选（因子、参数、规则模板），
QuantDesk 用自己的撮合、自己的样本外数据和自己的多重检验门禁决定是否采纳。

三条边界写在协议类型里，而不是写在文档里：

1. **提案是数据，不是代码**。`AgentProposal` 禁止多余字段（`extra="forbid"`），所以一个试图
   携带 `netPnl`、`equityCurve`、`sharpe` 的提案会被协议层直接拒收，而不是被悄悄丢弃。
2. **试次摘要只有 `train` / `validation`**。`AgentTrialSummary.segment` 是枚举，
   `segment="test"` 连构造都通不过——样本外窗口由引擎在 campaign 结束时开封一次。
3. **提案必须落在提供者自己声明的空间内**。`agent.manifest` 先声明因子白名单、参数区间、
   规则模板与每轮上限；`agent.propose` 的每个提案都会按它校验，越界即拒并指出是哪一条。

`agent.manifest` 请求参数为空，返回：

```json
{
  "agentVersion": "vibe-backtest-lab/0.1.15",
  "mode": "deterministic_search",
  "proposalSpace": {
    "factorIds": ["vibe.momentum.24", "vibe.atr.14"],
    "parameters": {"fastPeriod": [5, 30], "slowPeriod": [20, 120]},
    "ruleTemplates": ["threshold"],
    "maxProposalsPerRound": 8,
    "maxRounds": 5
  },
  "requires": ["candles"],
  "never": ["order_placement", "venue_data", "keys", "frontend"],
  "providerVersion": "vibe-backtest-lab/0.1.15",
  "warnings": []
}
```

`agent.propose` 请求（只含可据以决策的信息，`group` 区分加密组与股票组）：

```json
{
  "campaignId": "cmp-2026-09-16-01",
  "round": 2,
  "snapshotHash": "数据哈希",
  "universe": ["AAPLUSDT", "MSFTUSDT"],
  "interval": "1d",
  "group": "equity",
  "factorIds": ["vibe.momentum.24"],
  "dataProfile": {"bars": 2695, "fundingPoints": 337},
  "priorTrials": [
    {"proposalId": "p-1", "segment": "validation", "sharpe": 0.8, "returnPct": 1.2,
     "maxDrawdownPct": -4.0, "trades": 12, "verdict": "warn", "reason": "验证段衰减"}
  ],
  "budget": {"proposals": 8, "deadlineMs": 60000}
}
```

返回：

```json
{
  "proposals": [{
    "proposalId": "p-2",
    "kind": "parameter_set",
    "factorIds": ["vibe.momentum.24"],
    "parameters": {"fastPeriod": 12},
    "rule": null,
    "hypothesis": "低波动下的动量延续",
    "expectedFailureMode": "震荡市反复止损"
  }],
  "warnings": [],
  "stopReason": ""
}
```

`agent.reflect` 输入上一轮的试次与剩余轮数，返回一段书面反思（`reflection`，一句人话，不是收益声明）
与下一轮提案；校验规则与 `agent.propose` 完全相同，但**请求必须携带同样的上下文字段**
（`factorIds` / `group` / `interval` / `universe` / `horizonBars`），其中 `round` 是**上一个已完成轮次**
的编号，返回的提案属于下一轮。

这条是实测补上的：第一版的 `AgentReflectRequest` 只带试次，于是第 2 轮起提供者无从知道战役冻结的
因子空间，只能自己猜——实测中它提案到了空间之外的因子上，而引擎按 manifest 核对后会把整轮拒掉。
代理只能收窄搜索空间，不能自己扩大它，所以空间必须随每次请求一起走。

`stopReason` 非空表示提供者认为没有更多候选，引擎据此收尾战役。

**QuantDesk 侧的职责**（插件不得代劳）：撮合、费用、资金费、强平与净值；训练/验证/测试分段与
Walk-Forward；campaign 级的 Deflated Sharpe 与 PBO/CSCV；试验计数与审计；晋升（只能由人点击，
且只升到"候选版本"，不进任何自动执行）。代理异常、超时或非法输出只让代理活动进入 degraded，
主回测结果不受影响。

## 安装与管理

网页「设置 → 外部插件」可以填写 GitHub HTTPS 地址和分支/标签。远程安装只允许 `https://github.com/OWNER/REPO`，安装过程只执行 `git clone` 和清单校验，不运行 `setup.py`、`pip install` 或仓库脚本。新插件默认禁用。

命令行：

```bash
quantdesk plugins list
quantdesk plugins install https://github.com/OWNER/REPO --ref v1.2.0
quantdesk plugins dependencies my-strategy
quantdesk plugins dependencies my-strategy --install
quantdesk plugins enable my-strategy
quantdesk plugins check my-strategy
quantdesk plugins disable my-strategy
quantdesk plugins update my-strategy --ref v1.3.0
quantdesk plugins uninstall my-strategy
# 确认不再需要历史数据时：
quantdesk plugins uninstall my-strategy --purge-data
```

本地开发时也可以把本地目录传给 `plugins install`。服务器额外读取 `QUANTDESK_PLUGIN_PATH`（使用系统路径分隔符分隔多个目录），适合只读挂载或开发中的仓库。

已安装仓库位于 `QUANTDESK_HOME/plugins/<id>`，插件数据位于 `QUANTDESK_HOME/plugin-data/<id>`，启停状态写入权限为 600 的 `QUANTDESK_HOME/plugins.toml`。Docker 部署中的这些路径都处于持久化 `/data` volume。

更新采用先拉取、校验，再原子替换的流程。更新仓库的 `plugin.id` 必须与现有插件一致；失败会恢复旧代码和旧启停状态。成功更新后插件自动停用，必须重新检查依赖并启用。卸载只允许已停用的托管插件，删除代码和独立运行时，默认保留 `plugin-data/<id>`；只有明确使用 `--purge-data` 才删除私有数据。

## 依赖与安全边界

安装插件仓库时不会执行任何仓库代码。用户单独触发依赖安装后，QuantDesk 会在 `plugin-runtimes/<id>` 新建专属虚拟环境，并用 `pip --require-hashes --only-binary=:all:` 安装锁文件。锁文件哈希变化后，插件会标为“依赖未就绪”，直到重建运行时。插件不能修改主程序的 Python 环境。

每次调用还包含以下限制：

- 只传入基础运行变量和清单 `permissions.env` 明确声明的变量；其他 API key 不会进入插件进程。
- `HOME`、`TMPDIR` 指向该插件的私有数据目录，插件输出限制为 1 MB，并受清单超时控制。
- macOS 使用通过探针验证的 `sandbox-exec`；Linux/Docker 使用 Bubblewrap，隐藏用户主目录和 QuantDesk 数据，只把插件代码只读挂载、插件数据读写挂载、独立运行时只读挂载。
- Docker 的 `QUANTDESK_PLUGIN_SANDBOX=required` 为故障关闭策略。沙箱启动失败时，健康检查、启用和业务调用都会拒绝执行。
- 本地默认 `preferred`。系统沙箱不可用时仍保留进程、环境白名单、超时和输出限制，但网页会显示黄色“系统隔离降级”。如需本地也强制拒绝降级，设置 `QUANTDESK_PLUGIN_SANDBOX=required`。

操作系统沙箱用于缩小插件权限，并不等同于独立虚拟机。允许网络且拿到指定 API key 的插件能够把该 key 发往网络，因此审核权限声明和仓库代码仍然必要。
