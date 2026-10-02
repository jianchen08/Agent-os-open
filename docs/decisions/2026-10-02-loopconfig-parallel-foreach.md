# 2026-10-02 LoopConfig 并行 for-each 扩展——单调用 ≡ 循环单元素展开（等价原则）

## 背景

autonomous 主循环的「LLM 轮 ↔ 工具轮」交替目前是**模拟**出来的：
`config/pipelines/autonomous.yaml` 的 main 循环体用 `{{state.core_plugin}}`
动态核心槽 + `core_type` 状态机（llm_call|tool_execute）+ post 链四条 set
路由拼装而成；工具批执行整体锁在 Rust 原生插件 tool_core 的 for 循环内部
（`plugins/shared/pipeline/core/tool_core/src/lib.rs` 的 `run()`），一次
step 消费整批调用。由此产生三个到不了的需求：

1. **逐调用 step 语义**：管道插件无法精细控制每个工具的执行（审批、守卫、
   自定义处理都只能「挡整轮」，粒度到不了单调用）；
2. **并行执行**：批内工具调用只能串行跑完，且被 Rust cdylib 的 exec_lock
   进一步串行化（见决策 3）；
3. **特定工具执行 step**：无法在管道里显式写一个「工具执行」step 复用。

用户裁定设计公理：**单调用 ≡ 循环的单元素展开（等价原则）**——不为一套
「并行/单调用」另起插件群，所有设计在现有 DSL 基础上做，不另搞内核
中间件（内核 gate 中间件方案已被用户明确否决，见 Alternatives 1）。

引擎现状事实（设计边界）：step 级 loop_config 已存在（agentos-core
`types.rs` 的 `LoopConfig{enabled,max_iterations}`，引擎 `pipeline_loop.rs`
已执行串行重复循环）；插件调用只借 `&state`（invoke_plugin），返回后才
merge（merge_and_project）；messages 只接受 op 声明（`{_ops:[...]}`），
一次 apply（内存 + message_slots 表）。

## 决策

### 1. LoopConfig additive 扩展（缺省零行为变化）

`LoopConfig` 新增字段：

- `over`：迭代集合，格式固定 `"state.<顶层键>"`（模板原文）；
- `as`：迭代变量名，每个迭代把当前元素写入迭代局部 state 顶层；
- `max_concurrency`：缺省 1 = 串行链式；>1 = 有界并行快照；-1 = 不限；
  0 非法，编译期报错；
- `collect`：键名列表，并行归并策略 = 按迭代序拼接数组；
- `consume`：true = 循环收尾把 over 源键清成 `[]`；
- `consume_keys`：额外在循环收尾清 `[]` 的键（如 `pre_decided_results`）。

`over` 存在时该 step 是 **for-each**；`over` 缺省时维持既有串行重复循环
语义，一字不变。编译期校验（compiler.rs）：`over` 必须带 `as`；
`max_concurrency=0` 或 `<-1` 报错；`collect`/`consume`/`consume_keys`
仅 for-each 支持；`as` 禁用保留名（messages/ended/suspended/next_phase/
current_phase）与含点名字。全部字段 serde default，旧 YAML 零变化
（G10 DSL 冻结下的 additive 扩展）。

### 2. 执行语义

迭代 = 对 over 数组逐元素执行 step.items；体内插件只依赖迭代变量
（`current_call`）与快照，**零批知识**。

- **max_concurrency=1（串行链式）**：每迭代从上一迭代合并后的 parent
  state 取快照，等价旧串行循环。
- **max_concurrency>1（有界并行）**：所有迭代从循环开始时的同一 parent
  快照克隆，经 `buffer_unordered(max_concurrency)` 并发执行；迭代间隔离。

归并（按迭代下标序，确定性）：

- messages ops 逐迭代「一次 apply」——seq 分配 / 落库 / 实录全在父侧，
  迭代内只缓冲不 apply；迭代内跨 step 的写后读 messages 不保证（v1 已知
  限制）；
- `collect` 键按迭代序拼接（循环开始时先清 `[]`，杜绝陈旧）；
- 其余顶层键 last-index-wins；`_plugin_errors` 拼接；
- `ended`/`suspended` 任一迭代置位即父置位。

迭代内 `persist_step_trace` 跳过：轨迹颗粒度 = 配置 step，父 step 收尾
统一落；checkpoint 计步不变。

### 3. tool_core 契约收窄：批处理回迁 Python sidecar，单调用契约

执行核从 Rust cdylib 批处理回迁 Python sidecar 插件，契约收窄为**单调用**：

读 `state.current_call`（`{name, args|arguments, id}`）→ 预填短路
（`pre_decided_results` 按 call_id/工具名命中即出预填结果，不执行）→ 经
tool-executor capability 调内核唯一执行漏斗
（`handle_tool_executor_invoke`，`kernel/crates/api/src/capability_router.rs`）
→ 输出 schema 校验（tool_output_contracts）→ 截断
（`config/system/tool_core_config.yaml` 的 tool_output 命名空间，缺省
16384/12288/2048）→ 发 tool_start/tool_result 事件（event-bus.emit）→
产单条 tool 消息 op（复用 SDK agentos_plugin_sdk.tool_result_protocol，
契约夹具 `sdk/tests/contracts/tool_result_messages.fixture.json` 双车道）
→ 写 collect 键（tool_results/_full_tool_results/_executed_tool_results
各自单元素数组）与 `submitted_task_ids` 增量。

