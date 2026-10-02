# task.* 域读写点迁移清单（pipeline-state.update 退役前置核查）

> 调查日期：2026-10-02。目标：退役内核 capability `pipeline-state.update`（任务域直写通道），
> mid-step 工具调用改写 transient 暂存态（transient.set），边界 merge 收编进 state。
> 本文是完整的 task.* 读写点清单，所有结论带证据行号。

---

## 0. 写面本体（被退役对象）

**内核校验与落点**：`kernel/crates/api/src/capability_router.rs`
- L2202-2246 `handle_pipeline_state_update`：仅允许 `task.*` 前缀键（键校验 L2220-2228，
  违规报错 L2223-2226）；写序 = ① 冷路径先行 `upsert_state_fields` 批量事务 upsert
  （L2234，DB `pipeline_state` 表，全成才 commit，B6 写序倒置）→ ② 热路径内存 registry
  合并（L2239-2246，`agentos_session::pipeline_state_registry::global_registry()`）。
- 读侧配对：`handle_pipeline_state_list` L2070-2160（内存 registry 行 + DB 任务域镜像补齐
  L2104-2160）；`/api/v1/pipelines/state` HTTP 同构（routes.rs L1413 起，DB 补齐 L1453-1470）。

**替代面现状**：transient 四方法已在内核落地（capability_router.rs L2252-2320 起
`handle_transient_set/get/list`）——中间态**不落库**（内存寄存器，per-(tenant,pipeline) →
per-key），同 key 连续写节流合并。即：迁移的本质代价 = 失去 DB 持久化 + 失去聚合读面即时可见，
直到「边界 merge 收编进 state」发生。

---

## 1. 写点清单（pipeline-state.update 的全部调用方）

`grep -rn '"update"' plugins/ --include=*.py`（排除测试）核对：写面接缝共 **5 处**
（3 个单行 + tasks 插件 2 文件内 4 处多行调用），全部经 `get_capability("pipeline-state").call("update", ...)`。

### W1. task_evaluate —— 评估闸门（LLM 工具 + 内部自动完成链）★调用量最大

- 写面接缝：`plugins/shared/tools/task_evaluate/server.py` L52-55
  （`_write_task_state` → `handle.call("update", ...)`；L47 注入 `tool_mod.set_state_writer`）。
- 实际写点：`plugins/shared/tools/task_evaluate/tool.py`（经 L93 `_write_task_state`）：

| 行号 | 写入键 | 场景（所在函数） |
|---|---|---|
| L192-195 | `task.status="evaluating"` | `task_evaluate_func`：任务 RUNNING 时先置评估中 |
| L215-219 | `task.status="failed"` + `task.ended_at` | `task_evaluate_func`：非法/失败终态 |
| L232-234 | `task.status="completed"` + `task.ended_at` | `task_evaluate_func`：评估通过终态 |
| L371-373 | `task.eval_total_calls` | `execute`（L319）：全局评估调用计数 |
| L828-832 | `task.eval_retry_count` + `task.eval_total_calls` | `_handle_evaluation_result`（L754）：渐进重试计数——L823-827 注释明言不落 state 会让耗尽判定永不触发、无限循环 |
| L941-945 | `task.status="completed"` + `task.ended_at` | `_complete_task`（L888） |
| L979-981 | `task.merge_gate_failures` | `_complete_task`：合并门控失败计数（L979-994：落账失败必须拒绝重试，防无限重试） |
| L1026-1030 / L1042-1046 | `task.status="failed"` + `task.ended_at` | `_complete_task` 门控拒绝路径 |
| L1061-1065 | `task.status="completed"` + `task.ended_at` | `_complete_task` |
| L1181-1185 | `task.status="failed"` + `task.ended_at` | `_fail_task`（L1153） |

- 场景定性：**LLM 工具**（`config/agents/main/agentos.yaml` L168 tool_ids 含 `task_evaluate`，
  core 步骤内被模型调用）+ auto_complete 自动完成链（同进程函数级调用，非 LLM 发起）。
