//! # Lingxi AgentOS 0.2 — Kernel Core Library
//!
//! 本 crate 是 0.2 架构的"宪法层"，定义内核与所有插件之间的接口契约。
//! 所有内核组件和插件（Rust 原生 / MCP 边车 / Python SDK）都围绕本 crate 的 trait 构建。
//!
//! ## 模块组织
//!
//! - [`traits`]: 插件抽象接口——PipelinePlugin（含 Input/Core/Output 子 trait）、
//!   PluginInvoker（透明分发 in_process / sidecar）、CapabilityRegistry、
//!   DependencyResolver、LlmProvider、PluginLoader
//! - [`types`]: 共享数据结构——RouteSignal（4 种，移除了 Delegate/Fork/Decision）、
//!   ErrorPolicy、PluginContext、PluginResult、PluginError、TenantContext 等
//!
//! ## 设计决策
//!
//! - 管道插件混合方案（Rust 原生 + MCP 边车）：[方案总纲 §3.2]
//! - 路由信号精简为 4 种：[方案总纲 §3.5]
//! - 按需加载全局原则：[方案总纲 §3.7]
//! - 多租户上下文穿透：[方案总纲 §3.4]
//!
//! [方案总纲 §3.2]: docs/0.2_rust_plugin_solution.md
//! [方案总纲 §3.5]: docs/0.2_rust_plugin_solution.md
//! [方案总纲 §3.7]: docs/0.2_rust_plugin_solution.md
//! [方案总纲 §3.4]: docs/0.2_rust_plugin_solution.md

pub mod traits;
pub mod types;
