# 2026-10-02 触发求值统一模型：内核 trigger-svc 三视图

## 背景

- 现状：CONDITION 触发器靠 5s 后台轮询（triggers_ext/triggers/manager.py `_poll_conditions`，GAP-2 state 聚合求值 + 边沿检测）；条件表达式由 Python 独立实现（`condition_parser.py`），与内核管道 DSL 的 `eval_condition`（Rust）构成双语言并存——同构逻辑两处实现，属"两处相似必漂移"病灶。
- EVENT 触发器已有内核终态事件接线（server.py:94 `_on_domain_event` → `evaluate_event`，task_completed/task_failed 可达）；外部点火端点已有（`POST /triggers/{id}/trigger`，http_api.py:187）。
- 触发器的设计目的之一：覆盖 step 边界覆盖不到的窗口（core 长步骤内部可达分钟级）——"state 任意位置命中即执行"需要 staged + committed 两个观察视图。
- 信号响应模型（本日讨论定稿）：动作权力五档（观察/联动/变换/门禁/启动）× 信号源；触发器横跨观察、联动、启动三档，求值机制应当统一。

## 决策

1. **求值上收内核**：新 capability `trigger-svc`（register / unregister / reconcile / ack）。条件表达式并入内核 `eval_condition` 家族（与 when/routes/while 同语言）——前置核查 1（`docs/working/触发器重构前置核查/1-表达式语法对比.md`）结论：Python 侧是 Rust 侧真子集，**直接迁移无需转换器**（预估 2.5–4.5 人日）；配套修复 `json_eq` 的 int/float 跨型相等（condition.rs:905 注释宣称与 Python 对齐但实现相反，属注释级契约背离），bool 参与数值比较的归属随修复一并拍板。
2. **挂点收敛："一个锚点 + 一次订阅 + 一处内联"**：
   - committed 视图：store 统一写入口锚点（两条写路径——引擎 merge pipeline_loop.rs:1164/:1530 与 capability handler——已汇合到同一组 `upsert_state_field(s)`）；每次写带 changed keys，按 watched-keys 索引增量求值；
   - staged 视图：`transient.set` 进程内挂钩（覆盖 step 边界之间的盲区，零延迟）；
   - run 内控制流：DSL 新增 `triggers` 段（jump/set 动作），编译进执行计划，merge 边界内联求值。
3. **边沿簿记内核持有**（per-trigger per-view last-boolean）；注册时对当前 state 种子求值一次；触发器声明 watched 视图（staged | committed）防双触发。
4. **点火可靠性**：fire log + ack（at-least-once）+ reconcile 周期重投（60s，可配）；`HookEventBus` best-effort 只作通知通道不作送达承诺——触发器语义必须至少最终触发一次。
5. **动作分域**：message / command → 触发器插件带外执行（跨 run 有效）；jump / set → **仅 DSL 编译期声明的触发器可用**（引擎合并边界内联求值，跳转计入现有护栏 steps × 4）——动控制流必须过编译期目标校验，与 routes 同哲学；运行时注册（trigger_setup / HTTP）禁止控制流动作。
6. **触发器插件薄化**：triggers_ext 职责收缩为注册面（trigger_setup 工具、HTTP API）+ 动作面（message 注入 / command 分发）；求值、边沿、轮询、state 拉取全部上收。

## Alternatives Considered

- **维持插件求值 + 轮询加速**：延迟与空转成本死结，5s 已是妥协产物；且 state 拉取经 run_coroutine_threadsafe 跨线程（15s 超时）——否。
- **DSL 触发器翻译成 CONDITION 触发器**（复用 dsh 翻译器模式）：范式错位——CONDITION 是带外启动权（注消息开新一轮），jump/set 是流内参与权；异步延迟语义与边界同步不符——否。
- **内核只发原始 state.updated 事件、求值留插件**：高频写下事件量失控，求值压力转移而非消失；且保留双表达式语言——否（被 trigger-svc 注册制取代）。
- **运行时注册允许 jump/set**：目标无编译期存在性校验 = 悬空跳转——否。
- **按调用点挂多个求值锚点**（引擎 merge、capability handler 各一）：两条写路径已在 store 层汇合；锚点应放数据必经之路，路口锚点使每条新增写路径都成为漏触发机会——否，收敛为单锚点。
- **PostToolUse 式逐事件枚举**（Claude Code 式按事件点挂权）：与单锚点同因——枚举式挂点覆盖不了未来新增的写路径——否。

## 影响

- triggers_ext **净删代码**：condition_parser、5s 轮询线程、state provider、边沿簿记、`task.trigger.registry.*` 直写全部退休；manifest 加 `domain_event` 订阅（接收 `trigger.fired`）。
- 表达式语言归一为内核一份；存量条件触发器注册为 0 条（dev 库核查），迁移面 = 工具契约示例与测试表达式，全部为 ASCII 子集。
- 语义补齐项：`json_eq` int/float 跨型相等（bool 归属随修复拍板）；CJK 标识符在 Rust 侧显式报错（Python 侧唯一超集方向，仓库存量为零）。
- 公理界定补句（同步 AGENTS.md）：**声明式配置谓词（when/routes/triggers）的求值属配置解释（内核），业务裁决（评估、审批）归插件**。
- 为 hook_bridge（外部 agent 钩子适配，独立 ADR）铺路：外部 PreToolUse/PostToolUse 翻译到包裹点与 trigger-svc，Session 类翻译到域事件。

## 归档

本文件；前置核查报告 `docs/working/触发器重构前置核查/`。关联：[状态写面收敛](2026-10-02-state-write-surface-convergence.md)（同日，持久写面与暂存层的语义前提）。
