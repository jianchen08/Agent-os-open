# 2026-10-02 状态写面收敛：边界唯一提交 + 过程态暂存 + 持久写面声明制

## 背景

- step 边界的存在意义：声明闸、原子合并、traces 轨迹、重建回放、决策点（when/routes）、可观测切面。由此推出不变量：**run state 的提交点应当唯一**（`merge_and_project`，pipeline_loop.rs:1291）。
- 现状存在第二写路径：插件经 `pipeline-state.update` 直写 store（B6 写序倒置：DB 批量事务先落、成功才写内存 registry，capability_router.rs:2205）。三个问题：
  1. **重建洞**：直写不进 traces（轨迹 step 级统一落、不钻插件）——崩溃后"checkpoint + 回放 traces"重建与"从 DB 最终态恢复"两条路径结果不一致；
  2. **域特判泄漏**：`task.*` 前缀白名单硬编码在内核（capability_router.rs:2225 附近）——"任务域"是插件承载的概念，内核不该知道它；
  3. **三代可见性**：引擎 run 本地 state 只由 merge 推进（不读 session registry），直写值对引擎 when/routes 不可见，只对 DB/api 层可见。
- 关键设计事实（用户裁决 2026-10-02）：**一个 task 对应一个 run**；`task.*` 是带命名空间的 run state；把任务事实写进 state 是为了**统一审计**（一份 state 历史 + 一套 traces）。因此"域事实独立存储"是过度设计，统一审计才是目标。
- 前置核查 3（`docs/working/触发器重构前置核查/3-task域读写点迁移清单.md`）：直写写点共 5 处接缝、4 个插件，按持久语义分两类——
  - **过程态**（如 `evaluating` 标志）：可安全迁 transient 暂存 + 边界收编；
  - **持久态五类硬约束**：终态（completed/failed + ended_at）、持久计数（`eval_total_calls`/`eval_retry_count`——直写当初正是为修"无限评估循环"缺陷而写，R-2）、触发器注册表（`task.trigger.registry.*`，跨重启存活）、事件驱动写（task_service 对账/复位）、跨管道挂号清除。
  - 另：内核对 task.* 无语义写——「补落默认 completed」实为 task_reminder 插件经 state_updates 走边界 merge（路径 A），不在直写路径上。

## 决策

1. **run state 提交点唯一**：step 边界 `merge_and_project`；一切过程态经 `PluginResult.state_updates` 或暂存收编进入，此外无写 DB 通道（runs 表簿记除外，属内核执行记录）。
2. **过程态走暂存**：mid-step 工具/插件的中间标志写 transient（`transient.set`，内存、即时、易失）；`merge_and_project` 收编声明过的暂存键（复用 `transient_state` 声明闸）。
3. **持久写面保留但收窄为声明制**：终态/持久计数/注册表等持久类键保留窄化的 `pipeline-state.update` 通道——**删除 `task.*` 前缀特判**，改为 manifest `state.writes` 声明 + `_plugin_id`（invoker 注入信任锚点，插件不可伪造）在 G6 授权单点同域校验；未声明即拒绝。内核只执行"声明才可写"机制，不执行"哪个域存在"语义。
4. **观察权与提交权分离**：committed 视图触发求值锚 store 统一写入口（两条写路径已汇合 `upsert_state_field(s)`：pipeline_loop.rs:1164/:1530 与 capability handler）；staged 视图挂 `transient.set` 进程内挂钩。观察不破坏边界不变量——不变量管辖的是提交权。
5. 触发器注册表持久层（`task.trigger.registry.*`）随触发求值统一模型上收内核 trigger-svc，triggers_ext 的直写点随之消失。

## Alternatives Considered

- **`task.*` 特判保留**：域知识泄漏进内核，每个新域都要改内核，违背"一切皆插件"公理——否。
- **直写全量退役**（本讨论曾倾向的方案）：前置核查 3 证明五类持久写有硬约束（持久计数迁暂存 = 无限评估循环缺陷回归 R-2；触发器注册表需跨重启存活），全量退役破坏功能——否，改为"过程态迁暂存 + 持久态声明制收窄"两分法。
- **域记录独立写面/独立存储**：1:1 task-run 模型下任务事实生命周期与 run 重合，统一审计恰是设计目标，独立存储制造两套审计真相——否。
- **写面全开放（删前缀检查不加闸）**：任意插件可覆盖任意键，安全性与声明闸双双倒退——否。

## 影响

- 正面：重建两条路径一致（暂存不承诺持久；持久写有声明、有边界、有迹可循）；内核业务域特判清零；触发器 committed 锚点一处覆盖全部提交变化；统一审计（一份 state 历史 + 一套 traces）保住。
- 迁移面：`task_evaluate` 11 个写点按两类拆分（过程态→transient 暂存；终态/持久计数→声明制持久写面）；task_service / workspace_lifecycle 事件驱动与镜像写补 `state.writes` 声明；聚合读面（`pipeline-state.list`，GAP-2）语义改 staged∪committed 合并视图（消费方盘点见前置核查 2）。
- 崩溃语义写死：暂存 = 易失是特性（事实可由会话重放再得出）；需要持久的写必须声明走持久写面。
- 兼容：内部代码一刀切，不留兼容层；存量触发器注册表随 trigger-svc 上收一次性迁移。

## 归档

本文件；前置核查报告 `docs/working/触发器重构前置核查/`。关联：[触发求值统一模型](2026-10-02-trigger-eval-unification.md)（同日）、后续 hook_bridge ADR（独立立项）。
