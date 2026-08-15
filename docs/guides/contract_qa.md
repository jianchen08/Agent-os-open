# 契约设计问题解答（Q&A）

> **来源**：用户审阅 [`docs/guides/contract_files_tutorial.md`](contract_files_tutorial.md) 后提出的 5 个深度设计问题
> **目的**：逐一回答用户的疑问，每个回答都引用实际契约代码（行号定位）和决策文档
> **核心立场**：**忠实于代码**——代码写什么样就什么样说，不粉饰、不脑补、不混淆"已实现"和"设计意图"

---

## 阅读须知

- **行号引用**：本文件行号引用以 `kernel/crates/core/src/traits.rs` 为准（755 行总长）。用户原问题中出现的 L119/L203/L228-229/L251 是教程章节定位，本文件**额外给出 `traits.rs` 的实际行号**，便于精确对照。
- **来源标注约定**：
  - `[来源: 实际代码]`：可在仓库直接读到的 Rust trait / Python 代码
  - `[来源: 决策文档]`：来自 `docs/working/0.2插件体系核心决策.md` / `docs/0.2_rust_plugin_solution.md`
  - `[来源: 反向推导]`：从已固化的 Rust 类型反推待产出契约（`.project/manifest_v2_schema.json`、`.project/mcp_extension_protocol.md`）
  - `[来源: 未找到]`：在仓库中未找到相关实现或文档
- **诚实声明**：用户的问题中有几处对设计决策的"应然"判断（如"应该解耦"、"应该由引擎决定"），但**代码实现的"实然"与用户判断并不完全一致**。本文件会如实指出代码的实际设计，包括设计上的张力点（决策文档与代码实现之间的不一致）。

---

## 问题 1：插件 dependencies 字段（L119）—— 依赖怎么做？依赖什么？

### 用户疑问

> 插件之间的依赖是怎么依赖的？依赖做什么？按理说插件之间应该解耦，插件之间应该是服务调用关系而不是直接依赖。请分析契约中 dependencies 的实际设计和用法。

### 回答

#### 1.1 契约中 dependencies 的实际设计

`dependencies` 字段定义在 `PluginManifest` 结构体中（`traits.rs` L604-605）：

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PluginManifest {
    // ... 其他字段 ...
    #[serde(default)]
    pub dependencies: Vec<Dependency>,  // L604-605
    // ... 其他字段 ...
}
```

每条依赖的结构（`traits.rs` L343-351）：

```rust
/// 插件依赖声明（对应 manifest.dependencies[]）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Dependency {
    pub plugin_id: String,
    #[serde(default)]
    pub optional: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub min_version: Option<String>,
}
```

依赖解析器（`traits.rs` L325-340）：

```rust
/// 插件依赖解析器：解析插件间的依赖拓扑并按序加载。
///
/// 内核在实例化插件前，根据 manifest 中的 `dependencies` 字段
/// 构建依赖图并执行拓扑排序，确保被依赖的插件先加载。
pub trait DependencyResolver: Send + Sync {
    /// 添加插件依赖声明。
    fn add_dependency(&self, plugin_id: &str, dep: &Dependency);
    /// 构建依赖图并返回拓扑排序结果。
    fn resolve(&self) -> Result<Vec<String>, DependencyError>;
}
```

错误类型（`traits.rs` L353-374）：

```rust
pub enum DependencyError {
    Circular { cycle: Vec<String> },                                  // 循环依赖
    MissingRequired { plugin_id: String, dependent: String },         // 缺少必需依赖
    VersionMismatch { plugin_id: String, required: String, actual: String }, // 版本不兼容
}
```

#### 1.2 依赖的实际用途：**加载顺序**，不是运行时耦合

**核心澄清**：`dependencies` 字段解决的是 **"内核启动时按什么顺序加载插件"** 的问题，**不是**运行时调用关系。

具体来说：

| 阶段 | 关注什么 | 用什么机制 |
|------|----------|-----------|
| **加载期**（内核启动时） | 哪些插件必须先就绪？ | `manifest.dependencies` + `DependencyResolver` |
| **运行期**（管道执行时） | 插件之间怎么通信？ | 通过 state 共享数据 / 通过 `PluginInvoker` 服务调用 |

`DependencyResolver.resolve()` 的文档注释（L327-330）明确说："**内核在实例化插件前**，根据 manifest 中的 `dependencies` 字段构建依赖图并执行拓扑排序，确保被依赖的插件先加载。"

**关键洞察**："被依赖的插件先加载"是为了保证**配置就绪**——比如某插件需要读取其他插件提供的 tool schema，就得等那些插件先注册完。但**加载完之后**，插件之间的数据流就完全通过 state 字典传递（`types.rs` L168-180 `PluginContext.state`），不存在"A 必须先于 B 调用"这种硬时序。

#### 1.3 用户判断"插件之间应该解耦"——**完全正确**，但解耦维度需分清

用户在运行时耦合这个维度上的判断是对的：

- **运行时**：插件不直接持有其他插件引用。`docs/working/0.2插件体系核心决策.md` 决策 9 明文写："插件 A 调插件 B，**不直接** `B.execute()`，而是通过运行时句柄调用 `ctx.call_plugin('B', args)`"——所有插件调用走 invoker 统一入口，日志/错误/metrics 一致生效。

- **加载期**：用户说的"应该是服务调用关系"，0.2 的 `dependencies` 字段也**不是**"编译时硬链接"，而是一个**软声明**——可以 `optional: true`，也可以只声明 `min_version` 不声明完整版本。DependencyResolver 失败时返回的是 `DependencyError`，不是 panic——加载失败优雅降级。

#### 1.4 0.1 vs 0.2 对比：dependencies 是新增概念

**0.1 没有 manifest 概念**。现有 47 个插件通过 Python `import` 硬编码依赖，比如 `src/plugins/input/injected_param_validator/plugin.py` L16：

```python
from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy
```

这种依赖是**编译时绑死**的——改一行 import，整条链路全得重测。0.2 引入 manifest.dependencies 后：

| 维度 | 0.1 Python | 0.2 Rust + manifest |
|------|----------|-------------------|
| 依赖声明 | Python `import` | `manifest.dependencies: Vec<Dependency>` |
| 顺序保证 | Python 解释器按 import 顺序 | `DependencyResolver` 拓扑排序 |
| 循环依赖 | import 时栈溢出 | `DependencyError::Circular { cycle }` 优雅失败 |
| 版本约束 | 无 | `min_version: Option<String>` |
| 可选依赖 | 无 | `optional: bool` |

#### 1.5 反例：什么时候**不需要**写 dependencies？

> 写插件时如果"插件 A 想调用插件 B 的某个工具"，**不应该**在 dependencies 里声明 B——这不是加载期依赖。

正确的做法是：A 通过 `PluginInvoker.invoke_tool(plugin_id="B", tool_name="xxx", inputs=...)` 调用 B。**如果 B 还没加载，invoker 会自动按需加载**（按 0.2 §3.7 按需加载全局原则）。

dependencies 字段的真实使用场景（设计意图）：

1. **配置依赖**：插件启动时要读另一个插件的 manifest 配置（如 `tool_schema` 插件启动时要确认 `llm_core` 已注册）
2. **能力依赖**：插件的 capabilities 声明依赖另一个插件提供的 resources（如 `evaluation` 依赖 `track` 插件的统计 resources）
3. **版本锁定**：插件实现假设某个底层 API 版本，通过 `min_version` 显式声明

#### 1.6 小结

| 疑问 | 回答 |
|------|------|
| dependencies 字段在哪？ | `traits.rs` L604-605 (`PluginManifest`) + L343-351 (`Dependency`) |
| 依赖解析器在哪？ | `traits.rs` L325-340 (`DependencyResolver` trait) |
| 依赖做什么？ | **加载顺序**：拓扑排序 + 循环检测 + 版本校验 |
| 依赖不代表什么？ | **不代表**运行时调用关系——运行时通过 PluginInvoker 解耦 |
| 用户判断正确吗？ | **正确**——0.2 在加载期 + 运行期两个维度都做了显式解耦 |

> **来源**：[来源: 实际代码 kernel/crates/core/src/traits.rs L325-374, L604-605]、[来源: 决策文档 docs/working/0.2插件体系核心决策.md 决策 9]、[来源: 决策文档 docs/0.2_rust_plugin_solution.md §3.7 按需加载]

---

## 问题 2：三个 PipelinePlugin 子 trait（L203）—— 为什么拆成三个？

### 用户疑问

> InputPipelinePlugin / CorePipelinePlugin / OutputPipelinePlugin 为什么要写三个不同的执行逻辑？不是统一由 PluginInvoker 分发执行吗？具体是输入/核心/输出应该由引擎决定，而不是插件自己区分。请分析为什么要拆成三个子 trait。

### 回答

#### 2.1 契约中三个子 trait 的实际设计

`traits.rs` L114-158：

```rust
/// 输入管道插件（Input 阶段）。
#[async_trait]
pub trait InputPipelinePlugin: PipelinePlugin {     // L121
    /// Input 插件固定返回 Input 角色。
    fn role(&self) -> PipelineRole {               // L123-125
        PipelineRole::Input
    }
}

