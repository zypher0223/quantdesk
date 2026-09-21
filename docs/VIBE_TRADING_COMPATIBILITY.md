# Vibe-Trading 兼容性验证记录（阶段 0）

日期：2026-09-16 · 结论：**可接入，但接入面必须收窄为「元数据 + 白名单因子计算」**
本记录只做静态验证与受控克隆，**没有执行 Vibe-Trading 的任何代码**，也没有把它装成 QuantDesk 插件。

---

## 一、来源与固定件

| 项 | 值 |
|---|---|
| 仓库 | `https://github.com/HKUDS/Vibe-Trading`（PyPI 包名 `vibe-trading-ai`） |
| 版本 | **v0.1.15**（当前最新 tag，共 12 个 tag：v0.1.4 … v0.1.15） |
| 提交 | `cc54832cb50de29d14bb10097b18e08f0a843650` |
| 许可证 | MIT（另有各 zoo 的 `LICENSE.md`：alpha101 / gtja191 / qlib158 / academic，需随因子署名） |
| Python | `requires-python = ">=3.11"` |
| 克隆位置 | `~/.quantdesk/vendor/vibe-trading`（**在 QuantDesk 仓库之外**，不是插件目录） |
| 体积 | 116 MB（含 `frontend/` 2.8M、`desktop/` 708K、`agent/src/channels/` 804K —— 均为本次排除项） |
| 依赖锁定 | `requirements-lock.txt`（**4613 行，3978 条 sha256 哈希**，由 `uv pip compile --universal --python-version 3.11 --generate-hashes` 生成）<br>sha256 = `d0d041364e5a6e00bb228f37514ccfd03c00256b2a142c88c77a20a962d9bcd6` |
| 命令入口 | `vibe-trading`（`cli:main`）与 `vibe-trading-mcp`（`mcp_server:main`） |
| 本机 uv | `~/.local/bin/uv` 可用（阶段 2 生成带哈希的最小锁文件） |

---

## 二、公开接口（静态核对，命令与实现都在仓库里）

`vibe-trading alpha` 提供五个子命令（`agent/src/factors/cli_handlers.py`，`_DISPATCH` 在 1071 行附近）：

| 子命令 | 实现位置 | 我们是否使用 |
|---|---|---|
| `alpha list` | `cmd_alpha_list`（L172） | **用**（目录） |
| `alpha show <id>` | `cmd_alpha_show`（L286） | 可用（公式与来源） |
| `alpha bench` | `cmd_alpha_bench`（L542） | **不用**：它按 Vibe 自己的 universe/period 取数，违反"不得用其他数据源替换 Bybit" |
| `alpha compare` | `cmd_alpha_compare`（L808） | **不用**（同上） |
| `alpha export-manifest` | `cmd_alpha_export_manifest`（L870） | **用**（元数据来源） |

`export-manifest` 的行为（L870–897）：
- `Registry()` → `export_manifest()` → 写 JSON 文件；支持 `--out PATH` 与 `--force`（默认拒绝写仓库外路径）；
- `Registry._scan()`（`registry.py` L219）**按 AST 扫描** `zoo/<zoo_id>/*.py`，不在扫描期执行因子代码；
- manifest 结构：`{generated_at, zoos: [{zoo_id, alphas: [{id, module_path, meta}]}], health: {loaded, failed}}`；
- 产物包含 `health`，可直接作为"目录可用性"证据。

---

## 三、因子规模与形状（关键发现）

**462 个 alpha，分布与报告一致**（按 `zoo/*/非下划线 .py` 静态计数）：

| zoo | 数量 | 来源署名 |
|---|---|---|
| alpha101 | 101 | Kakushadze《101 Formulaic Alphas》(arXiv:1601.00991) |
| gtja191 | 191 | 国泰君安 2014 短周期因子报告 |
| qlib158 | 154 | Microsoft Qlib（Apache-2） |
| academic | 12 | Fama-French 5 + Carhart 代理 |
| fundamental | 4 | 基本面 |
| **合计** | **462** | |

**每个 alpha 的形状（`zoo/alpha101/alpha_001.py`）：**

