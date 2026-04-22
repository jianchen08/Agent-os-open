# Agent OS 稳定性 & 长期任务测试设计

> 版本: 1.1 | 日期: 2026-04-19 | 覆盖: 评估引擎稳定性、工作空间隔离、管道并发、长期任务生命周期

***

## 一、测试目标

### 1.1 核心验证点

| 验证维度       | 具体目标                                                         | 对应模块                                           |
| ---------- | ------------------------------------------------------------ | ---------------------------------------------- |
| **评估闭环**   | evaluator\_agent 真实调用 LLM + 工具验证，输出 `evaluation_result` JSON | `evaluation/engine.py`, `evaluator_agent.yaml` |
| **评估重试**   | 评估未通过 → 反馈给执行 Agent → 改进后重评估 → 直到通过或重试耗尽                     | `task_evaluate.py`, `executor.py`              |
| **错误暴露**   | 管道异常/配置缺失/Agent 不存在 → 直接抛异常，不掩盖问题                            | `engine.py._evaluate_agent()`                  |
| **工作空间隔离** | 短期任务文件保存在 `.ai_workspaces/{task_id}/`，不污染项目根目录               | `task_worker.py`, `param_inject.py`            |
| **管道并发**   | 子管道在 ThreadPoolExecutor 中运行，不阻塞主 event loop                  | `engine.py`, `input_adapter.py`                |
| **长期任务**   | 容器创建 → 方案准备 → 子任务委派 → 跨执行持久化                                 | `task_submit.py`, `task_worker.py`             |

### 1.2 不测试什么

- LLM 本身的生成质量（不可控）
- 网络超时/断连（基础设施问题）
- 第三方 API 的可用性（MiniMax、搜索服务等、

***

## 二、测试分层架构

```
┌──────────────────────────────────────────────────────────────┐
│  L4: E2E 集成测试  @pytest.mark.integration                  │
│  真实 LLM + 真实文件系统 + 真实管道                             │
│  验证完整业务闭环（调研→评估→重试→通过）                         │
│  运行: pytest tests/suites/ --run-integration -m e2e          │
├──────────────────────────────────────────────────────────────┤
│  L3: 闭环测试  Mock LLM + 真实管道                            │
│  模拟各种 LLM 响应（通过/失败/格式错误/超时）                    │
│  验证状态流转和重试逻辑                                        │
├──────────────────────────────────────────────────────────────┤
│  L2: 组件集成测试  Mock + 部分真实                             │
│  EvaluationEngine + PipelineEngine 集成                       │
│  TaskWorker + 子管道集成                                      │
│  param_inject workspace 注入集成                               │
├──────────────────────────────────────────────────────────────┤
│  L1: 单元测试  全 Mock                                        │
│  _parse_evaluation_result 解析                                │
│  _build_agent_eval_prompt 构建                                │
│  workspace 默认值计算                                         │
│  结果映射和状态回写                                            │
└──────────────────────────────────────────────────────────────┘
```

***

## 三、Mock 策略

### 3.1 Mock 层级

```
L1 单元测试:
  MockLLM          → 返回预定义文本（含/不含 evaluation_result JSON）
  MockAgentRegistry → 返回 evaluator_agent 配置字典
  MockTaskService   → 记录 complete_evaluation/fail_task 调用
  MockPipelineFactory → 返回 Mock PipelineEngine

L2 组件集成:
  MockLLM          → 模拟不同响应模式
  真实 PipelineEngine → 用 Mock 路由表和插件
  真实 EvaluationEngine → 注入 Mock 依赖

L3 闭环测试:
  MockLLM          → 按调用次数返回不同响应（第一次 failed，第二次 passed）
  真实 PipelineEngine
  真实 EvaluationExecutor
  MockTaskService   → 验证状态回写

L4 E2E 集成:
  真实 LLM (MiniMax-M2.7)
  真实 PipelineEngine
  真实 AgentRegistry
  真实文件系统（tmp_path fixture）
```

### 3.2 MockLLM 设计

````python
class MockLLMResponse:
    """模拟 LLM 的各种响应模式"""

    @staticmethod
    def eval_passed(score=95.0, feedback="评估通过"):
        return (
            "## 评估完成\n\n"
            "```json\n"
            f'{{"evaluation_result": {{"passed": true, "score": {score}, '
            f'"feedback": "{feedback}"}}}}\n'
            "```\n"
        )

    @staticmethod
    def eval_failed(score=40.0, feedback="报告缺少核心概念"):
        return (
            "## 评估完成\n\n"
            "```json\n"
            f'{{"evaluation_result": {{"passed": false, "score": {score}, '
            f'"feedback": "{feedback}"}}}}\n'
            "```\n"
        )

    @staticmethod
    def eval_no_json():
        """模拟 LLM 没有输出 JSON 格式的结论"""
        return "报告质量不错，包含了 async/await 和事件循环的概念。建议补充更多代码示例。"

    @staticmethod
    def eval_malformed_json():
        """模拟 LLM 输出格式错误的 JSON"""
        return '{"evaluation_result": {"passed": true, "score": 95'  # 缺少闭合括号

    @staticmethod
    def sequence(*responses):
        """按调用顺序返回不同响应"""
        call_count = 0
        def next_response():
            nonlocal call_count
            resp = responses[min(call_count, len(responses) - 1)]
            call_count += 1
            return resp
        return next_response
````

***

## 四、测试套件详细设计

### 4.1 套件 A：评估引擎稳定性

**文件**: `tests/suites/core/test_evaluation_stability.py`
**标记**: `@pytest.mark.core` + `@pytest.mark.unit`
**数量**: 12 个测试

#### A1. 评估结果解析（L1 单元）

