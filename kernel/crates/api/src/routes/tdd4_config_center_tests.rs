//! TDD-4: AppState.config_center 字段 + with_config_center builder 测试。
//! 设计依据：docs/working/重要设计/统一配置加载方案.md 阶段 1。

use super::*;
use agentos_config::config_center::ConfigCenter;

#[test]
fn test_new_app_state_has_no_config_center() {
    // 契约：默认构造的 AppState，config_center 为 None（未接线降级）
    let state = AppState::new();
    assert!(state.config_center.is_none());
}

#[test]
fn test_with_config_center_injects_instance() {
    // 契约：with_config_center 注入后，config_center 为 Some
    let temp = tempfile::tempdir().unwrap();
    let cc = Arc::new(ConfigCenter::new(temp.path().to_path_buf()));

    let state = AppState::new().with_config_center(cc);

    assert!(state.config_center.is_some());
}

#[test]
fn test_injected_config_center_can_load() {
    // 契约：注入的 ConfigCenter 能 load 配置文件（端到端可用性）
    let temp = tempfile::tempdir().unwrap();
    let config_dir = temp.path().join("config");
    std::fs::create_dir_all(&config_dir).unwrap();
    std::fs::write(config_dir.join("test.yaml"), "key: value\n").unwrap();

    let cc = Arc::new(ConfigCenter::new(config_dir));
    let state = AppState::new().with_config_center(cc);

    let cc = state.config_center.as_ref().expect("应已注入");
    let val = cc.load("test.yaml").expect("load 应成功");
    assert_eq!(val["key"], "value");
}
