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

pub mod allocator;
pub mod auth;
pub mod capability_router;
pub mod chat_send_handler;
pub mod config_service;
pub mod contract;
pub mod family_watch;
pub mod http_dispatcher;
pub mod kernel_capabilities;
pub mod log_liveness;
pub mod metrics;
pub mod mode_view;
pub mod panic_log;
pub mod pipeline_loader;
pub mod plugin_lifecycle;
pub mod plugin_notices;
pub mod plugin_watcher;
pub mod routes;
pub mod run_chain;
pub mod run_signal;
pub mod server;
pub mod session_routes;
pub mod sidecar_warmup;
pub mod system_diagnostics;
/// 测试期用户空间隔离钉桩。编进库本体（非 cfg(test)）：integration tests
/// 链接的是非 cfg(test) 产物，须 pub 可达方可复用同一套锁+恢复语义。
pub mod test_env;
pub mod timeout_nesting;
pub mod tools;
pub mod venv_provision;
pub mod ws_events;
pub mod ws_session;
pub mod ws_ticket;

pub use capability_router::KernelCapabilityRouter;
pub use pipeline_loader::{
    load_pipeline_config, load_pipeline_config_by_name, load_step_library,
    validate_no_name_conflicts,
};
pub use server::start_server;
