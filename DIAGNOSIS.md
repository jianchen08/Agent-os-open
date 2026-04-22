# 真实执行记录诊断报告

> 基于 2026-04-23 的实际管道执行日志和任务数据，分析具体失败原因。

## 执行概览

执行了一个三层任务树：

```
6fc706874587 (L1 主管道) "创建 e2e_time_agent"
├── e8323c15478a (L2 子任务) "创建 e2e_time_agent 时间处理 Agent"  ← FAILED
└── f3a015cdd5f1 (L2 子任务) "创建 e2e_time_agent 配置文件"       ← FAILED
```

最终结果：**全部失败**。

---

## 任务 1: 6fc706874587 — 创建 e2e_time_agent（父任务）

**文件**: `data/tasks/tree_6fc706874587/6fc706874587.yaml`
**管道日志**: `logs/pipeline_3b43082ac7dd.log` (2.7MB, 44 次迭代)
**管道记录**: `data/pipelines/3b43082ac7dd.yaml`

**最终状态**: `failed`
**错误**: `管道迭代耗尽，Agent 未完成评估`

**分析**:
- 这是 L1 主任务，由主管道直接处理
- 它提交了两个子任务（e8323c15478a 和 f3a015cdd5f1）
- 子任务都失败了，通知回传后父任务也没能继续完成
- 最终因迭代耗尽而失败

---

## 任务 2: e8323c15478a — 创建 e2e_time_agent 时间处理 Agent

**文件**: `data/tasks/tree_6fc706874587/e8323c15478a.yaml`
**管道日志**: `logs/pipeline_ddf68594a1fa.log` (2.2MB)
**管道记录**: `data/pipelines/ddf68594a1fa.yaml`

**最终状态**: `failed`
**错误**: `管道迭代耗尽，Agent 未完成评估`
**目标 Agent**: `agent_maker`
**评估指标**: `file_check`, `semantic_check`

### 问题分析

1. **workspace 路径问题**：
   - `workspace: D:/Jianguoyun/Agent os` — 任务根目录就是项目根目录
   - `file_check` 的路径: `../../{{workspace}}/generated_resource.yaml` — 路径含 `../../` 前缀
   - `semantic_check` 的参考模板: `../../config/templates/resource_generation_report_template.md` — 同样含 `../../`
   - 这些路径是从父任务的模板继承的，在子任务的 workspace 上下文中解析可能出错

2. **eval_retry_count**: `{file_check: 1, semantic_check: 1}` — 只重试了 1 次就耗尽了
   - 实际 max_retries 默认是 3，说明管道在评估重试完成前就因迭代耗尽终止了

3. **结果**: `result: null` — Agent 没有产出任何有效结果

---

## 任务 3: f3a015cdd5f1 — 创建 e2e_time_agent 配置文件

**文件**: `data/tasks/tree_6fc706874587/f3a015cdd5f1.yaml`
**管道日志**: `logs/pipeline_3fab6d1ca8bc.log` (514KB, 34 次迭代)
**管道记录**: `data/pipelines/3fab6d1ca8bc.yaml`

**最终状态**: `failed`
**结果**: `{overall_passed: false, summary: "0/1 指标通过", metrics: [{metric_id: file_check, passed: false, message: "文件不存在或无法访问"}]}`
**目标 Agent**: `general_agent`
**评估指标**: `file_check`, `format_valid`

### 详细日志追踪

#### 执行过程（34 次迭代）

```
iter 1-2:   LLM 分析任务 → 调用 file_read 查看目录结构
iter 3-6:   LLM 尝试读取 api_tester_test.yaml（错误的文件）
            反复失败: "文件不存在: D:\Jianguoyun\Agent os\.ai_workspaces\f3a015cdd5f1\config\agents\executor\api_tester_test.yaml"
iter 7-16:  LLM 继续在错误的路径中查找和操作
iter 17:    LLM 调用 task_evaluate(action="auto_complete")
            → file_check: failed (文件不存在)
            → format_valid: success
            → 结果返回给 LLM: "评估未通过，请根据以下反馈继续改进"
iter 18-34: LLM 继续改进，再次调用 task_evaluate
            → file_check 再次失败
            → eval_retry_count.file_check 达到 3 → 重试耗尽
            → 任务标记为 failed
```

### 关键 Bug 定位

#### Bug 1: Agent 在 workspace 中写文件，但 file_check 在另一个路径检查

**根因**: workspace 隔离导致路径不匹配。

- Agent 在 `.ai_workspaces/f3a015cdd5f1/` 工作目录下操作
- 但 file_check 的 `input_params.path` 是 `../../{{workspace}}/reports/{{task_id}}_report.md`
  - 这个路径模板来自 acceptance_criteria，但 `{{workspace}}` 和 `{{task_id}}` 变量可能没有被正确替换
  - 最终 file_check 去检查的路径和 Agent 实际写文件的路径不一致

**验证**: 查看 file_check 的实际检查路径 vs Agent 写入路径：

