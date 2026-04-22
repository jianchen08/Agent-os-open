# Agent OS 渐进式真实验证方案

> 核心原则：**用真实调用验证真实体验**，不依赖 Mock，每一步都有严格断言，
> 失败就停下来修，修完继续跑，直到全流程跑通。

## 当前状态评估

| 模块 | 状态 | 说明 |
|------|------|------|
| 管道引擎 | 基本跑通 | 单轮 LLM + 工具调用循环正常 |
| CLI 交互 | 基本跑通 | 流式输出、斜杠命令、状态栏正常 |
| 任务系统 | **未跑通** | 3 个已知致命 Bug（路径/模板变量/评估者路径） |
| 工作空间 | **未跑通** | workspace 创建了但文件没正确合并回主分支 |
| Git 集成 | **未跑通** | worktree 创建了但没清理，合并流程未触发 |
| 前端 Web | **未跑通** | 基础框架在，但前后端对接有断层 |
| 多智能体协作 | 未验证 | L1→L2→L3 委派链路未端到端验证 |

---

## 第一部分：任务系统完整流程与验证

### 1.0 任务系统全流程图

任务系统有两条路径，取决于评估方式。先理解完整流程，再逐环节验证。

#### 路径 A：执行者自评模式（默认）

```
用户输入
  │
  ▼
┌──────────────────────────────────────────────────────────┐
│ 主管道（L1/L2 Agent）                                     │
│                                                           │
│  LLM 分析任务 → 决定委派 → 调用 task_submit 工具          │
│       │                     │                             │
│       │                     ▼                             │
│       │            ┌─────────────────┐                   │
│       │            │ task_submit 执行  │                   │
│       │            │  ① 创建 TaskModel │                   │
│       │            │  ② 状态 = pending │                   │
│       │            │  ③ 发布 task.submitted 事件           │
│       │            │  ④ 返回 task_id   │                   │
│       │            └────────┬────────┘                    │
│       │                     │                             │
│  ChildTaskGuard 检测到    EventBus                       │
│  活跃子任务 → 返回          │                             │
│  route_signal=wait         ▼                             │
│       │         ┌─────────────────────────┐              │
│  主管道挂起     │ TaskWorker 收到事件       │              │
│  等待子任务完成  │  ① task_service.start_task()            │
│                │     pending → running      │              │
│                │  ② 创建子 PipelineEngine   │              │
│                │  ③ 注册 terminal Event     │              │
│                │  ④ engine.run(user_input)  │              │
│                └────────┬──────────────────┘              │
│                         │                                 │
│                         ▼                                 │
│  ┌──────────────────────────────────────────┐            │
│  │ 子管道执行（L2/L3 Agent）                  │            │
│  │                                          │            │
│  │  迭代 1: LLM 调用 → 分析任务              │            │
│  │  迭代 2: LLM 调用工具执行任务              │            │
│  │  迭代 3: LLM 只输出文本（无工具调用）       │            │
│  │           ↓                              │            │
│  │    TaskReminder 检测到纯文本输出           │            │
│  │    → 注入系统提醒："请用 task_evaluate 评估" │           │
│  │    → route_signal=next_llm               │            │
│  │           ↓                              │            │
│  │  迭代 4: LLM 调用 task_evaluate 工具       │            │
│  │           ↓                              │            │
│  │  ┌────────────────────────────────┐      │            │
│  │  │ task_evaluate 工具执行           │      │            │
│  │  │  ① 获取 task_id（injected_params）│     │            │
│  │  │  ② 创建 EvaluationExecutor      │      │            │
│  │  │   ③ executor.run_evaluation()   │      │            │
│  │  └────────────┬───────────────────┘      │            │
│  │               ▼                           │            │
│  │  ┌────────────────────────────────┐      │            │
│  │  │ EvaluationExecutor             │      │            │
│  │  │  ① MetricLoader 加载指标定义    │      │            │
│  │  │  ② EvaluationEngine.evaluate() │      │            │            │
│  │  │  ③ 对每个 metric:               │      │            │
│  │  │    - tool 类型 → 调用真实工具    │      │            │
│  │  │    - agent 类型 → 创建评估子管道  │      │            │
│  │  │    - human 类型 → 占位返回      │      │            │
│  │  │  ④ ExpectEvaluator 判定 pass/fail│     │            │
│  │  │  ⑤ ResultMapper 映射结果        │      │            │
│  │  └────────────┬───────────────────┘      │            │
│  │               ▼                           │            │
│  │  ┌────────────────────────────────┐      │            │
│  │  │ task_evaluate 返回结果           │      │            │
│  │  │                                │      │            │
│  │  │  全部通过:                      │      │            │
│  │  │    task_service.complete_evaluation │  │            │
│  │  │    (evaluating → completed)     │      │            │
│  │  │    → on_state_change 回调触发    │      │            │
│  │  │    → terminal Event.set()       │      │            │
│  │  │    → 子管道结束                  │      │            │
│  │  │                                │      │            │
│  │  │  失败但未耗尽:                   │      │            │
│  │  │    返回评估反馈给 LLM            │      │            │
│  │  │    LLM 继续改进 → 重新评估       │      │            │
│  │  │                                │      │            │
│  │  │  失败且耗尽:                     │      │            │
│  │  │    task_service.complete_evaluation │  │            │
│  │  │    (evaluating → failed)        │      │            │
│  │  │    → 子管道结束                  │      │            │
│  │  └────────────────────────────────┘      │            │
│  └──────────────────────────────────────────┘            │
│                         │                                 │
│                         ▼                                 │
│  ┌────────────────────────────────┐                      │
│  │ TaskWorker 后处理               │                      │
│  │  ① 检查 task 状态               │                      │
│  │  ② 如果仍是 running:            │                      │
│  │     有 result → move_to_evaluating │                   │
│  │     无 result → fail_task        │                      │
│  │  ③ 等待 terminal Event（终态通知）│                     │
│  │  ④ lifecycle 钩子处理            │                      │
│  └────────────┬───────────────────┘                      │
│               ▼                                           │
│  TaskService.on_state_change 回调                         │
│    → EventBus 发布 task_state_changed                     │
│    → 父管道的 TaskEventReceiver 收到通知                    │
│    → 注入通知消息到父管道的 messages                        │
│               ▼                                           │
│  主管道恢复执行                                            │
│    ChildTaskGuard 检测到子任务已完成                        │
│    → 不再返回 wait                                        │
│    → LLM 生成最终回答给用户                                │
└──────────────────────────────────────────────────────────┘
```

#### 路径 B：评估者 Agent 模式（evaluation_mode=true）

当指标类型为 `agent`（如 semantic_check、function_verify）时，评估流程不同：

```
task_evaluate 工具调用
  │
  ▼
EvaluationExecutor.run_evaluation()
  │
  ▼
EvaluationEngine._evaluate_agent()
  │
  ├─ 从 agent_registry 获取 evaluator_agent 配置
  ├─ 构建评估指令 prompt（含指标描述、验收标准、待评估内容）
  │
  ▼
pipeline_factory() 创建独立子管道
  │
  ▼
evaluator_agent 子管道执行
  │
  ├─ 迭代 1: LLM 分析评估任务
  ├─ LLM 输出纯文本 → TaskReminder（evaluation_mode=true）
  │   └─ 检测输出中是否包含 evaluation_result JSON
  │       ├─ 检测到 → 解析 {"passed": true/false, "score": 0-100, ...}
  │       │          → route_signal=end（停止评估管道）
  │       │          → 返回解析后的结果
  │       └─ 未检测到 → 注入提醒："请输出结构化评估结论 JSON"
  │                    → route_signal=next_llm（继续评估）
  │
  ▼
EvaluationEngine._parse_evaluation_result()
  │
  ├─ 从管道最终输出中提取 evaluation_result JSON
  ├─ 支持格式: 嵌套 {"evaluation_result": {...}}
  │            直接 {"passed": true, ...}
  │            Markdown code block 中的 JSON
  │
  ▼
结果返回给 task_evaluate → 返回给执行者 Agent
```

