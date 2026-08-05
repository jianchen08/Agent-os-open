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
//! 插件 cdylib export 一个 `extern "C" fn plugin_create() -> *mut ()`，返回
//! `Box<dyn PipelinePlugin>` 的裸指针（堆分配，所有权转移给内核）。内核 dlopen
//! 拿到符号、调用、把指针还原为 `Box<dyn PipelinePlugin>`。
//!
//! unsafe 仅存在于 loader 的指针还原（和插件侧的 `Box::into_raw`），由 SDK 封装。
//! 插件作者的业务代码（impl 块）零 unsafe。
//!
//! ## 依赖方向
//!
//! 插件要调内核 capability（执行工具 / 发事件）。约定：内核构造 `PluginCtx` 时
//! 注入一个 `HostServices` 实现（包 CapabilityRouter）；插件在 `execute` 里经
//! `ctx.host` 调用——与 sidecar（JSON-RPC）、wasm（host.call import）走同一 router。

use serde::{Deserialize, Serialize};

pub use serde_json;

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
    /// 调用内核 capability，返回结果 JSON 字符串。
    ///
    /// - `capability`：能力名（如 `"tool-executor"` / `"event-bus"`）。
    /// - `method`：方法名（如 `"invoke"` / `"emit"`）。
    /// - `params_json`：方法参数（序列化后的 JSON 字符串）。
    ///
    /// 返回 `Ok(result_json)` 或 `Err(error_msg)`。
    fn call_capability(
        &self,
        capability: &str,
        method: &str,
        params_json: &str,
    ) -> Result<String, String>;
}

// ── PipelinePlugin：插件实现的主契约 ───────────────────────────────────

/// 原生管道插件契约。插件（如 tool_core）`impl` 此 trait。
///
/// `execute` 接收 `&ExecContext`（含 ctx + host capability 句柄），返回 state 更新
/// Patch 的 JSON 字符串。
///
/// 注：普通 Rust trait，依赖同 rustc 版本编译保证 vtable 一致。
pub trait PipelinePlugin: Send + Sync {
    /// 执行插件逻辑。
    ///
    /// - `ectx`：执行上下文（ctx 含 state/config，host 是内核 capability 句柄）。
    /// - 返回 `Ok(state_updates_json)`（state 更新 Patch，序列化为 JSON 字符串）
    ///   或 `Err(error_msg)`。
    fn execute(&self, ectx: &ExecContext) -> Result<String, String>;
}

// ── PluginCtx：传给 execute 的上下文 ──────────────────────────────────

/// 传给插件 `execute` 的上下文。
///
/// `state` / `config` 用 `String`（JSON 字符串）传递，插件侧自行解析为 Value。
/// `host` 是内核注入的 capability 句柄（`None` = 未注入，插件降级跳过 capability）。
#[derive(Clone, Default, Serialize, Deserialize)]
pub struct PluginCtx {
    /// 管道状态（JSON 字符串）。
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
}

/// 执行上下文（含 host 句柄）——传给插件 execute 的完整上下文。
///
/// 内核构造时填入 PluginCtx + host；插件从 ctx 读 state、从 host 调 capability。
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
/// 返回的指针必须经内核的 `box_from_raw` 还原释放，否则泄漏。
pub fn plugin_into_raw<P: PipelinePlugin + 'static>(plugin: P) -> *mut () {
    let boxed: Box<dyn PipelinePlugin> = Box::new(plugin);
    // 双重 Box：外层 thin pointer 跨 C-ABI，内层 fat pointer 携带 vtable。
    Box::into_raw(Box::new(boxed)) as *mut ()
}

/// 内核侧：把构造函数返回的裸指针还原为 `Box<dyn PipelinePlugin>`。
///
/// # Safety
/// `ptr` 必须由 [`plugin_into_raw`] 产生（指向堆上 `Box<Box<dyn PipelinePlugin>>`），
/// 且只能还原一次（重复还原 UB）。
pub unsafe fn box_from_raw(ptr: *mut ()) -> Option<Box<dyn PipelinePlugin>> {
    if ptr.is_null() {
        return None;
    }
    // SAFETY: 调用方保证 ptr 来自 plugin_into_raw（外层 Box<Box<dyn PipelinePlugin>>）。
    // 还原外层 Box，取出内层 Box<dyn PipelinePlugin>（所有权转移给调用方）。
    let outer: Box<Box<dyn PipelinePlugin>> = unsafe { Box::from_raw(ptr as *mut Box<dyn PipelinePlugin>) };
    Some(*outer)
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

    struct DummyPlugin;
    impl PipelinePlugin for DummyPlugin {
        fn execute(&self, _ectx: &ExecContext) -> Result<String, String> {
            Ok(r#"{"ok":true}"#.into())
        }
    }

    #[test]
    fn raw_pointer_roundtrip() {
        let ptr = plugin_into_raw(DummyPlugin);
        assert!(!ptr.is_null());
        // SAFETY: ptr 来自 plugin_into_raw，仅还原一次。
        let boxed = unsafe { box_from_raw(ptr) }.unwrap();
        let ectx = ExecContext { ctx: PluginCtx::default(), host: None };
        assert_eq!(boxed.execute(&ectx).unwrap(), r#"{"ok":true}"#);
    }

    #[test]
    fn null_pointer_returns_none() {
        // SAFETY: null 指针，box_from_raw 内部判空返回 None。
        assert!(unsafe { box_from_raw(std::ptr::null_mut()) }.is_none());
    }
}
