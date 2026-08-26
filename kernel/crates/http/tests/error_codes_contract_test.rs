//! 机械闸：`config/error_codes.json`（单一真值源）↔ `ApiError` 代码副本锁一致。
//!
//! 仿 kernel_capabilities 机械闸模式（读真实仓库文件断言，不读代码副本）：
//! - 9 个 ApiError 变体的 code/source/retryable 与 json 逐条一致；
//! - 信封序列化形状（code/message/source/retryable/details/request_id）与 json 语义一致；
//! - Internal 原文透传不脱敏（用户裁定）。

use axum::http::StatusCode;
use axum::response::IntoResponse;
use agentos_http::error::ApiError;
use serde_json::Value;

/// 仓库 config/error_codes.json 的绝对路径（CARGO_MANIFEST_DIR 相对，同 kernel_capabilities 模式）。
fn repo_error_codes_path() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("..")
        .join("config")
        .join("error_codes.json")
}

fn load_error_codes() -> Value {
    let path = repo_error_codes_path();
    let raw = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("读取 error_codes.json 失败（{path:?}）: {e}"));
    serde_json::from_str(&raw).unwrap_or_else(|e| panic!("error_codes.json 解析失败: {e}"))
}

/// 按 code 查 json 码表条目。
fn code_entry<'a>(doc: &'a Value, code: &str) -> &'a Value {
    doc["codes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["code"] == code)
        .unwrap_or_else(|| panic!("码表缺 code={code}"))
}

/// 9 个 ApiError 变体 → (code, source, retryable) 的代码侧真值。
fn api_error_truth() -> Vec<(ApiError, &'static str, &'static str, bool)> {
    vec![
        (ApiError::BadRequest { message: "m".into() }, "BAD_REQUEST", "kernel", false),
        (ApiError::Unauthorized { message: "m".into() }, "UNAUTHORIZED", "kernel", false),
        (ApiError::Forbidden { message: "m".into() }, "FORBIDDEN", "kernel", false),
        (ApiError::NotFound { message: "m".into() }, "RESOURCE_NOT_FOUND", "kernel", false),
        (ApiError::Conflict { message: "m".into() }, "CONFLICT", "kernel", false),
        (
            ApiError::UnprocessableEntity { message: "m".into() },
            "UNPROCESSABLE_ENTITY",
            "kernel",
            false,
        ),
        (ApiError::Internal { message: "m".into() }, "INTERNAL_ERROR", "kernel", true),
        (
            ApiError::ServiceUnavailable { message: "m".into() },
            "SERVICE_UNAVAILABLE",
            "kernel",
            true,
        ),
        (ApiError::WebSocket { message: "m".into() }, "WEBSOCKET_ERROR", "kernel", false),
    ]
}

fn status_of(err: &ApiError) -> StatusCode {
    match err {
        ApiError::BadRequest { .. } => StatusCode::BAD_REQUEST,
        ApiError::Unauthorized { .. } => StatusCode::UNAUTHORIZED,
        ApiError::Forbidden { .. } => StatusCode::FORBIDDEN,
        ApiError::NotFound { .. } => StatusCode::NOT_FOUND,
        ApiError::Conflict { .. } => StatusCode::CONFLICT,
        ApiError::UnprocessableEntity { .. } => StatusCode::UNPROCESSABLE_ENTITY,
        ApiError::Internal { .. } => StatusCode::INTERNAL_SERVER_ERROR,
        ApiError::ServiceUnavailable { .. } => StatusCode::SERVICE_UNAVAILABLE,
        ApiError::WebSocket { .. } => StatusCode::INTERNAL_SERVER_ERROR,
    }
}

/// 闸 1：9 个变体的 code/source/retryable 与 json 码表逐条一致。
#[test]
fn api_error_variants_match_error_codes_json() {
    let codes = load_error_codes();
    for (err, code, source, retryable) in api_error_truth() {
        let entry = code_entry(&codes, code);
        assert_eq!(err.error_code(), code, "error_code() 与码表 code 不一致");
        assert_eq!(
            err.source().as_str(),
            source,
            "source 与码表不一致（code={code}）"
        );
        assert_eq!(
            err.retryable(),
            retryable,
            "retryable 与码表不一致（code={code}）"
        );
        assert_eq!(
            entry["source"], source,
            "码表 source 与代码副本不一致（code={code}）"
        );
        assert_eq!(
            entry["retryable"], retryable,
            "码表 retryable 与代码副本不一致（code={code}）"
        );
        assert_eq!(
            entry["http_status"].as_u64().unwrap(),
            status_of(&err).as_u16() as u64,
            "码表 http_status 与变体状态码不一致（code={code}）"
        );
    }
}

/// 闸 2：sources.enum 与 ErrorSource 枚举一致。
#[test]
fn error_source_enum_matches_json() {
    let codes = load_error_codes();
    let enum_values: Vec<&str> = codes["sources"]["enum"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    let code_values = ["kernel", "plugin", "llm", "infra", "frontend"];
    assert_eq!(
        enum_values, code_values,
        "sources.enum 与 ErrorSource 代码副本不一致"
    );
}

/// 闸 3：信封序列化形状——code 为稳定机器码（非 HTTP 状态码字符串）、
/// message 原文透传、source/retryable/details/request_id 字段齐全。
#[tokio::test]
async fn error_envelope_shape_and_internal_passthrough() {
    let resp = ApiError::Internal {
        message: "io error: 磁盘写入失败 (path: /tmp/x)".into(),
    }
    .into_response();
    assert_eq!(resp.status(), StatusCode::INTERNAL_SERVER_ERROR);

    let body = axum::body::to_bytes(resp.into_body(), 64 * 1024)
        .await
        .unwrap();
    let json: Value = serde_json::from_slice(&body).unwrap();

    assert_eq!(json["error"]["code"], "INTERNAL_ERROR", "code 应为稳定机器码");
    assert_eq!(
        json["error"]["message"],
        "io error: 磁盘写入失败 (path: /tmp/x)",
        "Internal 原文透传不脱敏"
    );
    assert_eq!(json["error"]["source"], "kernel");
    assert_eq!(json["error"]["retryable"], true);
    assert!(json["error"]["details"].is_null());
    assert!(json["error"]["request_id"].is_null());
}

/// 闸 4：非 Internal 变体同样输出完整信封（以 NotFound 为例）。
#[tokio::test]
async fn error_envelope_for_non_internal_variant() {
    let resp = ApiError::NotFound {
        message: "会话不存在".into(),
    }
    .into_response();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);

    let body = axum::body::to_bytes(resp.into_body(), 64 * 1024)
        .await
        .unwrap();
    let json: Value = serde_json::from_slice(&body).unwrap();

    assert_eq!(json["error"]["code"], "RESOURCE_NOT_FOUND");
    assert_eq!(json["error"]["message"], "会话不存在");
    assert_eq!(json["error"]["source"], "kernel");
    assert_eq!(json["error"]["retryable"], false);
}

/// 闸 5：码表内所有 code 唯一（防重复码）。
#[test]
fn error_codes_json_has_unique_codes() {
    let codes = load_error_codes();
    let mut seen = std::collections::HashSet::new();
    for entry in codes["codes"].as_array().unwrap() {
        let code = entry["code"].as_str().unwrap();
        assert!(seen.insert(code.to_string()), "码表存在重复 code={code}");
    }
}
