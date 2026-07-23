//! P3-4: 企微 webhook 模拟回调测试（TDD RED）。
//!
//! 验证 ADR §3.3 示例 + 附录 E.1.1（企微是唯一需要 raw body + 自定义响应的 webhook 用例）。
//!
//! **范围说明**：企微真实回调（真实 corp_id/EncodingAESKey/真实 HTTP 回调）需部署后
//! 人工验证（需企微账号 + 公网回调 URL）。本测试用**本地模拟请求**验证 dispatcher 链路：
//! 1. manifest 声明企微双方法端点（GET 验证 + POST 回调，同 path 不同 method）；
//! 2. GET 验证：query 透传（msg_signature/timestamp/nonce/echostr）+ 插件回 echostr 明文；
//! 3. POST 回调：raw body（加密 XML）字节级透传 + 插件回加密 XML（encrypt_response 产物）。
//!
//! 验签算法参考 src/channels/wecom/crypto.py（SHA1 排序拼接 + AES-256-CBC），
//! 本测试模拟其输入/输出契约（不依赖 Python，纯 Rust 进程内 handler）。

use std::collections::HashMap;
use std::sync::Arc;

use agentos_core::traits::{
    CapabilityRegistry, HttpEndpoint, HttpHandleCapability, HttpHandleRequest,
    HttpHandleResponse,
};
use agentos_plugin_loader::CapabilityRegistryImpl;
use base64::Engine;

use agentos_api::http_dispatcher::{dispatch_http, DispatchOutcome, HttpDispatcher};

/// 模拟企微回调 handler（对应 Python `wecom.handle_callback` 的 Rust 镜像）。
///
/// - GET 验证 URL：从 query 取 echostr，验签后原样回显明文（crypto.decrypt_echo 产物）；
/// - POST 回调：raw body 是加密 XML，验签 + 解密 + 处理 + encrypt_response 回加密 XML。
///
/// 本模拟简化为：GET 回 query.echostr 原样；POST 回一个固定的加密 XML 响应体，
/// 但**把收到的 raw_body 解码后回显到响应 header x-recv-raw**，以便测试断言字节级透传。
struct WecomCallbackHandler;

#[async_trait::async_trait]
impl HttpHandleCapability for WecomCallbackHandler {
    async fn handle(&self, req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
        match req.method.as_str() {
            // GET 验证 URL：企微发 query(msg_signature, timestamp, nonce, echostr)，
            // 插件验签后解密 echostr 返回明文。模拟：原样回 query.echostr。
            "GET" => {
                let echostr = req.query.get("echostr").cloned().unwrap_or_default();
                Ok(HttpHandleResponse {
                    status: 200,
                    headers: HashMap::from([(
                        "content-type".to_string(),
                        "text/plain".to_string(),
                    )]),
                    body: base64::engine::general_purpose::STANDARD.encode(echostr.as_bytes()),
                    body_encoding: "base64".to_string(),
                })
            }
            // POST 被动回调：raw body 是加密 XML（企微格式：<xml><Encrypt>...</Encrypt></xml>）。
            // 插件验签（SHA1 of 排序拼接 token+timestamp+nonce+msg_encrypt）+ AES 解密 + 处理 +
            // encrypt_response 回加密 XML。模拟：回固定加密 XML，并把收到的 raw body 回显到 header。
            "POST" => {
                // 模拟验签：从 query 取 msg_signature/timestamp/nonce，
                // 真实实现会对 sorted([token,timestamp,nonce,encrypt]).join("") 算 SHA1。
                // 这里只验证 query 字段透传到位。
                let _sig = req.query.get("msg_signature").cloned().unwrap_or_default();
                let _ts = req.query.get("timestamp").cloned().unwrap_or_default();
                let _nonce = req.query.get("nonce").cloned().unwrap_or_default();

                // 模拟 encrypt_response 产物（固定加密 XML）
                let encrypted_reply = "<xml><Encrypt>SIMULATED_ENCRYPTED_REPLY</Encrypt></xml>";
                Ok(HttpHandleResponse {
                    status: 200,
                    headers: HashMap::from([
                        (
                            "content-type".to_string(),
                            "application/xml".to_string(),
                        ),
                        // 回显收到的 raw body（解码后），供测试断言字节级透传
                        (
                            "x-recv-raw".to_string(),
                            req.raw_body.clone(),
                        ),
                    ]),
                    body: base64::engine::general_purpose::STANDARD
                        .encode(encrypted_reply.as_bytes()),
                    body_encoding: "base64".to_string(),
                })
            }
            _ => Err(format!("unsupported method: {}", req.method)),
        }
    }
}