```python
ALPHA_ID = "alpha101_001"
__alpha_meta__ = {
    'id', 'nickname', 'theme': [...], 'formula_latex',
    'columns_required': ['close'], 'extras_required': [], 'requires_sector': False,
    'universe': ['equity_us', 'equity_in', 'equity_kr'],
    'frequency': ['1D'], 'decay_horizon': 5, 'min_warmup_bars': 25, 'notes': ''
}
def compute(panel: dict) -> pd.DataFrame:   # 宽表：index=时间, columns=标的
```

抽样其他 zoo：`academic` 的 `universe=['equity_us','equity_cn','equity_hk']`、`frequency=['1d']`、`decay_horizon=252`；`qlib158` 同样是多市场**日线**。`base.py` 的协议是 `compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame`，并导出 `rank()` 等**横截面**算子。

### 三条硬结论

1. **462 个 alpha 基本都是「日线 × 股票横截面」因子**，不是 15m/1h 的单标的时序因子。把它们直接用在 BTC/ETH 或 15 分钟 K 线上属于误用。
2. 与决策 **D5** 的冲突因此是结构性的：横截面因子需要在**股票组**内计算（15 个合约剔除 SOXL/SOXS → 13 个），`BTC/ETH 不做横截面 IC`——正好与 D5 一致，不需要改决策，但**必须按 universe 分流**。
3. `alpha bench/compare` 依赖 Vibe 自己的取数路径（universe/period），**不能用于 QuantDesk**：因子值必须由 QuantDesk 提供 panel（来自 Bybit 本地快照），再由插件的隔离进程调用 `compute(panel)`。这条同时满足硬约束 1、2、3（价格只来自 Bybit；Vibe 不自行取数、不计算收益）。

---

## 四、依赖与隔离

- 锁定文件**自带 `--hash=sha256`**，与 QuantDesk 插件运行时对 `requirements.lock` 的要求（逐行 `==` + `--hash=sha256:…`、禁止 URL/选项、禁止二进制以外的安装）**兼容**。
- 但 `requirements-lock.txt` 是整仓依赖（含 LLM、channels、connectors 等 4613 行），**不能整包安装**：阶段 2 要用 `uv pip compile --generate-hashes` 生成**最小子集锁**（预计仅 `pandas`、`numpy` 及 `factors` 子系统实际 import 的少数几项），并让 `requirements.lock` 只含这些。
- 运行位置：`network = false`、`env = []`（不接收任何密钥）、沙箱（本机 `preferred`，容器 `bwrap` + `required`）。
- 本机无法用 `sandbox-exec` 做文件系统过滤（macOS 27 对任何 `file-read` 过滤直接 abort，已在上一轮记录），因此本机以"环境变量白名单 + 超时 + 输出上限 + 无网络"约束，容器内以 Bubblewrap 强制。

---

## 五、自我改进交易代理（本次要接的目标）

Vibe 仓库里与"自我改进"相关的模块（静态）：

| 模块 | 内容 | 我们的处理 |
|---|---|---|
| `agent/src/goal/`（`models.py`/`policy.py`/`store.py`/`context.py`） | `GoalStatus` 含 `BUDGET_LIMITED`、`USAGE_LIMITED`、`INSUFFICIENT_EVIDENCE`、`COMPLIANCE_BLOCKED`；`policy.reject_live_execution_objective()` | **借鉴其状态机与拒绝实盘目标的策略**，在 QuantDesk 侧实现 campaign 状态 |
| `agent/src/hypotheses/` | 假设注册表（Hypothesis + Registry） | 借鉴为 `agent_proposals` 的预注册 |
| `agent/src/memory/` | 分层记忆、压缩、语义链接 | 第一版只用"试验摘要"（D1 确定性搜索），不引入其记忆系统 |
| `agent/src/governance/`、`agent/src/live/`、`connector` | 治理与**实盘连接器** | **不接入**（明确排除项） |
| `vibe-trading run -p "..."` | LLM 驱动的研究代理（需要密钥与网络） | **不接入**：D1 决定第一版用确定性搜索；D4/约束 6 决定密钥不进入插件 |

结论：我们**不调用 Vibe 的代理循环**，只把它当作"因子目录 + 因子计算"的来源，并在 QuantDesk 侧实现受治理的改进循环（阶段 5/6）。这也是唯一能满足"Vibe 不得自行取数/算收益/拿密钥"的做法。