- 写面语义：mid-run 即时写（内存 registry 热路径 + DB 冷路径双落点），消费方依赖其
  **即时可见**（见 R2 stop_check）与**跨 run 持久**（计数键，见 R1）。

### W2. triggers_ext —— 触发器注册表（P25：state 是权威持久层）

- 写面接缝：`plugins/shared/tools/triggers_ext/server.py` L75-82（`_make_state_writer` →
  `handle.call("update", ...)`）。
- 键：`task.trigger.registry.<trigger_id>`（`triggers/manager.py` L47
  `TRIGGER_STATE_KEY_PREFIX`，每触发器一键覆盖写，L108-110 注释）。
- 场景：LLM 工具 `trigger_setup`（action=setup/cancel/update，tool.py L279-292）
  + HTTP `/ext/trigger_setup_tool/triggers*`（http_api.py L155 经 tool.execute）。
- 写面语义：**持久层写**——manager.py L620-636 注释：跨 sidecar 重启/迁移/回收存活，
  管道清理时随 state 天然 GC；写面未注入时注册表退化为纯内存、重启后丢失（L633-635）。

### W3. workspace_lifecycle —— task.ws_meta 即时镜像（管道 input 步骤插件）

- 写面接缝：`plugins/shared/pipeline/input/workspace_lifecycle/server.py` L43-49
  （`set_task_state_writer` 注入 → `handle.call("update", ...)`）。
- 实际写点：`plugin.py` L522-538 `_mirror_task_ws_meta` → `{"task.ws_meta": ws_meta}`（L536）；
  继承/恢复路径补写同款（L540 起 `_mirror_inherited_ws_meta`）。
- 场景：**管道步骤插件**（init 步骤，on_task_start 创建工作空间后）。
- 写面语义：L41-43 注释明言镜像动机 = 绕过 state_updates 的引擎回写延迟，
  「运行中即时可见」，供 task_evaluate 合并门控等运行中读面即时消费；
  主写仍随 state_updates 入引擎 state（镜像失败不阻断 init）。

### W4. task_service（tasks 插件）—— 事件驱动写（系统插件，非工具非步骤）

- 入口：`plugins/shared/system/tasks/server.py` L38-70（`@plugin.on_domain_event`：
  run.started 复位 L46-52；run.completed/run.failed 派生 L57-70）；启动调和 L118-125。
- 实际写点（`events.py` / `reconcile.py`，直接 `state_capability.call("update", ...)`）：

| 行号 | 写入键 | 场景 |
|---|---|---|
| events.py L232-247（call 在 L236-237） | `{"task.status": "running"}` | run.started 新 run 激活复位（复活语义单点，用户侧终态豁免 L224-226） |
| events.py L291-299 | `{"task.status": "failed"}` | task_failed 终态对账（kill 方未随写的补偿，防前端永久看到 running） |
| events.py L143-163（call 在 L315-322） | `{"task.subtasks_pending.<task_id>": null}` | 子任务终态→**父管道**挂号键清除（null 即回执，无键删除语义） |
| reconcile.py L150-167 | `{"task.status": "pending_evaluation"}` | U13 启动调和（残留 running 行对齐投影） |

- 场景定性：**event-bus 域事件订阅**（内核 run 终态广播）与 on_load 启动调和——
  都发生在**步骤边界之外**（run 之间 / 进程启动时），没有可搭车的「步骤边界 merge」时点。

### W5. 核对结果：无其他调用方

- `mode_*` 六个面板服务、review、monitoring、workspace、child_task_guard、stop_check、
  task（task_service 工具面）、task_submit、channel_common 的 `get_capability("pipeline-state")`
  全部只 `call("list", {})`（逐一核对：mode_coding server.py L145、mode_godot L165、
  mode_planning L159、mode_research L146、mode_roleplay L209、mode_writing L143、
  review L545-546、monitoring L94-95/L437-438、workspace L115-118、task/server.py L41-42、
  child_task_guard L39-40、stop_check L40-41、task_submit L46-47、triggers_ext L62-63）。
- SDK 标准能力名单仅登记名字（`plugins/sdk/src/agentos_plugin_sdk/capability.py` L112），
  无 update 便捷封装；MCP crate 仅声明能力名（`kernel/crates/mcp/src/capability.rs` L34-37）。

