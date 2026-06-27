---
name: 创建工具
description: 创建或修改工具时加载。内置工具创建规范：TDD 流程、命名规范、BuiltinTool 继承、ToolCategory 枚举、路径规范、代码模板要点。
---

# 创建工具

## 工具创建流程（TDD 模式）

### 1. 需求分析
- 明确工具的名称、描述、核心能力、分类（ToolCategory）
- 明确输入输出接口和使用场景

### 2. 编写测试（Red 阶段）
- 测试写到 `src/tools/builtin/test_{tool_id}.py`
- 覆盖：核心功能、边界情况、错误处理
- 测试中引用尚未实现的工具类（import {ToolId}Tool）
- 运行测试确认失败（Red）

### 3. 生成工具代码（Green 阶段）
- 代码写到 `src/tools/builtin/{tool_id}.py`
- 写最少的代码让测试通过，不做过度设计
- 运行测试确认通过（Green）

### 4. 重构优化（Refactor 阶段）
- 测试通过前提下优化代码
- 运行测试确认无回归

### 5. 验证提交
- 运行全部测试通过
- yaml_validate 验证配置格式（如有配置）

## 命名规范

| 对象 | 规范 | 示例 |
|------|------|------|
| 工具 ID | snake_case | `web_search` |
| 文件名 | snake_case.py | `web_search.py` |
| 类名 | PascalCase | `WebSearchTool` |

## 工具类规范

- 所有工具继承 `BuiltinTool` 基类
- 实现 `get_tool_definition()` 静态方法
- 实现 `execute()` 异步方法
- 中文注释，类型注解，遵循项目现有风格

## ToolCategory 枚举（必须使用大写）

| 枚举值 | 说明 |
|--------|------|
| FILE | 文件操作 |
| SEARCH | 搜索 |
| WEB | Web 操作 |
| MEMORY | 记忆检索 |
| TASK | 任务管理 |
| SYSTEM | 系统工具 |
| EXECUTION | 命令执行 |
| ANALYSIS | 分析 |
| EVALUATION | 评估 |
| AGENT | Agent 调用 |
| MONITORING | 监控 |

## 路径规范

| 资源 | 路径 |
|------|------|
| 工具代码 | `src/tools/builtin/{tool_id}.py` |
| 工具测试 | `src/tools/builtin/test_{tool_id}.py` |
| 工具配置（可选） | `config/tools/` |
| 隔离策略（可选） | `config/isolation/isolation_policy.yaml` |

## 注意事项

- 创建工具只需创建 `{tool_id}.py`，**无需修改 `__init__.py`**，系统自动发现
- 隔离策略由 `config/isolation/isolation_policy.yaml` 统一管理，代码中不设置隔离属性
- 如需特殊隔离，在该配置文件中添加工具配置
- 为工具设置准确的 category 标签（对应 ToolCategory 枚举）
- 工具名称要能反映功能（名称用于配置匹配）

## 验证清单

创建工具后逐项核对：
- [ ] 工具代码文件 `src/tools/builtin/{tool_id}.py` 存在
- [ ] 测试文件 `src/tools/builtin/test_{tool_id}.py` 存在且通过
- [ ] 继承 BuiltinTool，实现 get_tool_definition() 和 execute()
- [ ] ToolCategory 设置正确
- [ ] 命名规范一致（tool_id snake_case / 类名 PascalCase）
- [ ] 无需改 __init__.py