````
test_parse_nested_evaluation_result
  输入: '{"evaluation_result": {"passed": true, "score": 95, "feedback": "OK"}}'
  期望: {passed: True, score: 95.0, feedback: "OK"}

test_parse_direct_evaluation_result
  输入: '{"passed": false, "score": 40, "feedback": "缺少概念"}'
  期望: {passed: False, score: 40.0, feedback: "缺少概念"}

test_parse_evaluation_result_with_surrounding_text
  输入: "评估完成！\n```json\n{...}\n```\n以上是结论"
  期望: 正确从文本中提取 JSON

test_parse_evaluation_result_empty_input
  输入: ""
  期望: None

test_parse_evaluation_result_no_json
  输入: "报告质量不错，建议补充代码示例。"
  期望: None

test_parse_evaluation_result_malformed_json
  输入: '{"evaluation_result": {"passed": true'
  期望: None
````

#### A2. 评估 Prompt 构建（L1 单元）

```
test_build_prompt_with_all_fields
  params = {criteria: "...", content: "...", summary: "..."}
  期望: prompt 包含"评估标准"、"待评估内容"、"任务执行摘要"三个段落

test_build_prompt_criteria_from_task_desc
  params = {criteria: "报告包含 async/await 核心概念"}  # 从任务描述自动填充
  期望: prompt 包含具体的评估标准

test_build_prompt_empty_params
  params = {}  # 所有字段为空
  期望: prompt 仍包含基本的评估指令和 JSON 输出格式要求
```

#### A3. Agent 查找（L1 单元）

```
test_find_agent_by_config_id
  registry.get("system_evaluator_agent") → 返回配置
  期望: 正确找到

test_find_agent_by_name_fallback
  registry.get("evaluator_agent") → None
  遍历 registry.list_all() 找 name="evaluator_agent"
  期望: 通过 name 匹配找到

test_find_agent_not_found
  evaluator_id = "nonexistent_agent"
  期望: 抛出 RuntimeError("Agent 'nonexistent_agent' not found in registry")
```

#### A4. 评估前置条件校验（L2 集成）

```
test_evaluate_agent_pipeline_factory_none_raises
  pipeline_factory=None, agent_registry=MockRegistry
  期望: 抛出 RuntimeError("Agent evaluation requires pipeline_factory but it is None")

test_evaluate_agent_registry_none_raises
  pipeline_factory=MockFactory, agent_registry=None
  期望: 抛出 RuntimeError("Agent evaluation requires agent_registry but it is None")

test_evaluate_agent_not_found_raises
  evaluator_id = "nonexistent_agent"
  期望: 抛出 RuntimeError("Agent 'nonexistent_agent' not found in registry")

test_evaluate_tool_not_in_registry_raises
  tool_registry 中没有 evaluator_id 对应的工具
  期望: 抛出 RuntimeError("Tool 'xxx' not found in registry")

test_evaluate_tool_registry_none_raises
  tool_registry=None
  期望: 抛出 RuntimeError("Tool evaluation requires tool_registry but it is None")
```

***

### 4.2 套件 B：评估重试闭环

**文件**: `tests/suites/task/test_evaluation_retry.py`
**标记**: `@pytest.mark.task` + `@pytest.mark.unit`
**数量**: 6 个测试

#### B1. 重试循环（L3 闭环）

```
test_retry_then_pass
  Mock: 第一次评估 failed(score=40) → 返回 retry
  Agent 收到反馈后"改进"
  Mock: 第二次评估 passed(score=95)
  期望: 任务最终 status=completed, result.overall_passed=true

test_retry_exhausted
  Mock: 连续 3 次评估 failed(score=30/40/45)
  期望: 任务 status=failed, eval_retry_count={semantic_check: 3}

test_retry_feedback_contains_details
  Mock: 第一次评估 failed
  验证: retry 返回的 message 包含 "[semantic_check] 未通过" 和具体 feedback

test_skip_state_update_on_retry
  Mock: 第一次评估 failed → retry
  验证: executor 的 complete_evaluation 未被调用（skip_state_update=True）
  验证: 只有 task_evaluate 自己管理状态
```

#### B2. criteria 自动填充（L2 集成）

```
test_criteria_auto_filled_from_task_description
  task.metadata.acceptance_criteria.semantic_check.input_params = {}
  task.description = "写一份关于 Python 异步编程的报告"
  期望: params["semantic_check"]["criteria"] == "写一份关于 Python 异步编程的报告"

test_criteria_not_overwritten_if_provided
  task.metadata.acceptance_criteria.semantic_check.input_params = {criteria: "自定义标准"}
  期望: params["semantic_check"]["criteria"] == "自定义标准"（不被覆盖）
```

***

### 4.3 套件 C：工作空间稳定性

**文件**: `tests/suites/task/test_workspace_stability.py`
**标记**: `@pytest.mark.task` + `@pytest.mark.unit`
**数量**: 5 个测试

```
test_default_workspace_is_task_id_based
  task_submit 不传 workspace
  期望: TaskWorker 计算 workspace = f".ai_workspaces/{task_id}"

test_workspace_injected_to_file_tools
  state["workspace"] = ".ai_workspaces/abc123"
  file_write 被调用时
  期望: inputs["workspace"] == ".ai_workspaces/abc123"

test_two_tasks_isolated
  task_a workspace = ".ai_workspaces/aaa/"
  task_b workspace = ".ai_workspaces/bbb/"
  task_a 创建 file_a.txt, task_b 创建 file_b.txt
  期望: file_a.txt 只在 aaa/ 目录，file_b.txt 只在 bbb/ 目录

test_custom_workspace_preserved
  task_submit 传 workspace = "src/output"
  期望: TaskWorker 使用 "src/output"，不覆盖为 .ai_workspaces/

test_workspace_empty_string_uses_default
  workspace = ""
  期望: 自动设为 f".ai_workspaces/{task_id}"
```

