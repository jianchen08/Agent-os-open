# Evolution 模块 - Agent 自进化能力

## 需求

Agent OS 系统需要自主扩展能力的闭环机制，当 Agent 发现自身缺少某种能力时，
能够自动分析缺口、生成代码、安全审查、热加载并记录审计日志。

### 核心功能

1. **能力缺口分析**：识别 Agent 缺失的能力，进行四层筛选（工具层→配置层→插件层→核心层）
2. **代码生成**：根据缺口生成 BuiltinTool 或 MCP Server 代码
3. **安全审查**：静态分析 + 沙箱执行 + 权限检查 + 资源限制检查
4. **热加载**：运行时动态加载生成的代码
5. **进化日志**：记录每一步进化操作的审计日志
6. **回滚管理**：进化失败时自动回滚到安全状态
7. **进化引擎**：编排完整的闭环流程

## 逻辑

### 进化闭环流程

```
evolve(capability, context)
  → GapAnalyzer.analyze_gap()          # 识别能力缺口
  → GapAnalyzer.four_layer_filter()    # 四层筛选确定最优方案
  → CodeGenerator.generate_*()          # 生成代码
  → CodeGenerator.validate_contract()   # 契约校验（AST）
  → SecurityReviewer.review()           # 安全审查
  → HotLoader.load_plugin()             # 热加载
  → EvolutionLog.log_record()           # 记录日志
  → 失败时 RollbackManager.rollback()   # 自动回滚
```

### 四层筛选策略

1. **TOOL 层**：搜索已有工具是否能满足需求
2. **CONFIG 层**：检查是否可通过配置变更满足
3. **PLUGIN 层**：检查是否有可安装的插件满足
4. **CORE 层**：需要核心代码修改（最高成本）

### 安全审查流程

1. 静态分析（危险导入、危险模式、代码注入）
2. 沙箱执行（受限环境 + 超时控制）
3. 权限检查（声明权限 vs 允许权限）
4. 资源限制（死循环、大内存分配检测）

## 结构

### 文件清单

| 文件 | 用途 |
|------|------|
| `__init__.py` | 包导出，提供 create_evolution_engine() 工厂函数 |
| `types.py` | 类型定义（枚举、数据类） |
| `gap_analyzer.py` | 能力缺口分析 + 四层筛选 |
| `code_generator.py` | BuiltinTool / MCP Server 代码生成 + 契约校验 |
| `security_reviewer.py` | 安全审查（静态分析 + 沙箱 + 权限 + 资源限制） |
| `hot_loader.py` | 运行时热加载（整合已有热加载设施） |
| `evolution_log.py` | 进化日志（审计记录） |
| `rollback_manager.py` | 回滚管理器（整合已有回滚设施） |
| `engine.py` | 进化引擎（编排完整闭环） |

### 依赖关系

```
engine.py
  ├── gap_analyzer.py
  ├── code_generator.py
  ├── security_reviewer.py
  ├── hot_loader.py
  ├── evolution_log.py
  └── rollback_manager.py
        └── 复用 src/pipeline/rollback.py 的经验
```

### 外部依赖

- `src/tools/registry.py` - ToolRegistry（工具注册中心）
- `src/tools/types.py` - Tool 类型定义
- `src/tools/builtin/base.py` - BuiltinTool 基类
- `src/tools/interfaces.py` - 工具接口定义
