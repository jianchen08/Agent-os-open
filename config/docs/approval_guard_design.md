# 安全检查插件改造设计文档

## 一、背景与问题

### 1.1 当前问题

**问题 1：检测不拦截**

`security_check`（优先级 70）检测到危险操作时，写入以下 state 但无人读取：

```python
# 现状：只写 state，不执行任何流程控制
return {
    "security.decision": {"allowed": False, "reason": "...", "approval_required": True},
    StateKeys.APPROVAL_REQUIRED: True,
}
```

`level_guard`（优先级 65）检测到越权时同样只写 state：

```python
return {"security.level_decision": {"allowed": False, "reason": "..."}}
```

**`PipelineEngine` 在 input chain 执行完后直接执行 core，完全忽略这些 state。** 危险操作畅通无阻。

**问题 2：输入路由表解析时机错误**

```
当前流程：
  输入路由表解析(state) → (插件列表, target) → 执行 input 插件 → 执行 core
                                    ↑
                              target 在 input 之前就决定了
                              input 插件的结果无法影响 target
```

输入路由表在 input 插件执行**之前**解析 target，导致 input 插件的检测结果无法影响后续流程。这和输出路由表（在 output 执行**之后**仲裁）不对称。

### 1.2 根因

1. 插件只写 state 不执行流程控制
2. 输入路由表的 target 解析时机在 input 插件之前，无法感知 input 插件的结果

---

## 二、设计方案

### 2.1 核心原则

**检测、拦截、等待审批是同一行为的三个阶段，不可拆分。**

```
检测到危险操作 → 拦截（不放行）→ 等待审批 → 审批通过=放行 / 拒绝=继续拦
```

这三件事必须在同一个插件（`security_check`）内完成。拆分出来就面临"谁读 state"的问题，且多一层传递就多一层丢失的风险。

**唯一例外**：`level_guard` 的越权拦截不需要等审批（权限判断是确定性的），检测到就拦。

### 2.2 引擎改造：输入路由表后置解析

当前输入路由表在 input 插件之前解析，target 在插件执行前就确定了。改造为：**输入路由表拆成两步——先解析插件列表，执行完后再解析 target。**

```
改造后流程：
  输入路由表解析(state) → 插件列表
                           ↓
                      执行 input 插件
                           ↓
                      state 已更新
                           ↓
              输入路由表解析(updated_state) → target (core / end / wait)
                           ↓
                      根据 target 决定后续
```

**与输出路由表对称：**

```
输入路由：input 执行完 → 解析更新后的 state → 决定这一轮后续（core / end / wait）
输出路由：output 执行完 → 仲裁路由信号 → 决定下一轮（next_llm / next_tool / end / delegate）
```

**这样引擎不需要硬编码任何 `skip_remaining` 检查。** 流程控制完全由路由表和插件负责。

### 2.3 插件职责划分

| 插件 | 职责 | 流程控制方式 |
|------|------|-------------|
| `isolation_guard`（优先级 25） | 写入隔离环境信息到 state | 无流程控制 |
| `level_guard`（优先级 65） | 检测 Agent 层级权限，写入 `security.level_decision` | 无流程控制（由输入路由表根据 state 决定） |
| `security_check`（优先级 70） | 检测危险操作 + 写入 `security.decision` + 等待审批 | 无流程控制（由输入路由表根据 state 决定）；等待审批在插件内部 await |

### 2.4 拦截结束时的结果传递

当输入路由表判断 target=end（安全拦截、审批拒绝、越权等），core 不会执行，`RAW_RESULT` 为空。但 LLM 需要知道"工具执行失败，原因是什么"，否则无法调整后续行为。

**解决方案**：输入路由表在 target=end 时，将拦截原因写入 `RAW_RESULT`。

```yaml
input_routes:
  # 安全拦截 → 结束，并把原因写入 RAW_RESULT
  - name: security_blocked
    condition: "security.decision.get('allowed') == False"
    target: end
    plugins: []
    result: "工具执行被拦截: {security.decision.reason}"

  # 越权拦截 → 结束
  - name: level_blocked
    condition: "security.level_decision.get('allowed') == False"
    target: end
    plugins: []
    result: "权限不足，无法调用工具: {security.level_decision.reason}"

  # 正常执行
  - name: normal_execution
    condition: "core_type == 'tool_execute'"
    target: core
    plugins:
      - isolation_guard
      - level_guard
      - security_check

  # LLM 调用
  - name: llm_call
    condition: "core_type == 'llm_call'"
    target: core
    plugins: []
```

引擎在 target=end 时，读取匹配条目的 `result` 模板，用 state 中的值填充后写入 `StateKeys.RAW_RESULT`。

### 2.5 输入路由表配置示例（无拦截）

简单场景下不需要 result 模板，与现有配置兼容：

```yaml
input_routes:
  - name: normal_execution
    condition: "core_type == 'tool_execute'"
    target: core
    plugins:
      - isolation_guard
      - level_guard
      - security_check
```

### 2.6 `security_check` 的三种行为

```
输入
  │
  ├─ 非 tool_execute → 放行（return，写入 allowed=True）
  │
  ├─ tool_execute + 无危险操作 → 放行（写入 allowed=True）
  │
  ├─ tool_execute + 危险操作（block 规则）→ 写入 allowed=False（输入路由表 target=end）
  │
  └─ tool_execute + 危险操作（needs_approval 规则）→ await 等待审批
       ├─ 审批通过 → 写入 allowed=True → 放行
       └─ 审批拒绝 → 写入 allowed=False → 输入路由表 target=end
```

