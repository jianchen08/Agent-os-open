//! Agent 配置加载器（tool_ids 窄接口版）。
//!
//! 设计依据：docs/working/内核服务与Agent绑定审计_20260826.md 变更#1（双轨收敛）
//!
//! agent 全量配置的唯一事实源是 context_build 插件
//! （plugins/shared/pipeline/input/context_build/plugin.py 自持加载）；内核不再
//! 把整个 agent yaml 泛化注入 state，只保留"按 agent_id 解析 tool_ids 做工具面
//! 过滤"的窄接口（K10：工具面是执行时契约，不是 agent 配置）。
//! 走 `ConfigCenter.load()`（mtime 缓存 + 失败回滚 + 审计）。

use crate::config_center::ConfigCenter;
use crate::pipeline::find_agent_yaml;
use tracing::warn;

/// [`resolve_agent_tool_ids`] 的失败原因（K5 可见性：配置断链不得静默跳过）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AgentToolIdsError {
    /// agent yaml 文件不存在（agent_id 打错字 / 文件未建）
    Missing,
    /// yaml 解析失败或顶层非对象（配置损坏）
    Corrupt,
}

impl std::fmt::Display for AgentToolIdsError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Missing => write!(f, "agent yaml not found"),
            Self::Corrupt => write!(f, "agent yaml parse failed or top-level is not an object"),
        }
    }
}

/// 按 `agent_id` 从 `config/agents/**/<agent_id>.yaml` 解析 `tool_ids`（K10 窄接口）。
///
/// 走 ConfigCenter.load()（mtime 缓存 + 失败回滚 + 审计），用 find_agent_yaml
/// 递归定位（支持 agents/main/xxx.yaml 分类子目录）。
///
/// 返回语义：
/// - `Ok(Some(tool_ids))`：yaml 带 `tool_ids` 键（含显式空表 = agent 声明零工具）
/// - `Ok(None)`：yaml 存在且解析正常但无 `tool_ids` 键（白名单未声明）
/// - `Err`：yaml 缺失 / 解析失败 / 顶层非对象（K5 失败可见：调用方负责把
///   `_agent_config_missing` 标记写进 state 供诊断出口可见——agent_id 打错字/
///   yaml 写坏会被下游默认提示词 + 全量工具面放大成"照跑"）
pub fn resolve_agent_tool_ids(
    cc: &ConfigCenter,
    agent_id: &str,
) -> Result<Option<Vec<String>>, AgentToolIdsError> {
    let agents_dir = cc.config_root().join("agents");
    let Some(path) = find_agent_yaml(&agents_dir, agent_id) else {
        warn!(
            target: "agent-config-load",
            agent_id = %agent_id,
            reason = "agent yaml not found",
            "agent 配置加载失败（诊断出口可见）"
        );
        return Err(AgentToolIdsError::Missing);
    };

    // 转成相对 config_root 的路径，供 ConfigCenter.load() 用（享受 mtime 缓存）
    let rel_path = path
        .strip_prefix(cc.config_root())
        .unwrap_or(&path)
        .to_string_lossy()
        .to_string();

    let agent_cfg = match cc.load(&rel_path) {
        Ok(v) => v,
        Err(e) => {
            // ConfigCenter 已保留旧缓存，但当前文件确实坏了：Err 暴露
            warn!(
                target: "agent-config-load",
                agent_id = %agent_id,
                reason = %format!("agent yaml parse failed: {e}"),
                "agent 配置加载失败（诊断出口可见）"
            );
            return Err(AgentToolIdsError::Corrupt);
        }
    };

    let Some(agent_obj) = agent_cfg.as_object() else {
        // 顶层非对象（如纯标量 yaml）：同样视为配置损坏
        warn!(
            target: "agent-config-load",
            agent_id = %agent_id,
            reason = "agent yaml top-level is not an object",
            "agent 配置加载失败（诊断出口可见）"
        );
        return Err(AgentToolIdsError::Corrupt);
    };

    Ok(agent_obj
        .get("tool_ids")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|t| t.as_str().map(str::to_string))
                .collect()
        }))
}

#[cfg(test)]
mod tests {
    //! resolve_agent_tool_ids 窄接口测试（审计变更#1 双轨收敛后重写）。

    use super::*;
    use std::path::PathBuf;

    fn setup(config_root: PathBuf, agent_id: &str, content: &str) -> ConfigCenter {
        let agents_dir = config_root.join("agents");
        std::fs::create_dir_all(&agents_dir).unwrap();
        std::fs::write(agents_dir.join(format!("{agent_id}.yaml")), content).unwrap();
        ConfigCenter::new(config_root)
    }

