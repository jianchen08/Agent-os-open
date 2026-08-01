//! # Lingxi AgentOS 原生插件 SDK（abi_stable FFI-safe）
//!
//! 这是 abi_stable 的 **interface crate**——被内核（host/user crate）和插件
//! （implementation cdylib）共同依赖，定义跨 cdylib 边界的 FFI-safe 契约。
//!
//! ## 为什么用 abi_stable
//!
//! Rust 的 `dyn Trait` vtable 跨 rustc 版本不保证稳定。若插件和内核用不同
//! rustc 编译，`Box<dyn PipelinePlugin>` 可能 UB。abi_stable 的 `#[sabi_trait]`
//! 生成 FFI-safe 的 trait 对象，**跨 rustc 版本安全**——让内核升级 rustc 时
//! 不必强求所有插件同步重编。
//!
//! ## 三方角色
//!
//! - **interface crate**（本 crate）：声明 `PipelinePlugin` / `HostServices` trait
//!   + RootModule 加载契约。
//! - **implementation crate**（插件，如 tool_core）：`impl PipelinePlugin` +
//!   `#[export_root_module]` 导出构造函数，编译为 cdylib。
//! - **user crate**（内核 NativePluginLoader）：`load_root_module` 加载插件 cdylib，
//!   拿 `PipelinePlugin_TO` trait 对象直接调 `execute`。
//!
//! ## 依赖方向
//!
//! 插件要调内核 capability（执行工具 / 发事件）。约定：内核构造 `PluginCtx` 时
//! 注入一个 `HostServices` 实现（包 CapabilityRouter）；插件在 `execute` 里经
//! `ctx.host` 调用——与 sidecar（JSON-RPC）、wasm（host.call import）走同一 router。
//!
//! ## 关于 unsafe
//!
//! 本 crate 不手写 unsafe。abi_stable 的 `#[sabi_trait]` / `#[derive(StableAbi)]`
//! 宏会生成必要的 unsafe 实现这是 FFI 的本质），其安全性由 abi_stable 保证。
//! 插件作者代码（impl 块）零 unsafe。

// 不加 forbid(unsafe_code)——会与 abi_stable 宏生成的 unsafe 冲突。

use abi_stable::{
    declare_root_module_statics, library::RootModule, package_version_strings,
    prefix_type::PrefixTypeTrait, sabi_trait, sabi_types::VersionStrings, StableAbi,
    std_types::{ROption, RResult, RString},
};

pub use abi_stable;
pub use serde_json;

// ── HostServices：插件 → 内核 capability 调用（FFI-safe）──────────────

/// 插件调用内核 capability 的句柄。
///
/// 内核实现此 trait（包 `CapabilityRouter`），构造 `PluginCtx` 时注入。
/// 插件经它调 `tool-executor.invoke`（执行工具）/ `event-bus.emit`（发事件）等
/// capability——与 sidecar 经 JSON-RPC 反调内核走同一 router，能力等价。
///
/// 方法用 `RResult` / `RString`（abi_stable FFI-safe 类型）而非 `Result` / `String`，
/// 这是跨 cdylib 边界的必要条件。
#[sabi_trait]
pub trait HostServices: Send + Sync {
    /// 调用内核 capability，返回结果 JSON。
    ///
    /// - `capability`：能力名（如 `"tool-executor"` / `"event-bus"`）。
    /// - `method`：方法名（如 `"invoke"` / `"emit"`）。
    /// - `params_json`：方法参数（序列化后的 JSON 字符串）。
    ///
    /// 返回 `ROk(result_json)` 或 `RErr(error_msg)`。
    #[sabi(last_prefix_field)]
    fn call_capability(
        &self,
        capability: RString,
        method: RString,
        params_json: RString,
    ) -> RResult<RString, RString>;
}

/// `HostServices` trait 对象的类型别名（`'static` 生命周期 + `RArc` 指针）。
///
/// 内核构造时用 `HostServices_TO::from_value(...)` 包实现；
/// 插件持有 `RArc<HostServices_TO<'static, RBox<()>>>` 调用。
pub type HostServicesBox = HostServices_TO<'static, abi_stable::std_types::RBox<()>>;

// ── PipelinePlugin：插件实现的主契约（FFI-safe）──────────────────────

/// 原生管道插件契约。插件（如 tool_core）`impl` 此 trait。
///
/// `execute` 接收 `PluginCtx`（含 state/config/host 句柄），返回 state 更新 Patch。
/// 与内核 `agentos_core::PipelinePlugin` 概念对齐，但本 trait 是 FFI-safe 的，
/// 用于跨 cdylib 边界；那个 core trait 是进程内抽象，二者不冲突。
#[sabi_trait]
pub trait PipelinePlugin: Send + Sync {
    /// 执行插件逻辑。
    ///
    /// - `ctx`：上下文（state/config/tenant/host capability 句柄）。
    /// - 返回 `ROk(state_updates_json)`（state 更新 Patch，序列化为 JSON 字符串）
    ///   或 `RErr(error_msg)`。
    ///
    /// state_updates 用 JSON 字符串传递（而非 `HashMap<String, Value>`），
    /// 因为 serde_json::Value 在 abi_stable 边界需额外包一层，字符串最简最稳。
    #[sabi(last_prefix_field)]
    fn execute(&self, ctx: PluginCtxRef) -> RResult<RString, RString>;
}