---

## 2. 内核内部 task.* 写者（不走 pipeline-state.update 的旁路）

**结论：内核自身零 task.* 语义写**。内核对键语义零知识，只透传；直接写的键只有运行域
`run_status`（capability_router.rs L839-847、L905-913、L966-973、L1038-1046、L1082-1090）
与 `suspend_request_id`（L859-871、L917-928）。`server.rs` L1275-1276 注释确认：
「任务状态（task.status/ended_at）由任务域插件裁决写入（task_evaluate 经 pipeline-state.update
落 state），内核只广播 run 终态事件」。
AGENTS.md「有证据内核才补落默认 completed」的实际实现是 task_reminder 插件经 state_updates
补落（见 B2，plugin.py L490-524），不是内核代码。

内核侧 task.* 入 state 的三条旁路（迁移时**不动**，但构成读面语义的一部分）：

| 旁路 | 键 | 证据 |
|---|---|---|
| B1. chat.send_message state overlay（出生/登记，内核全量批量 upsert_state_fields 落表） | 出生键 task.goal/status/description/acceptance_criteria/dependencies/submitted_by/lineage.*/task.parent_project_id/task.orchestration + task.id 登记；提交者管道 task.owned.<id>.* + task.subtasks_pending.<id> | chat_send_handler.rs L424-436；plugins/shared/task_birth.py L99-123；task_submit/tool.py L2023-2039（birth_state）、L2090-2105（挂号）；review/server.py L216-242（复盘登记）；tasks/http_api.py L424-436（重跑复位 task.status=running + ended=false） |
| B2. 引擎步骤边界 merge（state_updates；persistent_fields 声明字段投影 upsert_state_field） | task.status/task.ended_at（task_reminder 补落 completed L516-517、耗尽 failed+failure_class/failure_reason L546-549、L262 running；stop_check 终态收束 L179-186、超线 failed L211）；task.authorized_write_zones / task.authorized_read_zones（security_check L767-780） | pipeline_loop.rs L79-82（persistent_fields 投影）、L1288（merge_and_project）；task_reminder/plugin.json L22/L34-35 声明 |
| B3. resume_pipeline state_overlay（B13① 不透明透传，随派发 overlay 恢复合并后应用） | 调用方透传的 task.* 终态键清除 | capability_router.rs L1007-1012；bin/agentos-kernel.rs L1338 |

---

## 3. 读点清单（谁读 task.*）

读面有两个视图，迁移设计必须分别覆盖：

- **聚合视图**（registry ∪ pipeline_state 表，经 `pipeline-state.list` / `/api/v1/pipelines/state`）：
  mid-run 的 update 热路径写 registry 即时可见（对运行中其他管道/外部轮询方）。
- **在飞视图**（引擎 run 内 state 副本，插件 `ctx.state`）：mid-run 的 update **不可见**
  （stop_check/plugin.py L26-28、L308 注释明确此双视图问题），当轮可见性靠
  tool_core 派生投影键 `task_evaluation_completed` 补（task_reminder/plugin.py L477-484）。

