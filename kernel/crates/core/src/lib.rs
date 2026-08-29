//! # Lingxi AgentOS 0.2 — Kernel Core Library
//!
//! 本 crate 是 0.2 架构的"宪法层"，定义内核与所有插件之间的接口契约。
//! 所有内核组件和插件（Rust 原生 / MCP 边车 / Python SDK）都围绕本 crate 的 trait 构建。
//!
//! ## 模块组织
//!
//! - [`traits`]: 插件抽象接口——PluginInvoker（透明分发 in_process / sidecar）、
//!   CapabilityRegistry、PluginLoader、StorageBackend（ADR ③④）
//! - [`types`]: 共享数据结构——PluginContext（含 ContentLoader ADR ⑦）、
//!   PluginResult、PluginError、TenantContext、SQLite 四表模型（ADR ④）、
//!   多分支模型（ADR ⑤，RunStatus::from_control_state 终态映射单点）
//!
//! ## 设计决策
//!
//! - 管道插件混合方案（Rust 原生 + MCP 边车）：[方案总纲 §3.2]
//! - 路由信号精简为 4 种：[方案总纲 §3.5]
//! - 按需加载全局原则：[方案总纲 §3.7]
//! - 多租户上下文穿透：[方案总纲 §3.4]
//!
//! ## ADR 修订（v2.0）
//!
//! - HookContext 改为标签化动态上下文 HashMap（ADR ⑨）
//! - PluginType 新增 Composite 组合插件类型（ADR ⑥）
//! - 所有插件均支持 InProcess + Sidecar 双路径（ADR ⑧）
//! - 新增 StorageBackend trait——SQLite 四表存储抽象（ADR ③④）
//! - PluginContext 新增 ContentLoader 实现内容懒加载（ADR ⑦）
//! - PluginManifest 新增 requires_content 字段（ADR ⑦）
//!
//! [方案总纲 §3.2]: docs/0.2_rust_plugin_solution.md
//! [方案总纲 §3.5]: docs/0.2_rust_plugin_solution.md
//! [方案总纲 §3.7]: docs/0.2_rust_plugin_solution.md
//! [方案总纲 §3.4]: docs/0.2_rust_plugin_solution.md

pub mod ids;
pub mod traits;
pub mod types;
