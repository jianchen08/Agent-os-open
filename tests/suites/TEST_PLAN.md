# Agent OS 测试执行计划

## 总览

| 指标   | 数值                                          |
| ---- | ------------------------------------------- |
| 测试总数 | \~1070（1057 已收集 + 13 新增长期任务单元测试）            |
| 收集错误 | 1（test\_task\_evaluate.py 导入错误，需先修复）        |
| 测试目录 | `tests/suites/`（8 个子目录）                     |
| 测试框架 | pytest + pytest-asyncio（asyncio\_mode=auto） |

## 测试规则

1. **集成测试保护**：`@pytest.mark.integration` 的测试默认跳过，需加 `--run-integration` 才执行
2. **异步测试**：`asyncio_mode=auto`，直接写 `async def test_xxx()` 即可
3. **testpaths 配置**：`pyproject.toml` 中配置为 `src/tests`，运行 `tests/suites` 时需显式指定路径
4. **conftest.py**：`tests/suites/conftest.py` 提供全局 fixture
5. **禁止删除/审批命令**：测试过程中禁止执行删除文件、删除目录等需要审批的危险命令（`rm`、`del`、`Remove-Item` 等）。只能用移动（`mv`/`Move-Item`）或复制（`cp`/`Copy-Item`）操作文件

## 执行顺序

```

Phase 1  单元测试 + 修复循环（Agent 1-5 并行，各自跑测试→修复→重跑直到全过）
    │
    ▼
Phase 2  集成测试 + 修复循环（Agent 6-8 并行，各自跑测试→修复→重跑直到全过）
    │
    ▼
Phase 3  端到端测试 + 修复循环（Agent 9-12，按依赖串行 E2E-1→2→3→4）
    │
    ▼
Phase 4  全量验证（确认无回归）
    │
    ▼
Phase 5  输出测试报告（tests/suites/TEST_REPORT.md）
```

***

<br />

***

## Phase 1：单元测试（5 个 Agent 并行）

不依赖 LLM，Mock 掉外部依赖，验证单个模块的逻辑正确性。

### Agent-1：core（522 个测试）

**路径**：`tests/suites/core/`

**测试范围**：

- `test_chain.py` — PluginChain 逻辑
- `test_engine.py` — PipelineEngine 执行
- `test_event_bus.py` — 事件总线
- `test_config_store.py` / `test_config_reload.py` — 配置系统
- `test_db.py` — 数据库连接
- `test_tasks.py` — 任务服务
- `test_tool_core.py` — 工具核心
- `test_templates.py` — 模板系统
- `test_scheduler.py` — 调度器
- `test_route.py` — 路由
- `test_triggers.py` — 触发器
- `test_websocket.py` — WebSocket
- `test_agent_config.py` — Agent 配置
- `test_concurrency.py` — 并发控制
- `test_evaluation.py` — 评估引擎
- `test_execution_record_storage.py` — 执行记录
- `test_pipeline_integration.py` / `test_pipeline_registry_v2.py` — 管道集成

**执行命令**：

```powershell
python -m pytest tests/suites/core -v --tb=short 2>&1
```

### Agent-2：stage（186 个测试）

**路径**：`tests/suites/stage/`

**测试范围**：

- `test_plugins.py` — 插件集成（ContextBuild / ErrorCheck / ResultFormat）
- `test_stage5_e2e.py` — Stage 5 端到端
- `test_guard_plugins.py` — 守卫插件
- `test_delegation_plugins.py` — 委派插件
- `test_real_acceptance.py` — 真实验收测试
- `test_stage4_blockers.py` — Stage 4 阻塞项
- `test_security_guard_integration.py` — 安全守卫集成

**执行命令**：

```powershell
python -m pytest tests/suites/stage -v --tb=short 2>&1
```

### Agent-3：m6\_plugins + memory（200 个测试）

**路径**：`tests/suites/m6_plugins/` + `tests/suites/memory/`

**测试范围**：

- `test_m6a_input_plugins.py` — 输入插件
- `test_m6b_input_plugins.py` — 输入插件
- `test_m6c_output_plugins.py` — 输出插件
- `test_m6d_output_plugins.py` — 输出插件
- `test_experience_bridge.py` / `test_experience_applier.py` / `test_experience_consolidator.py` — 经验系统
- `test_json_store.py` — JSON 存储
- `test_infrastructure.py` — 基础设施
- `test_m13_long_term_migration.py` — 长期任务迁移

**执行命令**：

```powershell
python -m pytest tests/suites/m6_plugins tests/suites/memory -v --tb=short 2>&1
```

### Agent-4：llm + agent + task（\~174 个测试）

**路径**：`tests/suites/llm/` + `tests/suites/agent/` + `tests/suites/task/`

**测试范围**：

