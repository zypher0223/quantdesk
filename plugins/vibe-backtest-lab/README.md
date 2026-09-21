# vibe-backtest-lab：Vibe-Trading Alpha Zoo 因子提供者

把 [Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) 0.1.15 的 Alpha Zoo 接成 QuantDesk
的 `factor_provider`。插件只做一件事：**在 QuantDesk 给出的已收盘日线上算因子，并把因子序列
交回去**。它不取行情、不下单、不算收益、不联网。

| 项目 | 值 |
| --- | --- |
| 上游 | `HKUDS/Vibe-Trading`，标签 `v0.1.15`，提交 `cc54832cb50de29d14bb10097b18e08f0a843650` |
| 许可 | MIT（`vendor/vibe-trading/LICENSE`、每个 zoo 自带 `LICENSE.md`/`NOTICE`） |
| 协议 | `api_version = "4"`，能力 `factor_provider`（v3 消息形状） |
| 依赖 | 独立虚拟环境：`numpy==2.4.6`、`pandas==2.3.3` + 它们自己的依赖，全部哈希锁定 |
| 因子 | 307 个候选里收录 **210** 个单标的日频价量因子；排除原因逐条记录 |
| 周期 | 只提供 `1d`；请求其它周期会被明确拒绝，而不是把日频因子换个标签 |

## 为什么需要独立虚拟环境

上游锁定的是 `numpy 2.4.6` / `pandas 2.3.3`，而引擎环境跑的是 `pandas 3.x`。把 zoo 的代码
拿去引擎解释器里跑，307 个候选里有 27 个直接崩（`pandas 3.0` 的破坏性变更）。插件清单里的
`[plugin.dependencies] requirements = "requirements.lock"` 让 QuantDesk 为它建一个独立 venv，
`command = ["python", "plugin.py"]` 里的 `python` 会被换成那个 venv 的解释器。锁文件是从上游
`requirements-lock.txt` 里摘出的最小传递闭包：`pandas`、`numpy`、`python-dateutil`、`six`、
`pytz`、`tzdata`。上游的 `scipy`/`bottleneck`/`rich`/`pydantic` 一个都不装——库内因子不 import
它们（`bottleneck` 只影响速度，zoo 自带 numpy 回退，见下面的适配垫片）。

## 白名单是构建产物，不是承诺

`factor_allowlist.json` 由 `tools/vendor_zoo.py` 生成，记录每个收录因子的模块路径与模块
sha256。插件启动时会重新计算每个模块的哈希，**任何一个字节不一致就拒绝提供全部因子**并说明
是哪个因子。这条检查在第一次真机运行时就生效过一次：`plugin.py` 早期版本把 `modulePath` 拼到
了多一层 `src` 的路径上，插件没有悄悄返回空序列，而是直接拒绝启动。

排除不是靠读元数据猜的，而是让因子自己在合成面板上跑一遍：

| 闸门 | 数量 | 含义 |
| --- | --- | --- |
| `cross_sectional_needs_panel` | 243 | 固定 A 的序列、把另一只标的从 B 换成 C 后，A 的取值变了：因子读了横截面 |
| `requires_true_vwap` | 43 | 需要真实 VWAP；QuantDesk 只有收盘价×成交量，derive 出来会退化成 `close` |
| `degenerate_constant` | 26 | 预热之后取值没有区分度（横截面算子落在单列上就是常数） |
| `requires_sector` | 19 | 需要行业分类 |
| `no_usable_values` | 6 | 2700 根日线之内没有任何非空取值，或实测直接崩 |
| `requires_fundamentals` | 4 | 需要 ROE/净利润等基本面字段 |
| `requires_panel_field` | 3 | 读取面板上没有的键（如 `benchmark_close`） |

横截面那一条值得单独说明。v3 的 `factor.compute` 请求只携带**一个标的**的K线，而
`rank()`/`zscore()` 是跨标的算子：单列面板上 `rank()` 恒等于 0.5，算出来是一列没有信息的常数。
早期版本用静态扫描找 `rank`/`scale`/`zscore`，只查出 146 个；改成"换掉另一只标的再看结果"
之后查出 202 个以上——`academic` 整族用了模块内自定义的 `_cross_sectional_zscore`，静态扫描
完全看不见它，12 个因子会以"全 null"的形式混进白名单。