---

## 六、仍未知的三项（阶段 2 首日验证，全部有明确回退）

| # | 未知 | 验证方式 | 若不成立 |
|---|---|---|---|
| U1 | 最小依赖子集能否跑通 `alpha export-manifest` 与 `compute(panel)` | 在隔离 venv 中用最小锁安装后离线执行；记录 import 面 | 退化为"只解析 `__alpha_meta__` AST"（不执行因子），因子计算改用自研实现 |
| U2 | 因子模块对 panel 的确切字段要求（`columns_required`/`extras_required` 是否覆盖 OHLCV） | 对白名单因子逐个核对 meta 与实际 import | 只保留字段可满足的因子 |
| U3 | 462 个中真正适配"日线股票横截面 + 可提供 panel"的数量 | 用 U1 的 venv 生成 manifest 后按 `universe`/`frequency`/`columns_required` 过滤 | 白名单以自研 28 时序因子为主，横截面因子按可用数量补充 |

---

## 七、阶段 0 结论

**可接入**，但接入面固定为：

1. **只读元数据**：`alpha export-manifest`（AST 扫描）→ 映射为 QuantDesk 的 `FactorDefinition`（`factor_definitions`）；
2. **只算因子**：QuantDesk 用本地 Bybit 快照构造 panel，交给插件的隔离进程调用白名单因子的 `compute(panel)`；
3. **横截面因子只在股票组、只在日线**（13 个合约，剔除 SOXL/SOXS）；BTC/ETH 只保留时序因子；每个因子的 `universe`/`frequency` 原样记录；
4. **不使用**：`alpha bench/compare`（自取数）、Vibe 的 `run` 代理循环、前端、桌面端、channels/连接器、通知、MCP、实盘相关模块；
5. **依赖**：阶段 2 生成带哈希的最小锁；整仓 4613 行锁文件仅作为哈希来源与版本固定依据。

**下一步**：阶段 1（插件协议 v4 `strategy_agent`），随后阶段 2 先做 U1–U3 三项验证。

---

## 八、阶段 2 实测结果（U1–U3 的答案，2026-09-16）

阶段 2 建成了 `plugins/vibe-backtest-lab/`：vendored zoo + 独立 venv + 构建期生成的白名单。
下面每一条都是在本机跑出来的，不是读文档推的。

### U1：最小依赖子集能跑通吗 —— 能，而且比预想更小

| 问题 | 结果 |
| --- | --- |
| 上游 manifest 导出 | 上游 `Registry().export_manifest()` 0.2 秒导出 462/462、0 失败（AST 扫描不执行因子） |
| 真正需要的第三方包 | **只有 pandas + numpy**。zoo 内 462 个模块**零第三方 import**；`base.py` 需要 `_backend.py`，后者惰性依赖 Vibe 设置树 |
| 最小锁 | `numpy==2.4.6`、`pandas==2.3.3`、`python-dateutil`、`six`、`pytz`、`tzdata`，全部取自上游 `requirements-lock.txt` 的哈希 |
| 4613 行整仓锁 | **不安装**，只作为版本与哈希来源 |

**版本差异是真问题**：引擎环境是 `numpy 2.5.3 / pandas 3.0.5`，上游锁定 `2.4.6 / 2.3.3`。
同一批候选因子在引擎解释器下有 **27 个直接崩**（`pandas 3.0` 破坏性变更），在插件独立 venv 里
**307/307 全部通过、0.2 秒**。这让"插件必须有自己的 venv"从洁癖变成了硬需求。

**一个上游依赖需要适配**：`base.py` → `src.factors._backend` → `src.config.accessor`（Vibe 设置树，
会拖进 pydantic）。解法不是改上游字节，而是 `tools/shims/src/config/accessor.py` 这个明确标注的
垫片，并且**固定回答"禁用 bottleneck"**：本插件不装 bottleneck，走 numpy 回退，同一份数据在任何
主机上得到同一串数字。上游源码保持逐字不变，垫片文件在 `SOURCE.json` 的 `shimFiles` 里单列。

