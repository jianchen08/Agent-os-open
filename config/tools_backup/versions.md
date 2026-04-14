# task_submit 工具版本记录

## v2 - 简化版本（推荐使用）
- 日期：2026-03-14
- 变更：删除 workflow 相关描述，只保留 agent 模式
- 文件：src/tools/builtin/task_submit.py
- Token 消耗：减少约 500 tokens

## v1 - 完整版本（包含 workflow）
- 日期：2026-03-14 之前
- 变更：完整版本，包含 agent 和 workflow 两种模式
- 备份文件：config/tools_backup/task_submit_v1_full.py

---

## 使用方式

### 当前使用 v2（推荐）
直接使用 `src/tools/builtin/task_submit.py`

### 切换到旧版本 v1
```bash
# 复制 v1 版本覆盖当前文件
copy config\tools_backup\task_submit_v1_full.py src\tools\builtin\task_submit.py
```

### 恢复 workflow 功能
从 v1 版本中复制以下内容到当前版本：
1. `target_type` 的 enum 值：`"workflow"`
2. `target_id` 的 Workflow 相关描述
3. `workflow_inputs` 参数定义
4. `execute` 方法中的 workflow 处理逻辑

---

## 版本对比

| 项目 | v1 | v2 |
|-----|----|----|
| target_type | agent, workflow | agent |
| target_id 描述 | Agent + Workflow | 仅 Agent |
| workflow_inputs | ✅ 有 | ❌ 无 |
| 预计 token 消耗 | ~2000 | ~1500 |