- `test_llm_core.py` — LLM 核心（Mock LLM）
- `test_prompt_assembly.py` — Prompt 组装
- `test_agent_create.py` — Agent 创建
- `test_agent_self_creation.py` — Agent 自创建
- `test_task_closed_loop.py` — 任务闭环
- `test_simple_loop.py` / `test_full_loop.py` / `test_full_task_loop.py` — 管道循环
- `test_task_manage.py` — 任务管理操作
- **`test_long_term_container.py`** — 长期任务容器（13 个测试，新增）

**执行命令**：

```powershell
python -m pytest tests/suites/llm tests/suites/agent tests/suites/task -v --tb=short 2>&1
```

### Agent-5：cli（31 个测试）

**路径**：`tests/suites/cli/`

**测试范围**：

- `test_cli_integration.py` — CLI 集成
- `test_cli_via_stdin.py` — stdin 交互
- `cli_loop_test.py` — CLI 循环
- `test_closed_loop.py` — 闭环测试
- `run_test.py` — 运行测试

**执行命令**：

```powershell
python -m pytest tests/suites/cli -v --tb=short 2>&1
```

***

## Phase 2：集成测试 + 修复循环（需 --run-integration）

依赖真实 LLM，不 Mock 核心组件，验证多模块协作。每个 Agent 跑测试→修复→重跑直到全过。

### Agent-6：llm + agent 集成（\~6 个测试）

**路径**：`tests/suites/llm/` + `tests/suites/agent/`

**测试范围**：

- `test_integration_llm.py` — 真实 LLM 调用
- `test_real_assembly.py` / `test_real_mode.py` — 真实 Prompt 组装
- `test_agent_self_creation_e2e.py` — Agent 自创建 E2E
- `test_create_agent_closed_loop.py` — 创建 Agent 闭环

**执行命令**：

```powershell
python -m pytest tests/suites/llm tests/suites/agent -v --run-integration --tb=short 2>&1
```

### Agent-7：task 集成（\~2 个测试）

**路径**：`tests/suites/task/`

**测试范围**：

- `test_task_submit_real.py` — 真实任务提交
- `test_e2e_task_submit.py` — E2E 任务提交

**执行命令**：

```powershell
python -m pytest tests/suites/task -v --run-integration --tb=short 2>&1
```

### Agent-8：cli 集成（\~4 个测试）

**路径**：`tests/suites/cli/`

**测试范围**：

- `config_cli_test.py` — 配置 CLI
- `real_cli_test.py` — 真实 CLI
- `real_cli_e2e_test.py` — 真实 CLI E2E
- `real_e2e_full_test.py` — 完整 E2E

**执行命令**：

```powershell
python -m pytest tests/suites/cli -v --run-integration --tb=short 2>&1
```

***

## Phase 3：端到端测试 + 修复循环（按依赖递进）

按复杂度和依赖关系从简到难，每个 Agent 跑测试→修复→重跑直到全过。后面的测试依赖前面的基础能力。

### E2E-1：短期任务单步执行（Agent 9）

**前置依赖**：无（基础能力验证）

**验证目标**：灵汐能接收用户消息、创建短期任务、TaskWorker 拾取执行、返回结果。

**发送消息给灵汐**：

```
帮我写一个 hello.py，内容是 print("Hello World")。
```

**验证**：

- 灵汐创建了短期任务（task\_scope != long\_term）
- TaskWorker 拾取并执行
- workspace 下生成 hello.py
- 文件内容为 `print("Hello World")`

**清理**：

```powershell
Move-Item -Path .ai_workspaces/* -Destination .ai_workspaces/_cleanup
```

***

### E2E-2：长期任务容器创建（Agent 10）

**前置依赖**：E2E-1 通过（灵汐基本任务能力正常）

**验证目标**：灵汐能判断长期任务、创建容器、容器不被 Worker 执行。

**发送消息给灵汐**：

```
这是一个长期任务：写一个猜数字小游戏。先用 task_submit 创建长期任务容器，参数：
- task_scope: "long_term"
- goal.title: "写一个猜数字小游戏"

创建完后告诉我容器的 task_id 和 status。
```

**验证**：

- 容器 task\_scope = long\_term
- 容器 status = pending（Worker 没有执行它）
- 返回了 task\_id

**清理**：

```powershell
Move-Item -Path .ai_workspaces/* -Destination .ai_workspaces/_cleanup
```

***

### E2E-3：方案准备子任务执行（Agent 11）

**前置依赖**：E2E-2 通过（容器创建正常）

**验证目标**：容器创建后能挂载方案准备子任务、solution\_preparation\_agent 执行并产出 docs/solution.md。

**发送消息给灵汐**：