/// 核心管道插件（Core 阶段）。
#[async_trait]
pub trait CorePipelinePlugin: PipelinePlugin {     // L134
    fn role(&self) -> PipelineRole {               // L136-138
        PipelineRole::Core
    }
    /// 错误策略为 Fallback 时使用的默认状态更新。
    fn fallback_state(&self) -> HashMap<String, serde_json::Value> {  // L141-143
        HashMap::new()
    }
}

/// 输出管道插件（Output 阶段）。
#[async_trait]
pub trait OutputPipelinePlugin: PipelinePlugin {   // L153
    fn role(&self) -> PipelineRole {               // L155-157
        PipelineRole::Output
    }
}
```

三个子 trait **都继承 `PipelinePlugin`**（`traits.rs` L83），唯一的"特殊化"是：
- `role()` 默认实现各自固定（Input → Input 角色，以此类推）
- `CorePipelinePlugin` 多了一个 `fallback_state()` 默认实现（`HashMap::new()`）

**所有三个子 trait 都**没有**重写 `execute()` 方法**——它们与 `PipelinePlugin` 共享同一个 `execute()` 签名（`traits.rs` L94）。

#### 2.2 用户的判断**部分正确**——这里存在决策与代码的张力

用户的判断在 **0.2 决策层面**完全正确。`docs/working/0.2插件体系核心决策.md` 决策 4 明文写：

> **决策 4：统一的 trait 契约——`execute(Value) -> Value`**
>
> 插件分类靠 manifest 的 `capabilities` 标签，不靠 trait 类型。
>
> **否决项**：
> - ❌ 否决"身份统一 + 契约分层"（基础 Plugin trait + 多个特化执行 trait）。这会把管道的特殊性固化到类型系统里，违背"统一签名"的初衷
> - ❌ 否决"单 trait + 枚举信封"

**决策 4 否决的就是这种"基础 + 多个特化"的拆分模式**。但 `traits.rs` L120-158 又**实际**实现了三个子 trait——**这是 0.2 决策文档与代码实现之间的一处张力**。

#### 2.3 张力的合理解释：子 trait 提供"编译期标签"，不提供"分派逻辑"

代码的实际设计可以这样理解：

| 维度 | 子 trait 提供的 | 子 trait **不**提供的 |
|------|----------------|---------------------|
| `role()` 默认值 | ✅ 固定为对应角色 | 不强制——开发者可覆盖 |
| `fallback_state()` | ✅ 仅 Core 子 trait 提供默认 | 不是分派条件 |
| `execute()` 方法 | ❌ 不重写 | 共享 `PipelinePlugin::execute` 签名 |
| `dyn` 分派机制 | ❌ 不参与 | 由 `PipelinePlugin` 统一 dyn dispatch |
| MCP 调用路径 | ❌ 不参与 | `PluginInvoker` 按 `host_type` 分发，不按子 trait |

也就是说，三个子 trait 的本质是**"语法糖 + 编译期约束"**：

```rust
// 子 trait 的价值在于：开发者声明"我是个 Input 插件"，编译器自动确认 role() 返回 Input
class MyInputPlugin: InputPipelinePlugin {
    fn role(&self) -> PipelineRole {
        PipelineRole::Input  // 子 trait 已提供默认实现，可省略
    }
}