```
file_check 检查的路径: (来自 task metadata)
  action: read
  path: ../../{{workspace}}/reports/{{task_id}}_report.md
  workspace: D:\Jianguoyun\Agent os

Agent 实际写入的文件:
  .ai_workspaces/f3a015cdd5f1/config/agents/executor/test/e2e_time_agent.yaml
```

问题很明显：
- file_check 期望在 `reports/` 目录下找到 `_report.md` 文件
- Agent 把文件写到了 `config/agents/executor/test/` 目录
- **路径约定不匹配**：验收标准中的文件路径和 Agent 实际输出的路径是两套不同的约定

#### Bug 2: LLM 反复读取错误文件

日志中反复出现：
```
"文件不存在: D:\Jianguoyun\Agent os\.ai_workspaces\f3a015cdd5f1\config\agents\executor\api_tester_test.yaml"
```

Agent 在尝试读取 `api_tester_test.yaml`（另一个不相关的文件），可能是 LLM 从之前的上下文中错误地引用了这个文件名。

#### Bug 3: 评估模板变量未替换

acceptance_criteria 中的路径使用了模板变量 `{{workspace}}` 和 `{{task_id}}`：

```yaml
file_check:
  input_params:
    action: read
    path: '../../{{workspace}}/reports/{{task_id}}_report.md'
```

但在 `_get_input_params` 方法中，这些变量没有被替换，导致 file_check 使用了错误的路径。

---

## 评估管道 b43708798950 — 评估者 Agent

**管道日志**: `logs/pipeline_b43708798950.log` (728KB, 20 次迭代)
**管道记录**: `data/pipelines/b43708798950.yaml`

### 问题分析

这是一个评估者 Agent 管道（evaluation_mode=true），用于执行 semantic_check 指标。

1. **模板文件路径错误**：
   - 评估标准中的 `reference_template: ../../config/templates/resource_generation_report_template.md`
   - 模板文件实际存在于 `config/templates/resource_generation_report_template.md`
   - 但评估者在 workspace 中运行，相对路径 `../../` 解析到了错误的位置

2. **评估者反复尝试读取模板文件**：
   - 迭代 1-15: 反复尝试 `file_read("../../config/templates/...")` 失败
   - 迭代 16-20: 尝试搜索替代文件，但也没找到正确的

3. **TaskReminder 的 evaluation_mode 行为**：
   - 评估者 Agent 一直没输出 `{"evaluation_result": ...}` JSON
   - TaskReminder 应该在每次纯文本输出后注入提醒
   - 但日志显示管道最终因 "Strategy error: approach needs change" 终止
   - **说明 TaskReminder 的提醒次数可能不够，或者管道的 stuck_detector 先触发了**

---

## 总结：系统性问题

| # | 问题 | 影响 | 严重度 |
|---|------|------|--------|
| 1 | **workspace 路径与验收标准路径不匹配** | file_check 永远找不到 Agent 写的文件 | 致命 |
| 2 | **acceptance_criteria 模板变量未替换** | `{{workspace}}` `{{task_id}}` 当作字面路径使用 | 致命 |
| 3 | **评估者 Agent 的参考文件路径解析错误** | semantic_check 无法读取模板文件 | 严重 |
| 4 | **LLM 在 workspace 中迷失** | 反复操作错误文件 | 中等 |
| 5 | **迭代耗尽但无有效评估** | 任务直接标记 failed 而非评估通过/失败 | 中等 |

---

## 修复建议

### 修复 1: 模板变量替换（最高优先级）

文件 `src/tools/builtin/task_evaluate.py` 的 `_get_input_params` 方法需要替换模板变量：

```python
# 在 _get_input_params 中，替换 {{workspace}} 和 {{task_id}} 等变量
import re

def _resolve_template_vars(value, task):
    if isinstance(value, str):
        workspace = (task.metadata or {}).get("workspace", "")
        replacements = {
            "{{workspace}}": workspace,
            "{{task_id}}": task.id,
        }
        for key, val in replacements.items():
            value = value.replace(key, str(val))
    return value
```

### 修复 2: 验收标准路径与 workspace 对齐

task_submit 在自动生成 acceptance_criteria 时，file_check 的路径应该基于 Agent 实际的工作目录：

```python
# file_check 的 path 应该是相对于 workspace 的路径
# 而不是相对于项目根目录的路径
input_params:
  action: read
  path: "config/agents/executor/test/e2e_time_agent.yaml"  # 相对于 workspace
```

### 修复 3: 评估者 Agent 的工作目录

评估者 Agent 应该在项目根目录运行（而非 workspace），因为模板文件在项目根目录下：

```python
# 在 _evaluate_agent 中，评估者管道的工作目录应设为项目根目录
eval_prompt += f"\n\n工作目录: {project_root}"
```

### 修复 4: LLM 上下文注入 workspace 路径提示

在子任务的 user_input 中更明确地告知 workspace 路径：

```
当前工作目录: D:\Jianguoyun\Agent os\.ai_workspaces\f3a015cdd5f1\
所有文件操作请在此目录下进行。
验收标准中的路径是相对于此目录的。
```