```
这是一个长期任务：写一个猜数字小游戏。请按以下步骤完成：

第1步：用 task_submit 创建长期任务容器，参数：
- task_scope: "long_term"
- goal.title: "写一个猜数字小游戏"

第2步：容器创建后，用 task_submit 提交方案准备子任务，参数：
- target_type: "agent"
- target_id: "solution_preparation_agent"
- parent_task_id: 第1步返回的容器task_id
- goal.title: "方案准备"
- goal.description: "写一个简单的猜数字小游戏，程序随机生成1-100的数字，玩家输入猜测，程序提示大了或小了，猜中后显示尝试次数。跳过与用户讨论阶段，直接生成方案，写入 docs/solution.md。"
- acceptance_criteria: file_check(path=docs/solution.md) + format_valid(path=docs/solution.md, type=markdown)

方案准备阶段不要调用 human_interaction，直接调研并生成方案。完成后告诉我容器进度和子任务状态。
```

**验证**：

- 容器下有 1 个子任务
- 子任务 status = completed
- 容器进度 = 100%（只有 1 个子任务，完成即 100%）
- workspace 下 docs/solution.md 存在且为 markdown 格式

**清理**：

```powershell
Move-Item -Path .ai_workspaces/* -Destination .ai_workspaces/_cleanup
```

***

### E2E-4：方案准备 + 方案细化完整链路（Agent 12）

**前置依赖**：E2E-3 通过（方案准备子任务执行正常）

**验证目标**：方案准备完成后灵汐能自动提交方案细化子任务、solution\_refinement\_agent 读方案并产出 docs/task\_plan.md。

**发送消息给灵汐**：

```
这是一个长期任务：写一个猜数字小游戏。请按以下步骤完成整个流程：

第1步：用 task_submit 创建长期任务容器，参数：
- task_scope: "long_term"
- goal.title: "写一个猜数字小游戏"

第2步：容器创建后，用 task_submit 提交方案准备子任务，参数：
- target_type: "agent"
- target_id: "solution_preparation_agent"
- parent_task_id: 第1步返回的容器task_id
- goal.title: "方案准备"
- goal.description: "写一个简单的猜数字小游戏，程序随机生成1-100的数字，玩家输入猜测，程序提示大了或小了，猜中后显示尝试次数。跳过与用户讨论阶段，直接生成方案，写入 docs/solution.md。"
- acceptance_criteria: file_check(path=docs/solution.md) + format_valid(path=docs/solution.md, type=markdown)

第3步：方案准备子任务完成后，用 task_submit 提交方案细化子任务，参数：
- target_type: "agent"
- target_id: "solution_refinement_agent"
- parent_task_id: 同一个容器task_id
- goal.title: "方案细化"
- goal.description: "读取 docs/solution.md，细化为可执行任务计划，输出 docs/task_plan.md。"
- acceptance_criteria: file_check(path=docs/task_plan.md) + format_valid(path=docs/task_plan.md, type=markdown)

方案准备阶段不要调用 human_interaction，直接调研并生成方案。全部完成后告诉我容器进度和两个子任务的状态。
```

**验证**：

| 验证项                | 预期结果             |
| ------------------ | ---------------- |
| 容器 task\_scope     | long\_term       |
| 容器下子任务数量           | 2 个（方案准备 + 方案细化） |
| 方案准备子任务 status     | completed        |
| 方案细化子任务 status     | completed        |
| 容器进度               | 100%             |
| docs/solution.md   | 存在且为 markdown 格式 |
| docs/task\_plan.md | 存在且为 markdown 格式 |

**如果灵汐卡在 human\_interaction**，测试 Agent 发送：

```
方案看起来不错，继续下一步吧。
```

**清理**：

```powershell
Move-Item -Path .ai_workspaces/* -Destination .ai_workspaces/_cleanup
```

### 失败处理（所有 E2E 通用）

**原则：不改 Agent prompt，改测试发送的消息或改源码。**

| 失败现象                             | 判断                   | 操作                                   |
| -------------------------------- | -------------------- | ------------------------------------ |
| 灵汐没判断为长期任务                       | 测试消息没触发长期任务判断        | 改测试消息措辞                              |
| EventBus 没发布事件                   | 测到了功能，事件发布有 bug      | 改源码                                  |
| TaskWorker 没拾取子任务                | 测到了功能，Worker 逻辑有 bug | 改源码                                  |
| 方案准备 Agent 卡在 human\_interaction | 测试消息没让 Agent 跳过讨论    | 改测试消息措辞                              |
| Agent 没产出 docs/solution.md       | Agent 执行异常           | 查看执行日志，改测试消息的 goal.description       |
| task\_evaluate 没通过               | 评估逻辑问题               | 改源码（评估器）或改测试消息的 acceptance\_criteria |
| 进度计算不对                           | 测到了功能，计算逻辑有 bug      | 改源码                                  |

***

## Phase 4：全量验证

所有阶段通过后，执行全量测试确认无回归：