| # | 读者 | 证据（文件:行号） | 读的键 | 视图 |
|---|---|---|---|---|
| R1 | task_reminder（放行检测，output 步骤） | plugin.py L477-484（信号②）、L490-495（`evaluation.detected_result`）、L646-700（messages 中 task_evaluate 成功证据）、L439-448（信号③挂号键）；plugin.json L58-59 | `task.status`、`task_evaluation_completed`、`evaluation.detected_result`、messages（tool_calls/tool 载荷）、`task.subtasks_pending.*`、task.id/task.goal | 在飞（ctx.state） |
| R2 | stop_check（判死，output 步骤） | plugin.py L272-294（ctx.state 缓存键）、L320-365（聚合行实时读 task.id/task.status 终态对账）；server.py L40-41 | `task.status`（缓存+聚合行）、`task.id` | 双视图（在飞 + 聚合实时读，专为对外部 update 写入的对账而生，L26-28/L308） |
| R3 | child_task_guard（output 步骤） | plugin.py L88、L153-161（活跃子任务 = lineage.parent + task.status 活跃）、L194；server.py L39-40 | `task.id`、`task.status`、`lineage.parent_pipeline_id` | 聚合 |
| R4 | task_evaluate（读评估输入与目标行） | tool.py L1370-1392（STATE_SUMMARY_KEYS：task.goal/status/description/acceptance_criteria/task.evaluation/lineage.parent_pipeline_id；L1383 status_str） | 同左 + `task.ws_meta`（L1416） | 聚合 |
| R5 | task_submit（继承工作空间） | tool.py L1497-1511（ws_meta → task.ws_meta 两路 fail-closed） | `ws_meta`、`task.ws_meta` | 聚合 |
| R6 | task_service 事件派生 | events.py L124、L231、L291（task.status 读取）；L157-163（挂号清除） | `task.status`、`task.subtasks_pending.*` | 聚合/事件载荷（内核 run 终态交出 state，ADR 2026-09-11，events.py L167-176） |
| R7 | tasks http_api（前端任务面板后端） | http_api.py L504-526（`_list_tasks_from_state` = pipeline-state.list）、L532/L580-581（task.* 行 + task.owned.<id>.* 容器行） | task.goal/status/parent_project_id/owned.* 等 | 聚合 |
| R8 | mode_* 六个面板服务 | mode_coding server.py L186、L219-222（task.status/task.id/task.goal/task.parent_project_id）；其余五服务同构（godot L165/L295、planning L159/L329、research L146/L448、roleplay L209/L1027、writing L143/L295） | task.status/task.id/task.goal/task.parent_project_id/mode.* | 聚合 |
| R9 | review / monitoring / workspace / channel_common | review server.py L558-560（task.status 终态判定）；monitoring L452-461（task.status/task.id/task.goal）；workspace L115-118；inbound_bridge L241/L276/L441（pipeline_id/thread 解析） | task.status 等 | 聚合 |
| R10 | triggers_ext 检查线程 | manager.py L600-625（state 聚合行 = CONDITION 求值上下文）、L730（按 pipeline_id 过滤）、L104/L607 | 聚合行全键（含 task.*）作条件求值 | 聚合 |
| R11 | 内核 DSL 条件求值 | engine/condition.rs L1001-1005（平键 task.status 求值）；pipeline_loader.rs L1235、L1269（终态收束条件 `task.status == completed/failed/cancelled`、active 过滤） | `task.status` | 在飞（引擎 state） |
| R12 | 内核 HTTP /api/v1/pipelines/state（前端任务树数据源） | routes.rs L1404-1470（summarize_state 按 ExportFields 白名单，L1453 DB 补齐注释「pipeline-state.update 热路径双写内存+表」）；system_diagnostics.rs L80-88 | 出口白名单内 task.*（tasks/plugin.json L519-532 export_fields：task.goal/status/ended_at/submitted_by/parent_project_id/owned.*/acceptance_criteria/description/dependencies/eval_retry_count/eval_total_calls/eval_summary/error） | 聚合 |
| R13 | 前端 | frontend/src/constants/api.ts L75（STATE='/api/v1/pipelines/state'）、L183（/ext/task_service/tasks 任务管理端点） | 同 R12/R7 | 聚合 |

---

## 4. M2 迁移清单表

目标改造三档：**暂存**（transient.set，边界 merge 收编）/ **读面合并视图**（读端改为
state ∪ transient 合并）/ **不变**（本就走 state 权威路径或语义就是持久写）。