***

### 4.4 套件 D：管道执行稳定性

**文件**: `tests/suites/core/test_pipeline_stability.py`
**标记**: `@pytest.mark.core` + `@pytest.mark.unit`
**数量**: 6 个测试

```
test_sub_pipeline_in_thread_pool_executor
  在已有 running event loop 中调用 _evaluate_agent
  验证: 使用 ThreadPoolExecutor 而非嵌套 asyncio.run()

test_max_iterations_enforced
  evaluator_agent 配置 max_iterations=15
  MockLLM 永远不输出 evaluation_result JSON
  期望: 管道在 15 次迭代后强制终止

test_evaluator_agent_config_loaded
  从 config/agents/system/evaluator_agent.yaml 加载配置
  验证: config.max_iterations == 15
  验证: config.plugins.enabled 包含 task_reminder
  验证: task_reminder 配置中 evaluation_mode == true

test_evaluator_agent_tool_ids_available
  验证 evaluator_agent 的 tool_ids（file_read, bash_execute, enhanced_search, evaluate）
  在工具注册表中均存在
  期望: 全部可找到，无缺失

test_event_loop_not_blocked_by_input_adapter
  input_adapter.receive() 使用 run_in_executor
  模拟 _read_multiline() 阻塞 2 秒
  验证: 期间其他 async task 可以执行

test_pipeline_state_passed_through
  engine.run(user_input=..., agent_config=..., task_id="__eval__test")
  验证: state["task_id"] == "__eval__test"
  验证: state["workspace"] 被正确传递
```

***

### 4.5 套件 E：长期任务执行

**文件**: `tests/suites/task/test_long_term_stability.py`
**标记**: `@pytest.mark.task` + `@pytest.mark.unit`
**数量**: 5 个测试

```
test_long_term_task_creates_container
  L1 Agent 提交 task_scope=long_term 的任务
  期望: 创建容器目录（.ai_workspaces/{task_id}/）
  期望: TaskWorker 不直接执行容器任务

test_solution_preparation_subtask_mounted
  ⚠️ 前置: 需确认 solution_preparation_agent 子任务自动挂载机制已实现
  容器创建后
  期望: 自动挂载 solution_preparation 子任务
  期望: 子任务的 parent_task_id 指向容器

test_long_term_workspace_persistence
  长期任务第一次执行 → 创建 solution.md
  长期任务第二次执行 → solution.md 仍存在
  期望: 工作空间跨执行持久化

test_long_term_to_short_term_delegation
  ⚠️ 前置: 需确认子任务结果回传到父任务的完整链路已实现
  长期任务 → 创建短期子任务
  TaskWorker 拾取子任务 → 执行 → 返回结果
  期望: 子任务结果回传到父任务

test_task_dependency_chain_execution_order
  ⚠️ 前置: 需确认依赖排序执行逻辑已实现
  task_a → task_b → task_c（B 依赖 A，C 依赖 B）
  期望: 执行顺序 A → B → C
  期望: 前置任务失败时后续任务不执行
```

***

### 4.6 套件 F：E2E 集成测试

**文件**: `tests/suites/cli/test_e2e_stability.py`
**标记**: `@pytest.mark.integration` + `@pytest.mark.e2e`
**前置**: `--run-integration` 参数 + CLI 测试基础设施（CLITestHarness）
**数量**: 4 个测试

> ⚠️ **基础设施依赖**: F1-F4 需要标准化的 CLI-in-test 启动方式（CLITestHarness），
> 能够以编程方式启动 CLI、发送消息、等待结果。需在编写测试前先实现此基础设施。

#### F1. 短期任务 — 高难度评估（大概率触发重试）

```
test_real_short_term_with_retry
  步骤:
    1. CLI 启动（-m 模式）
    2. 发送: "提交调研任务：让 research_agent 调研 Python GIL 机制
       （保存到 gil_report.md），验收标准使用 semantic_check 语义评估，
       评估要求是报告必须包含 CPython 源码级分析、至少 5 个性能基准测试数据、
       与 Rust/Go 并发模型对比表格。提交任务不要自己做"
    3. 等待: 任务提交 → research_agent 执行 → task_evaluate 评估
    4. 验证:
       - .ai_workspaces/{task_id}/gil_report.md 存在
       - 任务终态为 completed 或 failed
       - 如果 completed: result.overall_passed == True
       - 如果 failed: eval_retry_count 中至少有 1 次重试记录
  超时: 600 秒
  预期: 大概率触发 1-2 次重试（标准极高）
```

#### F2. 短期任务 — 必败评估（验证重试耗尽）

```
test_real_short_term_retry_exhausted
  步骤:
    1. CLI 启动（-m 模式）
    2. 发送: "提交调研任务：让 research_agent 写一份关于量子计算的调研报告
       （保存到 quantum.md），验收标准使用 semantic_check 语义评估，
       评估要求是报告必须包含 20 个以上可运行的 Python 代码示例、
       完整的量子门操作 API 文档、以及与 IBM Qiskit 的性能对比数据。
       提交任务不要自己做"
    3. 等待: 任务提交 → research_agent 执行 → task_evaluate 评估 → 重试
    4. 验证:
       - 任务终态为 failed
       - eval_retry_count[semantic_check] == 3（重试耗尽）
       - result.overall_passed == False
       - result 中包含失败原因
  超时: 600 秒
  预期: 重试 3 次后仍然失败（标准不可能达到）
```

#### F3. 短期任务 — 正常通过（验证基本流程）