    #[test]
    fn resolves_tool_ids_from_yaml() {
        // 契约：yaml 的 tool_ids 白名单被解析返回（工具面过滤的唯一输入）
        let temp = tempfile::tempdir().unwrap();
        let cc = setup(
            temp.path().to_path_buf(),
            "test_agent",
            "system_prompt: 你是助手\ntool_ids: [file_read, bash_execute]\n",
        );

        let ids = resolve_agent_tool_ids(&cc, "test_agent").unwrap().unwrap();

        assert_eq!(
            ids,
            vec!["file_read".to_string(), "bash_execute".to_string()]
        );
    }

    #[test]
    fn explicit_empty_tool_ids_is_some_empty() {
        // 契约：显式空表 = agent 声明零工具（Some([])），区别于"未声明"（Ok(None)）
        let temp = tempfile::tempdir().unwrap();
        let cc = setup(temp.path().to_path_buf(), "zero", "tool_ids: []\n");

        let ids = resolve_agent_tool_ids(&cc, "zero").unwrap();

        assert_eq!(ids, Some(vec![]));
    }

    #[test]
    fn yaml_without_tool_ids_key_is_none() {
        // 契约：yaml 正常但无 tool_ids 键 = 白名单未声明（Ok(None)，非配置损坏）
        let temp = tempfile::tempdir().unwrap();
        let cc = setup(temp.path().to_path_buf(), "no_tools", "name: t\n");

        let ids = resolve_agent_tool_ids(&cc, "no_tools").unwrap();

        assert_eq!(ids, None);
    }

    #[test]
    fn missing_agent_is_error() {
        // 契约（K5）：文件不存在 → Err(Missing)（调用方打 _agent_config_missing 标记）
        let temp = tempfile::tempdir().unwrap();
        let cc = ConfigCenter::new(temp.path().to_path_buf());

        assert_eq!(
            resolve_agent_tool_ids(&cc, "nonexistent"),
            Err(AgentToolIdsError::Missing)
        );
    }

    #[test]
    fn corrupt_agent_yaml_is_error() {
        // 契约（K5）：yaml 解析失败（缩进/语法坏）同样 Err，不返回半截配置
        let temp = tempfile::tempdir().unwrap();
        // tab 缩进在 YAML 里非法，稳定触发解析失败
        let cc = setup(temp.path().to_path_buf(), "broken", "a:\n\tb: 1\n");

        assert_eq!(
            resolve_agent_tool_ids(&cc, "broken"),
            Err(AgentToolIdsError::Corrupt)
        );
    }

    #[test]
    fn top_level_non_object_is_error() {
        // 契约（K5）：顶层非对象（纯标量 yaml）= 配置损坏
        let temp = tempfile::tempdir().unwrap();
        let cc = setup(temp.path().to_path_buf(), "scalar", "42\n");

        assert_eq!(
            resolve_agent_tool_ids(&cc, "scalar"),
            Err(AgentToolIdsError::Corrupt)
        );
    }

    #[test]
    fn finds_agent_in_subdirectory() {
        // 契约：支持 agents/main/xxx.yaml 分类子目录（find_agent_yaml 递归）
        let temp = tempfile::tempdir().unwrap();
        let agents_sub = temp.path().join("config/agents/main");
        std::fs::create_dir_all(&agents_sub).unwrap();
        std::fs::write(agents_sub.join("deep.yaml"), "tool_ids: [file_read]\n").unwrap();

        let cc = ConfigCenter::new(temp.path().join("config"));

        let ids = resolve_agent_tool_ids(&cc, "deep").unwrap().unwrap();
        assert_eq!(
            ids,
            vec!["file_read".to_string()],
            "子目录的 agent 也应找到"
        );
    }

    #[test]
    fn rereads_after_file_change() {
        // 契约：走 ConfigCenter.load → mtime 变了重读（热重载语义保留）
        let temp = tempfile::tempdir().unwrap();
        let cc = setup(temp.path().to_path_buf(), "a3", "tool_ids: [file_read]\n");
        assert_eq!(
            resolve_agent_tool_ids(&cc, "a3").unwrap().unwrap(),
            vec!["file_read".to_string()]
        );

        // 改文件（确保 mtime 变化）
        std::thread::sleep(std::time::Duration::from_millis(50));
        std::fs::write(
            temp.path().join("agents/a3.yaml"),
            "tool_ids: [bash_execute]\n",
        )
        .unwrap();

        assert_eq!(
            resolve_agent_tool_ids(&cc, "a3").unwrap().unwrap(),
            vec!["bash_execute".to_string()],
            "mtime 变了应重读到新内容"
        );
    }
}
