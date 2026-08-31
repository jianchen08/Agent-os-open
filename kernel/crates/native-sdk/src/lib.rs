//! # Lingxi AgentOS 原生插件 SDK（直接 trait 对象）
//!
//! 定义跨 cdylib 边界的插件契约。被内核（host）和插件（implementation cdylib）
//! 共同依赖。
//!
//! ## 为什么不用 abi_stable
//!
//! abi_stable 0.11.3 在 Windows + Rust 1.85 + release 优化下，C-ABI 运行时校验
//! （`InvalidCAbi`）失败——`extern "C" fn` 返回小元组类型时调用约定错位。
//! 本项目用 `rust-toolchain.toml` 锁定 rustc 1.85，内核与插件同版本编译，
//! `Box<dyn PipelinePlugin>` 的 vtable 天然一致，无需 abi_stable 的跨版本保证，
//! 也避免了它的运行时校验坑。
//!
//! ## 契约
//!
//! 插件 cdylib export 一个 `extern "C" fn agentos_plugin_create() -> *mut ()`，返回
//! `Box<dyn PipelinePlugin>` 的裸指针（堆分配，所有权转移给内核）。内核 dlopen
//! 拿到符号、调用、把指针还原为 `Box<dyn PipelinePlugin>`。
//!
//! unsafe 仅存在于 loader 的指针还原（和插件侧的 `Box::into_raw`），由 SDK 封装。
//! 插件作者的业务代码（impl 块）零 unsafe。
//!
//! ## 调用语义（B2：同一 execute 入口，约定字段区分）
//!
//! `execute` 承载两种语义，经 [`PluginCtx::tool_call_json`] 约定字段区分：
//! - `None`（缺省）：pipeline 语义，返回 state_updates JSON；
//! - `Some({"name": ...})`：工具调用语义（InProcess 工具插件），返回
//!   ToolExecutionResult 形状 JSON（`{success, data}` / `{success:false, error}`）。
//!   旧插件不认识该字段 → 按 pipeline 逻辑返回，调用侧归一（零破坏）。
//!
//! ## 依赖方向
//!
//! 插件要调内核 capability（执行工具 / 发事件）。约定：内核构造 `PluginCtx` 时
//! 注入一个 `HostServices` 实现（包 CapabilityRouter）；插件在 `execute` 里经
//! `ctx.host` 调用——与 sidecar（JSON-RPC）、wasm（host.call import）走同一 router。

use serde::{Deserialize, Serialize};

pub use serde_json;

// ── HostServices：插件 → 内核 capability 调用 ─────────────────────────

// ── HostServices：插件 → 内核 capability 调用 ─────────────────────────

/// 插件调用内核 capability 的句柄。
///
/// 内核实现此 trait（包 `CapabilityRouter`），构造 `PluginCtx` 时注入。
/// 插件经它调 `tool-executor.invoke`（执行工具）/ `event-bus.emit`（发事件）等
/// capability——与 sidecar 经 JSON-RPC 反调内核走同一 router，能力等价。
///
/// 注：这是普通 Rust trait（非 FFI-safe），依赖"内核与插件同 rustc 版本编译"
/// 保证 vtable 一致。toolchain 已锁定 1.85，满足此前提。
pub trait HostServices: Send + Sync {
    /// 调用内核 capability，返回**实现方（exe）持有**的结果 JSON 借用。
    ///
    /// - `capability`：能力名（如 `"tool-executor"` / `"event-bus"`）。
    /// - `method`：方法名（如 `"invoke"` / `"emit"`）。
    /// - `params_json`：方法参数（序列化后的 JSON 字符串）。
    ///
    /// 返回 `Ok(result_json)`（借用绑定 `&self` 生命周期）或 `Err(error_msg)`。
    ///
    /// # 跨分配器契约（2026-09-01，两个方向都不能省）
    ///
    /// exe 与 cdylib 的全局分配器可能不同（mi exe × 系统堆 dll），**任何跨边界的
    /// 所有权转移都是跨堆 free UB**。协议：
    /// - 入参 `capability`/`method`/`params_json`：dll 分配的 `&str` 只读借用，
    ///   exe 实现内部复制后使用（不持有、不释放其指针）。
    /// - 返回值：exe 内部缓冲（exe 堆分配/释放），借给 dll 只读消费；
    ///   dll **立即**解析（转 Value/拷贝），不存引用、不释放——下次同 host 调用
    ///   会覆盖缓冲。`&str` 生命周期与 `&self` 绑定，借用检查强制消费内完成。
    fn call_capability(&self, capability: &str, method: &str, params_json: &str)
        -> Result<&str, String>;
}

