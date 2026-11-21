// @feature: FP-0.2.一 插件协议·private 可见性 | @ci: rust-test
//! PluginManifest.visibility 字段 serde 契约（批 2 private 机制落地）。
//!
//! 设计真值：docs/working/LLM插件两类收敛_现状盘点与方案_20261118.md §6.4-e /
//! ADR 2026-11-18-llm-two-tier-plugin-convergence「private 机制」——manifest
//! `visibility` 字段只增不删：旧 manifest 无该字段 = 默认可见（宽容读），
//! 未知取值同样回落 public（字段只增不删的向前兼容面）；显式 "private" 才
//! 进入私有点位。manifest 顶层 `deny_unknown_fields` 语义不变（字段名写错仍
//! 拒绝，不因本字段放宽）。

use agentos_core::traits::{PluginManifest, PluginVisibility};

/// 最小合法 manifest JSON（visibility 由各用例按需拼接）。
fn manifest_json(extra: &str) -> String {
    format!(
        r#"{{
            "id": "sample",
            "name": "Sample",
            "version": "1.0.0",
            "plugin_type": "system",
            "language": "python",
            "host_type": "sidecar",
            "entry": "python server.py",
            "capabilities": {{}}{extra}
        }}"#
    )
}

#[test]
fn manifest_without_visibility_defaults_public() {
    let manifest: PluginManifest =
        serde_json::from_str(&manifest_json("")).expect("旧 manifest（无 visibility）必须可解析");
    assert_eq!(
        manifest.visibility,
        PluginVisibility::Public,
        "缺省 = 可见（loader 宽容读）"
    );
}

#[test]
fn manifest_private_visibility_parses() {
    let manifest: PluginManifest = serde_json::from_str(&manifest_json(
        r#",
            "visibility": "private""#,
    ))
    .expect("显式 visibility=private 必须可解析");
    assert_eq!(manifest.visibility, PluginVisibility::Private);
}

#[test]
fn manifest_unknown_visibility_value_falls_back_public() {
    // 字段只增不删：未来内核新增的取值（如 "internal"）落在旧内核上不得
    // 拒载整个插件——回落 public（可见是保守默认，不可见不是副作用）。
    let manifest: PluginManifest = serde_json::from_str(&manifest_json(
        r#",
            "visibility": "internal""#,
    ))
    .expect("未知 visibility 取值必须宽容读，不得拒载插件");
    assert_eq!(manifest.visibility, PluginVisibility::Public);
}

#[test]
fn manifest_visibility_typo_still_rejected_by_deny_unknown_fields() {
    // 字段名写错 = 声明错误（不是取值未知）——deny_unknown_fields 语义不变。
    let err = serde_json::from_str::<PluginManifest>(&manifest_json(
        r#",
            "visibilty": "private""#,
    ))
    .expect_err("未知字段名必须拒绝（fail-closed）");
    assert!(
        format!("{err:?}").contains("visibilty"),
        "错误信息应点名未知字段: {err:?}"
    );
}

#[test]
fn public_visibility_round_trips_without_emitting_field() {
    let manifest: PluginManifest = serde_json::from_str(&manifest_json("")).expect("解析");
    let value = serde_json::to_value(&manifest).expect("序列化");
    assert!(
        value.get("visibility").is_none(),
        "public（缺省）不得在序列化面新增字段——既有插件 serde 面字节不变: {value}"
    );
}

#[test]
fn private_visibility_serializes_as_lowercase_string() {
    let manifest: PluginManifest = serde_json::from_str(&manifest_json(
        r#",
            "visibility": "private""#,
    ))
    .expect("解析");
    let value = serde_json::to_value(&manifest).expect("序列化");
    assert_eq!(
        value.get("visibility").and_then(|v| v.as_str()),
        Some("private"),
        "private 在序列化面（/api/v1/plugins 等出口）为小写字符串"
    );
}