```
test_real_short_term_pass
  步骤:
    1. CLI 启动（-m 模式）
    2. 发送: "提交调研任务：让 research_agent 写一份关于 Python 列表推导式
       的简短教程（保存到 tutorial.md），验收标准使用 semantic_check 语义评估，
       评估要求是教程包含列表推导式的基本语法和至少 3 个实用示例。
       提交任务不要自己做"
    3. 等待: 任务提交 → research_agent 执行 → task_evaluate 评估
    4. 验证:
       - .ai_workspaces/{task_id}/tutorial.md 存在
       - 任务 status == completed
       - result.overall_passed == True
       - result.metrics 包含 semantic_check (score >= 80)
       - result 字段非 null（正确回写）
  超时: 600 秒
  预期: 一次通过或重试 1 次后通过（标准合理）
```

#### F4. 长期任务 — 真实人类交互 + 完整闭环验证

```
test_real_long_term_with_real_interaction
  前置:
    - CLIProcess 进程级测试（启动真实 CLI 子进程）
    - 测试通过 stdin/stdout 与 CLI 真实交互
    - CLI 退出后读取持久化文件做断言

  人类交互点（共 2 处）:
    交互点 A: 方案准备阶段 — solution_preparation_agent 发起 conversation
              与用户讨论方案细节，用户回复确认
    交互点 B: 方案评估阶段 — human_review 评估指标触发 choice 模式
              要求用户评审方案质量，用户选择通过

  步骤:
    1. CLI 启动（子进程）
    2. 发送: "提交长期任务：用 HTML+CSS+JS 开发一个贪吃蛇游戏，
       并加入 Roguelike 元素增强可玩性：随机道具（加速/减速/穿墙）、
       随机障碍物生成、多关卡递增难度、分数和生命值系统。
       项目名称：rogue_snake。提交任务不要自己做"
    3. 等待交互点 A（方案讨论）:
       - stdout 匹配 "方案" 或 "讨论" 或 "conversation" 或 "interaction"
       - 检测到 solution_preparation_agent 发起的 conversation 面板
    4. 【真实交互 A】通过 stdin 发送方案反馈:
       - "方案确认通过，请继续执行"
       - 如果进入 conversation 模式，先 /back 再继续
    5. 等待交互点 B（人类评审）:
       - stdout 匹配 "评审" 或 "review" 或 "确认" 或 "选项"
       - 检测到 human_review 评估触发的 choice 面板
    6. 【真实交互 B】通过 stdin 发送评审结果:
       - 发送 "1"（选择通过/确认）
    7. 等待方案准备完成:
       - stdout 匹配 "方案准备.*完成" 或 "completed" 或 "方案细化"
    8. 等待方案细化 + 执行子任务全部完成:
       - 轮询等待，最长 900 秒
       - 期间如果再次出现交互面板，发送确认回复
    9. 如需触发容器完成，发送后续消息:
       - "上面容器的所有子任务已完成，请立即调用 task_manage complete_container 完成容器"
    10. 等待容器 COMPLETED 或超时
    11. 发送 /exit，等待 CLI 退出

  活跃度监控（贯穿步骤 3-10，每 30 秒检查一次）:
    监控机制: 启动后台协程，每隔 30 秒采样一次 stdout，判断系统是否活跃
    活跃判定（满足任一即视为活跃）:
      - stdout 有新增行（LLM 正在输出或工具有新结果）
      - stdout 包含 "thinking" / "工具调用" / "task_submit" / "running"
        等关键字（表示 Agent 或工具正在工作）
      - 检测到交互面板（等待人类输入，属于正常等待）
    异常检测:
      - 连续 3 次（90 秒）无新增输出 → 记录警告日志
      - 连续 5 次（150 秒）无新增输出 → 判定卡死，主动发送唤醒消息:
        "请报告当前进度，是否有阻塞或错误？"
      - 连续 7 次（210 秒）仍无响应 → 判定死锁/循环，发送 /exit 终止测试
      - stdout 出现同一内容重复 >= 5 次 → 判定循环，记录并终止
    监控日志文件（实时写入，每 30 秒采样后立即落盘，不缓存）:
      1. test_f4_monitor.jsonl
         - 每行一条 JSON，含 timestamp/new_lines/status/detail
         - 含所有采样记录、异常检测触发记录、唤醒/终止决策记录
      2. test_f4_stdout.log
         - 完整 CLI stdout/stderr 输出，保留 ANSI 转义便于排查
      3. test_f4_stdout_clean.log
         - 去除 ANSI 的纯文本，便于文本搜索
      4. test_f4_panels.log
         - 每次检测到交互面板时，截取面板前后 20 行 stdout
         - 含面板类型（conversation/choice）、标题、选项内容、测试脚本发送的回复
      5. test_f4_records/
         - 执行记录：复制 data/pipelines/{pipeline_run_id}.yaml
         - 按子任务分组整理（每个子任务对应的 pipeline_run_id 单独一份）

  验证（CLI 退出后，读取持久化文件）:

    V1. 容器闭环:
        - data/tasks/ 下找到容器任务，status == COMPLETED
        - 容器 metadata.task_scope == "long_term"

    V2. 一级子任务 >= 4 个（不含二次嵌套）:
        - 方案准备子任务 (target=solution_preparation_agent)
        - 方案细化子任务 (target=solution_refinement_agent)
        - 至少 1 个执行子任务
        - 最终评估/容器完成闭环
        验证: container 的直接子任务列表长度 >= 4

    V3. 每个一级子任务闭环（提交→执行→评估→完成通知）:
        对每个一级子任务验证:
        - 提交: 子任务存在且 parent_task_id == container_id
        - 执行: 子任务 status 曾到达 RUNNING（终态为 COMPLETED 或 FAILED）
        - 评估: 子任务 status 曾到达 EVALUATING 或直接 COMPLETED
        - 完成通知: execution_record 中包含对应的系统通知记录
          （type=user, content 含 "[系统通知] 任务"）

    V4. 交互记录验证（交互信息存入执行记录，覆盖 2 个交互点）:
        - HumanInteractionService._requests 中存在 >= 2 条交互请求
        - HumanInteractionService._responses 中存在对应数量的响应
        - ExecutionRecordStorage 中包含 >= 2 条 human_interaction 工具调用记录
          （type=tool, name=human_interaction）
        - 其中至少 1 条 interaction_mode == "conversation"（方案讨论）
        - 其中至少 1 条涉及评审/评估（human_review）
        - 所有请求记录 status == "completed"（非 timeout/cancelled）

    V5. 工作空间产出:
        - .ai_workspaces/{container_id}/ 目录存在
        - docs/solution.md 存在（方案准备产出）
        - docs/task_plan.md 存在（方案细化产出）

    V6. 工具调用完整性:
        - ExecutionRecordStorage 中包含以下工具调用记录:
          - task_submit（任务提交）
          - human_interaction（人类交互，>= 2 次）
          - task_evaluate（任务评估）
          - task_manage（容器完成）

    V7. 无崩溃:
        - stdout 不包含 "Traceback (most recent call last)"
        - 所有子任务无异常错误记录

  超时: 1200 秒（含方案准备+细化+执行+交互等待）
  预期: 全流程一次通过或子任务少量重试后通过

  验证数据来源:
    - 任务数据: data/tasks/{task_id}.yaml（CLI 持久化）
    - 执行记录: data/pipelines/{pipeline_run_id}.yaml
    - 工作空间: .ai_workspaces/{container_id}/
    - 交互记录: 退出前通过 HTTP/status API 获取（或从执行记录中提取）
```