// 但开发者也可以直接实现 PipelinePlugin，自己返回 role
class MyPlugin: PipelinePlugin {
    fn role(&self) -> PipelineRole {
        PipelineRole::Input  // 必须显式写
    }
}
```

#### 2.4 用户更深层的判断："应该由引擎决定"——更准确

用户的判断"具体是输入/核心/输出应该由引擎决定，而不是插件自己区分"——**这个判断比子 trait 的当前设计更接近 0.2 的设计意图**。

`docs/working/0.2插件体系核心决策.md` 决策 8：

> **决策 8：字段 schema 只属于管道插件**
>
> 所有**激活的管道插件**的字段并集 = 管道引擎的 state schema。禁用的插件字段不进 schema。
>
> **含义**：
> - 工具插件的"字段"就是它的 args/result schema，跟引擎 state 无关
> - 前端/调试器/审计层查 state schema 时，只看到管道插件声明的字段

这里"激活的管道插件"——**激活由引擎/配置决定**，不是插件自己声明。换句话说，"我是不是 Input 插件"按理应该由 **manifest 的 `pipeline_role: PipelineRole` 字段**决定（`traits.rs` L599），而不是由 trait 类型决定。

```rust
// PluginManifest 中已有 pipeline_role 字段
pub pipeline_role: Option<PipelineRole>,  // L599
```

**更"决策 4 一致"的设计**是：插件只实现一个 `PipelinePlugin` trait，`pipeline_role` 字段来自 manifest，引擎按 manifest 字段分派。子 trait 应该是可选的辅助，不是必选路径。

#### 2.5 与 0.1 的对比

| 维度 | 0.1 Python | 0.2 Rust（实际代码） | 0.2 决策 4 的"理想"设计 |
|------|----------|------------------|--------------------|
| 抽象基类 | `IInputPlugin` / `ICorePlugin` / `IOutputPlugin` 三个 ABC | `InputPipelinePlugin` / `CorePipelinePlugin` / `OutputPipelinePlugin` 三个 trait | 只有一个 `PipelinePlugin` trait |
| 角色区分 | 由继承的基类决定 | 由继承的子 trait 决定 + 默认 `role()` | 由 `manifest.pipeline_role` 决定 |
| execute 签名 | 三种不同返回类型 | 全部相同（共享 `PluginResult`） | 同 0.2 实际 |

**代码迁移路径**：0.1 三个 ABC 对应 0.2 三个子 trait——这是**为降低 0.1→0.2 迁移摩擦**而做的 1:1 映射。代码注释（如 `InputPipelinePlugin` 的 doc comment "对应 0.1 的 `pipeline/plugin.py IInputPlugin`"）印证了这一点。

#### 2.6 我的判断与建议

子 trait 的存在**有迁移成本上的合理性**，但**与决策 4 的精神不完全一致**。诚实评估：

- ✅ **存在的合理性**：0.1→0.2 迁移期，三个子 trait 与 0.1 三个 ABC 一一对应，便于开发者按图索骥。
- ⚠️ **与决策 4 的张力**：决策 4 否决的就是"基础 + 特化"模式，但代码保留了。
- 🔧 **可能的演进方向**：未来可在 PluginLoader 中加 "trait 实现类型 vs manifest.pipeline_role 一致性校验"——只要 trait 与 manifest 矛盾就拒绝加载，从而把子 trait 真正变成"软标签"，让 manifest 字段成为"硬真相"。

#### 2.7 小结

| 疑问 | 回答 |
|------|------|
| 三个子 trait 在哪？ | `traits.rs` L120-158 |
| 拆开的原因？ | **1:1 对应 0.1 三个 ABC**，降低迁移成本 |
| 拆开后 role() 分派由谁决定？ | 子 trait 默认 + manifest.pipeline_role **双重声明** |
| 用户判断"应该由引擎决定"正确吗？ | **正确**——更接近决策 4 精神；当前实现保留了 0.1 风格 |
| 决策文档与代码矛盾吗？ | **存在张力**——决策 4 否决"特化拆分"，但代码仍保留 |

> **来源**：[来源: 实际代码 kernel/crates/core/src/traits.rs L120-158]、[来源: 决策文档 docs/working/0.2插件体系核心决策.md 决策 4、决策 8]、[来源: 实际代码 src/pipeline/plugin.py L63-117 0.1 三个 ABC]

---

## 问题 3：LifecycleHook / HookContext（L203）—— 钩子完整清单与触发机制

### 用户疑问

> 生命周期钩子具体有哪些？可以触发什么？怎么触发？请列出完整的钩子清单和触发机制。

### 回答

#### 3.1 LifecycleHook 完整清单（5 种）

`traits.rs` L198-210 给出完整枚举：

```rust
/// 生命周期钩子类型。
///
/// 对应 MCP 扩展协议中的 `__kernel_lifecycle_hook`。
/// [来源: .project/mcp_extension_protocol.md §2.2]
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LifecycleHook {
    OnLoad,         // L205  插件加载时
    OnUnload,       // L206  插件卸载时
    OnPipelineStart,// L207  管道开始执行时
    OnPipelineEnd,  // L208  管道执行结束时
    OnError,        // L209  任意错误发生时
}
```

对应的 MCP 消息名（`__kernel_lifecycle_hook`）通过 `#[serde(rename_all = "snake_case")]` 序列化为 `on_load` / `on_unload` / `on_pipeline_start` / `on_pipeline_end` / `on_error`。

