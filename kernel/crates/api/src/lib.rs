//! # AgentOS API — HTTP/WebSocket API 服务器
//!
//! 基于 Axum 0.8 构建，提供 RESTful API 和 WebSocket 流式响应。
//!
//! ## 模块组织
//!
//! - `server`: Axum HTTP/WebSocket 服务器——路由树、WebSocket 连接处理
//! - `routes`: HTTP 路由处理器——健康检查、Schema 聚合、能力清单
//!
//! [来源: docs/0.2_rust_plugin_solution.md §2.2 Web 框架映射]
//! [来源: docs/tasks/task_07_llm_api.md]

pub mod auth;
pub mod capability_router;
pub mod chat_send_handler;
pub mod config_service;
pub mod contract;
pub mod http_dispatcher;
pub mod kernel_capabilities;
pub mod metrics;
pub mod pipeline_loader;
pub mod plugin_lifecycle;
pub mod plugin_watcher;
pub mod routes;
pub mod run_chain;
pub mod server;
pub mod session_routes;
pub mod tools;
pub mod ws_session;

pub use capability_router::KernelCapabilityRouter;
pub use pipeline_loader::{load_pipeline_config, load_step_library, validate_no_name_conflicts};
pub use server::start_server;