#### 关键区别：两条路径的终止机制

| | 路径 A（执行者自评） | 路径 B（评估者 Agent） |
|---|---|---|
| **谁来评估** | 执行任务的 Agent 自己调 task_evaluate | 独立的 evaluator_agent |
| **怎么触发** | Agent 主动调用 task_evaluate 工具 | EvaluationEngine 自动创建子管道 |
| **怎么结束** | 工具返回结果 → 管道根据结果决定是否结束 | TaskReminder 检测到 JSON → 发 end 信号 |
| **结果怎么传** | 工具返回值直接给 LLM | 解析子管道输出 → 嵌入 task_evaluate 返回值 |
| **适用指标** | file_check, code_check, bash_check 等 | semantic_check, function_verify 等 |

---

### 1.1 每个环节的日志检查点和数据检查

以下是完整流程中**每个环节**应该检查的日志和数据：

#### 环节 1：task_submit 工具执行

**检查日志文件**: `logs/agent_os.log`

```
搜索关键词:
  "[TaskSubmit]"          — 工具执行日志
  "task.submitted"        — 事件发布
  "param_inject"          — 参数注入情况
```

**应看到的日志**:

```
[tools.builtin.task_submit] INFO: TaskSubmitTool creating task...
[tools.builtin.task_submit] INFO: Task created: task_id=xxx, status=pending
[pipeline.event_bus] INFO: Event published: task.submitted
```

**应检查的数据**:

| 数据项 | 在哪里看 | 正确值 |
|--------|---------|--------|
| `task_id` | task_submit 工具返回值 | 非空字符串 |
| `parent_task_id` | TaskModel.parent_task_id | 如果是子任务，应为父任务的 ID；如果是主管道直接创建，为空 |
| `status` | TaskModel.status | `pending` |
| `metadata.evaluation_metric_ids` | TaskModel.metadata | 评估指标 ID 列表 |
| `metadata.acceptance_criteria` | TaskModel.metadata | 验收标准字典 |
| `metadata.workspace` | TaskModel.metadata | 工作空间路径 |

**验证方法**:

```python
# Python REPL
from tasks.service import TaskService
ts = TaskService()
task = ts.get_task("task_xxx")  # 从日志中获取 task_id
print(f"status={task.status}")
print(f"parent={task.parent_task_id}")
print(f"metrics={task.metadata.get('evaluation_metric_ids')}")
print(f"ac={task.metadata.get('acceptance_criteria')}")
```

**常见失败**:

| 日志特征 | 问题 | 修复方向 |
|---------|------|---------|
| `"parent_task_id is None"` 警告 | param_inject 插件未注入 | 检查 param_inject 是否在插件链中 |
| 无 `"task.submitted"` 事件日志 | EventBus 为 None | 检查 `_event_bus` 是否创建 |
| `"SERVICE_UNAVAILABLE"` | TaskService 不可用 | 检查服务初始化顺序 |

---

#### 环节 2：EventBus 传递 + TaskWorker 接收

**检查日志文件**: `logs/agent_os.log`

```
搜索关键词:
  "TaskWorker"             — Worker 处理日志
  "_on_task_submitted"     — 事件回调
  "_execute_background_task" — 开始执行
```

**应看到的日志**:

```
[infrastructure.task_worker] INFO: TaskWorker received task.submitted: task_id=xxx
[infrastructure.task_worker] INFO: TaskWorker starting background task: task_id=xxx
[infrastructure.task_worker] INFO: Resolved agent config: config_id=yyy
[infrastructure.task_worker] INFO: Starting task xxx: pending → running
```

**应检查的数据**:

| 数据项 | 在哪里看 | 正确值 |
|--------|---------|--------|
| 事件是否到达 | 日志中是否有 `_on_task_submitted` | 必须出现 |
| Agent 配置是否找到 | 日志中 `Resolved agent config` | 非空 |
| 任务状态变更 | `starting task xxx: pending → running` | pending → running |
| workspace 路径 | 日志中 `workspace=` | 有效的目录路径 |

**如果没看到这些日志**:

| 现象 | 原因 | 检查 |
|------|------|------|
| 完全没有 TaskWorker 日志 | TaskWorker.start() 未被调用 | 搜索 `await task_worker.start()` |
| 收到事件但没开始执行 | Agent 配置找不到 | 检查 `agent_registry.get(target_id)` 的 target_id |
| 开始执行但立即失败 | 服务缺失 | 看具体异常栈 |

---

#### 环节 3：子管道执行

**检查日志文件**: `logs/agent_os.log`

```
搜索关键词:
  "engine.run" 或 "PipelineEngine"  — 管道启动
  "LLMCore"                          — LLM 调用
  "ToolCore"                         — 工具调用
  "_run_loop"                        — 迭代循环
  "iteration"                        — 迭代计数
```

**应看到的日志**（按时间顺序）:

```
# 迭代 1: LLM 分析任务
[pipeline.engine] INFO: PipelineEngine._run_loop: iteration=1, core_type=llm_call
[plugins.core.llm_core] INFO: LLMCore calling model: MiniMax-M2.7
[plugins.core.llm_core] INFO: LLM response received: text_len=xxx, tool_calls=0

# 如果 TaskReminder 触发（LLM 只输出文本无工具调用时）
[plugins.output.task_reminder] INFO: TaskReminder[iter=1][task=xxx]: injecting reminder #1/10
[plugins.output.task_reminder] INFO: triggering next_llm

# 迭代 2: LLM 调用工具
[pipeline.engine] INFO: PipelineEngine._run_loop: iteration=2, core_type=llm_call
[plugins.core.llm_core] INFO: LLM response received: tool_calls=1
[pipeline.engine] INFO: Route resolved: next_tool

# 迭代 3: 工具执行
[pipeline.engine] INFO: PipelineEngine._run_loop: iteration=3, core_type=tool_execute
[plugins.core.tool_core] INFO: ToolCore executing: current_time
[plugins.core.tool_core] INFO: Tool execution completed: success=True

# 迭代 4: LLM 处理工具结果
[pipeline.engine] INFO: PipelineEngine._run_loop: iteration=4, core_type=llm_call
```

**应检查的数据**:

| 数据项 | 在哪里看 | 正确值 |
|--------|---------|--------|
| `core_type` 变化序列 | 日志中每次迭代的 core_type | llm_call → tool_execute → llm_call 交替 |
| `raw_tool_calls` | 状态中 | 工具调用时非空列表 |
| `raw_result` | 状态中 | LLM 输出非空 |
| `iteration` | 状态中 | 递增，不应超过 max_iterations |
| 路由信号 | 日志中 `Route resolved` | next_tool / next_llm / end / wait |

**关键日志模式**:

```
正常流程:
  iter=1  core_type=llm_call    → LLM 分析
  iter=2  core_type=llm_call    → LLM 决定调工具 (tool_calls != [])
  route=next_tool
  iter=3  core_type=tool_execute → 执行工具
  route=next_llm
  iter=4  core_type=llm_call    → LLM 处理结果
  iter=5  core_type=llm_call    → LLM 只输出文本（无工具调用）
  TaskReminder 触发 → next_llm
  iter=6  core_type=llm_call    → LLM 调用 task_evaluate
  route=next_tool
  iter=7  core_type=tool_execute → task_evaluate 执行
```

---