// ── PipelinePlugin：插件实现的主契约 ───────────────────────────────────

/// 原生管道插件契约。插件（如 tool_core）`impl` 此 trait。
///
/// `execute` 接收 `&ExecContext`（含 ctx + host capability 句柄），返回 state 更新
/// Patch 的 JSON 字符串。
///
/// 注：普通 Rust trait，依赖同 rustc 版本编译保证 vtable 一致。
pub trait PipelinePlugin: Send + Sync {
    /// 执行插件逻辑，返回**实现方（dll）持有**的结果 JSON 借用。
    ///
    /// - `ectx`：执行上下文（ctx 含 state/config；host 是内核 capability 句柄）。
    /// - 返回 `Ok(state_updates_json)`（借用绑定 `&self` 生命周期）或
    ///   `Err(error_msg)`。
    ///
    /// # 跨分配器契约（2026-09-01，与 `HostServices` 对称）
    ///
    /// **跨边界零所有权转移**：调用方（内核）对返回串**立即拷贝**（转
    /// Value/落 state），不存引用、不释放——缓冲由 dll 分配与释放（dll 堆），
    /// 借用生命周期与 `&self` 绑定。禁止返回 `String`（exe drop = 跨堆 free UB，
    /// 2026-09-01 差分实验 10/15 复现真机段错误）。
    ///
    /// 插件实现惯例：把 JSON 存进 `&self` 的内部缓冲字段（`Mutex<String>`，
    /// execute 在 blocking 线程串行调用），返回其 `&str`。
    fn execute(&self, ectx: &ExecContext) -> Result<&str, String>;
}

// ── PluginCtx：传给 execute 的上下文 ──────────────────────────────────

/// 传给插件 `execute` 的上下文。
///
/// `state` / `config` 用 `String`（JSON 字符串）传递，插件侧自行解析为 Value。
/// `host` 是内核注入的 capability 句柄（`None` = 未注入，插件降级跳过 capability）。
#[derive(Clone, Default, Serialize, Deserialize)]
pub struct PluginCtx {
    /// 管道状态（JSON 字符串）。工具调用语义下是工具入参（inputs）。
    pub state_json: String,
    /// 插件配置（JSON 字符串）。
    pub config_json: String,
    /// 租户 ID。
    pub tenant_id: String,
    /// 会话 ID。
    pub session_id: String,
    /// 任务 ID。
    pub task_id: String,
    /// 管道 ID。
    pub pipeline_id: String,
    /// 工具调用语义标记（B2：native 工具插件，M2 计划任务 B）。
    ///
    /// 与生命周期钩子 `hook` 字段同构的约定字段模式：**同一 `execute` C-ABI 入口，
    /// 约定字段区分调用语义**。概念上等价于 PluginInput 的
    /// `{state: inputs, config: ..., tool_call: {name: tool_name}}`：
    /// - `None` → 原 pipeline 语义：execute 返回 state_updates JSON；
    /// - `Some(json)` → 本次是**工具调用**，值为 `{"name": tool_name}` JSON 字符串
    ///   （入参在 `state_json`）：插件走工具逻辑，返回 ToolExecutionResult 形状 JSON
    ///   （`{"success": true, "data": ...}` 或 `{"success": false, "error": "..."}`）。
    ///
    /// 旧插件不认识该字段（编译时无此字段，运行时直调无反序列化开销）→ 忽略，
    /// 按 pipeline 逻辑返回 state_updates；调用侧（invoker `invoke_native_tool`）
    /// 检测返回形状归一（能解析成 ToolExecutionResult 信封就用，否则包 success
    /// 信封）——旧插件零破坏。
    ///
    /// 注：跨 cdylib 边界按引用传 `PluginCtx` 本体（非序列化），本字段的
    /// `#[serde(default)]` 仅为 JSON 兼容（测试/日志序列化时缺省为 None）。
    #[serde(default)]
    pub tool_call_json: Option<String>,
}

impl PluginCtx {
    /// 解析 state_json 为 serde_json::Value（插件侧便利方法）。
    pub fn state_value(&self) -> serde_json::Value {
        serde_json::from_str(&self.state_json).unwrap_or(serde_json::Value::Null)
    }

    /// 解析 config_json 为 serde_json::Value。
    pub fn config_value(&self) -> serde_json::Value {
        serde_json::from_str(&self.config_json).unwrap_or(serde_json::Value::Null)
    }