***

## 五、提示词设计

### 5.1 E2E 测试的提示词模板

E2E 测试中发送给 L1 Agent 的消息需要精确控制行为，避免不确定性。

#### 模板 1：短期任务（评估一次通过）

```
提交一个{agent_type}任务：让 {agent_name} {task_description}，
保存到 {filename}，验收标准使用 semantic_check 语义评估，
评估要求是 {criteria}。提交任务不要自己做。
```

**示例**:

```
提交一个调研任务：让 research_agent 写一份关于 Python 列表推导式的
简短教程（保存到 tutorial.md），验收标准使用 semantic_check 语义评估，
评估要求是教程包含列表推导式的基本语法和 3 个以上实用示例。提交任务不要自己做。
```

#### 模板 2：短期任务（触发重试）

```
提交一个{agent_type}任务：让 {agent_name} {task_description}，
保存到 {filename}，验收标准使用 semantic_check 语义评估，
评估要求是 {high_bar_criteria}。提交任务不要自己做。
```

**示例**（高难度标准，大概率触发重试）:

```
提交一个调研任务：让 research_agent 写一份关于 Python GIL 的调研报告
（保存到 gil_report.md），验收标准使用 semantic_check 语义评估，
评估要求是报告必须包含 CPython 源码级别的 GIL 实现分析、至少 5 个性能
基准测试数据、以及与 Rust/TGo 语言的并发模型对比表格。提交任务不要自己做。
```

#### 模板 3：长期任务

```
提交一个长期任务：{high_level_goal}。
项目名称：{project_name}。提交任务不要自己做。
```

**示例**:

```
提交一个长期任务：开发一个基于终端的贪吃蛇游戏，支持方向键控制、
计分、游戏暂停/继续功能。项目名称：snake_game。提交任务不要自己做。
```

### 5.2 提示词设计原则

| 原则       | 说明                     | 反例              |
| -------- | ---------------------- | --------------- |
| **确定性**  | 明确指定 Agent 名称、文件名、评估标准 | "写个报告然后评估一下"    |
| **可验证**  | 评估标准必须能用工具客观验证         | "报告要写得好"        |
| **结尾指令** | 必须以"提交任务不要自己做"结尾       | 无结尾指令 → L1 自己做了 |
| **避免歧义** | 文件名用英文，路径明确            | "保存到某个文件"       |
| **可控难度** | 通过调整评估标准的严格程度控制是否触发重试  | 标准太模糊 → 不确定     |

### 5.3 评估标准难度梯度

| 难度     | criteria 示例                  | 预期结果          |
| ------ | ---------------------------- | ------------- |
| **低**  | "文件已创建且内容非空"                 | 一次通过          |
| **中**  | "报告包含 X 的核心概念（A、B、C）且结构清晰"   | 大概率一次通过，小概率重试 |
| **高**  | "报告必须包含源码级分析、5 个基准测试数据、对比表格" | 大概率重试 1-2 次   |
| **极高** | "报告必须包含 20 个代码示例和完整的 API 文档" | 可能重试耗尽        |

***

## 六、测试执行计划

### 6.1 日常开发（PR 合并前）

```bash
# 只跑 L1-L3（快速，无真实 LLM 调用）
pytest tests/suites/ -m "not integration" -v --timeout=120
```

### 6.2 每日构建（CI）

```bash
# L1-L3 + 稳定性套件
pytest tests/suites/ -m "not integration" -v --timeout=300
```

### 6.3 发版前（完整验证）

```bash
# 全部测试（含 E2E 真实 LLM）
pytest tests/suites/ --run-integration -v --timeout=900
```

### 6.4 稳定性专项

```bash
# 只跑新增的稳定性测试
pytest tests/suites/ -m "core or task" -k "stability or retry or workspace" -v
```

