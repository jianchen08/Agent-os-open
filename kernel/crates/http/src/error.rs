//! 统一错误类型（api 与 db-admin 共用）

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;
use thiserror::Error;

#[derive(Debug, Clone, Error)]
pub enum ApiError {
    #[error("bad request: {message}")]
    BadRequest { message: String },

    #[error("unauthorized: {message}")]
    Unauthorized { message: String },

    #[error("forbidden: {message}")]
    Forbidden { message: String },

    #[error("not found: {message}")]
    NotFound { message: String },

    /// 409 Conflict——配置写冲突（ETag/If-Match 不匹配，B4 乐观锁）。
    #[error("conflict: {message}")]
    Conflict { message: String },

    #[error("internal error: {message}")]
    Internal { message: String },

    /// 503 Service Unavailable——所需依赖（如存储后端）未就绪/未注入。
    #[error("service unavailable: {message}")]
    ServiceUnavailable { message: String },

    #[error("websocket error: {message}")]
    WebSocket { message: String },
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let (status, message) = match &self {
            ApiError::BadRequest { message } => (StatusCode::BAD_REQUEST, message.clone()),
            ApiError::Unauthorized { message } => (StatusCode::UNAUTHORIZED, message.clone()),
            ApiError::Forbidden { message } => (StatusCode::FORBIDDEN, message.clone()),
            ApiError::NotFound { message } => (StatusCode::NOT_FOUND, message.clone()),
            ApiError::Conflict { message } => (StatusCode::CONFLICT, message.clone()),
            // A12：内部错误细节（IO 报错含路径、底层库错误串等）不透传给客户端，
            // 避免泄漏服务端内部结构；细节完整保留在服务端 tracing（无 request_id
            // 基础设施，以 error 字段 + target 定位）。对外固定通用文案。
            ApiError::Internal { message } => {
                tracing::error!(
                    target: "api-error",
                    status = StatusCode::INTERNAL_SERVER_ERROR.as_u16(),
                    error = %message,
                    "internal error"
                );
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "internal server error".to_string(),
                )
            }
            ApiError::ServiceUnavailable { message } => {
                (StatusCode::SERVICE_UNAVAILABLE, message.clone())
            }
            ApiError::WebSocket { message } => (StatusCode::INTERNAL_SERVER_ERROR, message.clone()),
        };

        let body = Json(json!({
            "error": {
                "code": status.as_u16().to_string(),
                "message": message,
            }
        }));

        (status, body).into_response()
    }
}