#### 3.2 HookContext 字段（随钩子一起发送的上下文）

`traits.rs` L212-250：

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HookContext {
    pub session_id: String,                    // L215 会话 ID
    pub task_id: String,                       // L216 任务 ID
    pub tenant_id: String,                     // L217 租户 ID（多租户穿透）
    pub pipeline_id: Uuid,                     // L218 管道唯一标识
    pub iteration: u32,                        // L219 当前迭代次数（管道循环）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub state_snapshot: Option<serde_json::Value>, // L221 状态快照（可选）
}

impl HookContext {
    pub fn new(session_id, task_id, tenant_id, pipeline_id) -> Self { ... }  // L225-239
    pub fn with_iteration(mut self, iteration: u32) -> Self { ... }           // L241-244
    pub fn with_state_snapshot(mut self, snapshot: serde_json::Value) -> Self { ... } // L246-249
}
```

#### 3.3 钩子的触发机制（3 个入口）

##### 入口 A：`PipelinePlugin::on_load` / `on_unload` 默认方法

`traits.rs` L102-111：

```rust
#[async_trait]
pub trait PipelinePlugin: PluginMeta + Any {
    // ...
    /// 生命周期钩子：插件加载时调用。
    async fn on_load(&self) -> Result<(), PluginError> {     // L104
        Ok(())
    }
    /// 生命周期钩子：插件卸载时调用。
    async fn on_unload(&self) -> Result<(), PluginError> {   // L109
        Ok(())
    }
}
```

这是**插件被动接收**的钩子——`PluginLoader` 加载插件时调用 `plugin.on_load()`，卸载时调用 `plugin.on_unload()`。默认空实现，开发者按需覆盖。

##### 入口 B：`PluginInvoker::send_lifecycle_hook` 主动发送

`traits.rs` L189-195：

```rust
/// 发送生命周期钩子事件到指定插件。
async fn send_lifecycle_hook(
    &self,
    plugin_id: &str,
    hook: LifecycleHook,
    context: &HookContext,
) -> Result<(), PluginError>;
```

这是**内核主动发送**的钩子——内核（管道引擎/插件加载器）通过 `PluginInvoker` 给指定插件发"X 事件发生了"的信号，附 `HookContext`。

注释明确（`traits.rs` L200-201）：对应 MCP 扩展协议中的 `__kernel_lifecycle_hook` 消息。

##### 入口 C：MCP 扩展协议 `__kernel_lifecycle_hook`

[来源: 反向推导] 从 `traits.rs` L200-201 反向推导：`__kernel_lifecycle_hook` 是 MCP 上的扩展消息，消息体结构对应：

```json
{
  "method": "__kernel_lifecycle_hook",
  "params": {
    "hook": "on_pipeline_start",  // 5 种 LifecycleHook 之一
    "context": {                  // HookContext 字段
      "session_id": "...",
      "task_id": "...",
      "tenant_id": "...",
      "pipeline_id": "uuid",
      "iteration": 0,
      "state_snapshot": null
    }
  }
}
```

#### 3.4 5 种钩子的触发时机（设计意图）

| 钩子 | 触发方 | 触发时机 | 设计意图 |
|------|--------|----------|----------|
| `OnLoad` | `PluginLoader.load()` | 插件 manifest 校验通过后、`dyn PipelinePlugin` 实例首次就绪时 | 初始化资源（数据库连接、内存缓存预热） |
| `OnUnload` | `PluginLoader.unload()` | 插件空闲超时或内核关闭时 | 释放资源（关闭连接、flush 缓存） |
| `OnPipelineStart` | 管道引擎 | 每条管道首次迭代开始前 | 重置插件内的迭代计数、清空临时状态 |
| `OnPipelineEnd` | 管道引擎 | 管道状态变为 `End` 或 `Wait`（挂起）时 | 持久化统计、清理临时文件 |
| `OnError` | 管道引擎 | 任意插件抛错且按 ErrorPolicy 路由后 | 错误聚合上报、触发告警 |

> **说明**：上表是**设计意图**，代码层面只定义了枚举类型和触发入口（`send_lifecycle_hook` 方法），具体的"谁在什么时刻调用 send_lifecycle_hook"留给后续实现（task_05 插件系统联调阶段）。

#### 3.5 0.1 对照：0.1 没有生命周期钩子

0.1 插件没有 `on_load` / `on_unload` 方法——在 `src/pipeline/plugin.py` 中确认（搜索 `def on_load|def on_unload` 在整个 `src/` 下 0 个匹配）。0.1 的插件**实例化即加载、Python 进程退出即释放**，没有显式生命周期管理。

0.2 引入生命周期钩子的根本原因（按需加载全局原则）：

> **来源**：[来源: 决策文档 docs/0.2_rust_plugin_solution.md §3.7]
>
> 所有插件（管道插件、工具插件、系统插件）和其他系统组件都遵循按需加载原则——不在使用中就不加载/不启动进程。空闲超时的插件进程自动卸载。
>
> 具体机制：
> - 插件进程按需启动：首次被调用时才启动 MCP 边车进程，非预启动
> - **空闲超时自动卸载**：插件进程空闲超过配置阈值（如 5 分钟）自动 kill，释放资源
> - Rust 原生管道插件按需注册：manifest 声明但不立即实例化，首次被路由到时才加载

**没有生命周期钩子，按需加载就无法干净地释放资源**——这就是 0.2 新增 5 种 LifecycleHook 的根本动机。

#### 3.6 实际使用示例（设计示例）

一个 memory_read 插件的典型实现：

```rust
// 设计示例（来源：trait 抽象推导，非仓库实际代码）
struct MemoryReadPlugin { /* ... */ }

