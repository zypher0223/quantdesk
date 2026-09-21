# QuantDesk 下一阶段执行报告（交给 DeepSeek）

日期：2026-09-15  
项目目录：`/path/to/quantdesk`  
目标：在继续增加策略、Skill 或实盘接口前，先把 QuantDesk 建成可长期运行、低延迟、可恢复的短线行情与告警系统。

---

## 一、给执行者的任务

请在现有 QuantDesk 代码上继续开发，不要重写项目。第一轮只执行本报告的 **P0：实时行情数据层与低延迟告警**。完成 P0、全部测试和实际页面验证后停止，并提交实施结果报告。P1–P3 是后续路线，不要在本轮混入。

开始前必须确认正在修改的是此副本：

```bash
cd "/path/to/quantdesk"
engine/.venv/bin/python -c "import quantdesk; print(quantdesk.__file__)"
```

输出路径必须包含当前项目的 `engine/src/quantdesk`。如果虚拟环境仍指向其他副本，先修复可编辑安装，再开发。

## 二、不得破坏的现有约束

1. 固定合约池保持 17 个：AAPL、MSFT、GOOGL、AMZN、NVDA、META、TSLA、SNDK、MU、AMD、NBIS、SPCX、SKHY、SOXL、SOXS、BTC、ETH。
2. 股票和 ETF 都使用 Bybit TradFi USDT 永续合约行情；AMD 的交易所代码是 `AMDSTOCKUSDT`。
3. 技术指标、价格、成交量、资金费率、持仓量只使用交易所数据。公开证券代码只可用于新闻与基本面。
4. SPCX、SKHY 保持基本面分析启用。
5. 所有自动信号、共振和回测只使用已收盘 K 线。
6. 不新增下单、改仓或任何实盘交易接口。
7. 不读取、输出、迁移或覆盖用户的 API Key；自动测试不得调用付费模型。
8. 保留现有插件协议、子进程隔离、模拟盘、日志完整性、TradingAgents 队列与告警语义。
9. 所有 Bybit 网络访问统一使用 `configured_proxy()`，不得绕开用户代理配置。
10. 上游异常时不得把演示数据伪装成实时数据。交易工作区应优先保留最后一份真实数据并明确标记过期。

## 三、当前可用基线

现有版本已经具备：

- Bybit 固定合约池、15m/1h/4h/1d K线、ticker、资金费率与持仓量。
- 多周期共振；共振优先读取后台 SQLite K线缓存，本地缺失时才访问 Bybit。
- 三个内置规则策略、插件策略注册表与统一回测引擎。
- 模拟交易、止损、两级止盈、资金费结算、强平监控和不可修改交易日志。
- 真正的 TradingAgents 多智能体子进程与持久化异步队列。
- 插件安装、更新、卸载、依赖锁定、能力注册与运行隔离。
- 长期运行调度器、组合告警规则、数据质量门禁与运行监控。
- macOS `launchd` 常驻服务，以及尚未在真实 Linux 主机验证的 Docker Compose 部署。
- 最近基线：后端 **200 passed、1 skipped**；前端生产构建通过。

刚完成的刷新优化不能回退：

- 手动刷新只等待 K线与 ticker，共振在后台更新。
- 五分钟内的共振缓存不会因手动刷新被清空。
- 香港代理节点下，网页实测刷新约 0.49 秒。

## 四、为什么下一步必须做 P0

后台采集器目前每 20 秒只轮转一个合约。17 个合约完整轮转约需 340 秒，因此同一合约可能 5–6 分钟才更新一次。对日线研究影响不大，但会限制 BTC/ETH 合约和股票永续 15m 短线告警。

浏览器仍主要依赖 30 秒定时 REST 请求。代理换节点、连接池失效或 Bybit 断开时，首次 HTTPS 隧道会出现明显延迟。模拟盘标记价和告警也存在各自访问行情的路径，继续叠加功能会造成重复连接、口径不一致和请求突发。

因此要先建立一个进程内唯一的行情服务，让网页、共振、告警、模拟盘和研究层读取同一份标准化行情状态。

---

## 五、P0：实时行情数据层与低延迟告警

### 5.1 总体行为

实现一个由 FastAPI lifespan 启动和关闭的 `MarketDataService`：

1. 启动时从 SQLite 立即恢复最近 K线和衍生品快照，让页面不等待公网即可显示最后一份真实数据。
2. 使用 Bybit V5 公共 WebSocket 接收 ticker 与 K线更新；只在 K线确认收盘后写入正式 candles 表并触发策略/告警。
3. REST 负责启动回填、断线补洞、合约元数据与周期性校验，不再作为浏览器每 30 秒的主要数据源。
4. WebSocket 断线时指数退避重连并加入随机抖动；重连成功后用 REST 补齐断线窗口，再恢复实时流。
5. iKuuu 切换节点或代理短暂消失时，服务保持运行，页面继续显示最后真实快照和明确的 stale 状态。
6. 现有轮转采集器保留为数据对账与兜底任务，避免与实时服务重复写入或形成请求风暴。