#### 环节 4：task_evaluate 工具执行（路径 A：执行者自评）

**检查日志文件**: `logs/agent_os.log`

```
搜索关键词:
  "[TaskEvaluate]"         — 评估工具日志
  "EvaluationExecutor"     — 评估执行器
  "EvaluationEngine"       — 评估引擎
  "complete_evaluation"    — 状态回写
```

**应看到的日志**:

```
# 1. task_evaluate 工具被调用
[tools.builtin.task_evaluate] INFO: [TaskEvaluate] 自动评估 | task_id=xxx | metrics=['code_check']

# 2. EvaluationExecutor 开始
[evaluation.executor] INFO: Running evaluation for task xxx

# 3. 每个指标的评估
[evaluation.engine] INFO: Tool evaluation: code_check (evaluator_id=file_read)
[evaluation.engine] INFO: Tool evaluation completed: code_check -> success=True

# 4. 结果映射
[evaluation.mapper] INFO: Evaluation result: overall_passed=True

# 5. 状态回写
[evaluation.executor] INFO: Task xxx evaluation completed: passed
[tasks.service] INFO: Task xxx state change: evaluating -> completed
```

**应检查的数据**:

| 数据项 | 在哪里看 | 正确值 |
|--------|---------|--------|
| `metric_ids` | 日志 `metrics=[...]` | 与 task.metadata 中声明的一致 |
| 每个指标的 `passed` | 评估结果 | 全部 True（通过时） |
| `overall_passed` | 结果 | True（通过时）/ False（失败时） |
| 任务最终状态 | TaskService | `completed` 或 `failed` |
| `task_evaluation_completed` | 状态中 | True（通过时） |

**评估失败时的日志**:

```
# 失败但未耗尽重试次数
[tools.builtin.task_evaluate] INFO: Evaluation not passed, retry remaining: 2
# → 返回评估反馈给 LLM，LLM 继续改进

# 失败且重试次数耗尽
[tools.builtin.task_evaluate] INFO: Evaluation failed, retries exhausted
[tasks.service] INFO: Task xxx state change: evaluating -> failed
```

---

#### 环节 4B：评估者 Agent 执行（路径 B：agent 类型指标）

当指标类型是 `agent`（如 semantic_check、function_verify）时，会启动独立的评估子管道。

**检查日志文件**: `logs/agent_os.log`

```
搜索关键词:
  "Agent evaluation:"      — Agent 型评估启动
  "_evaluate_agent"        — Agent 评估方法
  "evaluator_agent"        — 评估 Agent
  "evaluation_result"      — 评估结果 JSON
```

**应看到的日志**:

```
# 1. Agent 型评估启动
[evaluation.engine] INFO: Agent evaluation: semantic_check (evaluator_id=evaluator_agent) — launching sub-pipeline

# 2. 评估子管道执行（关键：evaluation_mode=true 的 TaskReminder）
[plugins.output.task_reminder] INFO: TaskReminder[iter=x]: evaluation_mode=true
[plugins.output.task_reminder] INFO: TaskReminder[iter=x][task=__eval__semantic_check]: evaluation_result JSON detected, sending end signal

# 3. 结果解析
[evaluation.engine] INFO: Agent evaluation completed: semantic_check -> passed=True, score=85
```

**应检查的数据**:

| 数据项 | 在哪里看 | 正确值 |
|--------|---------|--------|
| evaluator_agent 配置 | agent_registry.get("evaluator_agent") | 非空，level 和配置正确 |
| 评估提示词 | `_build_agent_eval_prompt` 输出 | 包含指标描述、验收标准 |
| LLM 输出格式 | 子管道 raw_result | 包含 `{"evaluation_result": {"passed": ..., "score": ...}}` |
| JSON 解析结果 | `_parse_evaluation_result` 返回值 | 含 passed/score/feedback 字段 |
| TaskReminder 行为 | 日志 | `evaluation_mode=true` 时检测 JSON 并发 end 信号 |

**关键验证点**：

TaskReminder 在 `evaluation_mode=true` 时的行为与普通模式完全不同：

```python
# task_reminder.py execute() 方法关键逻辑:
if evaluation_mode:
    detected = self._detect_evaluation_result_json(raw_text)
    if detected is not None:
        # 检测到评估 JSON → 发 end 信号，停止评估管道
        return OutputResult(
            state_updates={"evaluation.detected_result": detected},
            route_signal=RouteSignal(route_type="end",
                reason="task_reminder: evaluation_result JSON detected"),
        )
    # 未检测到 → 注入提醒，让 LLM 继续输出
```

这段逻辑必须正确工作，否则评估管道永远不会停止。

**常见失败**:

| 日志特征 | 问题 | 修复方向 |
|---------|------|---------|
| 无 "Agent evaluation" 日志 | pipeline_factory 为 None | 检查 `_agent_os_pipeline_factory` 全局变量 |
| "Agent 'evaluator_agent' not found" | Agent 配置未注册 | 检查 config/agents/system/evaluator_agent.yaml |
| "evaluation_result JSON detected" 不出现 | LLM 没输出正确格式 JSON | 检查评估提示词和 evaluator_agent 的 system_prompt |
| 子管道超时 | LLM 一直在输出非 JSON 内容 | 检查 max_reminders 配置 |

---

#### 环节 5：TaskWorker 后处理

**检查日志文件**: `logs/agent_os.log`

```
搜索关键词:
  "TaskWorker:" 后面的状态
  "still RUNNING"
  "move_to_evaluating"
  "terminal state"
  "lifecycle"
```

**应看到的日志**（管道退出后）:

```
# 情况 A: 任务已被 task_evaluate 标记为 completed
[infrastructure.task_worker] INFO: TaskWorker: task xxx reached terminal state
[infrastructure.task_worker] INFO: TaskWorker: lifecycle on_eval_passed, task_id=xxx

# 情况 B: 管道退出但任务仍是 running（有 result）
[infrastructure.task_worker] INFO: TaskWorker: task xxx still RUNNING after pipeline exit, has result output -> moving to evaluating

# 情况 C: 管道退出但任务仍是 running（无 result）→ 失败
[infrastructure.task_worker] WARNING: TaskWorker: task xxx still RUNNING after pipeline exit. iterations=10/10, ended=True -> marking as failed
```

**应检查的数据**:

| 数据项 | 正确值 | 异常处理 |
|--------|--------|---------|
| terminal Event 是否被 set | 是（`evt.set()` 被调用） | 未 set → 父管道永远卡在 wait |
| 等待超时 | 默认 600s | 超时 → `fail_task` |
| lifecycle 钩子 | 根据终态调用 on_eval_passed/on_eval_failed | 钩子失败不影响任务状态 |

---

#### 环节 6：终态通知回传父管道

**检查日志文件**: `logs/agent_os.log`

```
搜索关键词:
  "task_state_changed"     — 状态变更事件
  "TaskEventReceiver"      — 事件接收插件
  "ChildTaskGuard"         — 子任务守卫
  "connection_confirmation" — 通知注入
```

**应看到的日志**:

```
# 1. 状态变更事件发布
[tasks.service] INFO: Task xxx state change: evaluating -> completed
[pipeline.event_bus] INFO: Event published: task_state_changed

# 2. 父管道的 TaskEventReceiver 收到通知
[plugins.input.task_event_receiver] INFO: TaskEventReceiver: received task_state_changed for task=xxx, new_status=completed

# 3. 父管道恢复
[plugins.output.child_task_guard] INFO: ChildTaskGuard: no active children, allowing pipeline to continue
```

**应检查的数据**:

