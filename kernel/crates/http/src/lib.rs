//! 内核 HTTP 共享基础（api / db-admin 共用）
//!
//! - [`error::ApiError`]：对外统一错误信封（`{"error": {"code", "message"}}`）
//! - [`auth`]：请求用户解析 + token 编解码（鉴权逻辑单一来源，避免跨 crate 复制漂移）
//!
//! 拆分背景：db-admin 独立 crate 后无法依赖 api（循环依赖），但管理面端点
//! （`/api/v1/db/*`）与 api 共用的错误类型与用户解析下沉至此，两方共同依赖。

pub mod auth;
pub mod error;

pub use error::ApiError;