/// 企微 manifest 应声明 GET 验证 + POST 回调双方法（同 path 不同 method）。
/// 设计依据：ADR §3.3 示例。
#[test]
fn test_wecom_manifest_declares_dual_method_endpoints() {
    use agentos_core::traits::PluginManifest;

    // 这就是将写入 channel_wecom/plugin.json 的 http_endpoints 声明。
    let json = r#"{
        "id": "channel_wecom",
        "name": "WeCom Channel",
        "version": "1.0.0",
        "plugin_type": "system",
        "language": "python",
        "host_type": "sidecar",
        "entry": "python server.py",
        "capabilities": {},
        "http_endpoints": [
            {
                "route_id": "wecom_callback",
                "method": "POST",
                "path": "/ext/channel_wecom/callback",
                "auth": "none",
                "handler_capability": "http.handle",
                "timeout_ms": 30000,
                "max_concurrency": 16,
                "description": "企微事件回调入口"
            },
            {
                "route_id": "wecom_verify",
                "method": "GET",
                "path": "/ext/channel_wecom/callback",
                "auth": "none",
                "handler_capability": "http.handle",
                "description": "企微 URL 验证（回 echostr 明文）"
            }
        ]
    }"#;

    let manifest: PluginManifest =
        serde_json::from_str(json).expect("wecom manifest must parse");
    assert_eq!(manifest.http_endpoints.len(), 2);
    // 同 path 不同 method
    assert_eq!(manifest.http_endpoints[0].path, "/ext/channel_wecom/callback");
    assert_eq!(manifest.http_endpoints[1].path, "/ext/channel_wecom/callback");
    assert_ne!(
        manifest.http_endpoints[0].method,
        manifest.http_endpoints[1].method
    );

    // 注册到 registry：双方法都应成功（path+method 不冲突）
    let registry = CapabilityRegistryImpl::new();
    for ep in &manifest.http_endpoints {
        registry
            .register_http_route("channel_wecom", ep.clone())
            .expect("wecom endpoint must register");
    }
    assert_eq!(registry.list_http_routes().len(), 2);
}

/// 构造企微双方法端点的 helper。
fn wecom_endpoints() -> [HttpEndpoint; 2] {
    [
        HttpEndpoint {
            route_id: "wecom_callback".to_string(),
            method: "POST".to_string(),
            path: "/ext/channel_wecom/callback".to_string(),
            auth: "none".to_string(),
            handler_capability: "http.handle".to_string(),
            timeout_ms: Some(30000),
            max_concurrency: Some(16),
            description: Some("企微事件回调入口".to_string()),
        },
        HttpEndpoint {
            route_id: "wecom_verify".to_string(),
            method: "GET".to_string(),
            path: "/ext/channel_wecom/callback".to_string(),
            auth: "none".to_string(),
            handler_capability: "http.handle".to_string(),
            timeout_ms: None,
            max_concurrency: None,
            description: Some("企微 URL 验证".to_string()),
        },
    ]
}

/// 构造注册了企微双方法端点的 dispatcher。
fn wecom_dispatcher() -> HttpDispatcher {
    let registry = Arc::new(CapabilityRegistryImpl::new());
    for ep in wecom_endpoints() {
        registry
            .register_http_route("channel_wecom", ep)
            .unwrap();
    }
    HttpDispatcher::new(registry, Arc::new(WecomCallbackHandler))
}

