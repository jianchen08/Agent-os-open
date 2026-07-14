//! # Lingxi MCP — MCP 协议客户端
//!
//! 实现基于 JSON-RPC 2.0 的 MCP 协议客户端，支持 stdio 和 HTTP 两种 transport。
//! 通过 stdin/stdout 与 Python 边车进程通信，完成 initialize 握手和 tools/call 调用。
//!
//! ## 模块组织
//!
//! - `client`: MCP 客户端——子进程管理、JSON-RPC 通信、initialize 握手、tools/call
//! - `error`: MCP 错误类型
//!
//! [来源: docs/0.2_rust_plugin_solution.md §3.1.1]
//! [来源: docs/tasks/task_05_plugin_system.md AC-04-4]

pub mod client;
pub mod error;

pub use client::{McpClient, McpTransport};
pub use error::McpError;