| 数据项 | 在哪里看 | 正确值 |
|--------|---------|--------|
| 事件是否发布 | EventBus 日志 | `task_state_changed` 出现 |
| 父管道是否订阅 | TaskEventReceiver 日志 | 收到事件 |
| 注入的消息内容 | 状态中 messages 列表 | 包含子任务完成通知 |
| ChildTaskGuard 行为 | 不再返回 wait | 允许继续 next_llm 或 end |
| 父管道是否恢复 | LLM 生成最终回答 | 用户看到回答 |

**最关键的检查**：如果父管道卡住不动：

```
1. 检查 ChildTaskGuard 是否仍然检测到活跃子任务
   → 日志: "has active child tasks" 仍然出现
   → 原因: 终态通知没有传到 ChildTaskGuard
   → 修复: 检查 _has_active_children 方法的实现

2. 检查 TaskEventReceiver 是否在主管道的输入插件链中
   → 如果不在，父管道永远不会收到通知
   → 修复: 检查 default.yaml 的 input plugins 配置

3. 检查 asyncio.Event 是否被正确 set
   → 日志: "TaskWorker: task xxx reached terminal state"
   → 如果没有，说明 TaskWorker 的后处理出了问题
```

---

### 1.2 逐步验证步骤

#### Step 1: 基础服务初始化

```bash
export MINIMAX_API_KEY="你的key"
python run.py real --debug
```

**日志检查清单**:

```
grep "Service created" logs/agent_os.log
✅ task_service
✅ message_queue
✅ tool_registry (含 core tools 数量)
✅ memory_service

grep "Task worker" logs/agent_os.log
✅ "Task worker initialized"

grep -i "event_bus" logs/agent_os.log
✅ EventBus 实例化（不应有 "not available" 或 "None" 警告）

grep "Pipeline config loaded" logs/agent_os.log
✅ 配置加载成功
```

---

#### Step 2: 简单任务提交 + 子管道执行

在 CLI 中输入：

```
请帮我创建一个任务，目标是"获取当前时间"，使用 current_time 工具完成，验收标准是返回了有效的时间信息
```

**日志检查清单**（按时间顺序逐项检查）:

```
# ① task_submit 工具调用
grep "TaskSubmitTool" logs/agent_os.log
✅ "Task created: task_id=xxx"
✅ "Event published: task.submitted"

# ② TaskWorker 接收
grep "TaskWorker" logs/agent_os.log
✅ "received task.submitted"
✅ "starting background task"
✅ "pending → running"

# ③ 子管道执行
grep "_run_loop" logs/agent_os.log
✅ 多个 iteration 日志（至少 2 个）
✅ core_type 在 llm_call 和 tool_execute 之间切换

# ④ 工具执行
grep "ToolCore" logs/agent_os.log
✅ "executing: current_time"
✅ "success=True"

# ⑤ TaskReminder 触发（如果 LLM 先输出了文本）
grep "TaskReminder" logs/agent_os.log
✅ "injecting reminder" 或 "skip, has tool_calls"

# ⑥ task_evaluate 被调用
grep "TaskEvaluate" logs/agent_os.log
✅ "自动评估 | task_id=xxx"

# ⑦ 评估执行
grep "EvaluationEngine" logs/agent_os.log
✅ 评估指标执行日志

# ⑧ 状态变更
grep "state change" logs/agent_os.log
✅ "evaluating -> completed" 或 "evaluating -> failed"

# ⑨ 通知回传
grep "TaskEventReceiver" logs/agent_os.log
✅ 收到 task_state_changed 事件

# ⑩ 父管道恢复
grep "ChildTaskGuard" logs/agent_os.log
✅ "no active children" 或类似
```

---

#### Step 3: 数据完整性检查

任务完成后，用 Python 检查数据：

```python
import sys; sys.path.insert(0, "src")
from tasks.service import TaskService

ts = TaskService()

# 列出所有任务
for t in ts.list_tasks():
    status = t.status.value if hasattr(t.status, 'value') else t.status
    parent = t.parent_task_id or "ROOT"
    print(f"  {t.id}: {status} | parent={parent} | title={t.title[:30]}")

# 检查具体任务
task = ts.get_task("task_xxx")  # 替换为实际 ID
print(f"status: {task.status}")
print(f"result: {task.result}")
print(f"metadata keys: {list(task.metadata.keys())}")
print(f"eval_metric_ids: {task.metadata.get('evaluation_metric_ids')}")
print(f"acceptance_criteria: {task.metadata.get('acceptance_criteria')}")
print(f"eval_retry_count: {task.metadata.get('eval_retry_count')}")
print(f"workspace: {task.metadata.get('workspace')}")
```

---

#### Step 4: 评估者 Agent 模式验证

要触发路径 B（agent 型评估），需要任务声明 agent 类型的评估指标：

在 CLI 中输入：

```
请帮我创建一个任务，目标是"写一个 Python 快速排序函数"，
验收标准包括语义质量评估
```

**额外检查的日志**:

```
grep "Agent evaluation" logs/agent_os.log
✅ "launching sub-pipeline"
✅ evaluator_agent 子管道启动

grep "evaluation_result JSON detected" logs/agent_os.log
✅ TaskReminder 在 evaluation_mode 下检测到 JSON
✅ "sending end signal"

grep "_parse_evaluation_result" logs/agent_os.log
✅ JSON 解析成功

# 如果没有检测到 JSON，检查：
grep "evaluation_mode" logs/agent_os.log
✅ TaskReminder 是否正确进入 evaluation_mode 分支
```

---

### 1.3 日志文件位置汇总

| 日志文件 | 位置 | 内容 |
|---------|------|------|
| 主日志 | `logs/agent_os.log` | 所有组件的运行日志 |
| 任务数据 | `data/tasks/tree_{root_id}/` | YAML 格式的任务持久化 |
| 管道记录 | `data/pipelines/` | 管道执行记录 |
| 会话数据 | `data/session/` | CLI 会话元数据 |

### 1.4 关键数据文件检查

```bash
# 检查任务持久化
ls data/tasks/
cat data/tasks/tree_*/task_*.yaml

# 检查管道执行记录
ls data/pipelines/

# 检查会话数据
cat data/session/.current_session_id
```

---

## 第二部分：工作空间 + Git + 资源合并验证

这部分验证的是：**Agent 在隔离工作空间里写完文件后，能否正确合并回主分支**。

这是任务系统最关键的实际价值——用户交给 Agent 一个任务，Agent 在独立环境中完成，成果自动合并回来。

### 2.0 工作空间与 Git 完整流程

```
用户: "帮我创建一个新 Agent"
  │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ L1 主管道                                                    │
│  LLM 分析 → 决定委派 → task_submit(target=resource_generator) │
└───────────┬─────────────────────────────────────────────────┘
            │ EventBus: task.submitted
            ▼
┌─────────────────────────────────────────────────────────────┐
│ TaskWorker._execute_background_task()                        │
│                                                              │
│  ① lifecycle.on_task_start(workspace)                       │
│     │                                                        │
│     ├─ 检测场景:                                             │
│     │   A: 已有项目 + 有 .git → git worktree                 │
│     │   B: 已有项目 + 无 .git → git init + worktree          │
│     │   C: 新项目 → 创建 .ai_workspaces/{task_id}/ + git init│
│     │                                                        │
│     ├─ 场景 A/B:                                             │
│     │   git worktree add .ai_workspaces/{task_id}            │
│     │   git checkout -b task/{task_id}                       │
│     │                                                        │
│     └─ 场景 C:                                               │
│         mkdir .ai_workspaces/{task_id}/                      │
│         git init + git add -A + git commit                   │
│                                                              │
│  ② 构建 user_input（含 workspace 路径提示）                  │
│  ③ engine.run(user_input, agent_config)                      │
│     │                                                        │
│     │  子管道执行：                                           │
│     │   LLM → 分析任务 → 调用 file_write 写文件              │
│     │   LLM → 调用 bash_execute 运行测试                     │
│     │   LLM → 调用 task_evaluate 评估                        │
│     │                                                        │
│  ④ 评估通过后:                                               │
│     lifecycle.on_eval_passed(task_id, workspace)             │
│     │                                                        │
│     ├─ 场景 A/B:                                             │
│     │   git add -A && git commit -m "task {id} completed"   │
│     │   git checkout main && git merge task/{task_id}        │
│     │   git worktree remove .ai_workspaces/{task_id}         │
│     │   git branch -d task/{task_id}                         │
│     │                                                        │
│     └─ 场景 C (子任务):                                      │
│         resource_merge: workspace → project_root             │
│         或 git merge feature/{task_id} → main                │
│                                                              │
│  ⑤ 清理:                                                    │
│     删除 workspace 目录                                      │
│     删除临时分支                                             │
└─────────────────────────────────────────────────────────────┘
```