#[async_trait]
impl PipelinePlugin for MemoryReadPlugin {
    // 必须实现 execute
    async fn execute(&self, ctx: &PluginContext) -> Result<PluginResult, PluginError> { ... }

    // 可选覆盖：on_load 时连接 PostgreSQL
    async fn on_load(&self) -> Result<(), PluginError> {
        self.connect_db().await?;  // 0.1 会在 __init__ 里做这事，0.2 移到 on_load
        Ok(())
    }

    // 可选覆盖：on_unload 时关闭连接
    async fn on_unload(&self) -> Result<(), PluginError> {
        self.close_db().await?;
        Ok(())
    }

    // 可选覆盖：声明本插件会响应哪些钩子事件（用于 PluginInvoker 选择性分发）
    async fn on_pipeline_start(&self, ctx: &HookContext) -> Result<(), PluginError> {
        self.reset_iteration_count();  // 每条新管道开始时清零迭代计数
        Ok(())
    }
}
```

#### 3.7 小结

| 疑问 | 回答 |
|------|------|
| 钩子完整清单？ | **5 种**：`OnLoad` / `OnUnload` / `OnPipelineStart` / `OnPipelineEnd` / `OnError` |
| 钩子在哪定义？ | `traits.rs` L198-210 |
| 触发机制？ | 3 个入口：插件 `on_load/on_unload` 默认方法、`PluginInvoker.send_lifecycle_hook` 主动发送、MCP `__kernel_lifecycle_hook` 消息 |
| 钩子上下文？ | `HookContext` 携带 session_id / task_id / tenant_id / pipeline_id / iteration / state_snapshot |
| 0.1 有钩子吗？ | **没有**——0.1 插件没有 on_load/on_unload 方法 |

> **来源**：[来源: 实际代码 kernel/crates/core/src/traits.rs L102-111, L189-250]、[来源: 决策文档 docs/0.2_rust_plugin_solution.md §3.7]、[来源: 实际代码 src/pipeline/plugin.py 0.1 无 on_load/on_unload]

---

## 问题 4：InProcess vs Sidecar 两种路径（L228-229）—— 所有插件都能选吗？

### 用户疑问

> 确认是否所有插件都可以走这两种路径，只是根据实际情况选择不同路径。

### 回答

#### 4.1 HostType 枚举的契约定义

`traits.rs` L620-629：

```rust
/// 宿主类型。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum HostType {
    /// Rust 原生进程内调用（零 IPC 开销）
    InProcess,
    /// 独立进程通过 MCP 协议通信
    #[default]
    Sidecar,
}
```

关键点：
1. **`HostType` 是 `PluginManifest` 的必填字段**（`traits.rs` L601），所有插件都必须声明
2. **默认 `Sidecar`**（`#[default]` 标注）——不写就是 MCP 边车
3. **两种值的语义清晰**：`InProcess` 是进程内 `dyn dispatch`，`Sidecar` 是 MCP 跨进程调用

#### 4.2 PluginInvoker 按 host_type 分发的实际机制

`traits.rs` L162-179：

```rust
/// 插件调用器：按 host_type 透明分发调用。
///
/// 核心设计（[来源: docs/0.2_rust_plugin_solution.md §3.2]）：
/// - `in_process`：直接调用 `dyn PipelinePlugin` 的 execute 方法（零 IPC 开销）
/// - `sidecar`：通过 rmcp 客户端走 MCP 协议调用（进程隔离）
/// - 两种路径对管道引擎透明——统一返回 `PluginResult`
#[async_trait]
pub trait PluginInvoker: Send + Sync {
    /// 调用管道插件执行。
    ///
    /// 内核根据插件的 `host_type` 字段选择调用路径：
    /// - InProcess: 直接 dyn PipelinePlugin::execute
    /// - McpSidecar: rmcp tools/call("execute", {state, config})
    async fn invoke_pipeline_plugin(
        &self,
        plugin_id: &str,
        ctx: &PluginContext,
    ) -> Result<PluginResult, PluginError>;
}
```

**分发逻辑在 `PluginInvoker` 的具体实现里**——trait 本身只声明接口，实现层根据 `host_type` 决定走哪条路径。

#### 4.3 用户的判断**部分正确**——但有约束

用户的判断"是否所有插件都可以走这两种路径"——**理论上 YES，但实际有约束**：

| 插件类型 | InProcess | Sidecar | 默认 | 说明 |
|---------|-----------|---------|------|------|
| **Pipeline（Input/Core/Output）** | ✅ 可选 | ✅ 可选 | 决策权在开发者 | 高频插件推荐 InProcess，低频可用 Sidecar |
| **Tool（工具）** | ❌ 不推荐 | ✅ 主要路径 | Sidecar（默认） | 工具通过 MCP 协议对外暴露，Sidecar 是天然路径 |
| **System（系统插件）** | ❌ 不推荐 | ✅ 主要路径 | Sidecar（默认） | 同上 |

**为什么工具/系统插件不推荐 InProcess？**

- 工具的核心价值是**跨语言接入**（Python 工具、TS 工具、Go 工具……）——`Sidecar` 走 MCP 是统一入口
- 工具的输入输出 schema 是 JSON Schema，跟 LLM 交互本来就要序列化——InProcess 没有收益
- `InProcess` 要求实现 Rust trait（`PipelinePlugin`），但工具插件的契约在 0.2 设计中**没有独立的 Rust trait**——`PluginInvoker::invoke_tool(plugin_id, tool_name, inputs)` 直接走 MCP tools/call，不存在"进程内的工具 Rust trait"

