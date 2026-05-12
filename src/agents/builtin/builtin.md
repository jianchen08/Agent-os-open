# 内置 Agent 组件

## 需求
### 职责
提供系统内置 Agent 配置的加载和管理能力，支持从 YAML 配置文件动态加载 Agent 定义。

### 对外接口
- 输入：配置文件目录路径（可选）
- 输出：AgentConfig 配置对象、Agent 名称列表

### 依赖
- 依赖模块：`src.agents.types`（AgentConfig、AgentType）
- 依赖库：PyYAML

## 逻辑
### 流程设计
1. 初始化时指定配置目录（默认 `config/agents/`）
2. 递归扫描目录下所有 `.yaml` 配置文件
3. 解析 YAML 内容，转换 agent_type 字符串为枚举
4. 创建 AgentConfig 对象并缓存
5. 提供按名称、类型、能力查询接口

### 数据流向
```
YAML 文件 → 解析 → AgentConfig → 缓存 → 查询接口
```

### 配置设计
#### 组件配置
| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| config_dir | 配置文件目录 | config/agents/ |

## 结构
### 文件清单（代码文件 - 具体接口）
#### loader.py
职责：内置 Agent 配置加载器
暴露接口：
- `BuiltinAgentLoader(config_dir: Path | None) -> None`：加载器类
  - `load_all() -> dict[str, AgentConfig]`：加载所有 Agent 配置
  - `load(agent_name: str) -> AgentConfig | None`：加载指定 Agent 配置
  - `list_agents(agent_type: AgentType | None) -> list[str]`：列出 Agent 名称
  - `get_system_agents() -> dict[str, AgentConfig]`：获取系统 Agent
  - `get_builtin_agents() -> dict[str, AgentConfig]`：获取内置 Agent
  - `get_agent_by_capability(capability: str) -> list[AgentConfig]`：按能力查询
  - `reload() -> dict[str, AgentConfig]`：重新加载配置
- `get_loader() -> BuiltinAgentLoader`：获取全局加载器实例
- `load_agent(agent_name: str) -> AgentConfig | None`：快捷加载方法
- `load_all_agents() -> dict[str, AgentConfig]`：快捷加载所有方法
- `AgentNames`：预定义 Agent 名称常量类

#### __init__.py
职责：模块导出
暴露接口：
- `BuiltinAgentLoader`：加载器类
- `AgentNames`：名称常量类
- `get_loader() -> BuiltinAgentLoader`
- `load_agent(agent_name: str) -> AgentConfig | None`
- `load_all_agents() -> dict[str, AgentConfig]`

### 测试策略
#### 组件测试
- 单元测试：配置加载、类型转换、缓存机制
- 集成测试：与实际配置文件的加载测试
- 边界测试：空目录、无效配置、缺失字段

## 实现
→ 见代码文件
