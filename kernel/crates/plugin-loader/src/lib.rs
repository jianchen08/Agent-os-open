//! # Lingxi Plugin Loader — 插件加载器
//!
//! 负责从文件系统发现插件、解析 manifest、验证 Schema、按需实例化。
//! 遵循按需加载全局原则：首次调用时才启动进程，空闲超时自动卸载。
//!
//! [来源: docs/0.2_rust_plugin_solution.md §3.7]