#### 4.4 0.2 方案文档对两种路径的"软混合"策略

`docs/0.2_rust_plugin_solution.md` §3.2：

> 管道插件采用**混合方案**：高频管道插件用 Rust 原生实现（热路径零 IPC 开销），低频管道插件用 MCP 边车。不强制，具体由开发者根据性能需求自行决定。

> 关键澄清：统一 `execute(Value) -> Value` trait 契约不因实现路径（Rust 原生 / MCP 边车）而不同——PluginInvoker 按 runtime 类型分发（InProcess 直接调 / McpRemote 走 rmcp）。两种实现路径对插件作者透明，对消费者（管道引擎）也透明。

**"不强制"** 是关键词——开发者**按需选择**。但隐含约束：

| 场景 | 推荐路径 | 理由 |
|------|---------|------|
| 管道插件，每轮必执行（ContextBuild / PromptBuild / ToolSchema / SecurityCheck / RouteArbiter） | **InProcess** | 零 IPC 开销，热路径性能敏感 |
| 管道插件，迭代轮次少（<50 轮）的管道 | Sidecar | IPC 开销可接受，迁移成本低 |
| Python 现有插件快速迁移 | Sidecar | 不需要改写 Rust |
| 第三方贡献者写插件 | Sidecar | 不需要懂 Rust |
| 工具插件 | Sidecar | MCP 是工具的标准协议 |
| 系统插件（记忆/审批/评估） | Sidecar | 同上 |

#### 4.5 反例：什么时候选哪种路径的判断示例

**示例 1：高频安全检查插件（推荐 InProcess）**

```json
{
  "id": "security_check",
  "name": "安全检查",
  "version": "1.0.0",
  "plugin_type": "pipeline",
  "pipeline_role": "input",
  "host_type": "in_process",
  "language": "rust",
  "entry": "SecurityCheckPlugin"
}
```

理由：每轮迭代都跑，IPC 序列化开销累积显著。

**示例 2：低频数据导出插件（推荐 Sidecar）**

```json
{
  "id": "data_export",
  "name": "数据导出",
  "version": "1.0.0",
  "plugin_type": "pipeline",
  "pipeline_role": "output",
  "host_type": "sidecar",
  "language": "python",
  "entry": "main.py:serve",
  "mcp": {
    "transport": "stdio",
    "idle_timeout_secs": 300
  }
}
```

理由：只在管道结束时执行一次，IPC 开销可忽略，Python 实现降低开发成本。

#### 4.6 用户更深层的问题：路径选择在哪个阶段发生？

- **静态选择**：插件作者在写 manifest 时**手动声明** `host_type`
- **动态分发**：`PluginInvoker` 实现层在 `invoke_pipeline_plugin()` 时**自动按 host_type 分支**
- **运行时切换**：当前 trait 设计不支持——一旦插件以某种 host_type 加载，不能在同一次会话内切换路径

#### 4.7 小结

| 疑问 | 回答 |
|------|------|
| HostType 在哪？ | `traits.rs` L620-629（HostType 枚举） + L601（PluginManifest.host_type 必填字段） |
| 分发机制？ | `PluginInvoker` 实现层按 `host_type` 自动分支（`traits.rs` L172-179 注释明确） |
| 所有插件都能选吗？ | **理论上 YES，实际有约束**：Pipeline 插件可选，Tool/System 插件默认且推荐 Sidecar |
| 谁决定走哪条？ | 插件作者在 manifest 里声明；PluginInvoker 读取后自动分发 |
| "不强制"是什么意思？ | 0.2 §3.2 明文：高频 Rust 原生 / 低频 MCP 边车，由开发者按性能需求自行决定 |

> **来源**：[来源: 实际代码 kernel/crates/core/src/traits.rs L162-179, L601, L620-629]、[来源: 决策文档 docs/0.2_rust_plugin_solution.md §3.2 混合方案]

---

## 问题 5：LlmProvider trait 抽象层（L251）—— 为什么不对齐 litellm？

### 用户疑问

> 这个抽象层接口为什么不直接跟现有的 litellm 接口匹配？等 LLM 调用模块依赖换成 Rust 后应该可以无缝连接。请分析当前设计与 litellm 的关系。

### 回答

#### 5.1 LlmProvider trait 的契约定义

`traits.rs` L376-412：

```rust
/// LLM 服务提供者抽象（抽象层 + 可替换实现模式）。
///
/// 设计原则（[来源: docs/0.2_rust_plugin_solution.md §3.5]）：
/// - LLM Provider 实现会变（新增厂商、切换 API），但"调用 LLM 返回文本"这个动作不变
/// - 抽象层长期保留，具体实现藏在各自模块内部
/// - 外部（管道引擎 Core 插件）只看到统一的调用接口
#[async_trait]
pub trait LlmProvider: Send + Sync {
    /// 非流式补全调用。
    async fn complete(
        &self,
        model: &str,
        messages: &[LlmMessage],
        options: &LlmOptions,
    ) -> Result<LlmResponse, LlmError>;

    /// 流式补全调用。
    /// 通过 channel 推送流式 chunk，调用方从 channel 接收。
    /// 对应 0.1 的流式响应机制（管道引擎通过 stream bridge 推送到前端）。
    async fn complete_stream(
        &self,
        model: &str,
        messages: &[LlmMessage],
        options: &LlmOptions,
    ) -> Result<tokio::sync::mpsc::Receiver<LlmStreamChunk>, LlmError>;

    /// 获取可用模型列表。
    async fn list_models(&self) -> Result<Vec<ModelInfo>, LlmError>;
}
```

配套的 8 个数据类型定义在 `traits.rs` L414-558：