### 2.1 前置修复（必须先修）

在测试工作空间/Git 流程之前，必须先修复以下 Bug（来自 DIAGNOSIS.md）：

| # | Bug | 位置 | 修复内容 |
|---|-----|------|---------|
| 1 | acceptance_criteria 模板变量未替换 | `task_evaluate.py` `_get_input_params` | 替换 `{{workspace}}` `{{task_id}}` |
| 2 | file_check 路径与 Agent 写入路径不匹配 | `task_submit.py` 自动生成验收标准时 | 基于实际 workspace 生成 file_check 路径 |
| 3 | 评估者 Agent 找不到模板文件 | `evaluation/engine.py` `_evaluate_agent` | 用项目根目录作为评估者工作目录 |

### 2.2 验证递进顺序

**必须按以下顺序逐步验证，前一个通过再测下一个。**

---

#### Step 1: 简单任务 — 验证基础管道 + 工具调用 + 评估

**目标**: 验证最基本的任务提交流程（不涉及 workspace 和文件写入）

**CLI 输入**:
```
请帮我创建一个任务，目标是"获取当前时间"，使用 current_time 工具完成，
验收标准是返回了有效的时间信息
```

**日志检查点**:
```
grep "task.submitted"    logs/agent_os.log  ✅ 事件发布
grep "TaskWorker.*received" logs/agent_os.log  ✅ Worker 收到
grep "ToolCore.*current_time" logs/agent_os.log  ✅ 工具执行
grep "TaskReminder.*injecting" logs/agent_os.log  ✅ 提醒注入（如有）
grep "TaskEvaluate.*auto_complete" logs/agent_os.log  ✅ 评估调用
grep "state change.*completed" logs/agent_os.log  ✅ 任务完成
grep "TaskEventReceiver.*received" logs/agent_os.log  ✅ 通知回传
```

**数据检查**:
```bash
cat data/tasks/tree_*/task_*.yaml | grep -A2 "status:"
# 应看到 completed 状态

ls -la .ai_workspaces/  # 检查 workspace 是否已清理
git worktree list       # 应只有 main
git branch              # 应只有 main
```

**通过标准**: 任务状态为 `completed`，无残留 workspace/branch

---

#### Step 2: 带语义评估的任务 — 验证路径 B（evaluator_agent）

**目标**: 验证 agent 类型的评估指标能正确运行

**CLI 输入**:
```
请帮我创建一个任务，目标是"用 Python 写一个冒泡排序函数并保存到 bubble_sort.py"，
验收标准包括代码质量语义评估
```

**额外日志检查点**:
```
grep "Agent evaluation" logs/agent_os.log  ✅ 评估者子管道启动
grep "evaluation_result JSON detected" logs/agent_os.log  ✅ JSON 被检测到
grep "_parse_evaluation_result" logs/agent_os.log  ✅ JSON 解析成功
grep "Agent evaluation completed" logs/agent_os.log  ✅ 评估完成
grep "semantic_check" logs/agent_os.log  ✅ 指标名出现
```

**数据检查**:
```bash
# 检查任务结果中是否包含 semantic_check 评估结果
cat data/tasks/tree_*/task_*.yaml
# 应看到:
#   evaluation_metric_ids: [..., "semantic_check"]
#   metrics:
#     - metric_id: semantic_check
#       passed: true (或 false 但有 feedback)
```

**关键验证**: evaluator_agent 必须输出 `{"evaluation_result": {"passed": ..., "score": ...}}` 格式的 JSON，TaskReminder 必须在 `evaluation_mode=true` 下检测到并终止管道。

**如果失败**: 检查 evaluator_agent 配置是否正确（config/agents/system/evaluator_agent.yaml），特别是 system_prompt 中是否要求输出 JSON 格式。

---

#### Step 3: 创建新 Agent — 验证工作空间 + Git 完整流程

这是最复杂的测试，验证完整的 Agent 创建流程：

```
用户请求 → L1 提交 → L2 子任务分配给l3→ L3 执行
→ 子任务在 workspace 中生成 YAML 配置文件，所有任务完成
→ L2的任务评估通过 → 文件合并回主项目
→ 清理 workspace 和分支
```

**CLI 输入**:
```
请帮我创建一个名为 calculator_agent 的 Agent，
功能是做数学计算（加减乘除）
```

##### 3a: Workspace 创建阶段检查

**日志检查点**:
```
grep "lifecycle.*on_task_start" logs/agent_os.log  ✅ 生命周期钩子触发
grep "workspace" logs/agent_os.log | head -20  ✅ workspace 路径解析
grep "worktree\|git init\|Scenario" logs/agent_os.log  ✅ 场景检测结果
```

**数据检查**:
```bash
# 检查 workspace 目录是否创建
ls -la .ai_workspaces/

# 检查 git 状态
git worktree list
# 应看到类似:
#   D:/Jianguoyun/Agent os                         xxxxx [main]
#   D:/Jianguoyun/Agent os/.ai_workspaces/{task_id} xxxxx [task/{task_id}]

git branch
# 应看到新分支 task/{task_id}
```

**检查项**:

| 检查 | 预期 | 如果不对 |
|------|------|---------|
| `.ai_workspaces/{task_id}/` 存在 | 目录存在 | lifecycle.on_task_start 失败 |
| 里面有 `.git/` | 是（或 worktree 链接到主仓库） | git init/worktree 失败 |
| `git worktree list` 显示新 worktree | 是 | worktree add 失败 |
| 新分支 `task/{task_id}` 存在 | 是 | branch 创建失败 |
| workspace 内有项目文件（如果是 worktree 模式） | 应有（sparse checkout 或全量） | checkout 失败 |

##### 3b: Agent 在 workspace 中写文件

**日志检查点**:
```
grep "file_write.*calculator" logs/pipeline_*.log  ✅ Agent 写入了配置文件
grep "ToolCore.*file_write.*success" logs/pipeline_*.log  ✅ 写入成功
```

**数据检查**:
```bash
# 检查 workspace 内是否创建了配置文件
find .ai_workspaces/ -name "calculator_agent*" -o -name "generated_resource*"

# 读取生成的文件内容
cat .ai_workspaces/{task_id}/config/agents/executor/test/calculator_agent.yaml
# 或
cat .ai_workspaces/{task_id}/generated_resource.yaml

# 检查 git 状态（在 workspace 内）
cd .ai_workspaces/{task_id} && git status && git log --oneline -3 && cd ../..
# 应看到未提交的变更或自动提交记录
```

**关键验证**:
- 文件内容非空（不是像之前 e2e_time_agent.yaml 那样 0 行）
- YAML 格式正确（能被解析）
- 包含 config_id、name、description 等必要字段

