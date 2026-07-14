//! # Lingxi Tenant — 多租户上下文
//!
//! 通过 `tokio::task_local!` 穿透整个异步调用栈，插件代码无需感知租户参数。
//! 跨 `tokio::spawn` 会丢失（除非显式 scope），天然防止跨管道/跨租户泄露。
//!
//! [来源: docs/0.2_rust_plugin_solution.md §3.4]
