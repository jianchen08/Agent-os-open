# 内置 Agent 配置说明

本目录包含AI Agent系统的所有内置Agent配置文件。

## 目录结构

```
builtin/
├── 系统Agent (阶段6)
│   ├── planner_agent.yaml         # 规划Agent
│   ├── evaluator_agent.yaml       # 评估Agent
│   └── recovery_agent.yaml        # 恢复Agent
│
├── 内置Agent (阶段3)
│   ├── main_agent.yaml            # 主Agent
│   ├── code_analyzer.yaml         # 代码分析Agent
│   └── test_generator.yaml        # 测试生成Agent
│
└── 进化Agent (阶段11)
    ├── task_decomposer.yaml       # 任务拆解Agent
    ├── tool_maker.yaml            # 工具生成Agent
    ├── error_router.yaml          # 错误路由Agent
    └── execution_auditor.yaml     # 执行审计Agent
```

## Agent 详解

### 系统 Agent（System Agents）

#### 1. Planner Agent（规划Agent）
**文件**: `planner_agent.yaml`

**职责**:
- 深度分析用户意图
- 生成可验证的验收标准
- 搜索匹配的工具/工作流/Agent
- 组合执行计划
- 评估风险和置信度

**特点**:
- 强调意图澄清和验收标准的可验证性
- 搜索历史成功案例以提升效率
- 评估任务置信度以决定执行策略

**使用场景**:
- 收到复杂任务时
- 需要制定执行计划时
- 需要评估任务风险时

---

#### 2. Evaluator Agent（评估Agent）
**文件**: `evaluator_agent.yaml`

**职责**:
- 理解验收标准
- 选择验证工具进行客观验证
- 逐条检查标准是否满足
- 生成详细的评估报告
- 提供恢复建议

**特点**:
- 使用工具验证，避免主观判断
- 每条标准都有明确的Pass/Fail
- 提供可操作的恢复建议

**使用场景**:
- 任务执行完成后
- 需要验证结果质量时
- 需要评估是否达标时

---

#### 3. Recovery Agent（恢复Agent）
**文件**: `recovery_agent.yaml`

**职责**:
- 分析失败原因
- 判断错误类型（临时/永久/可恢复/不可恢复）
- 选择恢复策略
- 执行恢复操作

**恢复策略升级路径**:
```
失败1次 → retry_same（直接重试）
失败2次 → retry_modified（带反馈重试）
失败3次 → rollback（回退一步）
失败4次 → replan（重新规划）
失败5次 → escalate（升级到人类）
```

**使用场景**:
- 任务执行失败时
- 需要从错误中恢复时
- 需要重试策略时

---

### 内置 Agent（Built-in Agents）

#### 4. Main Agent（主Agent）
**文件**: `main_agent.yaml`

**职责**:
- 执行各类通用任务
- 代码开发
- 文件操作
- 命令执行
- 问题分析
- 文档编写

**特点**:
- 通用性强，可处理多种任务类型
- 自主执行，有把握的操作直接执行
- 遇到错误自动修复

**使用场景**:
- 日常任务执行
- 代码编写和修改
- 文件管理和操作

---

#### 5. Code Analyzer（代码分析Agent）
**文件**: `code_analyzer.yaml`

**职责**:
- 分析代码结构
- 检查代码质量
- 识别潜在问题
- 评估性能
- 提供改进建议

**分析维度**:
- 架构层面：设计模式、耦合度、内聚性
- 代码层面：可读性、可维护性、复杂度
- 质量层面：编码规范、文档、测试
- 安全层面：输入验证、认证授权
- 性能层面：时间/空间复杂度、资源使用

**使用场景**:
- 代码审查时
- 需要重构前分析时
- 性能优化时

---

#### 6. Test Generator（测试生成Agent）
**文件**: `test_generator.yaml`

**职责**:
- 设计测试用例
- 生成测试代码
- 生成测试数据
- 验证测试覆盖率

**测试类型**:
- 单元测试（Unit Tests）
- 集成测试（Integration Tests）
- 端到端测试（E2E Tests）
- 性能测试（Performance Tests）

**特点**:
- 全面覆盖正常、边界、异常场景
- 测试独立可重复
- 包含Mock和Fixture

**使用场景**:
- 开发新功能需要测试时
- 补充测试覆盖率时
- 生成测试数据时

---

### 进化 Agent（Evolutionary Agents）

#### 7. Task Decomposer（任务拆解Agent）
**文件**: `task_decomposer.yaml`

**职责**:
- 分析复杂任务
- 识别依赖关系
- 生成可并行执行的子任务
- 优化执行顺序