##### 3c: 评估阶段

**日志检查点**:
```
grep "lifecycle.*on_before_evaluate" logs/agent_os.log  ✅ 评估前钩子触发
grep "git add\|git commit" logs/agent_os.log  ✅ 自动提交 workspace 变更
grep "TaskEvaluate.*auto_complete" logs/agent_os.log  ✅ 评估调用
grep "file_check" logs/agent_os.log  ✅ 文件检查指标
grep "Tool evaluation completed.*file_check.*success=True" logs/agent_os.log  ✅ 文件检查通过
```

**file_check 必须通过的条件**:
1. Agent 确实写入了文件
2. 文件在正确的路径下
3. `_get_input_params` 正确解析了 workspace 路径
4. file_read 工具能在 workspace 中找到文件

##### 3d: 合并回主项目（最关键）

**日志检查点**:
```
grep "lifecycle.*on_eval_passed" logs/agent_os.log  ✅ 评估通过钩子
grep "merge\|resource_merge\|copy.*to" logs/agent_os.log  ✅ 合并操作
grep "worktree remove\|branch -d" logs/agent_os.log  ✅ 清理操作
```

**数据检查**（合并后）:
```bash
# 检查主项目中是否有生成的文件
cat config/agents/executor/test/calculator_agent.yaml
# 内容应与 workspace 中写入的一致

# 检查 git log 是否有合并提交
git log --oneline -5
# 应看到类似 "Merge task/{task_id}" 或 "task {id} completed"

# 检查 workspace 是否已清理
ls .ai_workspaces/
# 对应的 task_id 目录应该已被删除（或保留但标记为已完成）

# 检查 worktree 和分支是否已清理
git worktree list
# 应只剩 main

git branch
# 不应有残留的 task/ 分支
```

**合并后的完整检查清单**:

| # | 检查项 | 命令 | 预期结果 |
|---|--------|------|---------|
| 1 | 生成的文件在主项目中 | `cat config/agents/executor/test/calculator_agent.yaml` | 非空，YAML 格式正确 |
| 2 | 文件内容与 workspace 中一致 | `diff` 对比 | 无差异 |
| 3 | git log 有提交记录 | `git log --oneline -5` | 有 task 完成的提交 |
| 4 | workspace 已清理 | `ls .ai_workspaces/` | 无残留 task_id 目录 |
| 5 | worktree 已删除 | `git worktree list` | 只有 main |
| 6 | 临时分支已删除 | `git branch` | 只有 main |
| 7 | 主分支无冲突文件 | `git status` | 干净状态 |
| 8 | 任务状态为 completed | `cat data/tasks/tree_*/task_*.yaml` | status: completed |

##### 3e: 验证生成的 Agent 能被加载

```bash
# 检查新 Agent 能被 AgentRegistry 加载
python -c "
import sys; sys.path.insert(0, 'src')
from agents.registry import AgentRegistry
reg = AgentRegistry()
reg.load_directory('config/agents')
agent = reg.get('calculator_agent')
if agent:
    print(f'OK: {agent.config_id} | level={agent.level.value} | name={agent.display_name}')
else:
    print('FAIL: calculator_agent not found in registry')
"
```

---

### 2.3 合并失败/回滚场景验证

验证当评估失败时，workspace 和 git 能正确回滚而不污染主分支。

#### 场景 A: 评估失败 → 不应合并

**操作**: 故意给一个不可能完成的任务
```
请创建一个任务，要求在 config/ 目录下创建一个 system_override.yaml 文件，
但验收标准要求文件大小超过 10MB（故意设不可能的条件）
```

**检查**:
```bash
# 任务应标记为 failed
cat data/tasks/tree_*/task_*.yaml | grep "status: failed"

# 主项目中不应有该文件
ls config/system_override.yaml  # 应不存在

# workspace 应已清理或标记为失败
git worktree list  # 应只有 main
git branch         # 应只有 main
```

#### 场景 B: 多层嵌套任务的 workspace 继承

验证父任务 → 子任务 → 孙任务的 workspace 路径正确传递。

```
workspace 路径链:
  .ai_workspaces/{parent_id}/          ← 父任务
  .ai_workspaces/{parent_id}/{child_id}/  ← 子任务（嵌套模式）
```

**检查**:
```bash
# 查看嵌套 workspace 结构
find .ai_workspaces/ -name ".git" -type d

# 每层的 workspace 路径应该正确拼接
cat data/tasks/tree_*/task_*.yaml | grep "workspace:"
```

#### 场景 C: Git 冲突处理

如果 workspace 中的修改与主分支有冲突：

```bash
# 日志中应看到冲突检测
grep "conflict\|CONFLICT" logs/agent_os.log

# 预期行为:
# - 检测到冲突 → 标记任务需要人工介入
# - 不自动 force 合并
# - 保留 workspace 供人工审查
```

---

### 2.4 真实执行记录中发现的问题

基于 `logs/` 和 `data/` 中的历史执行记录，以下问题已确认：

| # | 问题 | 真实证据 | 修复状态 |
|---|------|---------|---------|
| 1 | **e2e_time_agent.yaml 创建了但是空文件（0 行）** | `config/agents/executor/test/e2e_time_agent.yaml` 存在但为空 | 未修 |
| 2 | **workspace 不是 git worktree 而是普通目录** | `.ai_workspaces/f3a015cdd5f1/` 有自己的 `.git/` 而非 worktree 链接 | 未修 |
| 3 | **残留的 worktree 未清理** | `git worktree list` 显示 `task/e2e_time_agent_generation` (prunable) | 未修 |
| 4 | **合并流程从未触发** | 所有任务都在评估阶段失败，从未走到 `on_eval_passed` | 未修 |
| 5 | **workspace 内文件未合并到主项目** | `.ai_workspaces/f3a015cdd5f1/reports/` 中有文件但主项目没有 | 未修 |
| 6 | **模板变量 `{{workspace}}` `{{task_id}}` 未替换** | acceptance_criteria 路径中仍为字面值 | 未修 |

**清理当前残留状态的命令**:
```bash
# 清理残留 worktree
git worktree remove .ai_workspaces/e8e_time_agent_generation 2>/dev/null
git worktree prune

# 清理残留分支
git branch -D task/e2e_time_agent_generation 2>/dev/null

# 清理残留 workspace（谨慎）
rm -rf .ai_workspaces/6fc706874587/ .ai_workspaces/e8323c15478a/ .ai_workspaces/f3a015cdd5f1/

# 清理空文件
rm config/agents/executor/test/e2e_time_agent.yaml 2>/dev/null

# 清理任务数据（重新开始）
rm -rf data/tasks/tree_6fc706874587/
```

---

## 第三部分：前端全链路验证

### 2.1 服务启动顺序

```bash
# 终端 1：启动后端 API + WebSocket 服务
cd "d:\Jianguoyun\Agent os"
python start_server.py

# 终端 2：启动前端开发服务器
cd "d:\Jianguoyun\Agent os\frontend"
npm install   # 首次需要
npm run dev
```

### 2.2 前后端对接断点分析

| # | 断点 | 风险 | 验证方法 |
|---|------|------|---------|
| 1 | 端口一致性 | 后端 API 在 8888，WebSocket 可能在 8765 或 8888 | 检查前端连接 URL |
| 2 | CORS 配置 | 后端 CORS 是否允许 localhost:5173/5188 | 浏览器控制台 CORS 错误 |
| 3 | JWT Token 传递 | WebSocket 连接时 token 是 query param 还是 header | 检查前端 send 逻辑 |
| 4 | 消息格式匹配 | 前端 EventEnvelope 和后端 EventEnvelope 字段是否一致 | 对比前后端 type 定义 |
| 5 | 流式事件序列 | stream_start → stream_chunk → stream_end 是否完整 | Network 面板看 WS 消息 |
| 6 | 管道集成 | start_server.py 的管道是否连接到真实 LLM | 检查 start_server.py 配置 |