### U2：面板字段要求 —— 比预想干净

| 面板键 | 使用它的因子数 |
| --- | --- |
| `close` | 331 |
| `volume` | 199 |
| `high` / `low` / `open` | 128 / 122 / 68 |
| `vwap` | 43 |
| `amount` | 24 |
| `benchmark_close` | 3 |
| `fund:*`（ROE、净利润…） | 各 1 |

- `extras_required` **全为空**，没有一个因子声明额外数据源；
- 频率只有 `1d`(361) 与 `1D`(101) 两种写法，实际都是日频 —— 大小写不一致，接入时必须归一；
- `universe` 全是股票市场（`equity_cn/us/hk/in/kr`）+ 1 个 `crypto`；
- **`vwap` 我们给不了**：`turnover/volume` 恰好等于 `close`。所有 43 个需要 VWAP 的因子被排除，
  而不是喂一个伪装成 VWAP 的收盘价；
- **`amount` 可以给**：引擎的 `turnover = 收盘价 × 成交量`（派生值）。白名单收录的因子若有此项，
  响应里必带一条"这是派生成交额"的警告（约束 C1）。

### U3：462 个里真正可用的数量 —— 210 个，而且判定方式本身是个发现

最初的静态判据（元数据 + 扫 `rank/scale/zscore`）给出 307 个"可用"。**这是错的**：改成让因子自己
在合成面板上跑——固定 A 的序列，把另一只标的从 B 换成 C 再算一次，A 的取值若变化就说明因子读了
横截面——真正的可用数是 **210**。

| 闸门 | 数量 | 为什么排除 |
| --- | --- | --- |
| `cross_sectional_needs_panel` | 243 | v3 的 `factor.compute` 只带一个标的的K线；单列上 `rank()` 恒为 0.5 |
| `requires_true_vwap` | 43 | 我们没有真实 VWAP |
| `degenerate_constant` | 26 | 预热之后没有区分度 |
| `requires_sector` | 19 | 需要行业分类 |
| `no_usable_values` | 6 | 2700 根日线内无取值/实测崩 |
| `requires_fundamentals` | 4 | 需要基本面字段 |
| `requires_panel_field` | 3 | 读取 `benchmark_close` 等我们没有的键 |

静态扫描漏掉横截面的原因是 `academic` 整族用了**模块内自定义**的 `_cross_sectional_zscore`，
它不出现在 import 列表里。这 12 个因子在单列面板上全部返回 null，静态法会让它们以"一列空值"
的形式混进白名单。经验判据把它们拦下来了。

**预热长度也是实测的**：`qlib158` 有 25 个因子声明 `min_warmup_bars = n` 而窗口实际需要 `n+1`。
真机验证时 `vibezoo:qlib158_beta60` 的首个非空点正好在第 61 根 —— 按声明取数据就会以 null 开头。
白名单同时记录声明值与实测值，目录报较大者。

### 阶段 2 真机验收

| 项目 | 结果 |
| --- | --- |
| `factor.catalog` | 210 个因子，`mode=time_series`，`supportedTimeframes=["1d"]`，`sources=["bybit","derived"]`，0.04 秒（不 import pandas） |
| `factor.compute`（BTCUSDT 2365 根真实日线） | 6 个因子 × 2365 点，首个非空点与实测预热逐一吻合；两次调用逐值一致 |
| 拒绝路径 | 1h 周期、未知因子、K线不足、重复 ID 全部给出指名错误 |
| 耗时 | catalog 0.04 秒（不 import pandas）；引擎路径单次 compute 稳态 0.72–0.90 秒；直连协议 0.2 秒。安装后/重启后的第一个 zoo 请求观测到 12.9 秒，清字节码缓存无法复现，判定为冷文件缓存与首次导入，不是编译 |
| 完整性检查 | 启动时重算每个模块 sha256，任一不一致就拒绝提供**全部**因子。它在第一次真机运行时就抓到过 `plugin.py` 的路径拼接 bug |
| 隔离 | 清单 `network=false`、`env=[]`；vendored 代码里没有 socket/urllib/httpx/subprocess/os.environ，也不写文件（测试逐字节核对） |
| 线上影响 | 插件以**禁用**状态装入，`vibe-factors` 仍是唯一启用的 `factor_provider`（28 个因子，`source=plugin`） |

