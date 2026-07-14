//! # Lingxi API — HTTP/WebSocket API 服务器
//!
//! 基于 Axum 0.8.x 构建，提供 RESTful API 和 WebSocket 流式响应。
//! 使用 tower middleware 链实现认证、租户上下文注入等横切关注点。
//!
//! [来源: docs/0.2_rust_plugin_solution.md §2.2 Web 框架映射]
