# Agent 配置系统

## 需求

Agent OS 需要一个统一的 Agent 配置管理模块，用于：

1. **定义 Agent 的完整配置结构**：包括层级、类型、系统提示词、工具绑定、约束规则、产出物定义等 30+ 字段
2. **从 YAML 文件加载配置**：兼容旧文件中 28 个 Agent YAML 的结构，支持单文件和目录递归加载
3. **注册和查找 Agent**：按 ID、层级、类型、分类、标签、工具等多维度查询
4. **构建上下文**：将静态变量（会话级不变）和动态变量（每轮变化）组装为可注入的状态字典
5. **验证数据格式**：基于 input/output Schema 对 Agent 的输入输出数据进行校验

与管道系统的关系：Agent 配置系统是管道的「上游」，为 PipelineEngine 提供 Agent 级别的配置信息（层级映射到 AgentLevel、工具绑定映射到 ToolRegistry、约束映射到 InputPlugin 等）。

## 逻辑

### 设计思路

采用 **加载-注册-构建-验证** 四层架构：

```
YAML 文件 → AgentConfigLoader → AgentConfig → AgentRegistry（存储）
                                                    ↓
                                          ContextBuilder（上下文构建）
                                          SchemaValidator（数据验证）
```

1. **types.py** 定义纯数据结构，零逻辑，方便序列化和测试
2. **loader.py** 负责 YAML → AgentConfig 的映射，处理嵌套结构（static_vars → ContextConfig 等）和类型转换（"orchestrator" → AgentType.SPECIALIZED）
3. **registry.py** 提供内存字典存储 + 多维度查询，支持批量加载
4. **context_builder.py** 根据 ContextConfig 中每个变量的 type 字段，生成对应的值（rules 类型提取约束、path 类型读文件、timestamp 类型动态生成等）
5. **schema_validator.py** 基于 JSON Schema 的简化验证，检查 required 和 type

### 类型映射

| YAML agent_type | AgentType 枚举 |
|-----------------|---------------|
| main | MAIN |
| orchestrator | SPECIALIZED |
| specialized | SPECIALIZED |
| system | SYSTEM |

| YAML level | AgentLevel 枚举 |
|-----------|-----------------|
| L1 | L1_MAIN |
| L2 | L2_SUBTASK |
| L3 | L3_ATOMIC |

### 上下文变量类型

| type | 层级 | 行为 |
|------|------|------|
| rules | 静态 | 从 hard_constraints + soft_constraints 提取内容 |
| path | 静态 | 从文件路径读取内容 |
| inline (content) | 静态 | 直接注入 content 字段内容 |
| timestamp | 动态 | 运行时生成 ISO 8601 时间戳 |
| session | 动态 | 运行时生成会话信息占位 |
| agent | 动态 | 运行时生成 Agent 信息占位 |
| model | 动态 | 运行时生成模型信息占位 |
| retrieval | 动态 | 基于 tags 的知识库检索占位 |

## 结构

### 文件清单

```
src/agent_os/agents/
├── __init__.py            # 公共 API 导出（14 个符号）
├── types.py               # 数据类型定义（8 个数据类 + 2 个枚举）
├── loader.py              # AgentConfigLoader（YAML 加载 + 嵌套结构解析）
├── registry.py            # AgentRegistry（注册/查找/筛选/批量加载）
├── context_builder.py     # ContextBuilder（静态/动态/完整上下文构建）
├── schema_validator.py    # SchemaValidator（输入输出 Schema 验证）
└── README.md              # 本文档
```

### 配套文件

```
src/agent_os/config/agents/
├── test_main.yaml          # 测试用主控 Agent（L1 MAIN）
├── test_orchestrator.yaml  # 测试用编排 Agent（L2 SPECIALIZED）
├── test_evaluator.yaml     # 测试用系统 Agent（L3 SYSTEM）
├── invalid_no_id.yaml      # 测试用：缺少 config_id
└── invalid_bad_level.yaml  # 测试用：无效 level 值
```

### 类说明

| 类 | 文件 | 职责 |
|----|------|------|
| AgentConfig | types.py | Agent 完整配置数据类（30+ 字段） |
| AgentLevel | types.py | Agent 层级枚举（L1/L2/L3） |
| AgentType | types.py | Agent 类型枚举（main/specialized/system） |
| ContextConfig | types.py | 上下文配置（enabled + items） |
| ContextVarItem | types.py | 上下文变量项（type/path/tags/content 等） |
| KnowledgeConfig | types.py | 知识库配置 |
| RuleReinforcement | types.py | 规则强化配置 |
| DeliverableSpec | types.py | 产出物定义 |
| MetricRef | types.py | 评估指标引用 |
| AgentConfigLoader | loader.py | YAML 配置加载器 |
| AgentRegistry | registry.py | Agent 配置注册表 |
| ContextBuilder | context_builder.py | 上下文构建器 |
| SchemaValidator | schema_validator.py | Schema 验证器 |

### 使用示例

```python
from agents import (
    AgentConfigLoader,
    AgentRegistry,
    AgentLevel,
    ContextBuilder,
    SchemaValidator,
)

# 1. 加载配置
config = AgentConfigLoader.load_from_yaml("config/agents/main/main_agent.yaml")

# 2. 批量注册
registry = AgentRegistry()
count = registry.load_directory("config/agents/")
print(f"已加载 {count} 个 Agent 配置")

# 3. 查询
main_agents = registry.find_by_level(AgentLevel.L1_MAIN)
code_agents = registry.find_by_category("code")
tool_agents = registry.find_by_tool("file_read")

# 4. 构建上下文
builder = ContextBuilder(base_path=".")
context = builder.build_full_context(config)
# context = {"static": {...}, "dynamic": {...}}

# 5. 验证数据
validator = SchemaValidator()
errors = validator.validate_input(config, {"phase": "setup"})
if errors:
    print("验证失败:", errors)
```

### 测试

```bash
python -m pytest src/agent_os/tests/test_agent_config.py -v
# 76 passed
```