// ── PluginCtx：传给 execute 的上下文 ──────────────────────────────────

/// 传给插件 `execute` 的上下文（FFI-safe）。
///
/// 字段都是 abi_stable FFI-safe 类型：`RString`（替代 String）、`RArc`（替代 Arc）。
/// `state` / `config` 用 `RString`（JSON 字符串）传递，插件侧自行解析为 Value。
#[repr(C)]
#[derive(StableAbi)]
pub struct PluginCtx {
    /// 管道状态（JSON 字符串）。
    pub state_json: RString,
    /// 插件配置（JSON 字符串）。
    pub config_json: RString,
    /// 租户 ID。
    pub tenant_id: RString,
    /// 会话 ID。
    pub session_id: RString,
    /// 任务 ID。
    pub task_id: RString,
    /// 管道 ID。
    pub pipeline_id: RString,
    /// 内核 capability 句柄（None = 未注入，插件降级跳过 capability 调用）。
    pub host: abi_stable::std_types::ROption<HostServicesBox>,
}

impl PluginCtx {
    /// 解析 state_json 为 serde_json::Value（插件侧便利方法）。
    pub fn state_value(&self) -> serde_json::Value {
        serde_json::from_str(self.state_json.as_str()).unwrap_or(serde_json::Value::Null)
    }

    /// 解析 config_json 为 serde_json::Value。
    pub fn config_value(&self) -> serde_json::Value {
        serde_json::from_str(self.config_json.as_str()).unwrap_or(serde_json::Value::Null)
    }
}

/// `PluginCtx` 的按引用传递别名（sabi_trait 方法签名用）。
pub type PluginCtxRef<'a> = &'a PluginCtx;

// ── RootModule：cdylib 加载契约 ───────────────────────────────────────

/// 插件 cdylib 导出的根模块——含一个构造函数，返回 `PipelinePlugin` trait 对象。
///
/// `#[sabi(kind(Prefix(...)))]` 生成 FFI-safe 的静态引用类型 `NativePluginModule_Ref`，
/// 内核加载 cdylib 时拿这个引用调构造函数。
///
/// `#[sabi(missing_field(panic))]`：访问不存在的字段时 panic（版本不匹配的早期发现）。
#[repr(C)]
#[derive(StableAbi)]
#[sabi(kind(Prefix(prefix_ref = NativePluginModule_Ref)))]
#[sabi(missing_field(panic))]
pub struct NativePluginModule {
    /// 构造插件实例，返回 `PipelinePlugin` trait 对象（装箱在 `RBox`）。
    ///
    /// 插件实现这个函数指针，内部 `PipelinePlugin_TO::from_value(MyPlugin, TD_Opaque)`。
    #[sabi(last_prefix_field)]
    pub create_plugin: extern "C" fn() -> PipelinePlugin_TO<'static, abi_stable::std_types::RBox<()>>,
}

/// RootModule trait 实现——定义 abi_stable 如何加载本模块。
impl RootModule for NativePluginModule_Ref {
    declare_root_module_statics! {NativePluginModule_Ref}

    const BASE_NAME: &'static str = "agentos_native_plugin";
    const NAME: &'static str = "agentos_native_plugin";
    const VERSION_STRINGS: VersionStrings = package_version_strings!();
}

/// 便捷：插件构造 trait 对象（隐藏 TD_Opaque 细节）。
///
/// 插件实现侧用：`create_plugin_value(my_impl)` → 返回 `PipelinePlugin_TO`。
pub fn create_plugin_value<T>(plugin: T) -> PipelinePlugin_TO<'static, abi_stable::std_types::RBox<()>>
where
    T: PipelinePlugin + 'static,
{
    use abi_stable::sabi_trait::prelude::TD_Opaque;
    PipelinePlugin_TO::from_value(plugin, TD_Opaque)
}

// ── 重导出常用项，简化插件作者 import ─────────────────────────────────

pub mod prelude {
    pub use crate::{
        create_plugin_value, HostServices, HostServicesBox, NativePluginModule,
        NativePluginModule_Ref, PipelinePlugin, PluginCtx,
    };
    pub use abi_stable::{
        export_root_module, sabi_extern_fn, sabi_trait::prelude::TD_Opaque,
        std_types::{RBox, ROption, RResult, RString, RVec},
        StableAbi,
    };
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plugin_ctx_state_parses() {
        let ctx = PluginCtx {
            state_json: RString::from(r#"{"k":1}"#),
            config_json: RString::from("{}"),
            tenant_id: RString::from("t1"),
            session_id: RString::from("s1"),
            task_id: RString::from(""),
            pipeline_id: RString::from("p1"),
            host: ROption::RNone,
        };
        assert_eq!(ctx.state_value()["k"], 1);
    }
}