### 顺带修掉的引擎 bug

`load_manifest` 的能力版本闸门写成了"相等"而不是"至少"：

```python
if item in CAPABILITY_MIN_VERSION and api_version != CAPABILITY_MIN_VERSION[item]
```

后果是**一个 v4 插件无法声明任何 v1–v3 能力**——而"因子库 + 自我改进代理"正好需要
`api_version=4` 同时提供 v3 的 `factor_provider`。已改成 `int(api_version) < int(min_version)`
并补了回归测试（v4 清单声明 `factor_provider`/`backtest_validator`/`strategy_agent` 必须能加载，
v3 清单声明 `strategy_agent` 仍必须被拒绝）。

### 阶段 3 的两项待定

1. **横截面因子（243 个）目前是排除项**。要用它们必须让请求携带多标的同截面面板（v5 的
   `factor.panel`），而当前 v3 请求的形状决定了做不到。这不影响 210 个时序因子先跑起来；
2. **`factor_provider` 的交接**：`vibe-backtest-lab` 只提供 `1d`，而现有 15 次因子运行与 28 个
   已存因子定义**全是 `1h`** 的 `vibe:` 因子。若直接交接，日内因子研究会消失。阶段 3 的交接方案
   必须把这两族放进同一个提供者里，而不是简单地把能力从一边挪到另一边。

---

## 九、阶段 3–4 实测结果（2026-09-16）

### 阶段 3：受控因子库（详见 `docs/FACTOR_LIBRARY.md`）

- 提供者交接完成：`factor_provider` 只由 `vibe-backtest-lab` 持有，`vibe-factors` 只留
  `backtest_validator`；一个能力一个持有者。
- **两族因子必须由同一个提供者供给**：现有 15 次因子运行与 28 个已存因子定义**全是 `1h` 的
  `vibe.*`**。若按原计划把 `factor_provider` 单纯交给只提供 `1d` 的 zoo 插件，日内因子研究会
  整体消失。解法是把 `plugins/vibe-factors/plugin.py` **逐字节复制**进插件
  （`quantdesk_factors.py` + 溯源 sidecar），由一个提供者同时回答两族；测试每次比对 sha256，
  两边不一致就直接失败。
- 七道闸门落地（`engine/src/quantdesk/factor_gates.py`），四组扫描的结论：
  **238 个因子里 1 个通过全部七道闸门**（`vibe.macd.hist`，stock/1h、24 根视野、IC +0.0485、
  p=0.0025、净 +16.19 bps），但它**换到 72 根视野就反向**（净 −16.46 bps），属于对视野敏感的
  单点结果；另有 4 个候选层因子（只差预测性一项）。
- 两个必须记住的数据事实：**股票类日线只有 62–148 根**（2026-04 起），所以 210 个日频 zoo 因子
  无法在股票组上评估；**上游存在换皮因子**（`qlib158_beta60` 是 `roc60` 的单调变换，不是 beta），
  238 个名字里 33 个落在 14 个完全同秩的簇内。
- 闸门校准：单标的时噪声因子假阳性约 20%，2 标的与 13 标的下 0/20，因此 `coverage` 要求至少
  2 个标的达标（这条要求是预测性闸门校准的一部分，不只是覆盖率选择）。

### 阶段 4：事件驱动执行模型（Gate-B，详见 `docs/EXECUTION_MODEL.md`）

十项全部落地：订单生命周期、跨K线部分成交、止损/止盈/移动止损、同根K线取不利路径、maker/taker、
独立的清算费、高周期收盘前不可见、严格 as-of、K线上限来自配置、代理数据机器可读标注。
关键证据：**默认配置下的结果与加这一层之前逐位一致**（`DefaultParityTests` 把基线数字写死在测试里），
既存的 15 个执行模型断言一字未改。新增 47 个用例。

### 本阶段修掉的引擎 bug

1. **v4 清单无法声明 v1–v3 能力**（`api_version != min_version` 写成了相等判断）。后果是"因子库 +
   自我改进代理"这种 v4 插件在加载时被直接拒绝。已改成"至少"并补回归测试。
