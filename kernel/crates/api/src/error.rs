//! API 错误类型

use thiserror::Error;

#[derive(Debug, Clone, Error)]
pub enum ApiError {
    #[error("bad request: {message}")]
    BadRequest { message: String },

    #[error("not found: {message}")]
    NotFound { message: String },

    #[error("internal error: {message}")]
    Internal { message: String },

    #[error("websocket error: {message}")]
    WebSocket { message: String },
}
