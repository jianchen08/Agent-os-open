# Agent Builder 知识库 (V3)

## 使用说明

Tool-Agent Builder 用于自动创建工具或 Agent：
- **创建工具**：确定性任务（数据转换、API封装）→ 生成代码注册为 Tool
- **创建 Agent**：需要 LLM 推理（文本分析、内容生成）→ 生成配置注册为 Agent
- **复用资源**：自动搜索匹配的现有工具/Agent，避免重复创建

---

## Tool-Agent Builder V3 架构

V3 版本简化了构建流程，采用三路由模式：

```
solution_selector → solution_preparer → requirement_planner → 生成 → 测试
```

### 工作流步骤

| 步骤 | Agent | 说明 |
|------|-------|------|
| Step 1 | agent_solution_selector | 决定路由（reuse/tool/agent） |
| Step 2 | agent_solution_preparer | 准备方案（仅 tool 路由） |
| Step 3 | agent_requirement_planner | 生成详细规格 |
| Step 4a | agent_code_generator | 生成工具代码（tool 路由） |
| Step 4b | agent_config_generator | 生成 Agent 配置（agent 路由） |
| Step 4c | agent_test_designer | 设计测试用例（并行） |
| Step 5 | create_tool / create_agent | 写入数据库 |
| Step 6 | sandbox_run_tool / sandbox_run_agent | 沙盒测试 |

### 路由规则

| 路由 | 条件 | 说明 |
|------|------|------|
| reuse | 找到匹配的现有资源 | 直接返回现有工具/Agent ID |
| tool | 确定性逻辑任务 | 生成代码并注册为工具 |
| agent | 需要 LLM 推理 | 生成配置并注册为 Agent |

---

## 系统可用模型配置

创建 Agent 时，`model_name` 字段必须使用以下已配置的模型：

| 模型名称 | 描述 | 适用场景 |
|----------|------|----------|
| `deepseek-chat` | DeepSeek Chat 模型 | 通用对话、工具调用、代码生成 |
| `deepseek-reasoner` | DeepSeek Reasoner 模型 | 复杂推理、多步骤分析 |

### 模型参数说明

```yaml
# deepseek-chat 默认参数
temperature: 0.7
max_tokens: 4096
timeout: 60

# deepseek-reasoner 默认参数
temperature: 0.7
max_tokens: 8192
timeout: 120
```

### 模型选择建议

| Agent 类型 | 推荐模型 | temperature |
|------------|----------|-------------|
| 路由/选择类 | deepseek-chat | 0.2 |
| 规划类 | deepseek-chat | 0.3 |
| 生成类 | deepseek-chat | 0.5 |
| 复杂推理 | deepseek-reasoner | 0.7 |

---

## Agent 类型说明

### 结构类型 (agent_type)

| 类型 | 说明 |
|------|------|
| `atomic` | 原子 Agent，直接调用 LLM |
| `composite` | 复合 Agent，组合多个子 Agent |
| `main` | 主 Agent，用于与用户沟通 |

### 功能角色 (agent_role)

| 角色 | 说明 |
|------|------|
| `planner` | 规划类：任务分解、计划生成 |
| `executor` | 执行类：执行具体任务 |
| `router` | 路由类：任务分发、决策路由 |
| `general` | 通用类：通用对话助手 |

---

## System Prompt 规范

```markdown
# 角色
描述 Agent 的身份和定位

# 目标
描述 Agent 要完成的核心任务

# 能力
- 能力1
- 能力2
```

约束条件放在 `hard_constraints` 和 `soft_constraints` 字段中。

---

## 配置示例

```python
{
    "config_id": "agent_example",
    "name": "示例Agent",
    "description": "这是一个示例Agent",
    "agent_type": "atomic",
    "agent_role": "executor",
    "model_name": "deepseek-chat",  # 必须使用系统可用模型
    "model_params": {"temperature": 0.5},
    "system_prompt": "# 角色\n...\n\n# 目标\n...\n\n# 能力\n- ...",
    "tool_ids": [],
    "hard_constraints": ["约束1"],
    "soft_constraints": ["建议1"],
    "max_iterations": 5,
    "timeout_seconds": 60,
    "version": "3.0.0"
}
```

---

## 废弃的 Agent（V2 及更早）

以下 Agent 已在 V3 中废弃，不应再使用：

| 废弃 Agent | 原用途 | V3 替代 |
|------------|--------|---------|
| agent_tool_evaluator | 评估现有工具 | agent_solution_selector |
| agent_mcp_evaluator | 评估 MCP 工具 | agent_solution_selector |
| agent_code_evaluator | 评估代码方案 | agent_solution_selector |
| agent_agent_evaluator | 评估 Agent 方案 | agent_solution_selector |
| agent_code_builder | 生成代码 | agent_code_generator |
| agent_agent_builder | 生成 Agent | agent_config_generator |
| agent_mcp_wrapper | 封装 MCP | 已移除 |
