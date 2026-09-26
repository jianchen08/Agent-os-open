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
//! `.env` 定位：**用户空间根**（ADR 2026-09-13-unified-user-root，`<USER_ROOT>/.env`）
//! ——密钥是用户资产，落仓内会处于工作区还原的抹除风险面内；用户空间不可得时
//! 回落项目根 `.env`（保持旧行为）。读侧 [`project_env_path`] 与写侧
//! [`env_path_for_root`] 必须同源，否则会出现"写一处、读另一处"的静默分叉。

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;
use tracing::debug;

/// 上一次快照的 .env 内容（进程内单例；None = 尚未建立基线）。
///
/// 首次调用只注入「环境缺失」的变量（与启动加载规则一致，不覆盖
/// 系统环境）；之后每次调用与快照比对，检测 .env 自身的变更。
static ENV_SNAPSHOT: Mutex<Option<HashMap<String, String>>> = Mutex::new(None);

/// `.env` 文件路径（用户空间优先，回落项目根；文件须已存在）。
///
/// 供 invoker 插件指纹纳入 mtime。项目根经 `AGENTOS_CONFIG_ROOT`
/// （agentos-kernel.rs 启动时写入）的父目录推导。
pub fn project_env_path() -> Option<PathBuf> {
    let config_root = std::env::var("AGENTOS_CONFIG_ROOT").ok()?;
    let root = PathBuf::from(config_root);
    let project_root = root.parent()?;
    let env_path = env_path_for_root(project_root);
    env_path.is_file().then_some(env_path)
}

/// 指定项目根对应的 `.env` 路径（不要求已存在——写侧用于创建）。
///
/// 落点：**用户空间根 `.env`**（`<USER_ROOT>/.env`）优先；用户空间不可得时
/// 回落 `<project_root>/.env`。传 `project_root` 而非依赖进程环境，避免
/// 测试/多租户场景下环境变量的竞态。
pub fn env_path_for_root(project_root: &std::path::Path) -> std::path::PathBuf {
    if let Some(user_root) = agentos_core::user_space::user_root() {
        return user_root.join(".env");
    }
    project_root.join(".env")
}

/// 原子写入一组 .env 键值更新（GAP-4 写侧）。
///
/// 行级合并（不重排、不丢注释）：既有键原行更新、新键追加、空值移除；
/// 值含空白或 `#` 时加双引号（与 [`parse_env_text`] 的去引号回读对齐）。
/// 原子性：tmp 写入 + rename（与配置中心 atomic_write_yaml 同款），
/// 中断不会留下半截 .env。
pub fn write_env_updates(
    env_path: &std::path::Path,
    updates: &[(String, String)],
) -> Result<(), String> {
    let existing = std::fs::read_to_string(env_path).unwrap_or_default();
    let mut lines: Vec<String> = existing.lines().map(|l| l.to_string()).collect();

    for (key, value) in updates {
        let rendered = if value.is_empty() {
            String::new()
        } else if value.contains(' ') || value.contains('#') || value.contains('\t') {
            format!("\"{value}\"")
        } else {
            value.clone()
        };
        let prefix = format!("{key}=");
        // 找最后一个同名键行（用户手工可能重复声明，统一收敛为一行）
        let mut last_idx: Option<usize> = None;
        for (i, line) in lines.iter().enumerate() {
            let t = line.trim_start();
            if t == key || t.starts_with(&prefix) {
                last_idx = Some(i);
            }
        }
        match (last_idx, value.is_empty()) {
            (Some(i), true) => {
                lines.remove(i);
            }
            (Some(i), false) => {
                lines[i] = format!("{key}={rendered}");
            }
            (None, true) => {} // 本就不存在，无需移除
            (None, false) => lines.push(format!("{key}={rendered}")),
        }
    }

    let mut text = lines.join("\n");
    if !text.is_empty() {
        text.push('\n');
    }
    if let Some(parent) = env_path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("create .env dir: {e}"))?;
    }
    let tmp_path = env_path.with_extension("env.tmp");
    std::fs::write(&tmp_path, text.as_bytes()).map_err(|e| format!("write .env tmp: {e}"))?;
    if let Err(e) = std::fs::rename(&tmp_path, env_path) {
        // rename 失败 best-effort 清 tmp（D7：不留 .tmp 残骸）
        if let Err(cleanup_err) = std::fs::remove_file(&tmp_path) {
            tracing::warn!(
                target = %tmp_path.display(),
                error = %cleanup_err,
                "清理 .env.tmp 残骸失败"
            );
        }
        return Err(format!("rename .env: {e}"));
    }
    Ok(())
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