***

## 七、验收标准

### 7.1 测试通过标准

| 指标           | 标准                   |
| ------------ | -------------------- |
| L1-L3 测试通过率  | 100%（0 失败）           |
| L4 E2E 测试通过率 | ≥ 80%（允许 LLM 不确定性）   |
| 新增测试数量       | ≥ 37 个               |
| 单个测试执行时间     | L1-L3 < 5s，L4 < 600s |
| 测试覆盖的新代码行    | ≥ 80%                |

### 7.2 稳定性指标

| 指标      | 标准                     |
| ------- | ---------------------- |
| 评估重试成功率 | 重试后通过率 ≥ 70%           |
| 评估超时率   | < 5%（15 次迭代内完成）        |
| 管道死锁    | 0 次                    |
| 文件路径错误  | 0 次（所有文件在 workspace 内） |
| 状态回写正确率 | 100%（result 字段非 null）  |

***

## 八、文件组织

```
tests/suites/
├── core/
│   ├── test_evaluation_stability.py     ← 套件 A（12 个）✅ 可立即编写
│   └── test_pipeline_stability.py       ← 套件 D（6 个）✅ 可立即编写
├── task/
│   ├── test_evaluation_retry.py          ← 套件 B（6 个）⚠️ B1 需 Mock 管道
│   ├── test_workspace_stability.py       ← 套件 C（5 个）✅ 可立即编写
│   └── test_long_term_stability.py       ← 套件 E（5 个）⚠️ E2/E4/E5 待确认机制
└── cli/
    └── test_e2e_stability.py             ← 套件 F（4 个）🔴 需 CLITestHarness

总计: 38 个测试
```

***

## 九、依赖评估

> 基于代码库调研结果，对每个测试套件的实际依赖进行评估。

### 9.1 被测模块存在性

| #  | 被测模块                    | 文件路径                                        | 存在 | 关键接口位置                                                                                         |
| -- | ----------------------- | ------------------------------------------- | -- | ---------------------------------------------------------------------------------------------- |
| 1  | EvaluationEngine        | `src/evaluation/engine.py`                  | ✅  | `_parse_evaluation_result` (L503), `_build_agent_eval_prompt` (L557), `_evaluate_agent` (L378) |
| 2  | EvaluationExecutor      | `src/evaluation/executor.py`                | ✅  | `run_evaluation(skip_state_update=True)` (L75)                                                 |
| 3  | TaskEvaluateTool        | `src/tools/builtin/task_evaluate.py`        | ✅  | `_handle_evaluation_result` (L327), `_get_input_params` (L614), `_auto_complete` (L272)        |
| 4  | TaskWorker              | `src/infrastructure/task_worker.py`         | ✅  | workspace 默认值 (L298-301)                                                                       |
| 5  | ParamInjectPlugin       | `src/plugins/input/param_inject.py`         | ✅  | workspace 注入 (L138-141)                                                                        |
| 6  | InputAdapter            | `src/channels/cli/input_adapter.py`         | ✅  | `_read_multiline` + `run_in_executor` (L92)                                                    |
| 7  | TaskSubmitTool          | `src/tools/builtin/task_submit.py`          | ✅  | `_execute_long_term` (L500)                                                                    |
| 8  | evaluator\_agent.yaml   | `config/agents/system/evaluator_agent.yaml` | ✅  | `max_iterations=15`, `evaluation_mode=true`                                                    |
| 9  | PipelineEngine          | `src/pipeline/engine.py`                    | ✅  | `run()` (L89), `resume()` (L155)                                                               |
| 10 | TaskModel               | `src/tasks/types.py`                        | ✅  | `metadata: dict[str, Any]` (L127)                                                              |
| 11 | AgentRegistry           | `src/agents/registry.py`                    | ✅  | `get()`, `list_all()`                                                                          |
| 12 | MetricDefinition        | `src/evaluation/types.py`                   | ✅  | 完整 dataclass                                                                                   |
| 13 | HumanInteractionService | `src/human_interaction/service.py`          | ✅  | `set_human_interaction_service()` 可注入                                                          |

### 9.2 依赖状态矩阵

```
套件 A（评估引擎稳定性）   12 个测试 → ✅ 全部可立即编写
套件 B（评估重试闭环）      6 个测试 → ✅ 4 个可立即编写  ⚠️ 2 个需 Mock 管道
套件 C（工作空间稳定性）    5 个测试 → ✅ 全部可立即编写
套件 D（管道执行稳定性）    6 个测试 → ✅ 全部可立即编写（已替换 TaskReminder 测试）
套件 E（长期任务执行）      5 个测试 → ✅ 2 个可立即编写  ⚠️ 3 个待确认机制
套件 F（E2E 集成）          4 个测试 → 🔴 全部需 CLITestHarness 基础设施
───────────────────────────────────────────────────
合计 38 个 → 29 个可立即编写  5 个需 Mock/确认  4 个需基础设施
```

### 9.3 已修复的设计问题

| 问题                                                  | 原设计                 | 修正后                           | 原因                                             |
| --------------------------------------------------- | ------------------- | ----------------------------- | ---------------------------------------------- |
| A3 `test_find_agent_not_found`                      | "fallback 到 Mock"   | `raise RuntimeError(...)`     | 代码已改为抛异常，不降级到 Mock                             |
| D3 `test_task_reminder_evaluation_mode_from_config` | 测试 TaskReminder 类   | 测试 evaluator\_agent.yaml 配置加载 | `plugins/output/task_reminder.py` 不存在，改为验证配置文件 |
| D4 `test_task_reminder_detects_missing_eval_result` | 测试 TaskReminder 信号  | 测试 evaluator\_agent 工具可用性     | 同上                                             |
| D5 `_read_multillin` 拼写错误                           | `_read_multillin()` | `_read_multiline()`           | 方法名拼写修正                                        |

