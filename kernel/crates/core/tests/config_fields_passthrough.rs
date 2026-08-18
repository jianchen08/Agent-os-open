//! config_files.fields 的 UI 词汇透传（T1：PluginConfigEditor 消费 fields）。
//!
//! YAML target 的 fields 声明使用前端 UIInputFormField 词汇表
//! （select/toggle/number/options/min/max…），内核经 `EnvConfigField::extra`
//! flatten 原样透传——本测试锁定：解析不丢词汇字段 + 序列化往返保形
//! （/api/v1/schema 据此把声明送达前端 RJSF 表单）。

use agentos_core::traits::ConfigFileMapping;

#[test]
fn yaml_fields_ui_vocab_passthrough() {
    let raw = serde_json::json!({
        "id": "llm",
        "path": "config/models/llm.yaml",
        "label": "LLM 模型配置",
        "fields": [
            {
                "name": "defaults.chat",
                "type": "select",
                "label": "默认对话模型",
                "options": [
                    {"label": "GLM", "value": "glm-5.2"},
                    {"label": "DeepSeek", "value": "deepseek-v4"}
                ]
            },
            {
                "name": "defaults.tiers.large",
                "type": "select",
                "label": "大模型档位",
                "datasourceUri": "/api/v1/models"
            },
            {
                "name": "concurrency.chat",
                "type": "number",
                "label": "对话并发",
                "min": 1,
                "max": 64,
                "default": 8
            }
        ]
    });
    let m: ConfigFileMapping = serde_json::from_value(raw).expect("合法 config_files 声明应可解析");
    assert_eq!(m.fields.len(), 3);

    // 类型词汇进 field_type，UI 参数进 extra（解析期不丢弃）
    assert_eq!(m.fields[0].field_type, "select");
    let extra = m.fields[0].extra.as_ref().expect("options 应透传进 extra");
    assert!(
        extra.contains_key("options"),
        "extra 应含 options: {extra:?}"
    );
    assert_eq!(
        m.fields[1].extra.as_ref().unwrap().get("datasourceUri"),
        Some(&serde_json::json!("/api/v1/models"))
    );

    // 序列化往返：词汇字段平铺回字段对象（schema API 透传给前端）
    let out = serde_json::to_value(&m).expect("序列化");
    let opts = out["fields"][0]["options"]
        .as_array()
        .expect("options 应在序列化输出");
    assert_eq!(opts.len(), 2);
    assert_eq!(out["fields"][2]["min"], 1);
    assert_eq!(out["fields"][2]["default"], 8);
    assert!(
        out["fields"][0].get("extra").is_none(),
        "flatten 序列化不应产生 extra 键"
    );
}

#[test]
fn env_fields_without_extra_stay_compatible() {
    // env target 既有形态（无 UI 词汇）：extra=None，序列化不产生额外键
    let raw = serde_json::json!({
        "id": "api_keys",
        "path": ".env",
        "target": "env",
        "label": "密钥",
        "fields": [{"name": "X_KEY", "label": "Key"}]
    });
    let m: ConfigFileMapping = serde_json::from_value(raw).unwrap();
    // flatten 语义：无剩余键 → Some(空 map)（而非 None）——两种都算"无透传数据"
    assert!(
        m.fields[0].extra.as_ref().is_none_or(|e| e.is_empty()),
        "无 UI 词汇声明时 extra 应为空: {:?}",
        m.fields[0].extra
    );
    assert_eq!(
        m.fields[0].field_type, "secret",
        "缺省 type 仍是 secret（env 条目保守默认）"
    );
    let out = serde_json::to_value(&m).unwrap();
    let s = out.to_string();
    assert!(!s.contains("extra"), "None flatten 不应输出 extra 键: {s}");
}