实现前先核对 Bybit 官方 V5 WebSocket 文档当前的公共 linear 地址、topic 格式、确认收盘字段、连接订阅上限、ping/pong 和重连规则。不要凭记忆写协议。根据官方限制拆分连接与订阅。

### 5.2 订阅范围

- BTCUSDT、ETHUSDT：持续订阅 ticker 和 15m/1h/4h/1d K线。
- 15 个 TradFi 合约：持续订阅四周期收盘事件；ticker 可以根据官方 topic 限制分批订阅。
- 如果 17 个合约 × 多 topic 超出单连接限制，按固定、可测试的分片规则拆为多条连接。
- 不允许由浏览器传入任意 symbol；所有订阅必须经过固定合约池白名单。

### 5.3 建议代码结构

新增或调整以下位置，名称可按现有风格微调：

```text
engine/src/quantdesk/datahub/realtime.py       # Bybit WS 协议、重连、心跳、消息标准化
engine/src/quantdesk/datahub/market_service.py # 唯一行情状态、订阅分片、REST补洞、事件发布
engine/src/quantdesk/datahub/db.py             # ticker/mark/index/funding/OI 快照持久化与读取
engine/src/quantdesk/api/server.py              # lifespan 管理与快照/推送接口
engine/src/quantdesk/scheduler/service.py       # 降级为对账、回填与兜底
engine/src/quantdesk/alerts/engine.py           # 由已收盘K线事件触发目标规则
engine/src/quantdesk/paper/engine.py             # 优先读取统一标记价快照
web/src/services/api.ts                          # 快照与实时流客户端
web/src/hooks/use-market.ts                      # 本地快照启动、实时增量、重连与过期状态
web/src/data/market.ts                           # provenance/freshness/connection 类型
web/src/components/monitoring-workspace.tsx      # WS、重连、延迟、积压与最后事件监控
```

### 5.4 数据与事件协议

定义统一内部事件，至少包括：

```text
TickerUpdated
CandleUpdated       # 尚未收盘，只供当前图表更新
CandleClosed        # 可持久化、可触发共振/策略/告警
DerivativesUpdated
ConnectionChanged
BackfillCompleted
```

每个事件至少携带：

```text
venue
venue_symbol
event_type
interval（适用时）
exchange_ts
received_ts
sequence 或稳定 observation_key
source = websocket | rest_backfill | sqlite
payload
```

要求：

- `CandleClosed` 必须幂等；相同 `symbol + interval + open_ts` 重放不得重复触发告警。
- 乱序消息按交易所时间处理，旧 ticker 不得覆盖新 ticker。
- 当前未收盘K线可以更新图表，但不得进入正式策略、共振、回测或告警计算。
- REST 回填与 WebSocket 同时到达时，以交易所时间和完整性为准，SQLite 主键继续负责最终去重。

### 5.5 本地 API

建议提供：

```text
GET /api/market/snapshot?symbol=BTCUSDT&interval=15m
WS  /api/market/stream
```

快照响应至少包含：

- 最近已收盘 K线；可单独包含一根正在形成的 K线。
- ticker、mark price、index price、funding、OI。
- 各部分 `source`、`exchangeTs`、`receivedTs`、`ageMs`、`stale`。
- 当前上游连接状态、最近一次成功时间。

本地 WebSocket 只接受固定池 symbol 与四个合法周期；同一浏览器重连不得在服务端遗留订阅。若实现 SSE 更符合现有依赖，也可以使用 SSE，但必须支持断线自动恢复和事件游标。

### 5.6 前端行为

1. 页面先读取 `/api/market/snapshot`，快速显示 SQLite 中最后真实行情。
2. 随后连接本地实时流，增量更新 ticker 和当前K线。
3. 切换合约或周期时取消旧订阅；防止旧请求覆盖新选择的 generation 保护必须保留。
4. 实时流正常时取消 30 秒常规轮询；仅保留低频健康检查和断线后的 REST 兜底。
5. 上游断线后冻结最后真实数据，明显展示“行情已中断/数据距今多久”；不要自动切换演示行情。
6. 演示数据只允许在明确的演示模式或没有任何历史真实数据时使用，并持续显示“演示”。
7. 共振仍后台更新，不得重新阻塞 K线与 ticker。

### 5.7 告警与模拟盘

- `CandleClosed` 到达后，只评估该 symbol 且依赖该 timeframe 的规则，不要扫描全库。
- 告警仍必须经过数据质量门禁、确认次数、迟滞、静默时段、冷却和每日上限。
- 相同 observation 不得因 WebSocket 重放、REST 回填或进程重启重复通知。
- 模拟盘优先读取行情服务中的最新 mark price；缺失或过期时不得假定价格，按现有安全逻辑报告不可用。
- TradingAgents 不跟随每个实时事件运行。它继续作为低频、按需或定时的深度研判层。

### 5.8 代理与连接稳定性

- 代理地址由 `configured_proxy()` 读取，禁止硬编码 `127.0.0.1:7893`。
- 记录代理是否可达、WebSocket 连接状态、连续重连次数、最近 pong、消息延迟、最后成功 REST 回填。
- 代理切换导致连接关闭时，进程不能退出，FastAPI `/health` 仍应返回；状态页必须显示行情链路降级。
- 对 403 地区限制、429、超时、代理拒绝、远端无响应分别分类，避免都显示为“网络错误”。
- 不把代理 URL 中的用户名、密码或完整地址返回浏览器。

