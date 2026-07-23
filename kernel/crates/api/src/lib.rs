//! # Lingxi API — HTTP/WebSocket API 服务器
//!
//! 基于 Axum 0.8 构建，提供 RESTful API 和 WebSocket 流式响应。
//!
//! ## 模块组织
//!
//! - `server`: Axum HTTP/WebSocket 服务器——路由树、WebSocket 连接处理
//! - `routes`: HTTP 路由处理器——健康检查、Schema 聚合、能力清单
//! - `error`: API 错误类型
//!
//! [来源: docs/0.2_rust_plugin_solution.md §2.2 Web 框架映射]
//! [来源: docs/tasks/task_07_llm_api.md]

pub mod auth;
pub mod capability_router;
pub mod config_service;
pub mod error;
pub mod pipeline_loader;
pub mod routes;
pub mod server;
pub mod ws_session;

pub use auth::{
    login_handler, logout_handler, me_handler, refresh_handler, register_handler, RefreshResponse,
    RegisterRequest,
};
pub use error::ApiError;
pub use pipeline_loader::{
    load_pipeline_config, load_step_library, validate_no_name_conflicts, PipelineLoadError,
};
pub use routes::{AppState, HealthResponse, SchemaResponse};
pub use server::{build_router, start_server, WsRequest, WsResponse};
pub use capability_router::KernelCapabilityRouter;
