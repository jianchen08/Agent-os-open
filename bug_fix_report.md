# Bug修复报告：task_manage cancel 无法取消 running 状态任务

## 问题描述

调用 `task_manage` 工具的 `cancel` 操作时，无法取消正在运行中（running 状态）的任务，执行cancel操作会报错。

## Bug根因分析

### 定位过程

1. **状态机排查**：检查 `src/tasks/state_machine.py` 第109行，`running` 状态的合法转换列表中明确包含 `"cancelled"`，状态机无问题。

2. **service 层排查**：检查 `src/tasks/service.py` 的 `cancel_task()` 方法（第468行），该方法直接设置 `task.status = TaskStatus.CANCELLED`，不经过状态机校验，逻辑正常。

3. **Tool 层排查**：检查 `src/tools/builtin/task/tool.py` 的 `_cancel_task()` 方法（第1037行），发现关键问题。

### 根因

**文件**：`src/tools/builtin/task/tool.py`，原第1100行

**错误代码**：
```python
self._cancel_pipeline_recursive(task_id)
```

**`_cancel_pipeline_recursive`** 方法定义在 `TaskService` 类上（`src/tasks/service.py` 第838行），但代码中使用了 `self`（即 `TaskTool` 实例）来调用。`TaskTool` 类及其基类 `BuiltinTool` 均未定义该方法。

### 为什么只有 running 状态的任务受影响

`_cancel_pipeline_recursive` 的作用是递归取消任务关联的运行中 asyncio 管道。对于 running 状态的任务，这步操作是必要的（需要停止正在执行的管道）。当 `self._cancel_pipeline_recursive(task_id)` 抛出 `AttributeError` 后，异常被第1127行的通用 `except Exception as e` 捕获，返回"取消任务失败"错误，导致整个 cancel 操作失败。

对于非 running 状态的任务（如 pending、suspended），虽然代码路径相同也会触发该 AttributeError，但由于这类任务通常没有活跃管道需要取消，此调用本应是 no-op。不过严格来说，该 Bug 对所有状态都存在，只是对 running 任务影响最为显著。

## 修复方案

将 `self._cancel_pipeline_recursive(task_id)` 改为通过 `service` 实例调用：`service._cancel_pipeline_recursive(task_id)`。

`service` 变量已在第1059行通过 `self._get_task_service()` 获取，且后续第1101行的 `service.cancel_task_cascade()` 也通过同一实例调用，修复方式与现有代码风格一致。

## 修改内容

**文件**：`src/tools/builtin/task/tool.py`

**修改位置**：第1098-1100行（原代码）

**修改前**：
```python
# BUG-FIX-fix_20260514_cancel_cascade:
# 级联取消所有子任务，避免子任务管道继续执行
self._cancel_pipeline_recursive(task_id)
```

**修改后**：
```python
# BUG-FIX-fix_20260531_cancel_pipeline_recursive:
# 问题根因: _cancel_pipeline_recursive 是 TaskService 的方法，
#           但代码中用 self（TaskTool实例）调用，导致 AttributeError，
#           使 running 状态的任务无法被取消。
# 修复方案: 改为通过 service 实例调用该方法。
# 修复日期: 2026-05-31
service._cancel_pipeline_recursive(task_id)
```

## 验证

- `service` 变量在同方法第1059行已初始化：`service = self._get_task_service()`
- 同方法第1101行已通过 `service` 调用其他方法：`await service.cancel_task_cascade(task_id, reason=reason)`
- `TaskService._cancel_pipeline_recursive()` 是同步方法，调用方式与原代码一致（无需 await）
- 状态机允许 `running → cancelled` 转换，修复后 cancel 操作可正常完成
