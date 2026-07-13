# 配置文件目录结构

本目录包含系统的所有配置文件，按功能模块组织。

## 目录结构

```
config/
├── README.md                    # 本文档
├── agents/                      # Agent 配置 (按层级分类)
│   ├── main/                   # L1 主 Agent
│   │   ├── lingxi.yaml         # 灵汐主Agent (已优化)
│   │   └── main_agent.yaml     # 主控Agent
│   ├── orchestrator/               # L2 编排 Agent
│   │   ├── general_agent.yaml            # 通用任务编排
│   │   ├── solution_planning_agent.yaml   # 方案规划
│   │   ├── resource_manager_agent.yaml   # 资源管理
│   │   ├── ac_refine_agent.yaml          # AC细化
│   │   ├── modification_planner_agent.yaml # 修改规划
│   │   ├── resource_analyzer_agent.yaml   # 资源分析
│   │   └── resource_modifier_agent.yaml   # 资源修改
│   ├── executor/               # L3 执行 Agent
│   │   ├── planning_agent.yaml           # 执行规划
│   │   ├── design_agent.yaml             # 设计Agent
│   │   ├── research_agent.yaml           # 研究Agent
│   │   ├── discussion_agent.yaml         # 讨论Agent
│   │   ├── environment_setup_agent.yaml  # 环境设置
│   │   └── */                            # 各类专业执行Agent
│   └── system/                 # 系统 Agent
│       ├── agent_generator_agent.yaml    # Agent生成器
│       ├── tool_generator_agent.yaml     # 工具生成器
│       ├── rollback_manager_agent.yaml   # 回滚管理
│       └── */                            # 其他系统Agent
├── models/                     # 模型配置
│   ├── llm.yaml               # LLM 模型配置
│   └── embedding.yaml         # 嵌入模型配置 (新建)
├── tools/                      # 工具配置
│   ├── builtin_tools_config.yaml  # 内置工具配置
│   ├── tool_permissions.yaml      # 工具权限配置
│   └── */                         # 各类工具配置
├── system/                     # 系统配置
│   ├── redis.yaml             # Redis 配置
│   ├── memory_storage.yaml    # 存储配置
│   ├── context_window_config.yaml # 上下文窗口配置
│   ├── claude-code-router-config.json # Claude路由配置
│   └── system_resources_inventory.md  # 系统资源清单
├── evaluation/                 # 评估配置
│   ├── evaluation_metrics.yaml   # 评估指标
│   └── cost_control.yaml         # 成本控制
├── workflows/                  # 工作流配置 (按功能分类)
│   ├── task/                      # 任务相关工作流
│   │   ├── task_execution.yaml           # 任务执行工作流
│   │   └── task_planning_workflow.yaml   # 任务规划工作流
│   ├── design/                    # 设计相关工作流
│   │   └── solution_design_workflow.yaml # 解决方案设计工作流
│   ├── evaluation/                # 评估相关工作流
│   │   └── evaluation.yaml              # 评估工作流
│   └── resource/                  # 资源相关工作流
│       ├── resource_generation.yaml     # 资源生成工作流
│       └── resource_modification.yaml   # 资源修改工作流
│   说明：工作流配置按功能分为4类
│   - task/: 任务执行和规划相关工作流
│   - design/: 设计方案相关工作流
│   - evaluation/: 评估验收相关工作流
│   - resource/: 资源生成和修改相关工作流
├── workflow_templates/         # 工作流模板
│   ├── conditional_workflow.yaml    # 条件工作流模板
│   ├── parallel_workflow.yaml       # 并行工作流模板
│   └── simple_tool_workflow.yaml    # 简单工具工作流模板
├── ui/                        # UI 配置
│   └── themes/                    # 主题配置
├── triggers/                   # 触发器配置
└── examples/                  # 配置示例
    ├── api.yaml.example
    ├── app.yaml.example
    └── llm.yaml.example
```

## 配置文件说明

### 核心配置文件

| 文件                                 | 说明          | 位置      |
| ------------------------------------ | ------------- | --------- |
| `agents/lingxi.yaml`                 | 主 Agent 配置 | ✅ 已优化 |
| `models/llm.yaml`                    | LLM 模型配置  | ✅ 已整理 |
| `models/embedding.yaml`              | 嵌入模型配置  | ✅ 新建   |
| `tools/builtin_tools_config.yaml`    | 内置工具配置  | ✅ 已移动 |
| `tools/tool_permissions.yaml`        | 工具权限配置  | ✅ 已移动 |
| `system/redis.yaml`                  | Redis 配置    | ✅ 已移动 |
| `evaluation/evaluation_metrics.yaml` | 评估指标      | ✅ 已移动 |

### 配置优先级

1. 环境变量 (最高)
2. 命令行参数
3. 配置文件
4. 默认值 (最低)

## 使用说明

1. **开发环境**: 复制 `examples/*.example` 文件并修改
2. **生产环境**: 使用环境变量覆盖敏感配置
3. **测试环境**: 使用独立的配置文件

## 配置文件变更记录

### 已完成的整理

- ✅ 优化 `lingxi.yaml` 提示词，添加简单任务直接处理逻辑
- ✅ 将 LLM 配置移至 `models/llm.yaml`
- ✅ 创建独立的 `models/embedding.yaml` 嵌入模型配置
- ✅ 将工具配置移至 `tools/` 目录
- ✅ 将系统配置移至 `system/` 目录
- ✅ 将评估配置移至 `evaluation/` 目录
- ✅ 将示例配置移至 `examples/` 目录
- ✅ **按层级分类整理所有 Agent 配置文件**
  - L1 主 Agent → `agents/main/`
  - L2 编排 Agent → `agents/orchestrator/`
  - L3 执行 Agent → `agents/executor/`
  - 系统 Agent → `agents/system/`
- ✅ **按功能分类整理所有工作流配置文件**
  - 任务工作流 → `workflows/task/`
  - 设计工作流 → `workflows/design/`
  - 评估工作流 → `workflows/evaluation/`
  - 资源工作流 → `workflows/resource/`
- ✅ 清理 config 根目录和 workflows 根目录，移除散乱配置文件

### 主要改进

1. **lingxi 提示词优化**:

   - 添加简单任务处理原则
   - 明确区分直接处理和创建任务的情况
   - 保持其他功能不变

2. **配置文件分类**:
   - 按功能模块组织配置文件
   - **按 Agent 层级分类**: L1 主 Agent、L2 编排 Agent、L3 执行 Agent、系统 Agent
   - **按工作流功能分类**: 任务工作流、设计工作流、评估工作流、资源工作流
   - 分离嵌入模型配置
   - 统一示例文件位置
   - 清理根目录和子目录散乱文件

## 注意事项

- 敏感信息（API Key）应使用环境变量
- 配置文件支持 YAML 格式
- 修改配置后需要重启服务
- 嵌入模型配置已从 LLM 配置中分离
