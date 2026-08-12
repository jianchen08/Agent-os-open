// @feature: FP-0.2.一 插件协议 | @vision: V3 可嵌入 | @audit: T5#16 | @ci: rust-test
//! agentos-invoker 配置注入路径解析集成测试（阶段 4.4：为 invoker 补 tests/ 目录）。
//!
//! invoker 此前只有 src 内嵌单测、无独立 tests/ 目录（T5#16）。本文件以**外部消费者**
//! 视角覆盖公开纯函数 `resolve_config_path` 的路径归一化分支——这些分支在 src 内嵌
//! 测试中只随 build_injected_config 间接走过，这里穷尽：config/ 与 config\\ 前缀、
//! .yaml/.yml 后缀、反斜杠分隔、空段、深层下钻、缺段降级。

use agentos_invoker::shared::resolve_config_path;
use serde_json::json;

fn sample() -> serde_json::Value {
    json!({
        "models": {
            "llm": {"default_model": "glm"},
            "embedding": {"dim": 1024}
        },
        "channels": {"feishu": {"enabled": true}}
    })
}

#[test]
fn resolves_yaml_with_config_prefix() {
    let s = sample();
    let v = resolve_config_path(&s, "config/models/llm.yaml").unwrap();
    assert_eq!(v["default_model"], "glm");
}

#[test]
fn resolves_yml_extension() {
    let s = sample();
    let v = resolve_config_path(&s, "models/llm.yml").unwrap();
    assert_eq!(v["default_model"], "glm");
}

#[test]
fn resolves_without_extension() {
    let s = sample();
    let v = resolve_config_path(&s, "models/embedding").unwrap();
    assert_eq!(v["dim"], 1024);
}

#[test]
fn resolves_backslash_prefix_and_separators() {
    // config\\ 前缀 + 反斜杠分隔（Windows 形态）应归一化到同一目标
    let s = sample();
    let v = resolve_config_path(&s, "config\\models\\llm.yaml").unwrap();
    assert_eq!(v["default_model"], "glm");
}

#[test]
fn missing_segment_returns_none() {
    let s = sample();
    assert!(resolve_config_path(&s, "models/nope.yaml").is_none());
    assert!(resolve_config_path(&s, "nonexistent/a/b").is_none());
}

#[test]
fn empty_segments_are_skipped() {
    // 连续斜杠产生的空段应被忽略，不影响下钻
    let s = sample();
    let v = resolve_config_path(&s, "models//llm.yaml").unwrap();
    assert_eq!(v["default_model"], "glm");
}

#[test]
fn deep_nested_path_resolves() {
    let s = sample();
    let v = resolve_config_path(&s, "channels/feishu.yaml").unwrap();
    assert_eq!(v["enabled"], true);
}

#[test]
fn root_path_returns_full_config() {
    // 无段（纯前缀/扩展名剥离后为空）应返回 full_config 本身
    let s = sample();
    let v = resolve_config_path(&s, "config/").unwrap();
    assert!(v.get("models").is_some());
}
