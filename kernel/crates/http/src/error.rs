//! 统一错误类型（api 与 db-admin 共用）
//!
//! 对外信封：`{"error": {"code", "message", "source", "retryable", "details", "request_id"}}`。
//! code 为稳定机器码（单一真值源 `config/kernel/error_codes.json`，机械闸测试锁一致）；
//! HTTP 状态码由变体决定，不是 code 的一部分。

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;
use thiserror::Error;

/// 错误来源枚举（与 `config/kernel/error_codes.json` 的 sources.enum 一致，机械闸测试锁一致）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorSource {
    Kernel,
    Plugin,
    Llm,
    Infra,
    Frontend,
}

impl ErrorSource {
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorSource::Kernel => "kernel",
            ErrorSource::Plugin => "plugin",
            ErrorSource::Llm => "llm",
            ErrorSource::Infra => "infra",
            ErrorSource::Frontend => "frontend",
        }
    }
}

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

    /// 422 Unprocessable Entity——请求语义可解但目标资源状态不允许该操作
    /// （如目标配置文件已损坏，写入会破坏现场，拒绝执行）。
    #[error("unprocessable entity: {message}")]
    UnprocessableEntity { message: String },

    /// 429 Too Many Requests——限流拒绝（登录失败滑动窗口 / 注册限频）。
    /// `retry_after_secs` 写入 Retry-After 响应头，调用方据此退避。
    #[error("too many requests: {message}")]
    TooManyRequests {
        message: String,
        retry_after_secs: u64,
    },

    #[error("internal error: {message}")]
    Internal { message: String },

    /// 503 Service Unavailable——所需依赖（如存储后端）未就绪/未注入。
    #[error("service unavailable: {message}")]
    ServiceUnavailable { message: String },

    #[error("websocket error: {message}")]
    WebSocket { message: String },
}

impl ApiError {
    /// `map_err` 样板收口：上下文前缀 + 底层错误 Display。
    ///
    /// routes.rs / db_routes.rs 原同构闭包 ≥46 处（`.map_err(|e| ApiError::Internal
    /// { message: format!("ctx: {e}") })?`），统一为
    /// `.map_err(ApiError::internal("ctx"))?` 一行。上下文须为静态串——
    /// 需拼接动态信息（路径等）的少数调用点保留原闭包形态。
    pub fn internal<E: std::fmt::Display>(ctx: &'static str) -> impl Fn(E) -> ApiError {
        move |e| ApiError::Internal {
            message: format!("{ctx}: {e}"),
        }
    }

    /// 稳定机器码（单一真值源 `config/kernel/error_codes.json`）。
    pub fn error_code(&self) -> &'static str {
        match self {
            ApiError::BadRequest { .. } => "BAD_REQUEST",
            ApiError::Unauthorized { .. } => "UNAUTHORIZED",
            ApiError::Forbidden { .. } => "FORBIDDEN",
            ApiError::NotFound { .. } => "RESOURCE_NOT_FOUND",
            ApiError::Conflict { .. } => "CONFLICT",
            ApiError::UnprocessableEntity { .. } => "UNPROCESSABLE_ENTITY",
            ApiError::TooManyRequests { .. } => "TOO_MANY_REQUESTS",
            ApiError::Internal { .. } => "INTERNAL_ERROR",
            ApiError::ServiceUnavailable { .. } => "SERVICE_UNAVAILABLE",
            ApiError::WebSocket { .. } => "WEBSOCKET_ERROR",
        }
    }

    pub fn source(&self) -> ErrorSource {
        ErrorSource::Kernel
    }

    /// 是否可重试（网络/瞬时故障类为 true，语义错误为 false）。
    pub fn retryable(&self) -> bool {
        matches!(
            self,
            ApiError::Internal { .. }
                | ApiError::ServiceUnavailable { .. }
                | ApiError::TooManyRequests { .. }
        )
    }

    pub fn message(&self) -> &str {
        match self {
            ApiError::BadRequest { message }
            | ApiError::Unauthorized { message }
            | ApiError::Forbidden { message }
            | ApiError::NotFound { message }
            | ApiError::Conflict { message }
            | ApiError::UnprocessableEntity { message }
            | ApiError::TooManyRequests { message, .. }
            | ApiError::Internal { message }
            | ApiError::ServiceUnavailable { message }
            | ApiError::WebSocket { message } => message,
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        use axum::http::header;

        let status = match &self {
            ApiError::BadRequest { .. } => StatusCode::BAD_REQUEST,
            ApiError::Unauthorized { .. } => StatusCode::UNAUTHORIZED,
            ApiError::Forbidden { .. } => StatusCode::FORBIDDEN,
            ApiError::NotFound { .. } => StatusCode::NOT_FOUND,
            ApiError::Conflict { .. } => StatusCode::CONFLICT,
            ApiError::UnprocessableEntity { .. } => StatusCode::UNPROCESSABLE_ENTITY,
            ApiError::TooManyRequests { .. } => StatusCode::TOO_MANY_REQUESTS,
            ApiError::Internal { .. } => StatusCode::INTERNAL_SERVER_ERROR,
            ApiError::ServiceUnavailable { .. } => StatusCode::SERVICE_UNAVAILABLE,
            ApiError::WebSocket { .. } => StatusCode::INTERNAL_SERVER_ERROR,
        };

        let mut response_headers: Vec<(header::HeaderName, String)> = Vec::new();
        if let ApiError::TooManyRequests {
            retry_after_secs, ..
        } = &self
        {
            response_headers.push((
                header::HeaderName::from_static("retry-after"),
                retry_after_secs.to_string(),
            ));
        }

        // 内部错误细节（IO 报错含路径、底层库错误串等）原文透传不脱敏，
        // 同时完整保留在服务端 tracing（target: "api-error"）供定位。
        if let ApiError::Internal { message } = &self {
            tracing::error!(
                target: "api-error",
                status = status.as_u16(),
                error = %message,
                "internal error"
            );
        }

        let body = Json(json!({
            "error": {
                "code": self.error_code(),
                "message": self.message(),
                "source": self.source().as_str(),
                "retryable": self.retryable(),
                "details": null,
                "request_id": null,
            }
        }));

        let mut response = (status, body).into_response();
        for (name, value) in response_headers {
            if let Ok(v) = axum::http::HeaderValue::from_str(&value) {
                response.headers_mut().insert(name, v);
            }
        }
        response
    }
}
