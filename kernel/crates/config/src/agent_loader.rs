//! Agent 配置加载器（泛化注入版）。
//!
//! 设计依据：docs/working/重要设计/统一配置加载方案.md TDD-6 + 决策 4
//!
//! 与 `pipeline::load_agent_config`（返回强类型 AgentConfig）互补：
//! 本模块的 `load_agent_into_state` 把整个 agent yaml 顶层**泛化注入** state，
//! 不挑字段——加字段不用改内核（解决扩展性问题）。
//! 走 `ConfigCenter.load()`（享受 mtime 缓存 + 失败回滚 + 审计）。

use crate::config_center::ConfigCenter;
use crate::pipeline::find_agent_yaml;
use serde_json::Value;

/// 把 agent yaml 的所有顶层字段注入 state（泛化注入）。
///
/// 走 ConfigCenter.load()（mtime 缓存 + 失败回滚 + 审计），用 find_agent_yaml
/// 递归定位（支持 agents/main/xxx.yaml 分类子目录）。
///
/// 注入语义：
/// - 普通字段：`or_insert`（state 已有值不覆盖，调用方优先）
/// - `core_plugin`：`insert` 直接覆盖（agent 能切换核心插件）
///
/// 文件不存在 / 解析失败时 state 不变（静默降级，和旧版 load_agent_config_into_state 一致）。
pub fn load_agent_into_state(cc: &ConfigCenter, state: &mut Value, agent_id: &str) {
    let agents_dir = cc.config_root().join("agents");
    let Some(path) = find_agent_yaml(&agents_dir, agent_id) else {
        return; // 文件不存在，静默跳过
    };

    // 转成相对 config_root 的路径，供 ConfigCenter.load() 用（享受 mtime 缓存）
    let rel_path = path
        .strip_prefix(cc.config_root())
        .unwrap_or(&path)
        .to_string_lossy()
        .to_string();

    let agent_cfg = match cc.load(&rel_path) {
        Ok(v) => v,
        Err(_) => return, // 解析失败：ConfigCenter 已保留旧缓存，state 不变
    };

    let Some(state_obj) = state.as_object_mut() else {
        return;
    };
    let Some(agent_obj) = agent_cfg.as_object() else {
        return;
    };

    for (k, v) in agent_obj {
        if k == "core_plugin" {
            // core_plugin 特殊：直接覆盖（agent 能切换核心插件，如换 LLM 提供商）
            state_obj.insert(k.clone(), v.clone());
        } else {
            // 其余：or_insert（调用方注入优先，agent 配置仅补缺失）
            state_obj.entry(k.clone()).or_insert(v.clone());
        }
    }
}

#[cfg(test)]
mod tests {
    //! TDD-6: load_agent_into_state 泛化注入测试。

    use super::*;
    use std::path::PathBuf;

    fn setup(config_root: PathBuf, agent_id: &str, content: &str) -> ConfigCenter {
        let agents_dir = config_root.join("agents");
        std::fs::create_dir_all(&agents_dir).unwrap();
        std::fs::write(agents_dir.join(format!("{agent_id}.yaml")), content).unwrap();
        ConfigCenter::new(config_root)
    }

    #[test]
    fn test_injects_all_top_level_fields() {
        // 契约：整个 yaml 顶层注入 state（不只 5 个，验证泛化）
        let temp = tempfile::tempdir().unwrap();
        let cc = setup(
            temp.path().to_path_buf(),
            "test_agent",
            "system_prompt: 你是助手\ntool_ids: [file_read]\ncustom_field: hello\nnum: 42\n",
        );
        let mut state = serde_json::json!({});

        load_agent_into_state(&cc, &mut state, "test_agent");

        assert_eq!(state["system_prompt"], "你是助手");
        assert_eq!(state["tool_ids"][0], "file_read");
        assert_eq!(state["custom_field"], "hello", "自定义字段也应注入");
        assert_eq!(state["num"], 42);
    }

    #[test]
    fn test_does_not_override_existing_state_fields() {
        // 契约：state 已有的字段不被覆盖（or_insert，调用方优先）
        let temp = tempfile::tempdir().unwrap();
        let cc = setup(temp.path().to_path_buf(), "a1", "system_prompt: agent值\n");
        let mut state = serde_json::json!({"system_prompt": "调用方值"});

        load_agent_into_state(&cc, &mut state, "a1");

        assert_eq!(state["system_prompt"], "调用方值", "state 已有值不被覆盖");
    }

    #[test]
    fn test_core_plugin_overrides_state() {
        // 契约：core_plugin 直接覆盖 state（agent 能切换核心插件）
        let temp = tempfile::tempdir().unwrap();
        let cc = setup(
            temp.path().to_path_buf(),
            "a2",
            "core_plugin: custom_llm\nsystem_prompt: hi\n",
        );
        let mut state = serde_json::json!({"core_plugin": "default_llm"});

        load_agent_into_state(&cc, &mut state, "a2");

        assert_eq!(state["core_plugin"], "custom_llm", "core_plugin 应覆盖");
    }

    #[test]
    fn test_missing_agent_leaves_state_unchanged() {
        // 契约：agent 文件不存在时 state 不变（静默降级）
        let temp = tempfile::tempdir().unwrap();
        let cc = ConfigCenter::new(temp.path().to_path_buf());
        let mut state = serde_json::json!({"existing": true});

        load_agent_into_state(&cc, &mut state, "nonexistent");

        assert_eq!(state["existing"], true, "state 应不变");
        assert_eq!(state.as_object().unwrap().len(), 1);
    }

    #[test]
    fn test_finds_agent_in_subdirectory() {
        // 契约：支持 agents/main/xxx.yaml 分类子目录（find_agent_yaml 递归）
        let temp = tempfile::tempdir().unwrap();
        let agents_sub = temp.path().join("config/agents/main");
        std::fs::create_dir_all(&agents_sub).unwrap();
        std::fs::write(agents_sub.join("deep.yaml"), "name: 深层Agent\n").unwrap();

        let cc = ConfigCenter::new(temp.path().join("config"));
        let mut state = serde_json::json!({});

        load_agent_into_state(&cc, &mut state, "deep");

        assert_eq!(state["name"], "深层Agent", "子目录的 agent 也应找到");
    }

    #[test]
    fn test_rereads_after_file_change() {
        // 契约：走 ConfigCenter.load → mtime 变了重读（per-iteration 场景的基础）
        let temp = tempfile::tempdir().unwrap();
        let cc = setup(temp.path().to_path_buf(), "a3", "v: 1\n");
        let mut state = serde_json::json!({});

        load_agent_into_state(&cc, &mut state, "a3");
        assert_eq!(state["v"], 1);

        // 改文件（确保 mtime 变化）
        std::thread::sleep(std::time::Duration::from_millis(50));
        std::fs::write(temp.path().join("agents/a3.yaml"), "v: 2\n").unwrap();

        let mut state2 = serde_json::json!({});
        load_agent_into_state(&cc, &mut state2, "a3");
        assert_eq!(state2["v"], 2, "mtime 变了应重读到新内容");
    }
}