```powershell
python -m pytest tests/suites -v --tb=short 2>&1
```

**通过标准**：

- 0 errors
- 0 failures
- 收集测试数 >= 1070

***

## Phase 5：输出测试报告

全部测试完成后，生成报告文件 `tests/suites/TEST_REPORT.md`。

### 报告模板

```markdown
# Agent OS 测试报告

## 基本信息

| 指标 | 数值 |
|------|------|
| 测试总数 | xxx |
| 通过 | xxx |
| 失败 | xxx |
| 错误 | xxx |
| 跳过 | xxx |
| 通过率 | xx% |

## Phase 1：单元测试

### Agent-1：core（522 个测试）
- **结果**：通过 / 失败 x 个
- **失败用例**：
  | 文件 | 用例 | 失败原因 | 修复方式 | 改了源码还是测试 |
  |------|------|---------|---------|----------------|
  | xxx.py | test_xxx | AssertionError: ... | 修复了 xxx 函数 | 源码 |
  | xxx.py | test_xxx | ImportError: ... | 删除了对已移除函数的引用 | 测试 |

### Agent-2：stage（186 个测试）
- 同上格式

### Agent-3：m6_plugins + memory（200 个测试）
- 同上格式

### Agent-4：llm + agent + task（~174 个测试）
- 同上格式

### Agent-5：cli（31 个测试）
- 同上格式

## Phase 2：集成测试

### Agent-6：llm + agent 集成
- 同上格式

### Agent-7：task 集成
- 同上格式

### Agent-8：cli 集成
- 同上格式

## Phase 3：端到端测试

### E2E-1：短期任务单步执行
- **结果**：通过 / 失败
- **失败原因**：（如有）
- **修复方式**：（如有）

### E2E-2：长期任务容器创建
- 同上格式

### E2E-3：方案准备子任务执行
- 同上格式

### E2E-4：方案准备 + 方案细化完整链路
- 同上格式

## 遇到的问题与解决方式

记录测试过程中发现的问题，特别是 prompt/上下文/指令相关的问题（不改 prompt，通过调整指令解决）：

| # | 问题描述 | 问题类型 | 解决方式 | 是否改了 prompt |
|---|---------|---------|---------|----------------|
| 1 | xxx | 源码 bug | 修复了 xxx | 否 |
| 2 | xxx | 测试过时 | 更新断言适配新接口 | 否 |
| 3 | xxx | Agent 行为不符预期 | 在测试消息中补充了 xxx 指令引导 Agent | 否（通过指令解决） |
| 4 | xxx | 上下文变量缺失 | 在测试消息中显式指定了 xxx | 否（通过指令解决） |
| 5 | xxx | prompt 触发条件没满足 | 调整了测试消息措辞以触发正确的 prompt 分支 | 否 |

## 修复汇总

| 类别 | 数量 |
|------|------|
| 改源码修复 | x 处 |
| 改测试修复 | x 处 |
| 改测试消息修复 | x 处 |
| 未修复（遗留问题） | x 处 |

## 遗留问题

| # | 问题描述 | 影响范围 | 建议处理方式 |
|---|---------|---------|-------------|
| 1 | xxx | xxx | xxx |
```

***

## 长期任务单元测试矩阵

测试文件：`tests/suites/task/test_long_term_container.py`（Phase 1 Agent-4 执行）

| #  | 类别        | 用例                   | 验证点                                  |
| -- | --------- | -------------------- | ------------------------------------ |
| 1  | 容器创建      | 创建只需 goal.title      | 不需要 target\_type/target\_id/AC       |
| 2  | 容器创建      | 不能有 parent\_task\_id | `_validate_parent_task_id` 返回 False  |
| 3  | 容器创建      | 只能 L1 提交             | L2 提交返回 `L2_CANNOT_SUBMIT_LONG_TERM` |
| 4  | 容器创建      | 默认 short\_term       | 不指定 task\_scope 时要求 target\_type     |
| 5  | 子任务挂载     | 方案准备挂载               | parent\_task\_id 正确关联                |
| 6  | 子任务挂载     | 方案细化挂载               | 容器下列出 2 个子任务                         |
| 7  | 子任务挂载     | 多子任务列表               | list\_subtasks 返回正确数量                |
| 8  | 进度追踪      | 子任务完成→进度更新           | 0% → 50% → 100%                      |
| 9  | 进度追踪      | 部分失败→部分进度            | 50% + 错误信息可查                         |
| 10 | 进度追踪      | 无子任务→0%              | 空容器进度为 0                             |
| 11 | Worker 跳过 | 执行时跳过                | 长期任务状态不被 Worker 改变                   |
| 12 | Worker 跳过 | 恢复时跳过                | 长期任务不被 reset\_to\_pending            |
| 13 | 完整生命周期    | 端到端全流程               | 创建→挂载→完成→进度100%                      |