### 2.3 验证步骤

#### Step 1: 后端 API 启动

```bash
python start_server.py

# 另一个终端：
curl http://localhost:8888/health
curl -X POST http://localhost:8888/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"demo","password":"demo12345"}'
```

断言: `/health` 返回 200, `/login` 返回 JWT token

#### Step 2: 前端启动与登录

```
浏览器打开 http://localhost:5173
→ 输入 demo/demo12345
→ 登录成功跳转聊天页
→ 浏览器控制台无红色错误
```

#### Step 3: WebSocket 连接

```
浏览器 DevTools → Network → WS
→ 创建新会话后观察 WS 连接
→ 应收到 connection_confirmation
→ 连接保持不断开
```

#### Step 4: 消息收发 + 流式响应

```
输入 "你好" 发送
→ 用户消息立即显示
→ AI 回复逐字流式出现
→ WS 面板: stream_start → chunk(s) → stream_end
→ 内容完整有意义
```

#### Step 5: 工具调用可视化

```
输入 "现在几点了？"
→ 应看到工具调用过程
→ 工具执行后 AI 继续回复
```

#### Step 6: 会话管理

```
创建新会话 → 发消息 → 切换旧会话 → 消息还在 → 删除会话
```

#### Step 7: 页面完整性检查

检查以下页面元素是否正确渲染：

| 检查项 | 预期 |
|--------|------|
| 登录页布局 | 用户名/密码输入框居中，有登录按钮 |
| 聊天主页布局 | 左侧会话列表 + 右侧聊天区域 |
| 消息气泡 | 用户消息右侧，AI 消息左侧，有头像/标记 |
| 输入框 | 底部固定，支持多行，有发送按钮 |
| 状态指示 | AI 回复时显示加载/打字动画 |
| 工具调用卡片 | 可展开查看工具名、参数、结果 |
| 侧边栏 | 会话列表可滚动，支持新建/删除 |
| 响应式 | 窗口缩小时布局不崩溃 |

#### Step 8: 异常场景

| 场景 | 操作 | 预期 |
|------|------|------|
| 网络断开 | 断 WiFi 后发消息 | 显示断开提示，不崩溃 |
| Token 过期 | 等 30 分钟 | 自动刷新或跳转登录 |
| 后端重启 | 重启 start_server.py | 前端自动重连 |
| 快速发送 | 连续发 5 条 | 按序处理不混乱 |
| 超长消息 | 5000 字 | 正常处理不卡顿 |

---

### 2.4 前端日志检查点

**浏览器控制台**（F12 → Console）:

```
应看到:
✅ WebSocket connected
✅ Session created / Thread loaded

不应看到:
❌ TypeError / Cannot read property
❌ 401 Unauthorized (除非 token 过期)
❅ WebSocket connection error
❅ CORS error
```

**后端日志**（`logs/agent_os.log`）:

```
应看到:
✅ WebSocket connection established: thread_id=xxx
✅ Message received: user_input
✅ Pipeline execution started
✅ Streaming response started
✅ Stream completed

不应看到:
❌ Pipeline not initialized
❌ Authentication failed
❌ JSON decode error
❌ Unexpected message type
```

---

## 第二部分 B：前端设计理念验证（基于 future/ 设计文档）

> 基于 `future/frontend-rendering-design.md` 和 `future/frontend-backend-protocol.md` 中的设计愿景和规范，
> 验证前端实现是否与设计目标一致。

### B.1 设计愿景对齐检查

设计文档定义了 5 个核心体验承诺：

| ID | 承诺 | 含义 | 当前验证方法 |
|----|------|------|-------------|
| C1 | 无所不能 | 所有功能可通过对话完成 | 发送各种类型任务（文件操作、搜索、代码修改）验证工具调用链路 |
| C2 | 开箱即用 | 首次使用无需配置 | 新用户注册后直接可用，无报错无引导 |
| C3 | 千人千面 | 同一功能不同场景不同呈现 | 对比 chat / floating / workspace 三种空间的同一功能展示 |
| C4 | 丝滑流畅 | 16ms 刷新、流式响应 | DevTools Performance 面板检查帧率，WS 面板检查 chunk 间隔 |
| C5 | 随心智适应 | 根据用户习惯调整 | 长期使用后 memory 系统是否影响回复（Phase 4 验证） |

### B.2 五大渲染空间验证

设计文档定义了 5 种渲染空间，每种有不同用途和交互方式：

| 空间 | 用途 | 当前状态 | 验证方法 |
|------|------|---------|---------|
| Chat Panel (聊天面板) | 主交互区，文字+卡片 | Phase 1 实现 | 发送消息，检查流式回复+工具卡片 |
| Floating Window (浮窗) | 临时辅助，悬停预览 | Phase 2 | 工具调用结果应支持浮窗查看 |
| Workspace Panel (工作区面板) | 深度操作，文件编辑 | Phase 3 | 创建 Agent 任务后检查 workspace 面板 |
| Dock Bar (停靠栏) | 常驻工具，快捷操作 | Phase 3 | 检查侧边栏常驻组件 |
| Fullscreen Overlay (全屏覆盖) | 沉浸式，代码编辑/图表 | Phase 4 | 暂无，后续验证 |

**验证步骤**：
1. Chat Panel: 发送 "帮我搜索一下项目里的所有 yaml 文件" → 结果以卡片形式展示
2. Floating Window: 鼠标悬停工具调用结果 → 应弹出浮窗详情（如果已实现）
3. Workspace Panel: 提交创建 Agent 的任务 → 右侧应出现工作区面板

### B.3 前后端通信协议验证

基于 `future/frontend-backend-protocol.md` 的 WebSocket 统一事件信封。

#### B.3.1 消息格式

每个 WebSocket 消息必须是：
```json
{
  "type": "事件类型",
  "data": { "事件数据" },
  "timestamp": "ISO8601",
  "request_id": "唯一ID"
}
```

**验证**: 在浏览器 DevTools → Network → WS 面板中，检查每条消息都包含这 4 个字段。

#### B.3.2 流式事件序列验证

| 事件 | 触发时机 | 需检查字段 |
|------|---------|-----------|
| `stream_start` | AI 开始回复 | `data.thread_id`, `data.message_id` |
| `stream_chunk` | 每个生成 token | `data.content` (增量文本) |
| `stream_end` | AI 回复完成 | `data.message_id`, `data.usage` |
| `thinking_start` | 思考开始 | `data.thread_id` |
| `thinking_chunk` | 思考内容 | `data.content` |
| `thinking_end` | 思考结束 | — |
| `execution_start` | 工具调用开始 | `data.tool_name`, `data.tool_id` |
| `execution_progress` | 工具执行进度 | `data.progress`, `data.message` |
| `execution_done` | 工具调用完成 | `data.result`, `data.success` |
| `iteration_start` | 管道迭代开始 | `data.iteration` |
| `iteration_end` | 管道迭代结束 | `data.iteration`, `data.summary` |
| `pipeline_start` | 管道启动 | `data.pipeline_id` |
| `pipeline_end` | 管道结束 | `data.status`, `data.summary` |
| `plugin_error` | 插件错误 | `data.plugin`, `data.error` |
| `pipeline_error` | 管道错误 | `data.error`, `data.recoverable` |