**等待审批的机制**：`execute()` 内部 `await` 一个异步函数，该函数向外部（Channel/WebSocket）发送审批请求，然后等待回复。在等待期间，`execute()` 不返回，后续插件不执行。审批通过后该函数返回，插件写入 `allowed=True` 并 return，输入路由表解析后 target=core，管道继续。

---

## 三、`security_check` 改造要点

### 3.1 现有返回方式（问题）

```python
# 危险操作 block
return {"security.decision": {"allowed": False, "reason": "...", "tool": tool_name}}

# 危险操作需要审批
return {
    "security.decision": {"allowed": False, "reason": "...", "tool": tool_name, "approval_required": True},
    StateKeys.APPROVAL_REQUIRED: True,
}
```

### 3.2 改造后返回方式

```python
# 危险操作 block → 写入 allowed=False，输入路由表根据 state 决定 target=end
return {"security.decision": {"allowed": False, "reason": "...", "tool": tool_name}}

# 危险操作需要审批 → 插件内部 await，审批通过/拒绝后写入对应结果
# 审批通过
return {"security.decision": {"allowed": True, "reason": "approved", "tool": tool_name}}
# 审批拒绝
return {"security.decision": {"allowed": False, "reason": "rejected", "tool": tool_name}}
```

### 3.3 等待审批的 state 设计

审批通过后，外部系统在恢复管道时写入标记，`security_check` 在执行前检查：

```python
# 外部（Channel/WebSocket）审批通过后写入
ctx.state["security.approval_result"] = {"approved": True, approver: "user_id", ...}

# security_check execute() 开头检查
if ctx.state.get("security.approval_result", {}).get("approved"):
    return {"security.decision": {"allowed": True, "reason": "previously approved"}}
```

---

## 四、`level_guard` 改造要点

`level_guard` 不需要改动代码逻辑，只需要**输入路由表**配置对应条件即可。

`level_guard` 继续写入 `security.level_decision`，输入路由表在 input 插件执行后检查这个 state：

```yaml
# 输入路由表中：越权 → 结束
- name: level_blocked
  condition: "security.level_decision.get('allowed') == False"
  target: end
  plugins: []
```

---

## 五、引擎改造要点

### 5.1 现有引擎循环（问题）

```python
# 当前：路由表在 input 之前解析，target 提前确定
plugin_names, target = self.input_route_table.resolve(state)
if target == "end": break
if target == "wait": break
# 执行 input 插件（但 target 已经确定了，input 的结果无法影响 target）
input_results = await input_chain.execute(input_ctx)
# 直接执行 core（不管 input 插件有没有拦截）
core_result = await core_plugin.execute(core_ctx)
```

### 5.2 改造后引擎循环

```python
# 改造后：输入路由表拆成两步
# 第一步：解析插件列表
plugin_names = self.input_route_table.resolve_plugins(state)
# 执行 input 插件
input_results = await input_chain.execute(input_ctx)
# 第二步：用更新后的 state 解析 target
target, matched_entry = self.input_route_table.resolve_target(state)
if target == "end":
    # 将拦截原因写入 RAW_RESULT，让 LLM 知道为什么结束
    if matched_entry and matched_entry.result:
        result_msg = matched_entry.format_result(state)
        state[StateKeys.RAW_RESULT] = result_msg
    state[StateKeys.ENDED] = True
    break
if target == "wait":
    self._suspended_state = dict(state)
    break
# target == "core"：执行 core
core_result = await core_plugin.execute(core_ctx)
```

### 5.3 `InputRouteTable` 接口改造

```python
class InputRouteTable:
    def resolve_plugins(self, state: dict[str, Any]) -> list[str]:
        """根据 state 解析需要执行的 input 插件列表。"""
        ...

    def resolve_target(self, state: dict[str, Any]) -> tuple[str, InputRouteEntry | None]:
        """根据 state 解析路由目标和匹配的条目。

        Returns:
            (target, matched_entry)：
            - target: "core" / "end" / "wait"
            - matched_entry: 匹配的路由条目（用于读取 result 模板）
        """
        ...

class InputRouteEntry:
    result: str | None  # 拦截原因模板，如 "工具执行被拦截: {security.decision.reason}"

    def format_result(self, state: dict[str, Any]) -> str:
        """用 state 中的值填充 result 模板。"""
        if not self.result:
            return ""
        return self.result.format(**state)
```

原有的 `resolve()` 方法可以保留为兼容方法，内部调用这两个新方法。

---

## 六、涉及文件

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/pipeline/engine.py` | 修改 | 输入路由表拆成两步：先解析插件，input 执行后再解析 target |
| `src/pipeline/route.py` | 修改 | `InputRouteTable` 拆分为 `resolve_plugins()` + `resolve_target()` |
| `src/plugins/input/security_check.py` | 修改 | block/needs_approval 写入 state；needs_approval 内部 await 等待审批 |
| `src/plugins/input/level_guard.py` | 无需修改 | 继续写入 state，流程控制由路由表负责 |
| `config/pipelines/default.yaml` | 修改 | 输入路由表添加安全拦截条件 |
| `config/pipelines/l2-subtask.yaml` | 修改 | 输入路由表添加安全拦截条件 |
| `docs/project/charter.md` | 更新原则 | 反映"检测+拦截+等待审批不可拆分"原则 |

---

## 七、测试要点

| 场景 | 期望结果 |
|------|---------|
| 非 tool_execute | 放行，target=core |
| tool_execute + 无危险操作 | 放行，target=core |
| tool_execute + block 规则匹配 | target=end |
| tool_execute + needs_approval 规则匹配 | await 等待审批，通过后 target=core，拒绝后 target=end |
| level_guard 越权检测 | target=end |
| 审批通过后恢复执行 | target=core |
| pause_guard 暂停 | target=wait |
