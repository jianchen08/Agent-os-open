//! API 错误类型

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

    #[error("not found: {message}")]
    NotFound { message: String },

    /// 409 Conflict——配置写冲突（ETag/If-Match 不匹配，B4 乐观锁）。
    #[error("conflict: {message}")]
    Conflict { message: String },

    #[error("internal error: {message}")]
    Internal { message: String },

    #[error("websocket error: {message}")]
    WebSocket { message: String },
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let (status, message) = match &self {
            ApiError::BadRequest { message } => (StatusCode::BAD_REQUEST, message.clone()),
            ApiError::Unauthorized { message } => (StatusCode::UNAUTHORIZED, message.clone()),
            ApiError::NotFound { message } => (StatusCode::NOT_FOUND, message.clone()),
            ApiError::Conflict { message } => (StatusCode::CONFLICT, message.clone()),
            ApiError::Internal { message } => (StatusCode::INTERNAL_SERVER_ERROR, message.clone()),
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