**验证步骤**：
1. 发送 "你好" → WS 面板应看到: `pipeline_start` → `iteration_start` → `stream_start` → `stream_chunk`(多次) → `stream_end` → `iteration_end` → `pipeline_end`
2. 发送 "搜索 yaml 文件" → 应额外看到: `execution_start` → `execution_progress` → `execution_done`
3. 检查 `timestamp` 字段是否为合法 ISO8601 格式
4. 检查 `request_id` 是否在相关事件间保持一致（可追踪完整请求链路）

#### B.3.3 执行控制验证

| 控制功能 | 协议定义 | 验证方法 |
|---------|---------|---------|
| 停止生成 | 前端发 `{type: "stop_generation"}` | AI 回复中途点击停止按钮 → 后端停止 → 收到 `stream_end` |
| 审批机制 | 后端发 approval 请求 → 前端发 `{resume_action: {approved: true/false}}` | 触发需要审批的工具 → 检查审批 UI |

### B.4 Chat 交互组件验证

设计文档定义了 8 种聊天交互组件，用于富内容展示：

| 组件 | 用途 | 触发场景 |
|------|------|---------|
| form | 表单输入 | 任务需要参数收集 |
| chart | 图表展示 | 数据分析结果 |
| gallery | 图片画廊 | 图片相关输出 |
| table | 表格展示 | 结构化数据展示 |
| progress | 进度条 | 长任务执行中 |
| code_block | 代码块 | 代码生成/展示 |
| status_card | 状态卡片 | 任务状态/工具结果 |
| decision | 决策确认 | 需要用户选择 |

**当前验证重点**（Phase 1-2 应实现的组件）：
1. `status_card`: 工具调用结果应使用 status_card 渲染 → 检查工具执行后显示
2. `code_block`: AI 输出代码应使用 code_block → 发送 "写一个 hello world" 检查
3. `progress`: 长时间任务应显示进度 → 提交创建 Agent 任务时检查
4. `decision`: 需要用户确认的操作 → 触发审批场景检查

### B.5 UI Schema 就绪检查

设计文档定义了 UI Schema 结构：`identity + actions + rendering + clients`

**验证**：检查前端代码中是否已实现 UI Schema 解析：

```bash
# 检查前端是否有 UI Schema 相关类型定义
grep -r "UISchema\|ui_schema\|UiSchema" frontend/src/ --include="*.ts" --include="*.tsx" -l

# 检查是否有渲染空间路由
grep -r "rendering.*space\|ChatPanel\|FloatingWindow\|WorkspacePanel" frontend/src/ -l

# 检查是否有交互组件注册
grep -r "status_card\|code_block\|progress\|decision\|form\|chart" frontend/src/ -l
```

预期：至少找到 ChatPanel 和 status_card/code_block 的实现文件。

### B.6 场景和快捷操作验证

基于 `config/ui/default.yaml` 和 `config/ui/vscode.yaml`：

**默认场景**：
- 快捷操作: chat, search, memory → 应出现在首页或侧边栏
- 验证: 新用户登录后，检查快捷操作是否可见且可点击

**VSCode 场景**：
- 快捷操作: explain_code, fix_bug, generate_test, search_code → 带 Ctrl+Shift+X 快捷键
- 验证: 如果支持 VSCode 扩展，检查快捷键注册

### B.7 16ms 刷新承诺验证

设计文档要求前端以 `requestAnimationFrame` 实现 16ms 批量刷新：

```bash
# 浏览器 DevTools → Performance → 录制一次消息发送
# 检查：
# 1. 帧率是否保持 60fps（16.67ms/帧）
# 2. WS chunk 到达后是否在 1 帧内更新 DOM
# 3. 长消息渲染是否使用虚拟滚动
```

---

## 第三部分：迭代修复循环

```
执行验证 → 检查日志和数据 → 发现问题 → 修复 → 重新验证
                                        ↑
                                  同一问题失败3次 → 暂停出诊断报告
```

### 快速诊断命令

```bash
# 检查管道配置
python -c "
import sys; sys.path.insert(0, 'src')
from pipeline.config import load_pipeline_config
c = load_pipeline_config('config/pipelines/default.yaml')
print('OK:', c.name, '| routes:', len(c.input_route_table._routes), '/', len(c.output_route_table._routes))
"

# 检查 Agent 注册表
python -c "
import sys; sys.path.insert(0, 'src')
from agents.registry import AgentRegistry
reg = AgentRegistry()
reg.load_directory('config/agents')
for k, v in reg._agents.items():
    print(f'  {k}: level={v.level.value}')
"

# 检查工具注册表
python -c "
import sys; sys.path.insert(0, 'src')
from tools.registry import ToolRegistry
from tools.builtin import register_core_tools
tr = ToolRegistry()
registered = register_core_tools(tr, session=None)
for t in registered:
    print(f'  {t.name}: {t.description[:50]}')
"

# 检查评估指标
python -c "
import sys; sys.path.insert(0, 'src')
from evaluation.loader import MetricLoader
loader = MetricLoader()
loader.load_all()
for mid, m in loader.metrics.items():
    print(f'  {mid}: type={m.metric_type.value}, evaluator={m.evaluator_id}')
"

# 检查端口
python -c "
import socket
for port in [8888, 8765, 5173]:
    s = socket.socket()
    try:
        s.connect(('localhost', port))
        print(f'Port {port}: OPEN')
    except: print(f'Port {port}: CLOSED')
    finally: s.close()
"
```

---

## 第四部分：验收标准

### 任务系统验收（CLI）

| 验收项 | 标准 | 日志检查点 |
|--------|------|-----------|
| 任务创建 | task_submit 返回 task_id，状态 pending | `[TaskSubmit]` `Task created` |
| 事件传递 | TaskWorker 收到并开始执行 | `TaskWorker received task.submitted` |
| 子管道执行 | LLM 调用 + 工具执行成功 | `_run_loop` `ToolCore executing` |
| TaskReminder | 纯文本输出时注入提醒 | `TaskReminder injecting reminder` |
| 路径A评估 | task_evaluate 执行 → completed | `[TaskEvaluate] 自动评估` `state change: evaluating -> completed` |
| 路径B评估 | evaluator_agent → JSON → end | `Agent evaluation` `evaluation_result JSON detected` |
| 结果回传 | 父对话收到通知继续 | `TaskEventReceiver received` `ChildTaskGuard no active children` |
| 错误恢复 | 失败后可重试 | `retry remaining` |

### 前端验收（浏览器）

| 验收项 | 标准 |
|--------|------|
| 登录 | demo 账号登录跳转 |
| WebSocket | 连接建立 + confirmation |
| 消息收发 | 用户消息显示 + AI 流式回复 |
| 工具可视化 | 工具调用过程可见 |
| 会话管理 | 创建/切换/删除正常 |
| 页面布局 | 无崩溃/重叠/溢出 |
| 异常处理 | 断网不白屏 |

---

## 附录：快速启动清单

```bash
# 1. 环境准备
pip install -e ".[dev]"
export MINIMAX_API_KEY="你的key"

# 2. 快速诊断
python -c "import sys; sys.path.insert(0,'src'); from pipeline.config import load_pipeline_config; c=load_pipeline_config('config/pipelines/default.yaml'); print('OK:', c.name)"

# 3. CLI 验证（任务系统）— 带 debug 日志
python run.py real --debug

# 4. Web 验证（双终端）
# 终端1: python start_server.py
# 终端2: cd frontend && npm run dev
# 浏览器: http://localhost:5173

# 5. 任务完成后检查数据
python -c "
import sys; sys.path.insert(0, 'src')
from tasks.service import TaskService
ts = TaskService()
for t in ts.list_tasks():
    s = t.status.value if hasattr(t.status, 'value') else t.status
    print(f'{t.id}: {s} | parent={t.parent_task_id or \"ROOT\"} | {t.title[:40]}')
"
```
