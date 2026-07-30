//! # Lingxi Plugin Loader — 插件加载器
//!
//! 负责从文件系统发现插件、解析 manifest、验证 Schema、按需实例化。
//! 遵循按需加载全局原则：首次调用时才启动进程，空闲超时自动卸载。
//!
//! ## 模块组织
//!
//! - `loader`: 插件加载器实现——双根扫描、manifest 解析校验、按需加载
//! - `registry`: 能力注册表 + 依赖解析器
//! - `error`: 错误类型
//!
//! [来源: docs/0.2_rust_plugin_solution.md §3.7]
//! [来源: docs/tasks/task_05_plugin_system.md]

pub mod enablement;
pub mod error;
pub mod loader;
pub mod native_loader;
pub mod registry;
pub mod wasm_loader;

pub use enablement::{PluginEnablement, PluginProfile, ProfileEntry};
pub use error::LoaderError;
pub use loader::{AllowlistConfig, AllowlistEntry, AllowlistMode, PluginLoaderImpl};
pub use native_loader::NativePluginLoader;
pub use registry::{CapabilityRegistryImpl, DependencyResolverImpl};
pub use wasm_loader::{
    HostCapability, WasmCapabilityChecker, WasmHostRegistry, WasmRuntime, WasmRuntimeConfig,
    WASM_DEALLOC_FN, WASM_EXECUTE_FN, WASM_MEM_EXPORT,
};
