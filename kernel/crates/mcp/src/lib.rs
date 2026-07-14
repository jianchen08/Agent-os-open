//! # Lingxi MCP — MCP 协议客户端
//!
//! 基于 rmcp（Anthropic 官方 Rust MCP SDK）封装，提供进程管理和崩溃隔离。
//! 内核层在 rmcp 之上做进程管理 + 错误处理 + 崩溃隔离封装，不重写协议层。
//!
//! [来源: docs/0.2_rust_plugin_solution.md §3.1.1]