## 六、P0 测试要求

### 6.1 自动测试

新增有意义的测试，至少覆盖：

1. ticker 与 K线 WebSocket 消息标准化。
2. 未收盘 K线不触发规则，确认收盘后只触发一次。
3. 同一闭合 K线经 WS 和 REST 重放仍只写入、评估一次。
4. 乱序 ticker 不覆盖更新数据。
5. 断线指数退避、重连成功、补洞后继续消费。
6. 代理不可达时服务和本地快照 API 仍可用。
7. 固定池以外 symbol 和非法周期被拒绝。
8. 浏览器切换 symbol 后旧事件不能覆盖当前图表。
9. 模拟盘遇到过期 mark price 时保持安全降级。
10. 不调用真实模型、不发通知、不触发任何交易行为。

必须运行：

```bash
engine/.venv/bin/python -m pytest engine/tests -q -p no:cacheprovider
cd web && npm run typecheck && npm run build
```

### 6.2 实际联调

在香港代理节点下验证：

1. AAPL、BTC、ETH 的 15m 和 1h 页面可加载。
2. 页面刷新不会重新请求四周期共振。
3. 切换标的后价格、symbol、周期不会串数据。
4. 暂停代理，再恢复代理，QuantDesk 进程保持存活并自动恢复行情。
5. 重启 QuantDesk 后，页面先显示 SQLite 最后真实数据，再恢复实时流。
6. 运行监控显示真实连接状态和最近消息延迟。

不要为了联调等待真实 15m 收盘。用录制消息或本地 fixture 做闭合事件测试，再用真实市场只验证连接与增量更新。

### 6.3 验收指标

- 有本地快照时，页面主要行情首次可见：目标 **≤500 ms**。
- 正常网络下，交易所 ticker 到页面：目标 p95 **≤2 s**。
- 15m K线确认收盘到入库和告警评估：目标 p95 **≤15 s**。
- 代理恢复后自动重连：目标 **≤30 s**。
- 同一闭合 K线重复入库数：**0**。
- 同一 observation 重复告警数：**0**。
- 上游断线时页面错误标识必须出现，最后真实行情不得变成未标注演示数据。
- 现有 200 项测试不得回退，新增测试全部通过，前端生产构建通过。

## 七、P0 完成后必须提交的结果

最终回复按以下结构输出：

```text
1. 改动摘要
2. 新增/修改文件
3. 行情生命周期说明（启动、实时、断线、补洞、关闭）
4. 数据一致性和幂等保证
5. 自动测试结果
6. 香港代理实际测试数据
7. 未解决问题与风险
8. 是否满足每一条验收指标
```

不要只说“已完成”；必须给出测试数量、关键接口响应、重连结果和实测延迟。发现范围外问题时记录，不要顺手加入实盘或大规模改写。

---

## 八、P1–P3 后续路线（本轮不要执行）

### P1：模拟盘和回测与交易所风险口径对齐

- 接入 Bybit 风险限额分档、真实维持保证金率和逐仓强平参数，替换固定 MMR 近似。
- 资金费回测使用真实结算时间和可用历史 mark price，缺失时明确降级。
- 增加 walk-forward、训练/验证/样本外切分、参数稳定性与过拟合提示。
- 增加基准对比、收益分布、最大不利变动、连续亏损、资金容量和成交量约束。
- SOXL/SOXS 明确处理底层 ETF 日内杠杆与每日再平衡限制，不能把合约杠杆与 ETF 自带杠杆混为一层风险。

### P2：真正验证脱离本机部署

- 在 Linux + Docker Compose 上实际构建固定 TradingAgents 提交的镜像。
- 建立 SQLite schema version 和可回滚迁移，提供备份、恢复、完整性检查命令。
- 验证 Docker volume 迁移、服务异常重启、升级回滚和健康检查。
- 远程访问使用 HTTPS 反向代理或私有 VPN；Basic Auth 不得裸跑公网 HTTP。
- 在容器内实测 Bubblewrap required 模式，沙箱不可用时必须拒绝插件运行。

### P3：AI 成本治理、通知和上线前门禁

- 为 TradingAgents 增加单任务 token/金额预算、每日预算、超限拒绝、结果缓存和可见成本统计。
- 建立“规则信号先触发、人工决定是否启动多智能体”的流程，避免短线每个事件调用大模型。
- 接入微信通知时只发送摘要、触发条件、数据时间、失效条件和本地报告链接；API Key 不得进入消息。
- 在任何实盘开发前，至少完成连续 30 天模拟盘、告警去重统计、故障演练和回测/模拟偏差报告。

## 九、产品优先级结论

下一项应当是 **P0 实时行情数据层与低延迟告警**。它直接决定 15m 短线信号是否及时，也能统一 K线、共振、模拟盘、告警和研究层的数据口径。完成 P0 后再做风险模型和部署验证；继续增加更多策略或外部 Skill 应排在这些基础能力之后。
