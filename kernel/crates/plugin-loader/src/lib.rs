//! # Lingxi Plugin Loader — 插件加载器
//!
//! 负责从文件系统发现插件、解析 manifest、验证 Schema、按需实例化。
//! 遵循按需加载全局原则：首次调用时才启动进程，空闲超时自动卸载。
//!
//! ## 模块组织
//!
//! - `loader`: 插件加载器实现——双根扫描、manifest 解析校验、按需加载
//! - `registry`: 能力注册表 + 依赖解析器
//! - `capability_provider`: 把 manifest 的 provides.capabilities 注册成 CapabilityHandler（M4）
//! - `error`: 错误类型
//!
//! [来源: docs/0.2_rust_plugin_solution.md §3.7]
//! [来源: docs/tasks/task_05_plugin_system.md]

pub mod capability_provider;
pub mod enablement;
pub mod error;
pub mod loader;
pub mod native_loader;
pub mod registry;

pub use capability_provider::{
    register_provided_capabilities, CapabilityBridge, CapabilityRoute, McpBridge,
    ProvidedCapabilityHandler,
};
pub use enablement::{PluginEnablement, PluginProfile, ProfileEntry};
pub use error::LoaderError;
pub use loader::{AllowlistConfig, AllowlistEntry, AllowlistMode, PluginLoaderImpl};
pub use native_loader::NativePluginLoader;
pub use registry::{
    dependency_error_for, output_schema_error, provides_methods_unbacked,
    sort_manifests_topologically, validate_dependencies, version_gte, CapabilityRegistryImpl,
    DependencyResolverImpl, PluginScope, PluginScopeRegistry,
};