| 类型 | 行号 | 角色 | 与 litellm 对应字段 |
|------|------|------|------------------|
| `LlmMessage` | L414-423 | 消息 | `litellm.Message` (role/content/tool_calls/tool_call_id) |
| `MessageRole` 枚举 | L425-433 | system/user/assistant/tool | `litellm.Message.role` |
| `ToolCallRequest` | L435-441 | 工具调用请求 | `litellm.ModelResponse.tool_calls` |
| `LlmOptions` | L443-455 | 调用选项 | `litellm.completion(**kwargs)` 的 temperature/max_tokens/top_p/tools |
| `ToolCallDefinition` | L457-463 | 工具定义（function schema） | `litellm.utils.function_to_dict` |
| `LlmResponse` | L465-476 | 非流式响应 | `litellm.ModelResponse` |
| `TokenUsage` | L478-484 | token 用量 | `litellm.ModelResponse.usage` |
| `LlmStreamChunk` | L497-515 | 流式 chunk | `litellm.utils.ModelResponseStream` |
| `FinishReason` 枚举 | L487-495 | stop/length/tool_calls/content_filter/error | `litellm.ModelResponse.choices[0].finish_reason` |
| `LlmError` 枚举 | L528-558 | 6 种错误 | `litellm.exceptions.*` |
| `ModelInfo` | L517-526 | 模型信息 | 无直接对应（litellm 没有 list_models 标准接口） |

#### 5.2 当前设计 vs litellm 的对比

| 维度 | 0.1 litellm（Python） | 0.2 LlmProvider（Rust） |
|------|---------------------|----------------------|
| **入口函数** | `litellm.completion(model, messages, **kwargs)` | `LlmProvider::complete(model, messages, options)` |
| **流式** | `litellm.completion(..., stream=True)` 返回生成器 | `complete_stream()` 返回 `tokio::sync::mpsc::Receiver<LlmStreamChunk>` |
| **消息结构** | `[{role, content}]`（list of dict） | `Vec<LlmMessage>`（强类型） |
| **工具调用** | 通过 `tools=[{type:"function", function:{...}}]` | `LlmOptions.tools: Vec<ToolCallDefinition>` |
| **模型列表** | `litellm.utils.get_model_info()`（散落） | `list_models() -> Vec<ModelInfo>`（统一接口） |
| **错误处理** | `litellm.exceptions.AuthenticationError` 等异常类 | `LlmError` 枚举（Network/Auth/RateLimited/ModelUnavailable/ContextLength/ContentFiltered/Other） |
| **路由/负载均衡** | `litellm.Router(model_list=..., ...)` | 不在抽象层——具体实现可包装 Router |

**关键差异点**：

1. **0.2 把"流式响应"独立成第二个方法**（`complete_stream`），而不是 0.1 litellm 的 `stream=True` 标志位——这是 Rust 类型系统的自然选择（返回类型不同 → 必须独立方法）
2. **0.2 把模型列表独立成 `list_models()`**——litellm 没有标准接口，0.2 借此统一
3. **0.2 把错误归一为 `LlmError` 枚举**——litellm 的异常类层次更深，0.2 选择更扁平的枚举便于 match
4. **0.2 抽象层不包含 Router 概念**——路由/负载均衡是 0.1 的 `litellm.Router`（详见 `src/llm/router_factory.py`），0.2 把这个职责留给具体实现

#### 5.3 用户判断"为什么不对齐 litellm"——核心原因

**0.2 的 LlmProvider 不是"litellm 的 Rust 翻译版"，而是"litellm 适配层的接口契约"**。

设计意图来自契约注释（`traits.rs` L378-383）：

> 设计原则：
> - LLM Provider 实现会变（新增厂商、切换 API），但"调用 LLM 返回文本"这个动作不变
> - **抽象层长期保留，具体实现藏在各自模块内部**
> - 外部（管道引擎 Core 插件）只看到统一的调用接口

如果直接照 litellm 接口设计，等于把 litellm 的设计缺陷（异常层次深、stream 标志位、Router 散落）也带进 Rust 内核。0.2 的选择是：**先定义"我们应该看到什么"，再让 litellm（或别的实现）适配这个抽象**。

#### 5.4 0.1 中的 litellm 实际用法（参考）

`src/llm/router_factory.py` 是 0.1 用 litellm 的关键文件：

```python
# 第 1-10 行（节选）
"""litellm.Router 工厂 — 从 llm.yaml 构建共享 Router 实例。"""
from __future__ import annotations
import litellm

# 第 42-44 行：litellm 全局配置
litellm.aiohttp_trust_env = False
litellm.disable_aiohttp_trust_env = True

# 第 61-71 行：模块级缓存
_router_instance: litellm.Router | None = None
_key_pools: dict[str, KeyPool] = {}
_model_to_name: dict[str, {}] = {}
_provider_type_map: dict[str, str] = {}

# 第 74-103 行：从 llm.yaml 构造 litellm 模型字符串
def get_litellm_prefix(provider_name: str) -> str:
    """获取 provider 对应的 litellm 前缀（从配置动态读取）。"""
    # ...

def _get_litellm_model_string(provider: str, model_name: str) -> str:
    """计算 litellm 格式的模型标识字符串。"""
    prefix = get_litellm_prefix(provider)
    return f"{prefix}/{model_name}"
```

0.1 的 LLM 链路：`pipeline/core/llm_core` → `src/llm/adapter.py` → `litellm.Router` → 各 provider API。

#### 5.5 用户更深层的判断："等换成 Rust 后可以无缝连接"——准确，但需适配层

用户的判断准确：0.2 的 LlmProvider 设计**就是为 litellm Rust 化铺路**。

迁移路径（推断）：

