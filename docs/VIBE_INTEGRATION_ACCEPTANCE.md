# Vibe-Trading 接入验收报告（阶段 9）

本文件由 `scripts/acceptance_check.py` **从实测生成**：每一行的证据都是刚刚跑出来的（实时接口、磁盘上的字节、测试套件、静态扫描）。跑不动的检查写「待定」并给出原因，不会静默通过。

- 生成时间：2026-09-16 09:37:17
- 目标地址：http://127.0.0.1:4173
- 结果：**通过 28** / 未通过 0 / 待定 0

| # | 检查项 | 结果 | 证据 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | 协议 v4 与能力表 | ✅ 通过 | GET /api/plugins → apiVersion=4, supported=['1', '2', '3', '4'] | strategy_agent 在能力表里：True |
| 2 | 插件清单与启用状态 | ✅ 通过 | vibe-backtest-lab 0.2.0 启用 [factor_provider,strategy_agent]；vibe-factors 0.2.0 启用 [backtest_validator] | 无效清单 0 个 |
| 3 | 一个能力一个持有者 | ✅ 通过 | backtest_validator=['vibe-factors']；factor_provider=['vibe-backtest-lab']；strategy_agent=['vibe-backtest-lab'] | 每个能力只有一个启用持有者 |
| 4 | 因子目录：两族因子 | ✅ 通过 | provider=vibe-backtest-lab，zoo 210 个 + 自研 28 个 | 目录来源 plugin |
| 5 | 白名单与 vendored 字节一致 | ✅ 通过 | 白名单 210 个，逐模块 sha256 校验，0 个不一致 | 上游提交 cc54832cb50d（v0.1.15） |
| 6 | vendored 代码无网络/子进程/环境变量 | ✅ 通过 | 扫描 vendored 代码 474 个文件，命中 0 处 | 没有网络、子进程或环境变量读取 |
| 7 | 上游来源与许可固定 | ✅ 通过 | https://github.com/HKUDS/Vibe-Trading @ v0.1.15 (cc54832cb50d)，481 个文件，许可 MIT | 垫片文件：src/config/__init__.py, src/config/accessor.py |
| 8 | 受控因子库来自闸门扫描 | ✅ 通过 | 闸门扫描 3 组，入库 5 个（其中已验证 1 个） | 库由扫描报告决定，改库只能重跑扫描 |
| 9 | 战役治理：D1/D2 与窗口 | ✅ 通过 | D1 拒绝 LLM 模式=✓；D2 拒绝超预算=✓；拒绝窗口重叠=✓；至少有一个真实战役=✓ | 因子目录 provider=vibe-backtest-lab；库内战役 7 个 |
| 10 | 测试段封存（读与写都不可） | ✅ 通过 | camp-5fc74e54a：读测试段 HTTP 409，写测试段 HTTP 422 | 封存期间两件事都做不到 |
| 11 | D3：只有人能晋升 | ✅ 通过 | 无批准人晋升 → HTTP 422：[{'type': 'string_too_short', 'loc': ['body', 'approvedBy'], 'msg': 'S | 晋升必须署名，代理没有这条路径（模型层或服务层拒绝都算） |
| 12 | 队列支持战役轮次 | ✅ 通过 | RUN_KINDS / REQUEST_MODELS / STUDIES 都含 campaign：True True True | 一轮战役在 worker 里执行，不占 HTTP 请求 |
| 13 | 引擎全量测试 | ✅ 通过 | 1054 passed, 2 skipped, 26 subtests passed in 61.63s (0:01:01) | engine/tests 全量 |
| 14 | 执行模型（Gate-B） | ✅ 通过 | 61 passed, 7 subtests passed in 0.14s | Gate-B：默认逐位一致 + 十项执行模型 |
| 15 | 战役治理测试 | ✅ 通过 | 58 passed in 0.30s | 预注册、预算、可见性、编排 |
| 16 | 插件协议与白名单测试 | ✅ 通过 | 132 passed, 1 skipped in 5.51s | 协议 v4、白名单、两族因子 |
| 17 | 前端逻辑测试 | ✅ 通过 | ℹ pass 115（npm test） | 前端纯逻辑测试，含封存与晋升规则 |
| 18 | 前端产物含代理面板 | ✅ 通过 | index-B2m9yiux.js 878 KB，含代理战役面板：True | 服务端从这里提供页面 |
| 19 | 浏览器实测面板 | ✅ 通过 | 浏览器检查通过：浏览器检查通过。 | 真实战役数据下的渲染、封存提示与晋升可用性 |
| 20 | 文档落点 | ✅ 通过 | 6/6 份文档在位 | 阶段 0/3/4 的结论都有落点 |
| 21 | 排除项未被引入 | ✅ 通过 | 插件侧无下单路径：True；引擎侧屏蔽清单命中：无 | 排除项（Vibe 前端/桌面/连接器/通知/MCP/实盘）未被引入 |
| 22 | 新表幂等、无需迁移步骤 | ✅ 通过 | 两次打开：5 5 True | 新表走 CREATE TABLE IF NOT EXISTS，重复打开既不改形状也不报错 |
| 23 | 容器隔离（bwrap） | ✅ 通过 | 镜像设置 QUANTDESK_PLUGIN_SANDBOX=required 并安装 bubblewrap：True | 本机 macOS 的 sandbox-exec 探针失败（已知），容器内由 bwrap 强制 |
| 24 | 本机沙箱状态如实上报 | ✅ 通过 | 策略 preferred，enforced=False：检测到 sandbox-exec，但沙箱探针失败：探针被 SIGABRT 终止（无 stderr 输出）；按 preferred 策略，插件仍会运行，但没有操作 | 未强制时页面与 API 都会明说，不假装有隔离 |
| 25 | 阶段 10 保持关闭 | ✅ 通过 | engine/src/quantdesk/campaigns.py 拒绝非确定性模式：True | 阶段 10（LLM 辅助）保持关闭，需要单独开启 |
| 26 | 代理数据标注（C1） | ✅ 通过 | 回测结果带代理数据标注：True；因子库文档记录股票类日线只有 5 个月：True | 约束 C1：代理/派生数据机器可读标注 |
| 27 | 战役级 DSR/PBO | ✅ 通过 | 53 passed in 1.31s | campaign_stats.py 已落地 |
| 28 | 开封判决接口 | ✅ 通过 | POST/GET /api/campaigns/{uid}/verdict 已在路由里 | 阶段 6 交付 |

## 复现方式

```bash
cd engine && ./.venv/bin/python -m pytest -q          # 引擎全量
cd web && npm test && npm run build                    # 前端逻辑与产物
cd web && node scripts/campaign-smoke.mjs              # 浏览器实测
engine/.venv/bin/python scripts/acceptance_check.py    # 重新生成本报告
```

## 仍然待定的两项

第 27、28 项属于阶段 6（战役级 DSR/PBO 与开封判决）。它们尚未落地时本报告写「待定」，而不是用「没有结果」冒充「通过」——这正是 Gate-C 要求的诚实：多重检验校正与一次性开封是晋升前的最后一道门，缺了它就不该有晋升结论。