### 9.4 需创建的测试基础设施

| 基础设施                        | 影响的测试         | 说明                                      |
| --------------------------- | ------------- | --------------------------------------- |
| MockLLMResponse             | B1 重试测试       | 按调用次数返回不同 LLM 响应（已设计，需编写）               |
| MockAgentRegistry           | A3 Agent 查找测试 | 模拟 `get()` 和 `list_all()`               |
| MockTaskService             | B1, E1        | 记录 `complete_evaluation`/`fail_task` 调用 |
| MockPipelineFactory         | B1 重试测试       | 返回可控的 Mock PipelineEngine               |
| MockHumanInteractionService | F4            | 自动返回确认响应                                |
| CLITestHarness              | F1-F4         | 编程式启动 CLI、发送消息、等待结果                     |
| pytest markers              | 全部            | 新增 `core`, `task`, `unit`, `e2e` 标记     |

### 9.5 待确认事项

| 事项                                         | 影响测试 | 确认方式                                    |
| ------------------------------------------ | ---- | --------------------------------------- |
| `solution_preparation_agent` 子任务自动挂载是否完整实现 | E2   | 阅读 task\_submit.py `_execute_long_term` |
| 子任务结果回传到父任务的完整链路                           | E4   | 阅读 task\_worker.py 父任务状态更新逻辑            |
| 依赖排序执行逻辑是否实现                               | E5   | 搜索 dependency\_validator 的使用点           |

***

## 十、测试计划

> 基于 v1.1 依赖评估结果制定的分阶段执行计划。

### 10.1 阶段总览

```
阶段 1（无阻塞）     → 29 个测试可立即编写
阶段 2（Mock 基础设施） → 5 个测试需 Mock 管道/确认机制
阶段 3（E2E 基础设施）  → 4 个测试需 CLITestHarness

总工作量分布：
  阶段 1 ████████████████████████████████ 76%
  阶段 2 █████                         13%
  阶段 3 ████                          11%
```

### 10.2 阶段 1：无阻塞测试（29 个）

**前置条件**: 无

**工作内容**:

| 步骤  | 套件          | 测试数 | 文件                             | 说明                                                    |
| --- | ----------- | --- | ------------------------------ | ----------------------------------------------------- |
| 1.1 | A1          | 6   | `test_evaluation_stability.py` | `_parse_evaluation_result` 解析，纯静态方法测试                 |
| 1.2 | A2          | 3   | `test_evaluation_stability.py` | `_build_agent_eval_prompt` 构建，纯静态方法测试                 |
| 1.3 | A4          | 5   | `test_evaluation_stability.py` | 前置条件校验，全部期望 RuntimeError                              |
| 1.4 | A3          | 3   | `test_evaluation_stability.py` | Agent 查找，需简单 MockAgentRegistry fixture                |
| 1.5 | C1-C5       | 5   | `test_workspace_stability.py`  | workspace 默认值 + 注入 + 隔离，需 tmp\_path                   |
| 1.6 | D1+D2+D5+D6 | 4   | `test_pipeline_stability.py`   | ThreadPool + max\_iterations + event\_loop + state 传递 |
| 1.7 | D3+D4       | 2   | `test_pipeline_stability.py`   | evaluator\_agent 配置加载 + 工具可用性验证                       |
| 1.8 | E1+E3       | 2   | `test_long_term_stability.py`  | 容器创建 + workspace 持久化                                  |

**执行顺序建议**: 1.1 → 1.2 → 1.3 → 1.4 → 1.5 → 1.6 → 1.7 → 1.8

**交付物**:

- 6 个测试文件
- conftest.py 新增 fixture（metric\_def, task\_model, loader 等）
- pyproject.toml 新增 markers

### 10.3 阶段 2：需 Mock 基础设施的测试（5 个）

**前置条件**:

- 阶段 1 完成
- 创建 MockLLMResponse、MockPipelineFactory、MockTaskService

**工作内容**:

| 步骤  | 测试 ID                                     | 说明                      | Mock 需求                       |
| --- | ----------------------------------------- | ----------------------- | ----------------------------- |
| 2.1 | B1 `test_retry_then_pass`                 | 第一次 failed → 第二次 passed | MockPipelineFactory 返回按序响应的管道 |
| 2.2 | B1 `test_retry_exhausted`                 | 连续 3 次 failed           | 同上，3 次都返回 failed              |
| 2.3 | B1 `test_retry_feedback_contains_details` | 验证 retry 消息格式           | 构造 MetricResult，无需 Mock 管道    |
| 2.4 | B1 `test_skip_state_update_on_retry`      | 验证 executor 不回写状态       | MockTaskService 记录调用          |
| 2.5 | E2/E4/E5（可选）                              | 长期任务机制测试                | 取决于 10.5 待确认事项的结论             |

**交付物**:

- Mock 基础设施代码（写入 conftest.py 或 tests/suites/conftest.py）
- `test_evaluation_retry.py` 补充 4 个测试
- `test_long_term_stability.py` 补充 0-3 个测试（视确认结果）

### 10.4 阶段 3：E2E 集成测试（4 个）

**前置条件**:

- 阶段 1 完成
- CLITestHarness 基础设施实现
- MockHumanInteractionService 实现
- 真实 LLM API 可用（MiniMax API key）

**工作内容**:

| 步骤  | 测试 ID                                      | 说明               | 特殊依赖                        |
| --- | ------------------------------------------ | ---------------- | --------------------------- |
| 3.1 | 实现 CLITestHarness                          | 编程式 CLI 启动/消息/等待 | 核心基础设施                      |
| 3.2 | 实现 MockHumanInteractionService             | 自动确认方案           | F4 专用                       |
| 3.3 | F3 `test_real_short_term_pass`             | 正常通过流程           | 最简单，先验证 CLITestHarness 可用   |
| 3.4 | F1 `test_real_short_term_with_retry`       | 高难度重试流程          | 真实 LLM                      |
| 3.5 | F2 `test_real_short_term_retry_exhausted`  | 必败重试耗尽           | 真实 LLM                      |
| 3.6 | F4 `test_real_long_term_with_auto_confirm` | 长期任务自动化          | MockHumanInteractionService |

**交付物**:

- `tests/suites/cli/test_e2e_stability.py`
- CLITestHarness 工具类
- MockHumanInteractionService 实现

### 10.5 待确认事项与决策点

在阶段 2/3 执行前需确认以下事项：

| #  | 事项                                     | 确认方式                          | 影响    | 决策                    |
| -- | -------------------------------------- | ----------------------------- | ----- | --------------------- |
| Q1 | `solution_preparation_agent` 子任务自动挂载机制 | 阅读 `_execute_long_term` 代码    | E2 测试 | 如未实现 → 移除 E2 或标注 skip |
| Q2 | 子任务结果回传到父任务                            | 阅读 task\_worker 父任务更新逻辑       | E4 测试 | 如未实现 → 移除 E4 或标注 skip |
| Q3 | 依赖排序执行逻辑                               | 搜索 `dependency_validator` 使用点 | E5 测试 | 如未实现 → 移除 E5 或标注 skip |
| Q4 | E2E 测试运行环境                             | 确认 MiniMax API key 和网络环境      | F1-F4 | 需有效 API key           |
| Q5 | 现有 `test_evaluation.py` 兼容性            | L329-335 的 Mock 测试与新代码冲突      | 套件 A  | 需更新现有测试期望             |

### 10.6 时间线（按阶段顺序）

```
阶段 1 ──────────────────────────────────
  1.1-1.4 套件 A（12 个）
  1.5     套件 C（5 个）
  1.6-1.7 套件 D（6 个）
  1.8     套件 E 部分（2 个）
  预计: 25 个测试可快速编写完成

阶段 2 ──────────────────────────────────
  Mock 基础设施搭建
  套件 B 补充（4 个）
  套件 E 补充（0-3 个，视确认结果）

阶段 3 ──────────────────────────────────
  CLITestHarness 开发
  套件 F（4 个）
```

### 10.7 验收标准（分阶段）

| 阶段          | 通过标准                                |
| ----------- | ----------------------------------- |
| **阶段 1 完成** | 29 个测试全部通过，0 失败                     |
| **阶段 2 完成** | 34 个测试全部通过（含 Mock 测试）               |
| **阶段 3 完成** | 38 个测试，E2E 通过率 ≥ 90%                |
| **最终**      | 总计 38 个测试，L1-L3 100% 通过，L4 ≥ 90% 通过 |

### 10.8 测试修复原则

当测试执行出现失败时，所有修复行为必须遵循以下原则：

| # | 原则 | 说明 | 违反示例 |
| - | ---- | ---- | -------- |
| R1 | **禁止修改 Agent 提示词** | Agent 的 YAML 配置、system_prompt、instruction 等提示词内容属于产品逻辑，不得为通过测试而修改 | 测试失败后去调整 `evaluator_agent.yaml` 的提示词 |
| R2 | **提示词问题仅记录不修改** | 若分析认为失败根因可能源于提示词设计，必须在测试报告的「说明」部分明确指出这一可能性及建议调整方向，但不得擅自修改提示词内容 | 报告中写"建议优化 evaluator_agent 的结果解析提示词"是允许的，但不应直接去改 YAML |
| R3 | **修复代码而非降低标准** | 测试失败时应修复被测代码的逻辑缺陷，而非放宽断言阈值、删除断言或改写测试期望 | 将 `assert result.passed == True` 改为 `assert result is not None` |
| R4 | **Mock 行为必须忠于真实逻辑** | Mock 的返回值、副作用应反映真实模块的行为契约，不得为通过测试而虚构行为 | Mock 返回了真实代码永远不会产生的字段 |
| R5 | **修复范围最小化** | 每次修复只针对失败测试涉及的代码路径，不做"顺手"重构或无关改动 | 修复解析 bug 时顺便改了无关的日志格式 |

#### 10.8.1 提示词问题处理流程

```
测试失败
  ├─ 分析根因
  │    ├─ 代码缺陷 → 修复代码（R3）
  │    ├─ Mock 不准确 → 修正 Mock（R4）
  │    └─ 可能是提示词问题 ──┐
  │                          ▼
  │              在测试报告中记录：
  │              · 失败现象
  │              · 分析结论：可能源于提示词
  │              · 建议的提示词调整方向
  │              · 不修改提示词（R1/R2）
  │                          │
  │                          ▼
  │              标记测试为「提示词相关-待确认」
  │              不计入代码缺陷统计
  └─ 提交修复报告
```

#### 10.8.2 测试报告中提示词问题的说明模板

当分析认为问题可能源于提示词时，测试报告需包含以下内容：

```markdown
### [测试ID] 提示词相关问题说明

- **失败现象**: <具体断言失败信息>
- **根因分析**: 分析认为此失败可能源于 Agent 提示词的设计，
  而非被测代码逻辑缺陷。
- **提示词位置**: <YAML 文件路径及具体段落>
- **建议调整方向**: <描述性的调整建议，不包含具体修改内容>
- **处理方式**: 未修改提示词，标记为「提示词相关-待确认」
```