2. **日线因子运行会因持仓量超协议上限而崩**：`interval="1d", bars=2000` 时 `_carry_inputs`
   返回 47,977 条小时级 OI，超过协议 `max_length=20000`，抛 pydantic `ValidationError`。
   已改为保留最近 20,000 条并写明截断警告，附 3 个回归测试。

---

## 十、阶段 5–8 实测结果（2026-09-16）

### 阶段 5：战役治理（`campaigns.py` + `/api/campaigns`）

预注册把**假设、三段窗口、冻结的因子空间、预算**一次写死；`agent_proposals` 只能引用冻结空间里的
因子（D4），预算越界会记进 `agent_budget_ledger` 并把战役置为 `budget_limited` 且写明原因；
测试段在开封前**既不能写也读不到**（读 409、写 422）。新增 35 个测试。

### 阶段 6：战役级统计校正与 Gate-C 判决（`campaign_stats.py`）

- **DSR 按论文口径**：N=5/20/100/400 → 0.999741779 / 0.949622298 / 0.505012790 / 0.122659098，
  期望最大 Sharpe 按 `spread·[(1-γ)Φ⁻¹(1-1/N)+γΦ⁻¹(1-1/(N·e))]` 手算核对到 1e-12；
  **没有 N 就没有 DSR**（N≤1、试验 Sharpe 少于 2 个、缺样本长度一律返回不可用 + 原因）。
- **PBO 是提案维度**（列=提案），纯噪声 24 个种子集成均值 0.459524，真有边际全部 0.0，
  "只在样本内好"的构造 1.0；`method` 明说不是参数网格维度。
- **一次性开封判决**：同一份 payload 在开封前不含测试段的时间戳、分段名或数值（有断言）。

### 阶段 7：编排（`agent_campaign.py`）

插件内置确定性搜索代理（种子由战役+轮次+已有读数决定），引擎按 `agent.manifest` 核对每个提案；
一轮 = `agent.propose`（第 1 轮）/`agent.reflect`（之后）→ 入库 → 逐个在 train/validation 上评估 →
记账 → 达限收尾。**真机跑通**：一轮 2–3 个提案、11–13 个标的等权、真实数字（Sharpe −1.2 ~ −4.3）。

### 阶段 8：前端（`agent-campaign-workspace.tsx`）

代理活动面板：预注册表单、战役列表、六张图（Sharpe/回撤/收益×回撤/预算/台账/因子使用）、试验读数表、
提案与**人工晋升**（未署名不可点、无验证段读数不可点，按钮上写明缺什么）、DSR/PBO 卡片与判决横幅。
浏览器实测（`web/scripts/campaign-smoke.mjs`）12 项全过。

### 本阶段修掉的 4 个真 bug

1. **`record_trial` 拒绝测试段**：一次性样本外测量本来就发生在搜索结束之后，旧守卫让它无法留档；
   改成"已结束的战役只允许一次测试段写入（且必须已开封）"，搜索仍不得追加试验。
2. **判决端点不跑测试段**：第一次点击判决时测试段从未评估过，判决只能是 `inconclusive`；
   改为判决前由编排开封并评估一次（幂等：已有判决则不重跑）。
3. **前端未署名也能点晋升**：组件把空署名替换成占位串，绕过了自己的校验；已修，并补了空串用例。
4. **战役级 PBO 无输入**：评估器直接跑回测、不落 `run_id`，PBO 只能报"没有可用序列"；
   改为把每次评估写成一条真实运行（组合权益曲线），PBO 因此读到引擎自己的数字。

### 验收

`engine/.venv/bin/python scripts/acceptance_check.py` 从实测生成
`docs/VIBE_INTEGRATION_ACCEPTANCE.md`：**28 项全部通过，0 未通过、0 待定**。

---

## 十一、收尾版本核查（2026-09-16）

用户要求的最后一步是"检查最新版本，有问题直接修"。核查方式与结果：

