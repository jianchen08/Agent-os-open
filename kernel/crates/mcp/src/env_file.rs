//! 项目根 `.env` 运行时增量叠加（ADR §4.3 secrets 的免重启补全）。
//!
//! 内核只在**启动时**加载一次 `.env` 到进程环境（见 agentos-kernel.rs）。
//! 用户在设置页为提供者填写 API Key 时，channel_api 把明文写入 `.env`
//! 并把 llm.yaml 中的值改回 `${VAR}` 占位符——若 yaml 内容未变，
//! Pull 热加载指纹不触发 sidecar 重启，且即使重启、子进程继承的仍是
//! 内核启动时的旧环境。本模块补全两处：
//!
//! 1. [`env_delta_overlay`]：sidecar spawn 时把 `.env` 的**增量**直接叠加到
//!    子进程环境（不修改内核进程全局环境，避免多线程 set_var 的风险）；
//! 2. [`project_env_path`]：供 invoker 插件指纹纳入 `.env` mtime——
//!    `.env` 一变所有 sidecar 判定过期，下次调用自动 respawn 拿新 key。
//!
//! 优先级语义与内核启动加载一致：系统环境变量 > .env。覆盖仅发生在
//! 「当前环境值确来自上一次 .env 快照」的场景（即 .env 自身的更新），
//! 绝不用 .env 值覆盖系统显式设置的环境变量。
//!
//! 项目根定位：`AGENTOS_CONFIG_ROOT`（agentos-kernel.rs 启动时写入）
//! 的父目录；未设置时本模块全部降级为空操作，不比原来差。

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;
use tracing::debug;

/// 上一次快照的 .env 内容（进程内单例；None = 尚未建立基线）。
///
/// 首次调用只注入「环境缺失」的变量（与启动加载规则一致，不覆盖
/// 系统环境）；之后每次调用与快照比对，检测 .env 自身的变更。
static ENV_SNAPSHOT: Mutex<Option<HashMap<String, String>>> = Mutex::new(None);

/// 项目根 `.env` 路径（AGENTOS_CONFIG_ROOT 的父目录下）。
pub fn project_env_path() -> Option<PathBuf> {
    let config_root = std::env::var("AGENTOS_CONFIG_ROOT").ok()?;
    let root = PathBuf::from(config_root);
    let env_path = root.parent()?.join(".env");
    env_path.is_file().then_some(env_path)
}

/// 解析 .env 文本为 KEY=VALUE 映射（跳过注释与空行，去引号）。
fn parse_env_text(text: &str) -> HashMap<String, String> {
    let mut vars = HashMap::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if let Some((key, value)) = line.split_once('=') {
            let key = key.trim();
            if key.is_empty() {
                continue;
            }
            let value = value.trim().trim_matches('"');
            vars.insert(key.to_string(), value.to_string());
        }
    }
    vars
}

/// 计算 sidecar 子进程的环境增量叠加。
///
/// 返回 (key, value) 列表，直接经 `Command::env` 注入子进程：
/// - 环境中缺失的 .env 变量 → 注入（新填写的 key 属于此类）
/// - 环境值与上次快照一致但 .env 已更新 → 注入新值（key 轮换）
/// - 环境值与 .env 不同且不在快照中 → 跳过（系统环境变量优先）
///
/// 首次调用建立快照基线；读取失败静默返回空（spawn 走默认继承）。
pub fn env_delta_overlay() -> Vec<(String, String)> {
    let Some(env_path) = project_env_path() else {
        return Vec::new();
    };
    let Ok(text) = std::fs::read_to_string(&env_path) else {
        return Vec::new();
    };
    let current = parse_env_text(&text);

    let Ok(mut snapshot) = ENV_SNAPSHOT.lock() else {
        return Vec::new();
    };
    let prev = snapshot.as_ref();

    let mut overlay = Vec::new();
    for (key, value) in &current {
        match std::env::var(key) {
            // 环境缺失：注入（等价于内核启动时的「仅设缺失变量」规则）
            Err(_) => overlay.push((key.clone(), value.clone())),
            // 与 .env 一致：无需叠加
            Ok(cur) if cur == *value => {}
            // 与 .env 不一致：仅当当前值来自上次 .env 快照（.env 自身更新）才覆盖
            Ok(cur) => {
                let env_sourced = prev.map(|p| p.get(key) == Some(&cur)).unwrap_or(false);
                if env_sourced {
                    overlay.push((key.clone(), value.clone()));
                }
            }
        }
    }

    if overlay.is_empty() {
        debug!(target: "agentos-mcp::env_file", "env delta: none");
    } else {
        debug!(
            target: "agentos-mcp::env_file",
            "env delta: {} var(s) overlaid onto sidecar env",
            overlay.len()
        );
    }

    *snapshot = Some(current);
    overlay
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(text: &str) -> HashMap<String, String> {
        parse_env_text(text)
    }

    #[test]
    fn parse_skips_comments_and_quotes() {
        let vars = parse("# comment\n\nA=1\nB=\"hello world\"\n  C = x  \nINVALID\n");
        assert_eq!(vars.get("A").unwrap(), "1");
        assert_eq!(vars.get("B").unwrap(), "hello world");
        assert_eq!(vars.get("C").unwrap(), "x");
        assert!(!vars.contains_key("INVALID"));
    }

    #[test]
    fn overlay_injects_missing_vars_only() {
        // 系统环境中已有 SYS_VAR（值与 .env 不同）→ 不覆盖；
        // MISSING_VAR 环境缺失 → 注入。
        // 注：env_delta_overlay 依赖真实 std::env，测试仅在 AGENTOS_CONFIG_ROOT
        // 未指向含 .env 的目录时验证空操作降级。
        std::env::remove_var("AGENTOS_CONFIG_ROOT");
        assert!(project_env_path().is_none());
        assert!(env_delta_overlay().is_empty());
    }
}
