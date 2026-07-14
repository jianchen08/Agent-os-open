//! # Lingxi Config — 配置加载器
//!
//! 负责加载和解析 YAML 配置文件（管道配置、Agent 配置、工具配置等）。
//! 使用 figment 统一配置管理，支持 `${VAR}` 环境变量插值。
//!
//! [来源: docs/0.2_rust_plugin_solution.md §3.3]