    /// 解析 tool_call_json（插件侧便利方法，B2 工具调用语义）。
    ///
    /// - `Some(value)` → 本次 execute 是工具调用，value 形如 `{"name": "bash_execute"}`；
    /// - `None` → 原 pipeline 语义。
    ///
    /// 插件典型写法：
    /// ```ignore
    /// fn execute(&self, ectx: &mut ExecContext) -> Result<(), String> {
    ///     if let Some(tool_call) = ectx.ctx.tool_call_value() {
    ///         let name = tool_call.get("name").and_then(|v| v.as_str()).unwrap_or("");
    ///         let args = ectx.ctx.state_value();
    ///         return Ok(format!(r#"{{"success": true, "data": {{}}"}}"#)); // 工具逻辑
    ///     }
    ///     // …原 pipeline 逻辑…
    /// }
    /// ```
    pub fn tool_call_value(&self) -> Option<serde_json::Value> {
        self.tool_call_json
            .as_deref()
            .and_then(|s| serde_json::from_str(s).ok())
    }
}

/// 执行上下文（含 host 句柄与返回缓冲）——传给插件 execute 的完整上下文。
///
/// 内核构造时填入 PluginCtx + host + `out`（预分配返回缓冲）；插件从 ctx 读
/// state、从 host 调 capability、把结果写进 `out`（**跨分配器契约**：结果归内核
/// 堆，禁止以 `String` 返回——dll 堆上的缓冲内核无法安全 drop，见
/// [`PipelinePlugin`] 头注释）。
/// host 用 trait 对象指针传递（`*mut ()` 实为 `Box<dyn HostServices>`），
/// 内核负责构造和释放，插件只借用 `&dyn HostServices`。
pub struct ExecContext<'a> {
    pub ctx: PluginCtx,
    pub host: Option<&'a dyn HostServices>,
}

// ── 构造函数契约 + 安全封装 ────────────────────────────────────────────

/// 插件 export 的构造函数符号名。
pub const CREATE_FN_NAME: &[u8] = b"agentos_plugin_create";

/// 插件侧：把实现类型转成裸指针（构造函数返回值）。
///
/// 内部双重 Box：外层 `Box<Box<dyn PipelinePlugin>>` 是 thin pointer（可跨 C-ABI），
/// 内层 `Box<dyn PipelinePlugin>` 是 fat pointer（含 vtable）。内核用 [`box_from_raw`]
/// 还原外层得到内层。
///
/// 插件构造函数典型写法：
/// ```ignore
/// #[no_mangle]
/// pub extern "C" fn agentos_plugin_create() -> *mut () {
///     agentos_native_sdk::plugin_into_raw(ToolCore)
/// }
/// ```
///
/// # Safety
/// 返回的指针必须经内核的 `box_from_raw` 还原；还原后外层 Box 与实例均按
/// 进程级单例契约 leak（跨分配器 free=UB，见 `box_from_raw`），不提供释放路径。
pub fn plugin_into_raw<P: PipelinePlugin + 'static>(plugin: P) -> *mut () {
    let boxed: Box<dyn PipelinePlugin> = Box::new(plugin);
    // 双重 Box：外层 thin pointer 跨 C-ABI，内层 fat pointer 携带 vtable。
    Box::into_raw(Box::new(boxed)) as *mut ()
}