/// GET 验证 URL：dispatcher 把 query（msg_signature/timestamp/nonce/echostr）透传给插件，
/// 插件回 echostr 明文。
#[tokio::test]
async fn test_wecom_get_verify_passes_query_and_returns_echostr() {
    let dispatcher = wecom_dispatcher();

    // 企微 GET 验证请求的典型 query（参考 crypto.py + adapter.py）
    let query = HashMap::from([
        ("msg_signature".to_string(), "abc123sig".to_string()),
        ("timestamp".to_string(), "1700000000".to_string()),
        ("nonce".to_string(), "nonce_xyz".to_string()),
        ("echostr".to_string(), "ENCRYPTED_ECHOSTR_FROM_WECOM".to_string()),
    ]);

    let outcome = dispatch_http(
        &dispatcher,
        "/ext/channel_wecom/callback",
        "GET",
        vec![], // GET 无 body
        HashMap::new(),
        query,
    )
    .await;

    match outcome {
        DispatchOutcome::Handled(resp) => {
            assert_eq!(resp.status, 200);
            // 插件原样回显 echostr 明文（模拟 decrypt_echo 产物）
            let body = base64::engine::general_purpose::STANDARD
                .decode(&resp.body)
                .unwrap();
            assert_eq!(
                String::from_utf8(body).unwrap(),
                "ENCRYPTED_ECHOSTR_FROM_WECOM"
            );
        }
        other => panic!("GET verify should be handled, got {other:?}"),
    }
}

/// POST 回调：dispatcher 把加密 XML raw body 字节级透传给插件，
/// 插件回加密 XML（encrypt_response 产物），content-type 为 application/xml。
#[tokio::test]
async fn test_wecom_post_callback_passes_encrypted_xml_raw_body() {
    let dispatcher = wecom_dispatcher();

    // 企微 POST 回调的典型 raw body：加密 XML（含 0x?? 非可见字节也行，这里用文本模拟）
    let encrypted_xml = b"<xml><Encrypt>BASE64_ENCRYPTED_PAYLOAD_HERE</Encrypt></xml>";
    let query = HashMap::from([
        ("msg_signature".to_string(), "sha1sig_value".to_string()),
        ("timestamp".to_string(), "1700000001".to_string()),
        ("nonce".to_string(), "nonce_abc".to_string()),
    ]);
    let headers = HashMap::from([(
        "content-type".to_string(),
        "text/xml".to_string(),
    )]);

    let outcome = dispatch_http(
        &dispatcher,
        "/ext/channel_wecom/callback",
        "POST",
        encrypted_xml.to_vec(),
        headers,
        query,
    )
    .await;

    match outcome {
        DispatchOutcome::Handled(resp) => {
            assert_eq!(resp.status, 200);
            // content-type 由插件控制（application/xml）
            assert_eq!(
                resp.headers.get("content-type"),
                Some(&"application/xml".to_string())
            );
            // raw body 字节级透传：插件回显的 x-recv-raw 解码后必须与原加密 XML 一致
            let recv_raw_decoded = base64::engine::general_purpose::STANDARD
                .decode(resp.headers.get("x-recv-raw").unwrap())
                .unwrap();
            assert_eq!(recv_raw_decoded, encrypted_xml);
            // 响应 body 是 encrypt_response 产物（加密 XML）
            let reply = base64::engine::general_purpose::STANDARD
                .decode(&resp.body)
                .unwrap();
            let reply_str = String::from_utf8(reply).unwrap();
            assert!(
                reply_str.contains("SIMULATED_ENCRYPTED_REPLY"),
                "response should be encrypted XML, got: {reply_str}"
            );
        }
        other => panic!("POST callback should be handled, got {other:?}"),
    }
}

/// raw body 含非 UTF-8 字节（企微加密 payload 真实场景）也能字节级透传。
/// 这正是"绝不反序列化 body 再转发"铁律的硬性验证。
#[tokio::test]
async fn test_wecom_post_callback_non_utf8_raw_body_passes_through() {
    let dispatcher = wecom_dispatcher();

    // 含 0x00/0xFF 等非 UTF-8 字节的加密 payload
    let raw: Vec<u8> = vec![0x00, 0xFF, 0xAB, b'<', 0xCD, 0x01, b'>'];
    let outcome = dispatch_http(
        &dispatcher,
        "/ext/channel_wecom/callback",
        "POST",
        raw.clone(),
        HashMap::new(),
        HashMap::new(),
    )
    .await;

    match outcome {
        DispatchOutcome::Handled(resp) => {
            let recv_raw_decoded = base64::engine::general_purpose::STANDARD
                .decode(resp.headers.get("x-recv-raw").unwrap())
                .unwrap();
            assert_eq!(
                recv_raw_decoded, raw,
                "non-UTF-8 raw body must pass through byte-for-byte"
            );
        }
        other => panic!("POST callback should be handled, got {other:?}"),
    }
}