| 检查 | 命令/来源 | 结果 |
| --- | --- | --- |
| 最新标签 | `GET /repos/HKUDS/Vibe-Trading/tags` | **v0.1.15**（`cc54832cb50d`）——与我们的固定点**同一个提交**，没有更新的发布 |
| 标签序列 | 同上 | v0.1.15 > v0.1.14 > v0.1.13 > v0.1.12 > v0.1.11 |
| main 领先量 | `GET /compare/cc54832…...main` | 领先 **94** 个提交，落后 0 |
| 变更是否触及因子包 | 同一响应按路径过滤 | 变更 114 个文件，**落在 `agent/src/factors/**` 的为 0 个** |

结论：**不需要升级，也没有需要修的问题**。理由不是"看起来没事"，而是两件可核查的事实：

1. 上游最新发布就是我们固定并逐字节 vendor 的那个提交；
2. `main` 上那 94 个提交没有碰过因子包——因此 210 个白名单因子的模块 sha256、七道闸门的结论、
   以及 `factor_allowlist.json` 全部保持有效，不需要重新生成。

将来若要跟随 `main`，路径已经写在 `plugins/vibe-backtest-lab/README.md` 里：切换提交 → 重跑
`tools/vendor_zoo.py` → **复查 `factor_allowlist.json` 的差异**（收录数量、排除原因、每个模块的
sha256）→ 重跑闸门扫描，因为因子字节变了，库的证据也必须重做。

---

## 十二、数据回填：全部周期回溯到上市（2026-09-16）

按要求把固定 17 合约的K线回溯到各自上市日，并给加密类加上周线。

### 周期能力

`1w` 是本次新增的**引擎能力**（`TIMEFRAMES` 含 `1w`，Bybit 代码 `W`）。它同时引入了
`CORE_TIMEFRAMES = ("15m","1h","4h","1d")`：**信号可用性与多周期共振只看核心周期**，周线是分析
数据，不是交易前置条件——否则加一个研究周期就会静默改变现网判定。实时订阅用
`STREAMED_INTERVALS`（= 核心周期），周线由调度器维护；默认回填矩阵仍是 4 个周期，周线按需索取。

### 实测覆盖（零缺口，均回溯到上市）

| 合约 | 周期 | 根数 | 区间 |
| --- | --- | --- | --- |
| BTC | 15m / 1h / 4h / 1d / **1w** | 227,100 / 56,775 / 14,194 / 2,366 / **339** | 2020-03-25 → 2026-09-16（周线至 09-14） |
| ETH | 15m / 1h / 4h / 1d / **1w** | 193,062 / 48,265 / 12,066 / 2,011 / **288** | 2021-03-15 → 2026-09-16（周线至 09-14） |
| 15 个股票类 | 15m / 1h / 4h / 1d | 每个合约 62–14,164 根不等 | 各自上市日（2026-04-21 ~ 07-16）→ 2026-09-16 |

固定 17 合约合计 **788,045 根K线**，逐 (合约,周期) 用窗口函数核对 `ts` 间隔，**缺口数为 0**。

### 必须说清的两件事

1. **股票类永远不会有 1000 根日线**：这些永续的上市日是 2026-04-21 ~ 07-16（venue 元数据可查），
   日线只有 62–148 根。不是回填没做，是那段历史在 venue 上不存在。要更长历史只能用**标的股票本体**
   的日线做代理数据，并按约束 C1 打上 `dataProxies` 标注——这需要指定数据来源（当前仓库没有美股
   日线来源），所以没有擅自导入。
2. **非K线数据族仍有 8 个任务停在网络失败**：ETH 的资金费率/标记价K线/持仓量/风险档位与 NVDA 的
   风险档位，错误是 `ConnectError: SSL UNEXPECTED_EOF`（代理抖动），状态是"可从断点继续"。
   K 线任务（`trade_candle` 70 个）已全部完成。

### 顺带修掉的两个真 bug

1. `datahub/bybit.py` 里除了规范周期表，还藏着一份**内联的 Bybit 代码→毫秒字典**；新增周线后
   第一次拉取就 `KeyError: 'W'`。已改为统一取 `INTERVAL_MS[interval]`（单一来源），并加测试禁止
   再出现第二份表。
2. `/bybit/v5/market/kline` 网关代理把 `interval` 写死为 `'15'|'60'|'240'|'D'`，界面选周线直接
   422。已把 `W` 加入这个显式白名单（仍是白名单，不是放开任意字符串）。
