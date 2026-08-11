# Isolation 模块文档

## 需求

隔离执行环境模块，为 Agent OS 的工具执行提供安全隔离能力：

1. **隔离级别**：支持 Docker 容器隔离和宿主机直接执行两种模式
2. **自动决策**：根据操作类型和任务类型自动决定执行环境
3. **容器生命周期管理**：同一任务复用容器，任务结束时销毁
4. **权限控制**：操作审批、权限策略检查、工作空间管理
5. **检查点支持**：环境状态保存与恢复

## 逻辑

### 隔离决策流程

```
工具调用请求
  → IsolationDecider.decide(operation_type, task_type)
  → 根据 OperationType 和 TaskType 判断隔离级别
  → 生成 IsolationContext（provider + level + constraints）
  → 写入 state["execution_contexts"]
```

### 执行流程

```
IsolationExecutor
  → 从 state["execution_contexts"] 查找当前工具的隔离决策
  → provider == "docker" → DockerProvider 在容器中执行命令
  → provider == "host"   → 直接调用工具函数
  → 容器复用：同一 task_id 共享容器实例
  → 任务结束：销毁对应容器
```

### 隔离级别

| 级别 | 说明 |
|------|------|
| CONTAINER | Docker 容器隔离，网络/文件系统隔离 |
| HOST | 直接在宿主机执行，无隔离 |

### 操作类型

OperationType 枚举定义可能需要隔离的操作类别（文件操作、命令执行、网络访问等）。

### 任务类型

| 类型 | 说明 | 隔离策略 |
|------|------|---------|
| PROJECT | 长期任务（数周到数月） | 长期容器 |
| MODULE | 短期任务（数天到1周） | 中期容器 |
| ATOMIC | 原子任务（数小时到1天） | 短期容器或宿主机 |

### 权限控制

```
PermissionPolicy — 权限策略定义
  → PermissionChecker — 权限检查
  → ApprovalService — 危险操作审批
```

### 工作空间管理

```
Workspace — 工作空间（隔离的文件系统视图）
  → WorkspaceLifecycle — 工作空间生命周期管理
  → Checkpoint — 工作空间检查点（保存/恢复）
```

## 结构

### 文件清单

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `types.py` | IsolationLevel, TaskType, OperationType, IsolationContext, IsolationEnvironment, ExecutionResult, EnvironmentStatus | 核心类型定义 |
| `decider.py` | IsolationDecider | 隔离决策器（根据操作/任务类型决定隔离级别） |
| `manager.py` | IsolationManager | 隔离环境管理器（全局入口，线程安全） |
| `executor.py` | IsolationExecutor | 隔离执行器（Docker/宿主机分发执行） |
| `policy.py` | — | 隔离策略定义 |
| `permission_policy.py` | PermissionPolicy | 权限策略 |
| `permission_checker.py` | PermissionChecker | 权限检查器 |
| `approval.py` | — | 审批流程 |
| `tools.py` | — | 隔离工具集 |
| `checkpoint.py` | Checkpoint | 检查点（环境状态保存/恢复） |
| `workspace.py` | Workspace | 工作空间管理 |
| `workspace_lifecycle.py` | WorkspaceLifecycle | 工作空间生命周期 |
| `providers/base.py` | IsolationProvider | 隔离提供者抽象基类 |
| `providers/docker_provider.py` | DockerProvider | Docker 容器隔离提供者 |
| `providers/host_provider.py` | HostProvider | 宿主机执行提供者 |

### 子目录

| 目录 | 说明 |
|------|------|
| `providers/` | 隔离提供者实现（Docker / Host） |

### 依赖

- `docker`（可选） — Docker SDK for Python
- `pyyaml` — 配置文件解析
- Python 标准库：asyncio, dataclasses, enum, pathlib, json
