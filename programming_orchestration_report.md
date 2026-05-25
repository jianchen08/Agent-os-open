# 编程编排报告：压缩器增强 — 保留区和长期记忆提取功能

## 任务概述

为现有对话压缩器（`src/memory/context_compressor.py` 中的 `ContextCompressor` 类）添加两个新功能：
1. **保留区（PreservedZone）**：独立存储关键信息，每轮覆盖更新
2. **长期记忆提取（MemoryExtraction）**：从对话中提取可持久化的记忆项

## 执行路径

**路径 A：编码开发**（编码 → 测试 → 评估）

## 变更文件清单

### 新增/修改文件

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `src/memory/compressor/models.py` | 修改 | 新增 `PreservedZone` 和 `MemoryExtraction` 数据模型 |
| `src/memory/context_compressor.py` | 修改 | 新增 `PRESERVED_PROMPT`、`MEMORY_EXTRACTION_PROMPT` 类常量；新增 `extract_preserved`、`extract_long_term_memory` 异步方法；新增 `PreservedZone`、`MemoryExtraction` 的 import |
| `src/memory/compressor/__init__.py` | 修改 | 导出 `PreservedZone`、`MemoryExtraction` |
| `tests/suites/memory/test_preserved_and_memory.py` | 新增 | 32 个单元测试用例 |

## 功能实现详情

### 功能1：保留区（PreservedZone）

**数据模型** (`models.py`)：
- `user_requirements: str` — 用户原始需求/指令
- `key_decisions: str` — 关键决策记录
- `execution_plan: str` — 当前执行计划（只保留最新版）
- `constraints: str` — 活跃约束条件
- `pending_tasks: str` — 未完成任务状态

**提取方法** (`context_compressor.py`)：
- `async extract_preserved(messages, previous_l1, user_message, old_preserved) -> PreservedZone`
- 通过独立 LLM 调用（`PRESERVED_PROMPT`）从完整上下文中重新识别提取
- 异常安全：失败时返回空 `PreservedZone()`，不抛出异常
- 保留区独立于压缩块存储，避免拼接冲突

### 功能2：长期记忆提取（MemoryExtraction）

**数据模型** (`models.py`)：
- `user_profile_updates: str` — 写入 memory(tags=["user_profile"])
- `project_knowledge_updates: str` — 写入 memory(tags=["project_knowledge"])
- `experience_updates: str` — 写入 memory(tags=["experience"])

**提取方法** (`context_compressor.py`)：
- `async extract_long_term_memory(messages, previous_l1, user_message) -> MemoryExtraction`
- 通过独立 LLM 调用（`MEMORY_EXTRACTION_PROMPT`）从对话中提取记忆项
- 有值就填，没值就空，不做去重判断
- 异常安全：失败时返回空 `MemoryExtraction()`，不抛出异常

### 压缩流程（更新后）

```
旧保留区 + 压缩块序列 + 当前对话 → 压缩器 → {
    新保留区(覆盖) + 新压缩块(追加，现有逻辑不动) + 提取字段(写入memory)
}
```

## 硬约束满足情况

| 约束 | 状态 | 说明 |
|------|------|------|
| 压缩块的现有功能不能修改 | ✅ | `COMPRESS_PROMPT` 和 `compress_all` 的 l1/l2/keywords 逻辑完全未动 |
| 只能在压缩块之外添加新字段和功能 | ✅ | 新常量和方法独立于现有压缩块逻辑 |
| 保留区是覆盖更新，不是追加 | ✅ | 每轮重新识别提取，内容刷新到最新状态 |
| 长期记忆提取字段直接调用现有 memory 工具存储 | ✅ | 提取方法只产出数据，存储由调用方负责 |

## 测试覆盖

共 32 个测试用例，覆盖：

1. **PreservedZone 数据模型**（3个）：5字段验证、默认值、构造
2. **MemoryExtraction 数据模型**（3个）：3字段验证、默认值、构造
3. **extract_preserved 方法**（11个）：空消息、正常提取、空响应、JSON解析失败、异常安全、参数传递、自动提取用户消息、部分字段缺失
4. **extract_long_term_memory 方法**（9个）：空消息、正常提取、空响应、JSON解析失败、异常安全、参数传递、自动提取用户消息、部分字段缺失
5. **模块导出**（4个）：模型类正确导入导出
6. **硬约束验证**（3个）：compress_all 返回值结构不变、COMPRESS_PROMPT 未修改

## 执行过程

| 阶段 | Agent | 任务ID | 结果 |
|------|-------|--------|------|
| A1 编码 | code_writer_agent | 73f25328f8c5 | models.py ✅, __init__.py ✅, context_compressor.py 初始缺失方法 |
| A1 修复 | code_writer_agent | 3c2aeac8c8ed | context_compressor.py 补充方法 ✅ |
| A3 测试 | test_debug_agent | 5c1cc75a5c97 | 32 测试全部通过 ✅ |