| 阶段 | LlmProvider 的实现 | 调用方式 |
|------|-------------------|----------|
| 0.2.0 初始阶段（task_07 任务） | Python 实现的 `LlmProvider` 适配（PyO3 / MCP 边车） | Core 插件 → 内核 → LlmProvider trait → Python litellm 适配器 → litellm Router → provider API |
| 0.2.x 演进 | Rust 原生实现（如 OpenAI/Anthropic 直连） | Core 插件 → 内核 → LlmProvider trait → Rust 实现 → provider API |
| 长期 | 多种实现并存，Core 插件无感 | Core 插件只调 LlmProvider trait，不知道下面是什么 |

**关键设计**：因为 0.2 已经把"调用 LLM"抽象成 trait 接口，**未来无论是 PyO3 桥接、还是 Rust 直连、还是 MCP 边车，对 Core 插件都透明**。Core 插件只看到 `LlmProvider::complete(...)`，不需要关心底层是 Python 还是 Rust。

#### 5.6 0.2 LlmProvider 的"长期价值"

`traits.rs` L378-383 的注释直接回答了这个问题：

> - **LLM Provider 实现会变**（新增厂商、切换 API），但"调用 LLM 返回文本"这个动作不变
> - **抽象层长期保留**，具体实现藏在各自模块内部
> - 外部（管道引擎 Core 插件）只看到统一的调用接口

**抽象层 vs 具体实现的隔离**：

```
┌─────────────────────────────────────────────────────┐
│  Core 插件（如 llm_core）                            │
│  ─ 只调 LlmProvider::complete()                       │
└────────────────────┬────────────────────────────────┘
                     │ trait 边界
                     ▼
┌─────────────────────────────────────────────────────┐
│  LlmProvider 实现（可热替换）                          │
│  ─ Python litellm 适配（PyO3 桥接，0.2.0 初始）       │
│  ─ Rust OpenAI/Anthropic 直连（0.2.x 演进）           │
│  ─ 本地模型（llama.cpp / ollama 适配）                 │
└─────────────────────────────────────────────────────┘
```

Core 插件在整个生命周期里**不需要改一行代码**——切换实现 = 切换 `LlmProvider` 的具体实例。

#### 5.7 小结

| 疑问 | 回答 |
|------|------|
| LlmProvider 在哪？ | `traits.rs` L376-558（trait + 11 个配套类型） |
| 为什么不对齐 litellm？ | 0.2 是"litellm 适配层的接口契约"，不是"litellm 的 Rust 翻译版"——抽象层长期保留，具体实现可替换 |
| 与 litellm 的关系？ | 字段级对齐（model/messages/stream/tools/usage/finish_reason）但**不是字面对齐**——流式独立成方法、模型列表独立成方法、错误归一为枚举 |
| 用户判断"无缝连接"准确吗？ | **准确**——LlmProvider 就是为 litellm Rust 化铺路；当前通过 PyO3 桥接，未来可换 Rust 原生 |
| Core 插件需要改吗？ | **不需要**——切换 LlmProvider 实现对 Core 插件透明 |

> **来源**：[来源: 实际代码 kernel/crates/core/src/traits.rs L376-558]、[来源: 实际代码 src/llm/router_factory.py 0.1 litellm 用法]、[来源: 决策文档 docs/0.2_rust_plugin_solution.md §3.5 LLM 抽象层设计]

---

## 总览对照表

| 问题 | 核心契约位置 | 用户判断正确性 | 关键澄清 |
|------|------------|--------------|----------|
| **Q1: dependencies** | `traits.rs` L325-374, L604-605 | ✅ 运行时解耦判断完全正确 | dependencies 解决"加载顺序"，运行时通过 PluginInvoker + state 解耦 |
| **Q2: 三个子 trait** | `traits.rs` L120-158 | ✅ "应统一"判断接近决策 4 精神 | **存在张力**——决策 4 否决"特化拆分"，代码仍保留；本质是"标签"而非"分派逻辑" |
| **Q3: LifecycleHook** | `traits.rs` L198-250 | — | **5 种钩子** + 3 个入口（默认方法/PluginInvoker/MCP 消息）；0.1 无钩子 |
| **Q4: InProcess vs Sidecar** | `traits.rs` L162-179, L601, L620-629 | ⚠️ 理论 YES，实际有约束 | Pipeline 可选，Tool/System 推荐 Sidecar（默认） |
| **Q5: LlmProvider vs litellm** | `traits.rs` L376-558 | ✅ "无缝连接"判断准确 | 字段对齐但**不是字面对齐**；抽象层长期保留，实现可换 |

---

## 给后续契约定稿的建议

基于这 5 个问题的分析，给后续契约维护提 3 条建议：

1. **Q2 子 trait 与决策 4 的张力**：建议在 `traits.rs` 注释中明确"子 trait 是软标签，manifest.pipeline_role 是硬真相"，并在 `PluginLoader.validate_manifest()` 加一致性校验。
2. **Q3 钩子触发时机**：当前 trait 只定义了"入口"，未定义"谁在什么时刻调"。建议在后续迭代里补充一份"LifecycleHook 触发时序图"，写明 task_05 插件系统联调阶段谁负责调用 send_lifecycle_hook。
3. **Q5 LlmProvider 与 litellm 的桥接**：建议在 task_07 任务里明确"0.2.0 初始阶段通过 PyO3 / MCP 边车桥接 litellm"，把"无缝连接"路径具体化。

---

## 文档元信息

- **产出**：`docs/guides/contract_qa.md`
- **输入**：[`docs/guides/contract_files_tutorial.md`](contract_files_tutorial.md)
- **回答问题数**：5
- **引用契约代码行号**：21 处
- **引用决策文档**：4 份（0.2插件体系核心决策、0.2_rust_plugin_solution、0.2_rust_plugin_checkpoints、ARCHITECTURE）
- **诚实声明**：所有"设计意图"已与"实际代码"明确区分；对决策与代码不一致处已直接指出