预热长度也是**实测**的。`qlib158` 有 25 个因子声明 `min_warmup_bars = n`，而窗口实际需要
`n + 1` 根；只按声明取数据，因子序列的第一根会是 null。白名单里同时记录 `declaredWarmupBars`
与 `measuredWarmupBars`，目录里报的是两者较大值。真机验证时 `vibezoo:qlib158_beta60` 的首个
非空点正好落在第 61 根。

## 面板：只有引擎给的东西

因子拿到的面板由请求里的K线现搭：`open/high/low/close/volume` 直接来自K线，`amount` 取
`turnover`。引擎的 `turnover` 是 `收盘价 × 成交量`（不是逐笔成交额），所以只要请求里出现过
`amount`，响应就会带一条说明这是派生存列而不是逐笔成交额的警告。

`vwap` 一概不提供：用 `turnover / volume` 算出来的"VWAP"恰好等于 `close`，那不是近似，是伪造。
所有需要 VWAP 的因子因此被排除而不是被喂一个假的。

## 用法

```bash
cd engine
python -m quantdesk.cli plugins install ../plugins/vibe-backtest-lab
python -m quantdesk.cli plugins dependencies vibe-backtest-lab --install   # 建独立 venv
python -m quantdesk.cli plugins check vibe-backtest-lab                    # health
python -m quantdesk.cli plugins enable vibe-backtest-lab
```

一次请求最多算 32 个因子；超出请分批。实测：`factor.catalog` 约 0.04 秒（不 import pandas），
单次 `factor.compute` 走引擎路径稳态 0.7–0.9 秒（含进程启动、pandas 导入与沙箱探测），
不经引擎直接调协议 0.2 秒。插件刚安装后、或服务刚重启后的第一次 zoo 请求观测到过约 13 秒，
清掉字节码缓存并不能复现它，所以那是冷文件缓存/首次导入的开销，不是编译，且远在 120 秒超时之内。

## 重新生成白名单

上游升级时，`git -C ~/.quantdesk/vendor/vibe-trading fetch && git checkout <tag>`，然后：

```bash
~/.quantdesk/plugin-runtimes/vibe-backtest-lab/bin/python tools/vendor_zoo.py \
  --upstream ~/.quantdesk/vendor/vibe-trading
```

构建工具必须用插件自己的运行时解释器（只有那里有钉住的 pandas/numpy），并且**不会** import
上游的 registry——元数据用 AST 读取，所以构建过程不需要 pydantic，也不碰 Vibe 的设置树。
升级后请复查 `factor_allowlist.json` 的差异：收录数量、排除原因、以及每个模块的 sha256。

## 目录

```text
quantdesk-plugin.toml        清单：能力、独立依赖、无网络、无环境变量
plugin.py                    JSON-RPC 适配器：health / factor.catalog / factor.compute
requirements.lock            6 个包的哈希锁定版本（摘自上游锁文件）
factor_allowlist.json        构建产物：210 个收录因子 + 252 条排除记录
vendor/vibe-trading/         上游 MIT 源码副本（逐字复制）+ SOURCE.json 溯源
  src/config/                仅这两个文件是 QuantDesk 写的适配垫片（见下）
tools/vendor_zoo.py          构建工具：vendor + AST 元数据 + 实测闸门
tools/shims/                 适配垫片的真身
```

### 适配垫片

`base.py` 从 `src.factors._backend` 取 `HAS_BOTTLENECK`/`bn`，而 `_backend` 的惰性访问器会去读
Vibe 自己的设置树（`src.config.accessor`）——那会把 pydantic 和整套 agent 配置拖进一个因子
计算器。`tools/shims/src/config/accessor.py` 替掉了它，并固定回答"禁用 bottleneck"：本插件不装
bottleneck，走 numpy 回退，于是同一份数据在任何主机上得到同一串数字，而不是取决于碰巧装没装。
上游字节保持逐字不变，两个垫片文件在 `SOURCE.json` 的 `shimFiles` 里单独列出。