**拆解策略**:
- **水平拆解**: 按数据或资源拆解，可并行
- **垂直拆解**: 按功能或步骤拆解，有顺序
- **混合拆解**: 结合水平和垂直

**使用场景**:
- 收到复杂大任务时
- 需要并行执行提升效率时
- 需要优化执行顺序时

---

#### 8. Tool Maker（工具生成Agent）
**文件**: `tool_maker.yaml`

**职责**:
- 识别可自动化操作
- 设计工具接口
- 实现工具代码
- 在沙箱中验证
- 注册到工具库

**触发条件**:
- 同一操作被执行3次以上
- 用户明确要求自动化
- 现有工具无法满足需求

**工具质量标准**:
- 遵循代码规范
- 包含类型注解和文档
- 测试覆盖率 > 80%
- 安全可靠

**使用场景**:
- 发现重复操作时
- 需要自动化时
- 需要新工具时

---

#### 9. Error Router（错误路由Agent）
**文件**: `error_router.yaml`

**职责**:
- 分析错误特征
- 分类错误类型
- 路由到合适的处理器
- 优化路由规则

**错误分类**:
- **按来源**: 用户错误、代码错误、系统错误、外部错误
- **按可恢复性**: 临时错误、可恢复错误、永久错误
- **按严重性**: 致命、严重、中等、轻微

**使用场景**:
- 发生错误需要路由时
- 需要快速分类处理时
- 需要优化错误处理时

---

#### 10. Execution Auditor（执行审计Agent）
**文件**: `execution_auditor.yaml`

**职责**:
- 审计执行过程
- 分析性能瓶颈
- 识别资源浪费
- 检查合规性
- 提供优化建议

**审计维度**:
- **执行效率**: 时间分析、瓶颈识别
- **资源使用**: CPU、内存、磁盘、网络
- **工具使用**: 工具效果、优化机会
- **决策质量**: 决策准确性、置信度
- **合规性**: 审批流程、权限使用

**使用场景**:
- 任务执行完成后
- 需要性能分析时
- 需要优化流程时

---

## 配置格式说明

所有Agent配置遵循统一的YAML格式：

```yaml
name: agent_name              # Agent名称
description: |                # Agent描述（支持多行）
  详细描述

model: claude-3-5-sonnet-20241022  # 使用的模型
agent_type: system|main|atomic     # Agent类型
max_iterations: 50            # 最大迭代次数
timeout: 600                  # 超时时间（秒）
temperature: 0.7              # 温度参数

tags:                         # 标签
  - tag1
  - tag2

system_prompt: |              # 系统提示词
  详细的系统提示词

tool_names:                   # 可用工具列表
  - tool1
  - tool2

metadata:                     # 元数据
  version: "1.0.0"
  author: "System"
  created_at: "2024-12-27"
  phase: 3|6|11              # 开发阶段
  capabilities:               # 能力列表
    - capability1
    - capability2
```

## Agent 生命周期

```
┌─────────────┐
 │   阶段3     │  内置Agent (main, code_analyzer, test_generator)
 │  基础能力   │
 └─────────────┘
        │
        ▼
┌─────────────┐
 │   阶段6     │  系统Agent (planner, evaluator, recovery)
 │  系统协调   │
 └─────────────┘
        │
        ▼
┌─────────────┐
 │   阶段11    │  进化Agent (task_decomposer, tool_maker, error_router, execution_auditor)
 │  自我进化   │
 └─────────────┘
```

## 使用指南

### 加载Agent配置

```python
from src.agents.types import AgentConfig
import yaml

# 加载配置
with open("src/agents/builtin/main_agent.yaml", "r", encoding="utf-8") as f:
    config_dict = yaml.safe_load(f)

# 创建配置对象
config = AgentConfig(**config_dict)
```

### 创建Agent实例

```python
from src.agents.agent import Agent

# 创建Agent
agent = Agent(config=config)

# 执行任务
result = agent.execute("任务描述")
```

## 扩展指南

### 添加新的内置Agent

1. 在`builtin/`目录下创建新的YAML配置文件
2. 参考现有配置格式
3. 定义清晰的职责和能力
4. 编写详细的系统提示词
5. 配置合适的工具列表
6. 更新本README文档

### 最佳实践

1. **单一职责**: 每个Agent专注一个领域
2. **明确边界**: 清晰定义Agent的能力边界
3. **工具匹配**: 配置适合的工具列表
4. **详细提示**: 提供详细的系统提示词
5. **版本管理**: 在metadata中记录版本信息

## 相关文档

- [Agent设计规范](../../../docs/design/agent-design-spec.md)
- [Agent模块接口](../../../docs/modules/agent.md)
- [Agent类型定义](../types.py)

---

**最后更新**: 2024-12-27
**维护者**: System