选 Python sidecar 的原因：`NativePlugin.exec_lock`
（`kernel/crates/plugin-loader/src/native_loader.rs`）把同一 cdylib 的
并发 execute 串行化（UnsafeCell 返回缓冲的串行安全论证），并行拿不到真
并行；改造成缓冲池要碰 2026-09-01 SIGSEGV 的跨分配器 FFI 崩点，风险高。
Rust 实现保留在 src/ 作为休眠的批量加速器后备（G8 语义不变），不再被
manifest 引用。

### 4. autonomous.yaml 迁移（同一把刀）

main 体静态化：

- prepare 链原序保留；
- core 组变为：pipeline_tool_cache（when 门保留）→ **for-each step**
  （`over: state.raw_tool_calls`, `as: current_call`, max_concurrency
  配置化, collect 四键, `consume: true`,
  `consume_keys: [pre_decided_results]`，体内唯一 step
  pipeline_tool_core）→ pipeline_spill_guard；
- `{{state.core_plugin}}` 动态槽、core_plugin/core_type 分发键、post 链里
  raw_tool_calls→tool_core 的 set 路由全部退役；initial_state 的
  core_plugin/core_type 删除；
- security_check 等守卫内部以 `core_type=="tool_execute"` 为门的改为以
  `raw_tool_calls` 非空为门（行为等价：新流程 raw_tool_calls 非空当且仅当
  本轮有调用）；
- post 链其余路由条件不变（post 看到的 raw_tool_calls 恒 `[]`，与旧流程
  tool_core 消费即清后的形态一致）。

### 5. 目标 C 免费解决

单工具执行 step = pipeline_tool_core + step context 注入 `current_call`
（step.context 本来就 merge 进 state），同一契约的第三个调用点，无新插件。

### 6. 有意让渡（记录为接受的设计边界）

- 在飞批次的中途插控制（撤销在飞调用/单调用即时反应）不可表达——步与步
  之间是 barrier；max_concurrency=1 的串行链式可换回逐调用反应性。
- 并行下 raw_error 语义从「全批皆败才置」变为「单调用 task_failed 级才置」
  （消费方为监控/通道展示面，无控制流依赖）。
- `submitted_task_ids` 批内全局去重降级为对快照去重（批内重复提交属病态
  输入）。

## Alternatives Considered

1. **内核 gate 注册表/中间件**（capability 服务在 tool-executor 分发点
   逐调用咨询 before/after）：**否**。用户否决——另搞一套中间件，违背
   「在现有 DSL 基础上设计」；且每轮粒度与调用粒度的控制面分裂。
2. **每次工具调用摊平为外层循环独立迭代**：**否**。prepare 链（14 步）×N
   重跑、max_rounds 预算按半轮口径爆掉；被 parallel_for 取代。
3. **为并行另起一套单调用插件群**（call_dispatch/tool_exec_one/
   result_process/result_assemble 四插件）：**否**。违反等价原则；全部
   撤销，循环体 = 现有插件的单调用契约。
4. **静态多分支并行管道再聚合**（fan-out/fan-in DAG）：**否（缓行）**。
   工具调用 N 是运行时动态的（LLM 决定），for-each 才是原语；静态分支无
   眼前场景，将来可建在 parallel_for 之上。
5. **继续扩 pre_decided_results 表达逐调用控制**：**否**。决策时机在轮前，
   看不到轮内结果。
6. **Rust tool_core 直接并行化**（host bridge UnsafeCell 缓冲池化）：**否**。
   exec_lock 串行锁 + 跨分配器 FFI 崩点史（2026-09-01 真机 SIGSEGV），
   风险收益比差；Python sidecar 并发天然成立且用户明示不考虑边车负载。
7. **event-bus 异步裁决逐调用控制**：**否**。广播无背压，挡不住调用；
   hooks crate 定位就是 off-hot-path 观测。

## 影响

- **契约面**：LoopConfig 增字段（serde default，旧 YAML 零变化）；G10
  DSL 冻结下的 additive 扩展；pipeline_tool_core 从批契约收窄为单调用
  契约（内部消费方可全量感知，一刀切不留兼容层）；autonomous.yaml 重写
  main/core 组。
- **迁移账**：core_plugin/core_type 消费方清退（grep plugins/ 与
  frontend/src）；max_rounds 口径从半轮变整周期；tool_cache/
  conversation_mode 等以 raw_tool_calls 为判据的步骤逐一核对。
- **门禁**：新引擎代码带 Rust 测试；Python 插件带 pytest；diff coverage
  100%；mypy 基线 0 不增。
- **性能**：审批/守卫从「挡整轮」到 k=1 时逐调用；并行 N 调用受
  max_concurrency 有界；消息 op 落库从每批 1 次变每迭代 1 次（父侧逐迭代
  apply）。

## 归档

- 讨论记录：2026-10-02 用户三轮收敛定稿（等价原则 → DSL 原生 → LoopConfig
  扩展），同日实施（刀 1 引擎原语 / 刀 2 tool_core 收窄 + YAML 迁移）。
- 关联：G10 DSL 冻结制度（additive 扩展即本 ADR 的合规定位）；2026-09-01
  跨分配器 FFI SIGSEGV 事故记录（Rust 并行化被否的实证依据）。