/// 解析 .env 文本为键值映射（读侧公开入口，GAP-4 路由层掩码视图用）。
pub fn parse_env_text_for_read(text: &str) -> HashMap<String, String> {
    parse_env_text(text)
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
pub(crate) mod tests {
    use super::*;
    use std::fs;

    /// AGENTOS_CONFIG_ROOT 相关测试的进程级互斥锁。
    ///
    /// 该变量是进程全局状态，cargo test 默认多线程并行跑——一个测试 remove、
    /// 另一个 set 会互相踩（client.rs 的 .env 集成测试与本模块的空操作降级
    /// 测试都操作它）。跨模块共享此锁串行化。
    pub(crate) static TEST_ENV_MUTEX: Mutex<()> = Mutex::new(());

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
    fn write_env_updates_creates_and_merges() {
        // GAP-4：写侧（原子写 + 行级合并）——新文件创建、既有键更新、
        // 无关键与注释保留。
        let tmp = tempfile::tempdir().unwrap();
        let env = tmp.path().join(".env");
        fs::write(&env, "# hand-written\nKEEP_ME=1\nOLD=k1\n").unwrap();

        write_env_updates(&env, &[("NEW_KEY".to_string(), "v1".to_string())]).unwrap();
        let text1 = fs::read_to_string(&env).unwrap();
        assert!(text1.contains("KEEP_ME=1"), "无关键保留: {text1}");
        assert!(text1.contains("# hand-written"), "注释保留: {text1}");
        assert!(text1.contains("NEW_KEY=v1"), "新键追加: {text1}");

        write_env_updates(&env, &[("OLD".to_string(), "k2".to_string())]).unwrap();
        let text2 = fs::read_to_string(&env).unwrap();
        assert!(text2.contains("OLD=k2"), "既有键更新: {text2}");
        assert!(!text2.contains("OLD=k1"), "旧值不残留: {text2}");

        // 新路径（.env 不存在）直接创建
        let env2 = tmp.path().join("sub").join(".env");
        write_env_updates(&env2, &[("A".to_string(), "1".to_string())]).unwrap();
        assert!(fs::read_to_string(&env2).unwrap().contains("A=1"));
    }

    #[test]
    fn write_env_updates_empty_value_removes_key() {
        // 清空语义：空值 = 移除该变量（前端"清除已保存的 key"路径）
        let tmp = tempfile::tempdir().unwrap();
        let env = tmp.path().join(".env");
        fs::write(&env, "SMITHERY_API_KEY=secret\nOTHER=2\n").unwrap();
        write_env_updates(&env, &[("SMITHERY_API_KEY".to_string(), String::new())]).unwrap();
        let text = fs::read_to_string(&env).unwrap();
        assert!(!text.contains("SMITHERY_API_KEY"), "空值移除键: {text}");
        assert!(text.contains("OTHER=2"), "其余键不受影响: {text}");
    }

    #[test]
    fn write_env_updates_quotes_values_with_spaces() {
        // 值含空白/井号时加引号，回读语义不漂移（与 parse_env_text 对齐）
        let tmp = tempfile::tempdir().unwrap();
        let env = tmp.path().join(".env");
        write_env_updates(&env, &[("K".to_string(), "a b # c".to_string())]).unwrap();
        let text = fs::read_to_string(&env).unwrap();
        assert!(text.contains("K=\"a b # c\""), "{text}");
    }

    #[test]
    fn overlay_injects_missing_vars_only() {
        // 系统环境中已有 SYS_VAR（值与 .env 不同）→ 不覆盖；
        // MISSING_VAR 环境缺失 → 注入。
        // 注：env_delta_overlay 依赖真实 std::env，测试仅在 AGENTOS_CONFIG_ROOT
        // 未指向含 .env 的目录时验证空操作降级。
        // unwrap_or_else(into_inner)：测试 panic 导致锁中毒时继续执行
        // （锁只为串行化 env 操作，中毒无并发安全含义）
        let _guard = TEST_ENV_MUTEX.lock().unwrap_or_else(|p| p.into_inner());
        std::env::remove_var("AGENTOS_CONFIG_ROOT");
        assert!(project_env_path().is_none());
        assert!(env_delta_overlay().is_empty());
    }

    #[test]
    fn write_env_updates_rename_failure_cleans_tmp() {
        // D7：rename 失败（.env 目标被同名目录占位模拟占用）→ Err 且 .env.tmp 被清理
        // unwrap_or_else(into_inner)：与其余持锁点同款——测试 panic 污染锁后
        // 若在此 unwrap 会把「一个用例失败」放大成后续用例连锁失败
        let _guard = TEST_ENV_MUTEX.lock().unwrap_or_else(|p| p.into_inner());
        let tmp = tempfile::tempdir().unwrap();
        let occupied = tmp.path().join(".env");
        std::fs::create_dir_all(&occupied).unwrap();
        let err = write_env_updates(&occupied, &[("A".to_string(), "1".to_string())]).unwrap_err();
        assert!(err.contains("rename"), "rename 失败应带原因: {err}");
        assert!(
            !tmp.path().join(".env.tmp").exists(),
            "rename 失败后 .env.tmp 残骸必须被清理"
        );
        // 成功路径回归：正常目标原子写入
        let ok = tmp.path().join("ok.env");
        write_env_updates(&ok, &[("B".to_string(), "2".to_string())]).unwrap();
        assert!(ok.exists());
    }

    /// 重复键的实际处理：写入落在**最后一条同名行**（last_idx 语义），
    /// 更早的重复行原样保留（不重排、不丢行是行级合并的既有契约）。
    /// 读侧 parse_env_text 后写覆盖前写，故最终读到的就是新值。
    #[test]
    fn write_env_updates_updates_last_duplicate_occurrence() {
        let tmp = tempfile::tempdir().unwrap();
        let env = tmp.path().join(".env");
        fs::write(
            &env,
            "DUP=first
OTHER=x
DUP=second
",
        )
        .unwrap();

        write_env_updates(&env, &[("DUP".to_string(), "final".to_string())]).unwrap();
        let text = fs::read_to_string(&env).unwrap();
        assert_eq!(
            text.matches("DUP=").count(),
            2,
            "既有重复行原样保留，只更新最后一条: {text}"
        );
        assert!(text.contains("DUP=final"), "最后一条应写入新值: {text}");
        assert!(text.contains("DUP=first"), "更早的同名行不被删除: {text}");
        assert!(text.contains("OTHER=x"), "无关键保留: {text}");
        // 读侧语义：后写覆盖前写 → 解析出的就是新值
        assert_eq!(
            parse_env_text(&text).get("DUP").map(String::as_str),
            Some("final"),
            "读侧应按最后一条取值"
        );
    }

    /// 空值移除：不存在的键走 (None, true) 分支——不报错、不新增行；
    /// 存在的键被整行删除（含重复声明的多行只删处理到的那一行）。
    #[test]
    fn write_env_updates_empty_value_for_absent_key_is_noop() {
        let tmp = tempfile::tempdir().unwrap();
        let env = tmp.path().join(".env");
        fs::write(
            &env, "KEEP=1
",
        )
        .unwrap();

        write_env_updates(&env, &[("ABSENT".to_string(), String::new())]).unwrap();
        let text = fs::read_to_string(&env).unwrap();
        assert_eq!(text.trim(), "KEEP=1", "缺键空值不应改动文件: {text}");
        assert!(!text.contains("ABSENT"), "不得新增空行: {text}");
    }

    /// 父目录不存在 → 自动创建（create_dir_all 分支）。
    /// 对照：目标路径无父目录（相对单段路径）时同样不 panic。
    #[test]
    fn write_env_updates_creates_parent_directories() {
        let tmp = tempfile::tempdir().unwrap();
        let nested = tmp.path().join("a/b/c/.env");
        write_env_updates(&nested, &[("K".to_string(), "v".to_string())]).unwrap();
        assert!(nested.is_file(), "应创建父目录并写入");
        assert!(fs::read_to_string(&nested).unwrap().contains("K=v"));
    }

    /// parse_env_text_for_read 与 parse_env_text 同语义（读侧公开入口）。
    #[test]
    fn parse_env_text_for_read_matches_internal_parser() {
        let text = "# c
A=1
B=\"quoted\"

";
        assert_eq!(parse_env_text_for_read(text), parse_env_text(text));
        assert_eq!(
            parse_env_text_for_read(text).get("B").map(String::as_str),
            Some("quoted")
        );
    }

    /// 空文本 / 全注释文本 → 空映射（不得产生空键条目）。
    #[test]
    fn parse_env_text_handles_empty_and_comments_only() {
        assert!(parse_env_text("").is_empty());
        assert!(parse_env_text(
            "

"
        )
        .is_empty());
        assert!(parse_env_text(
            "# only
# comments
"
        )
        .is_empty());
        // 键为空的行跳过
        assert!(parse_env_text(
            "=orphan
"
        )
        .is_empty());
    }

    /// env_path_for_root：用户空间不可得时回落 `<project_root>/.env`
    /// （写侧用于创建，故不要求文件已存在）。
    #[test]
    fn env_path_for_root_falls_back_to_project_root() {
        let _guard = TEST_ENV_MUTEX.lock().unwrap_or_else(|p| p.into_inner());
        let original = std::env::var(agentos_core::user_space::USER_ROOT_ENV).ok();
        // 显式清空用户根环境变量，强制走回落分支
        std::env::remove_var(agentos_core::user_space::USER_ROOT_ENV);

        let root = std::path::Path::new("/tmp/proj");
        let p = env_path_for_root(root);
        assert!(
            p.starts_with(root) || agentos_core::user_space::user_root().is_some(),
            "用户空间不可得时应回落项目根: {p:?}"
        );
        assert_eq!(p.file_name().and_then(|n| n.to_str()), Some(".env"));

        if let Some(v) = original {
            std::env::set_var(agentos_core::user_space::USER_ROOT_ENV, v);
        }
    }
}