| 写者/读者 | 文件:行号 | 键 | 场景 | 目标改造 |
|---|---|---|---|---|
| task_evaluate | tool.py L192-195 | task.status=evaluating | LLM 工具，mid-run | **暂存**（评估中是过程态；但需评估下游 R2/R6 是否接受 merge 前不可见——评估中值无判死/收束消费者，风险低） |
| task_evaluate | tool.py L215-234、L941-945、L1061-1065、L1181-1185 | task.status=completed/failed + task.ended_at | LLM 工具，终态裁决 | **暂存 + 收编时机约束**：终态必须对 R2（判死）、R6（事件对账）、R12/R13（前端轮询）即时可见——merge 时机若晚于收束轮，恢复 R2 现有聚合实时读对账路径；见风险 R-1 |
| task_evaluate | tool.py L371-373、L828-832 | task.eval_total_calls / task.eval_retry_count | LLM 工具，跨 run 计数 | **不变（保持持久写）或换持久小写面**：transient 不落库直接违约——跨重启丢失 = 耗尽判定失效 = 无限评估循环（L823-827 原注释即为此而写）；见风险 R-2 |
| task_evaluate | tool.py L979-981 | task.merge_gate_failures | LLM 工具，门控计数 | **同上**：落账失败即拒绝重试的 fail-closed 语义（L979-994）依赖持久账本 |
| workspace_lifecycle | server.py L43-49；plugin.py L522-538 | task.ws_meta | 管道 init 步骤 | **暂存可议，倾向不变**：镜像存在动机就是「绕过引擎回写延迟、即时可见」（server.py L41-43）；transient 暂存 + 边界 merge 会退回延迟问题（task_evaluate 合并门控 R4 运行中读不到 ws_meta → 门控显式报错路径）。若 merge 点在 init 步骤边界紧后，则可迁 |
| triggers_ext | server.py L75-82；manager.py L47、L620-645 | task.trigger.registry.<id> | LLM 工具 + HTTP，持久层 | **不变（保持持久写）**：P25 明言 state 是注册表权威持久层，跨重启存活、随管道 GC；transient 内存语义直接违约（重启丢注册表）。此项与本核查（触发器重构）直接相关：注册表持久层需替代落点（独立表/文件）或保留一个窄化后的 task.* 写面 |
| task_service/events | events.py L232-247 | task.status=running | 事件驱动（run 之间） | **不变或新写面**：无步骤边界可搭车；run 之间 transient 生命周期存疑（若 transient 绑 run，复位写发生在新 run 派发后、读方是聚合读面——两头都不在 transient 域内）；见风险 R-3 |
| task_service/events | events.py L291-299 | task.status=failed | 事件驱动（run 终态后） | **同上**：对账写发生在 run 已终态后，无 merge 时点 |
| task_service/events | events.py L143-163、L315-322 | task.subtasks_pending.<id>=null | 事件驱动，**跨管道写父管道** | **不变或新写面 + 读面合并**：transient 按 (tenant,pipeline) 键空间，写父管道需跨管道 transient 语义；消费方 R1 信号③读在飞 state（plugin.py L439-448），null 回执靠唤醒新 run 的恢复链（stage_recover_history）从聚合态取——若迁 transient，恢复链取不到 null；见风险 R-4 |
| task_service/reconcile | reconcile.py L150-167 | task.status=pending_evaluation | on_load 启动调和 | **不变或新写面**：进程启动时点，无 run/步骤上下文 |
| 出生写面（B1） | task_birth.py、task_submit、review、tasks/http_api | 出生键/task.id/task.owned.*/task.subtasks_pending.<id> | chat.send_message overlay | **不变**：不走 pipeline-state.update，已是内核透传 + 落表路径（chat_send_handler.rs L424-436） |
| 边界 merge 写面（B2） | task_reminder/plugin.py、stop_check/plugin.py、security_check/plugin.py | task.status/ended_at/failure_class/failure_reason、task.authorized_*_zones | 管道步骤插件 state_updates | **不变**：这就是「边界 merge 收编进 state」的现状载体——迁移方案应把 mid-run 写收编到这一通道，而非反过来动它 |
| resume overlay（B3） | capability_router.rs L1007-1012 | 调用方透传 task.* 清除键 | resume 派发 | **不变** |
| 读端 R1 task_reminder | plugin.py L477-495、L439-448 | 完成证据/挂号 | 在飞读 | **读面合并视图**（若 mid-run 写迁 transient）：信号②依赖当轮可见性，现状靠 `task_evaluation_completed` 投影键解决同类问题——transient 版可复用此模式（边界 merge 前读 transient.get） |
| 读端 R2 stop_check | plugin.py L272-365 | 终态对账 | 双视图读 | **读面合并视图**：现有聚合实时读就是为 update 热路径可见性而生；写面迁 transient 后需改为 state ∪ transient 合并查 |
| 读端 R3-R10 | 见 §3 表 | task.* 各键 | 聚合读 | **读面合并视图**：`pipeline-state.list` 与 `/api/v1/pipelines/state` 需并入 transient 键区（内核 handle_transient_list 已具备枚举能力，capability_router.rs L2307 起），或这些读端维持读 state 且接受 merge 前陈旧 |
| 读端 R11 DSL 条件 | condition.rs、pipeline_loader.rs | task.status | 在飞读 | **不变**：条件求值读引擎在飞 state，merge 后自然可见 |
| 读端 R12/R13 HTTP/前端 | routes.rs L1404-1470；constants/api.ts | 出口白名单 task.* | 轮询 | **读面合并视图**：前端任务树轮询依赖 mid-run 终态即时可见（评估通过→前端立刻变绿） |