/// 内核侧：把构造函数返回的裸指针还原为 `Box<dyn PipelinePlugin>`。
///
/// **跨分配器契约（不能省）**：`ptr` 指向的双重 Box 全部由插件 cdylib 的分配器
/// 分配。进程内 exe 与 cdylib 的全局分配器可能不同（如内核 exe 用 mimalloc、
/// cdylib 用系统堆），任何一侧用 exe 的分配器释放 dll 分配的内存 = 跨分配器
/// free UB（堆损坏→随机 SIGSEGV，2026-09-01 最小差分实验定案：mi×drop=崩，
/// leak 全绿，六车道 140 轮）。因此本函数只读外层 Box 取出内层 fat pointer，
/// **外层 Box 永不 drop**（其堆块留在 dll 堆侧，随进程退出由 OS 回收）；内层
/// Box 同契约由调用方 `Box::leak` 保活（native_loader：进程级单例永不内核 drop）。
///
/// # Safety
/// `ptr` 必须由 [`plugin_into_raw`] 产生（指向堆上 `Box<Box<dyn PipelinePlugin>>`），
/// 且只能还原一次（重复还原 UB）。
pub unsafe fn box_from_raw(ptr: *mut ()) -> Option<Box<dyn PipelinePlugin>> {
    if ptr.is_null() {
        return None;
    }
    // SAFETY: ptr 由 plugin_into_raw 产生，指向堆上 Box<Box<dyn PipelinePlugin>>。
    // 只读解引用取出内层 fat pointer（Copy 栈值），不 touch 外层 Box 的堆块
    // （不 drop、不 free——跨分配器 UB）。内层 from_raw 还原后由调用方 Box::leak。
    let outer = unsafe { &*(ptr as *const Box<dyn PipelinePlugin>) };
    let inner_fat: *mut dyn PipelinePlugin =
        &**outer as *const dyn PipelinePlugin as *mut dyn PipelinePlugin;
    Some(unsafe { Box::from_raw(inner_fat) })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plugin_ctx_state_parses() {
        let ctx = PluginCtx {
            state_json: r#"{"k":1}"#.into(),
            ..PluginCtx::default()
        };
        assert_eq!(ctx.state_value()["k"], 1);
    }

    #[test]
    fn plugin_ctx_tool_call_value_parses() {
        // B2：tool_call_json 解析为 {"name": ...}（Some = 工具调用语义）。
        let ctx = PluginCtx {
            tool_call_json: Some(r#"{"name": "bash_execute"}"#.into()),
            ..PluginCtx::default()
        };
        let tc = ctx.tool_call_value().expect("tool_call 应解析为 Some");
        assert_eq!(tc["name"], "bash_execute");
    }

    #[test]
    fn plugin_ctx_tool_call_value_none_for_pipeline() {
        // 旧 pipeline 调用路径：tool_call_json 为 None → None（零破坏）。
        let ctx = PluginCtx::default();
        assert!(ctx.tool_call_value().is_none());
        // 非法 JSON 字符串也降级为 None（不 panic）
        let bad = PluginCtx {
            tool_call_json: Some("not json".into()),
            ..PluginCtx::default()
        };
        assert!(bad.tool_call_value().is_none());
    }

    #[test]
    fn plugin_ctx_serde_deserialize_without_tool_call_field() {
        // JSON 兼容：旧形态（无 tool_call_json 字段）反序列化 → None（#[serde(default)]）。
        let ctx: PluginCtx = serde_json::from_str(
            r#"{"state_json": "{}", "config_json": "{}", "tenant_id": "t", "session_id": "s", "task_id": "", "pipeline_id": "p"}"#,
        )
        .unwrap();
        assert!(ctx.tool_call_json.is_none());
        assert!(ctx.tool_call_value().is_none());
    }

    struct DummyPlugin {
        buf: std::cell::UnsafeCell<String>,
    }
    impl DummyPlugin {
        fn new() -> Self {
            Self {
                buf: std::cell::UnsafeCell::new(String::new()),
            }
        }
    }
    impl PipelinePlugin for DummyPlugin {
        fn execute(&self, _ectx: &ExecContext) -> Result<&str, String> {
            // 对称借用协议：结果存 &self 内部缓冲，返回其 &str（dll 侧持有）。
            // SAFETY: execute 由 loader 在 blocking 线程串行调用（见 trait 契约），
            // &self 期间无并发写者；内部缓冲借出 &str 且调用方同步消费。
            let buf = unsafe { &mut *self.buf.get() };
            buf.clear();
            buf.push_str(r#"{"ok":true}"#);
            Ok(buf.as_str())
        }
    }

    // SAFETY: execute 契约=blocking 线程串行（见 trait 契约），UnsafeCell 无并发写。
    unsafe impl Sync for DummyPlugin {}

    #[test]
    fn raw_pointer_roundtrip() {
        let ptr = plugin_into_raw(DummyPlugin::new());
        assert!(!ptr.is_null());
        // SAFETY: ptr 来自 plugin_into_raw，仅还原一次。
        let boxed = unsafe { box_from_raw(ptr) }.unwrap();
        let ectx = ExecContext {
            ctx: PluginCtx::default(),
            host: None,
        };
        // 内核侧立即拷贝返回串（跨分配器契约：不持有 dll 堆引用）。
        let out = boxed.execute(&ectx).unwrap().to_string();
        assert_eq!(out, r#"{"ok":true}"#);
    }

    #[test]
    fn null_pointer_returns_none() {
        // SAFETY: null 指针，box_from_raw 内部判空返回 None。
        assert!(unsafe { box_from_raw(std::ptr::null_mut()) }.is_none());
    }
}
