//! # Lingxi Invoker — 插件调用器
//!
//! 按 host_type 透明分发调用：
//! - InProcess: 直接调用 `dyn PipelinePlugin::execute`（零 IPC 开销）
//! - Sidecar: 通过 MCP 客户端走 JSON-RPC 协议调用（进程隔离）
//!
//! 两种路径对管道引擎透明——统一返回 `PluginResult`。
//!
//! ## 模块组织
//!
//! - `invoker`: PluginInvoker 实现——透明分发、MCP 调用、崩溃隔离
//!
//! [来源: docs/0.2_rust_plugin_solution.md §3.2]
//! [来源: docs/tasks/task_05_plugin_system.md AC-04-5/AC-04-6]

pub mod invoker;

pub use invoker::PluginInvokerImpl;