---

## 5. 风险清单（迁移设计必须回答）

- **R-1 终态即时可见性链**：task_evaluate 写 completed → 内存 registry → stop_check 聚合
  实时读判死收束（stop_check/plugin.py L320-365）→ 前端任务树轮询（R12/R13）。transient
  暂存若 merge 晚一轮，stop_check 与前端在窗口期看到旧值；且 task_reminder 信号②在飞不可见
  的旧问题会重现（现状靠 `task_evaluation_completed` 投影键补，plugin.py L477-484——可复用）。
- **R-2 持久计数键**：task.eval_retry_count / task.eval_total_calls / task.merge_gate_failures
  的存在理由就是跨 run/跨重启持久（tool.py L823-827、L979-994）。transient 不落库，直接迁移
  = 无限评估循环回归。这三键必须留在持久面。
- **R-3 事件驱动写无边界可搭**：task_service 的四处写（§1 W4）发生在 run 之间/进程启动时，
  transient 的「mid-run 暂存 → 边界 merge」模型对它们无定义（谁在哪个边界 merge？）。
- **R-4 跨管道写**：挂号清除写的是**父管道**的键（events.py L315-322），transient 按
  (tenant,pipeline) 键空间隔离，需要跨管道语义 + 父管道在读侧合并；消费方信号③读在飞
  state（plugin.py L439-448），null 回执依赖唤醒 run 的恢复链从持久态取。
- **R-5 写序与事务性丢失**：现 update 是 DB 批量事务 upsert 全成才写内存（capability_router.rs
  L2229-2246，B6 写序倒置消灭分代态）。transient + 边界 merge 后，「批量原子落库、失败不留
  半套」的保证需要重新设计（merge 点的失败语义）。
- **R-6 触发器注册表持久层**（本核查主对象）：`task.trigger.registry.*` 是 P25 权威持久层
  语义（manager.py L620-636），退役 update 必须先给它找等价持久落点，否则注册表退化为
  纯内存（L633-635 注释的降级即现实）。

## 6. 结论

1. `pipeline-state.update` 的调用方收敛于 **5 处接缝、4 个插件**（task_evaluate、triggers_ext、
   workspace_lifecycle、task_service），其中 task_evaluate 一个插件占 11 个写点、是绝对主力。
2. 内核自身**零 task.* 语义写**（只有 run_status/suspend_request_id），AGENTS.md 所述
   「内核补落默认 completed」实为 task_reminder 插件经 state_updates 边界 merge 补落——
   即「边界 merge 收编」的通道现状已存在且承载同类写（task.status 终态），迁移方向与现状自洽。
3. 可迁 transient 的只有「过程态」写（task.status=evaluating 等）；**终态、持久计数、
   触发器注册表、事件驱动写、跨管道挂号清除**五类各有一个硬约束（R-1~R-4、R-6），
   直接全量迁移会回归无限评估循环、注册表重启丢失、父管道回执丢失三个已知被修复过的缺陷。
4. 建议的退役形态：mid-run 过程态 → transient + 边界 merge；持久性写（计数/注册表/事件对账）
   收敛到一个窄化的持久写面（或保留 update 仅持久键白名单），而非全量退役。
