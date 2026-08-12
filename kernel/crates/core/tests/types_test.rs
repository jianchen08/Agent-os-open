// @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: rust-test
//! agentos-core 共享数据结构单元测试（阶段 4.1）。
//!
//! 覆盖 traits/types 的核心可序列化结构：序列化 round-trip、serde rename 规则、
//! builder 方法、Default 实现。这些是内核与所有插件之间的契约层，0% → 有测。

use agentos_core::types::*;
use serde_json::json;

#[test]
fn route_type_serde_uses_snake_case() {
    // rename_all = "snake_case"：变体名应序列化为 snake_case 字符串
    let cases = [
        (RouteType::NextLlm, "next_llm"),
        (RouteType::NextTool, "next_tool"),
        (RouteType::End, "end"),
        (RouteType::Wait, "wait"),
    ];
    for (variant, expected) in cases {
        let s = serde_json::to_string(&variant).unwrap();
        assert_eq!(s, format!("\"{expected}\""), "序列化 {variant:?}");
        // round-trip
        let back: RouteType = serde_json::from_str(&s).unwrap();
        assert_eq!(back, variant, "round-trip {variant:?}");
    }
}

#[test]
fn route_signal_builder_and_skip_none() {
    let sig = RouteSignal::new(RouteType::NextTool)
        .with_target(vec!["file_read".into()])
        .with_reason("需要读文件");
    // target/reason 已设置；payload 为 None 应被 skip_serializing_if 省略
    let json_str = serde_json::to_string(&sig).unwrap();
    assert!(json_str.contains("\"target\""), "target 应序列化");
    assert!(json_str.contains("file_read"));
    assert!(!json_str.contains("\"payload\""), "payload=None 应省略");
    // round-trip
    let back: RouteSignal = serde_json::from_str(&json_str).unwrap();
    assert_eq!(back.route_type, RouteType::NextTool);
    assert_eq!(back.target.as_deref(), Some(&["file_read".to_string()][..]));
    assert_eq!(back.reason, "需要读文件");
}

#[test]
fn error_policy_default_is_abort_and_lowercase() {
    // Default → Abort；rename_all = "lowercase"
    assert_eq!(ErrorPolicy::default(), ErrorPolicy::Abort);
    assert_eq!(serde_json::to_string(&ErrorPolicy::Abort).unwrap(), "\"abort\"");
    assert_eq!(serde_json::to_string(&ErrorPolicy::Retry).unwrap(), "\"retry\"");
}

#[test]
fn plugin_result_default_and_state_updates_round_trip() {
    let default = PluginResult::default();
    assert!(default.state_updates.is_empty());
    assert!(default.route_signal.is_none());
    assert!(!default.skip_remaining);
    assert!(default.error.is_none());

    let mut updates = std::collections::HashMap::new();
    updates.insert("k".to_string(), json!(42));
    let result = PluginResult::default()
        .with_state_updates(updates)
        .with_route_signal(RouteSignal::new(RouteType::End))
        .with_error(PluginError {
            message: "boom".into(),
            code: Some("E1".into()),
            source: None,
        });

    let s = serde_json::to_string(&result).unwrap();
    let back: PluginResult = serde_json::from_str(&s).unwrap();
    assert_eq!(back.state_updates.get("k"), Some(&json!(42)));
    assert_eq!(back.route_signal.unwrap().route_type, RouteType::End);
    let err = back.error.unwrap();
    assert_eq!(err.message, "boom");
    assert_eq!(err.code.as_deref(), Some("E1"));
}

#[test]
fn plugin_error_display_with_and_without_code() {
    let with_code = PluginError {
        message: "msg".into(),
        code: Some("C".into()),
        source: None,
    };
    assert_eq!(format!("{with_code}"), "[C] msg");
    let no_code = PluginError {
        message: "msg".into(),
        code: None,
        source: None,
    };
    assert_eq!(format!("{no_code}"), "msg");
}

#[test]
fn tenant_context_new_defaults_empty_collections() {
    let ctx = TenantContext::new("tenant-a", "sess-1");
    assert_eq!(ctx.tenant_id, "tenant-a");
    assert_eq!(ctx.session_id, "sess-1");
    assert!(ctx.user_id.is_none());
    assert!(ctx.role.is_none());
    assert!(ctx.permissions.is_empty());
    assert!(ctx.enabled_plugins.is_empty());
    assert!(ctx.credential_handle.is_none());
    // 序列化应省略 None 字段
    let s = serde_json::to_string(&ctx).unwrap();
    assert!(!s.contains("\"user_id\""));
    assert!(!s.contains("\"role\""));
}

#[test]
fn run_status_lowercase_serde() {
    assert_eq!(serde_json::to_string(&RunStatus::Running).unwrap(), "\"running\"");
    assert_eq!(serde_json::to_string(&RunStatus::Suspended).unwrap(), "\"suspended\"");
    assert_eq!(serde_json::to_string(&RunStatus::Completed).unwrap(), "\"completed\"");
    assert_eq!(serde_json::to_string(&RunStatus::Failed).unwrap(), "\"failed\"");
    let back: RunStatus = serde_json::from_str("\"failed\"").unwrap();
    assert_eq!(back, RunStatus::Failed);
}

#[test]
fn patch_type_snake_case_serde() {
    assert_eq!(
        serde_json::to_string(&PatchType::StateUpdate).unwrap(),
        "\"state_update\""
    );
    assert_eq!(
        serde_json::to_string(&PatchType::RouteSignal).unwrap(),
        "\"route_signal\""
    );
    assert_eq!(serde_json::to_string(&PatchType::Rollback).unwrap(), "\"rollback\"");
    // 全变体 round-trip
    for v in [
        PatchType::StateUpdate,
        PatchType::RouteSignal,
        PatchType::Error,
        PatchType::Lifecycle,
        PatchType::Rollback,
    ] {
        let s = serde_json::to_string(&v).unwrap();
        let back: PatchType = serde_json::from_str(&s).unwrap();
        assert_eq!(back, v);
    }
}

#[test]
fn target_type_tool_category_tool_source_round_trip() {
    // 这些枚举参与多租户/工具路由契约，确保稳定 round-trip
    let tt: TargetType =
        serde_json::from_str(&serde_json::to_string(&TargetType::LlmCall).unwrap()).unwrap();
    assert_eq!(tt, TargetType::LlmCall);
    let tc = serde_json::to_string(&ToolCategory::File).unwrap();
    let back: ToolCategory = serde_json::from_str(&tc).unwrap();
    assert_eq!(back, ToolCategory::File);
    let ts = serde_json::to_string(&ToolSource::Builtin).unwrap();
    let back2: ToolSource = serde_json::from_str(&ts).unwrap();
    assert_eq!(back2, ToolSource::Builtin);
}
