//! # Lingxi Invoker — 插件调用器
//!
//! 按 host_type 透明分发调用：
//! - InProcess: 直接调用 `dyn PipelinePlugin::execute`（零 IPC 开销）
//! - Sidecar: 通过 rmcp 客户端走 MCP 协议调用（进程隔离）
//!
//! 两种路径对管道引擎透明——统一返回 `PluginResult`。
//!
//! [来源: docs/0.2_rust_plugin_solution.md §3.2]
