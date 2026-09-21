# QuantDesk 外部适配器

本目录存放 QuantDesk 的可选外部数据与分析适配器。它们都通过插件协议通信，**不进入主引擎虚拟环境**。

## 共同边界

| 约束 | 说明 |
| --- | --- |
| 进程隔离 | 每个适配器是独立子进程，通过 stdin/stdout 的单行 JSON-RPC 通信 |
| 环境隔离 | 依赖装在各自的运行环境（`~/.quantdesk/plugin-runtimes/<id>`）的独立 venv 中，绝不装进 `engine/.venv` |
| 环境变量白名单 | 只透传清单里 `permissions.env` / `permissions.optional_env` 声明的变量 |
| 网络 | 由 `permissions.network` 控制；关闭时操作系统沙箱会拦截连接 |
| 超时与输出 | `timeout_seconds` 与 1 MB stdout 上限由引擎强制 |
| 默认关闭 | 所有适配器默认 `enabled = false`，未启用时 QuantDesk 完整运行 |

## openbb-research

基本面、财报、新闻、宏观与美股参考数据，输出**带发布时间的标准化证据**。

* 运行环境二选一：
  * OpenBB Platform REST：设置 `OPENBB_API_URL`，插件不导入 OpenBB；
  * OpenBB Python：在插件自己的 venv 中安装 `openbb`，插件在该进程中调用。
* 两者都没有时，插件报告"运行环境不可用"，**不会**退回到内置示例数据。
* Provider 由引擎按 `[openbb.providers]` 指定并下发；插件按顺序尝试并记录每个 Provider 的失败原因。
* 发布时间（`publishedAt`）与财务期末（`asOf`）分开保存；缺失发布时间会写入 warning，历史研判由引擎的时点校验拒绝该条证据。

```bash
# 安装（源码目录可直接安装）
curl -X POST http://127.0.0.1:4173/api/plugins/install \
  -H 'Content-Type: application/json' -d '{"source":"plugins/openbb-research"}'
```

### 许可

OpenBB 采用 AGPL-3.0 并提供商业许可选项。本适配器**只调用** OpenBB，不复制其代码、不把其依赖打进安装包；运行环境由使用方自行准备。对外分发或提供网络服务前，需重新确认 OpenBB 的许可条件。

## fincept-analytics

组合风险（VaR/CVaR/波动率/相关性/风险贡献）、组合优化与压力测试/情景分析。

* **只走官方 REST API**（`FINCEPT_API_URL` + `FINCEPT_API_KEY`）。
* 不克隆、不安装、不复制 Fincept Terminal 仓库代码；不使用其 Qt 界面、Agent、回测、模拟盘或券商代码；不使用其名称、Logo 或界面样式。
* 风险计算始终接收 QuantDesk 合约代码与 QuantDesk 收益率，价格事实来源仍是 Bybit。
* 结果只用于风险提示，不能绕过 QuantDesk 本地开仓限制，也不能提交订单或修改模拟盘持仓。

### 许可

Fincept Terminal 公开仓库为 AGPL-3.0-or-later，其说明指出直接链接或构建进另一分发产品会带来 AGPL 义务。因此本适配器**不接触其仓库代码**，仅通过官方 API 交互。打包前需重新进行许可验收。

## vibe-factors

自研的时序因子库（28 个价量/资金费率/持仓量因子，支持 `15m/1h/4h/1d`）与统计验证器
（移动分块 bootstrap、信号随机化 p 值、Deflated Sharpe、PBO/CSCV）。纯标准库运行，无依赖、
无网络、不下单、不重算盈亏。当前是唯一启用的 `factor_provider` 与 `backtest_validator`。

## vibe-backtest-lab

[Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) 0.1.15 Alpha Zoo 的因子提供者：462 个上游
因子里收录 **210** 个单标的日频价量因子，全部在 QuantDesk 给出的已收盘日线上计算。

* 上游 MIT 源码**逐字 vendor** 进插件目录，`vendor/vibe-trading/SOURCE.json` 记录仓库/标签/提交/
  锁定件哈希，每个 zoo 自带 `LICENSE.md`/`NOTICE`；
* 白名单 `factor_allowlist.json` 是构建产物：记录每个因子的模块路径与 sha256，插件启动时逐个
  重算哈希，**不一致就拒绝提供全部因子**；
* 排除是实测的，不是读元数据猜的：固定一只标的、换掉同截面的另一只标的再算一次，取值变化的
  就是横截面因子（243 个），v3 请求只带一个标的的K线，单列 `rank()` 是常数；
* 只提供 `1d`：zoo 的窗口以交易日计数，请求其它周期会被明确拒绝，而不是换个标签；
* `network=false`、`env=[]`，vendored 代码里没有网络/子进程/环境变量读取；
* 依赖跑在自己的 venv 里（`numpy 2.4.6` + `pandas 2.3.3`），因为引擎的 `pandas 3.x` 会让其中
  27 个因子直接崩。

详见 `plugins/vibe-backtest-lab/README.md` 与 `docs/VIBE_TRADING_COMPATIBILITY.md`。
