// @feature: FP-0.2.一 模式包注册扫描（约定即注册） | @ci: rust-test
//! 模式包目录扫描注册（设计稿 2026-09-15 §2.3「约定即注册」，内核唯一 additive
//! 接触点）。
//!
//! 装载期扫描模式包（出厂 `modes/` 与用户 `modes/` 根下、通过 G2 的插件包）的
//! 约定子目录并注册进包命名空间——用户加 agent/编排 = 放一个文件，零 manifest
//! 编辑：
//!
//! | 约定目录 | 注册为 | 本层校验 |
//! |---|---|---|
//! | `agents/*.yaml` | agent 键 `<mode_X>/<文件名 stem>` | yaml 可解析 + 顶层映射；agent schema 深校验留消费侧（Python agent_manager） |
//! | `pipelines/*.yaml` | 编排键 `<mode_X>/<文件名 stem>` | yaml 可解析 + 顶层映射 + 文件头 `task_kinds`（列表）结构最小校验；管道 schema 深校验对齐 G10 编译器 |
//!
//! 键永远带包命名空间（`mode_X/<stem>`，恰一个 `/`）——系统键（`autonomous`/
//! `main` 等）是裸键，与模式键结构不相交，同名冲突被键形结构排除；跨包同名
//! （同根同 id 双目录）与注册闸键冲突在扫描/注册处 fail-closed 报错。
//!
//! 概念分离：编排键是管道**定义**（函数）；`pipeline_id` 是一次运行的实例
//! ID，本模块不涉及。子目录缺席 = 零注册不报错（存量种子包尚无 agents/
//! pipelines 子目录）。

use std::collections::{BTreeMap, HashSet};
use std::path::{Path, PathBuf};

use agentos_core::traits::RegistrationGuard;
use tracing::info;

/// 约定子目录名：模式私有 agent（纯身份基座）。
const AGENTS_DIR: &str = "agents";
/// 约定子目录名：模式内编排。
const PIPELINES_DIR: &str = "pipelines";
/// 约定文件扩展名（契约即 `*.yaml`，不含 `.yml`）。
const CONVENTION_EXT: &str = ".yaml";
/// 管道文件头字段：编排参与解析链②路由的任务型标注（列表值）。
const TASK_KINDS_FIELD: &str = "task_kinds";

/// 模式包注册的一个 agent 条目（约定 `agents/<stem>.yaml`）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModeAgentEntry {
    /// agent 键：`<mode_id>/<文件名 stem>`，带包命名空间全局唯一。
    pub key: String,
    /// 提供者插件 id（包 plugin.json 的 id，即通过 G2 的插件）。
    pub plugin_id: String,
    /// yaml 文件路径（消费侧深校验/编辑定位用）。
    pub path: PathBuf,
}

/// 模式包注册的一个编排条目（约定 `pipelines/<stem>.yaml`）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModePipelineEntry {
    /// 编排键：`<mode_id>/<文件名 stem>`（管道定义；与运行实例 pipeline_id 概念分离）。
    pub key: String,
    /// 提供者插件 id。
    pub plugin_id: String,
    /// yaml 文件路径。
    pub path: PathBuf,
    /// 文件头 `task_kinds`（缺省/空 = 无路由标注；深语义由消费侧解释）。
    pub task_kinds: Vec<String>,
}

/// 单个模式包的约定资源注册集（扫描产出 → 注册闸消费）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModePackageResources {
    /// 提供者插件 id。
    pub plugin_id: String,
    /// 模式命名空间（包目录名，如 `mode_writing`）。
    pub mode_id: String,
    /// agents 约定目录注册的条目（按 stem 字典序）。
    pub agents: Vec<ModeAgentEntry>,
    /// pipelines 约定目录注册的条目（按 stem 字典序）。
    pub pipelines: Vec<ModePipelineEntry>,
}

/// 模式包扫描/注册的错误（fail-closed：调用方拒绝装载，不静默降级）。
#[derive(Debug, Clone, thiserror::Error)]
pub enum ModeResourceError {
    /// 键/包身份冲突：同根同 id 双目录、或注册键已被占用。
    #[error(
        "mode package resource conflict: '{key}' already present ({existing}); incoming {incoming}"
    )]
    Conflict {
        key: String,
        existing: String,
        incoming: String,
    },
    /// 约定文件不可解析或结构非法（顶层非映射 / task_kinds 非字符串列表）。
    #[error("mode package resource invalid: {path}: {reason}")]
    Parse { path: String, reason: String },
    /// 约定文件 IO 错误。
    #[error("mode package resource io error: {path}: {reason}")]
    Io { path: String, reason: String },
}

/// 扫描模式包约定子目录（装载期，设计稿 §2.3）。
///
/// `factory_modes_dir` / `user_modes_dir` 是两个模式包根（如
/// `plugins/shared/modes/` 与 `<USER_ROOT>/plugins/modes/`）；同 id 双根时
/// 用户副本赢（双根兜底语义）。`registrable_plugin_ids` 是已通过 G2（且按
/// 启用层放行）的插件 id 集合——未通过/被禁用的包不注册。
///
/// 返回按插件 id 字典序排列的包注册集（无约定资源的包不产出）；约定子目录
/// 缺席 = 零注册不报错。键冲突/yaml 非法/结构非法 → `Err`（fail-closed）。
pub fn scan_mode_package_resources(
    factory_modes_dir: &Path,
    user_modes_dir: Option<&Path>,
    registrable_plugin_ids: &HashSet<String>,
) -> Result<Vec<ModePackageResources>, ModeResourceError> {
    // 插件 id → (mode_id, 包目录)。同根同 id 双目录 = 包制作错误（跨包同名），
    // fail-closed；双根同 id = 用户副本赢（覆盖出厂条目，双根兜底语义）。
    // BTreeMap 保证产出顺序确定性（按插件 id）。
    let mut resolved: BTreeMap<String, (String, PathBuf)> = BTreeMap::new();
    resolve_mode_packages(factory_modes_dir, false, &mut resolved)?;
    if let Some(user_dir) = user_modes_dir {
        resolve_mode_packages(user_dir, true, &mut resolved)?;
    }

    let mut out = Vec::new();
    for (plugin_id, (mode_id, dir)) in &resolved {
        if !registrable_plugin_ids.contains(plugin_id) {
            continue;
        }
        let package = scan_package_conventions(mode_id, dir, plugin_id)?;
        if !package.agents.is_empty() || !package.pipelines.is_empty() {
            out.push(package);
        }
    }
    Ok(out)
}

/// 枚举一个模式包根下的包目录（`<modes_root>/<mode_id>/`），读出各包 plugin id。
///
/// `is_user` 为假（出厂根）时同 id 双目录 = 包制作错误，fail-closed；为真
/// （用户根）时同 id 覆盖已解析条目——双根同 id 用户赢。manifest 缺失/不可
/// 解析（未过 G2）或 id 为空的目录跳过。
fn resolve_mode_packages(
    modes_root: &Path,
    is_user: bool,
    resolved: &mut BTreeMap<String, (String, PathBuf)>,
) -> Result<(), ModeResourceError> {
    // 根缺席（存量部署/用户空间未启用）= 零注册，不报错。
    let Ok(entries) = std::fs::read_dir(modes_root) else {
        return Ok(());
    };
    for entry in entries.flatten() {
        let dir = entry.path();
        if !dir.is_dir() {
            continue;
        }
        let Some(mode_id) = entry.file_name().to_str().map(str::to_string) else {
            continue;
        };
        if mode_id.starts_with('.') {
            continue;
        }
        let Some(plugin_id) = read_package_plugin_id(&dir)? else {
            continue;
        };
        if plugin_id.is_empty() {
            continue;
        }
        if let Some((existing_mode, _)) = resolved.get(&plugin_id) {
            if !is_user {
                return Err(ModeResourceError::Conflict {
                    key: plugin_id,
                    existing: existing_mode.clone(),
                    incoming: mode_id,
                });
            }
        }
        resolved.insert(plugin_id, (mode_id, dir));
    }
    Ok(())
}

/// 读包 manifest 的 `id`（plugin.json 优先、plugin.yaml 兜底，与发现层同一
/// 解析顺序）。manifest 缺失/不可解析 = 包未过 G2 → `None`。
fn read_package_plugin_id(dir: &Path) -> Result<Option<String>, ModeResourceError> {
    for name in ["plugin.json", "plugin.yaml"] {
        let path = dir.join(name);
        if !path.is_file() {
            continue;
        }
        let text = std::fs::read_to_string(&path).map_err(|e| ModeResourceError::Io {
            path: path.display().to_string(),
            reason: e.to_string(),
        })?;
        let value: serde_json::Value = match serde_json::from_str(&text) {
            Ok(v) => v,
            Err(_) => match serde_yaml::from_str(&text) {
                Ok(v) => v,
                Err(_) => return Ok(None),
            },
        };
        return Ok(value.get("id").and_then(|v| v.as_str()).map(str::to_string));
    }
    Ok(None)
}

/// 扫描单个包的 agents/ pipelines/ 约定子目录。
fn scan_package_conventions(
    mode_id: &str,
    dir: &Path,
    plugin_id: &str,
) -> Result<ModePackageResources, ModeResourceError> {
    let mut agents = Vec::new();
    for (stem, path, _value) in scan_yaml_files(&dir.join(AGENTS_DIR))? {
        agents.push(ModeAgentEntry {
            key: format!("{mode_id}/{stem}"),
            plugin_id: plugin_id.to_string(),
            path,
        });
    }
    let mut pipelines = Vec::new();
    for (stem, path, value) in scan_yaml_files(&dir.join(PIPELINES_DIR))? {
        pipelines.push(ModePipelineEntry {
            key: format!("{mode_id}/{stem}"),
            plugin_id: plugin_id.to_string(),
            task_kinds: extract_task_kinds(&path, &value)?,
            path,
        });
    }
    Ok(ModePackageResources {
        plugin_id: plugin_id.to_string(),
        mode_id: mode_id.to_string(),
        agents,
        pipelines,
    })
}

/// 枚举约定目录下的 `*.yaml`（隐藏文件跳过；目录缺席 = 零注册），逐文件做
/// 可解析性 + 顶层映射最小校验。返回 `(stem, 路径, 解析值)`，按 stem 字典序。
fn scan_yaml_files(
    convention_dir: &Path,
) -> Result<Vec<(String, PathBuf, serde_yaml::Value)>, ModeResourceError> {
    let mut out = Vec::new();
    let Ok(entries) = std::fs::read_dir(convention_dir) else {
        return Ok(out);
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let Some(name) = entry.file_name().to_str().map(str::to_string) else {
            continue;
        };
        if name.starts_with('.') || !name.ends_with(CONVENTION_EXT) || !path.is_file() {
            continue;
        }
        let text = std::fs::read_to_string(&path).map_err(|e| ModeResourceError::Io {
            path: path.display().to_string(),
            reason: e.to_string(),
        })?;
        let value: serde_yaml::Value =
            serde_yaml::from_str(&text).map_err(|e| ModeResourceError::Parse {
                path: path.display().to_string(),
                reason: e.to_string(),
            })?;
        if !value.is_mapping() {
            return Err(ModeResourceError::Parse {
                path: path.display().to_string(),
                reason: "顶层必须是映射（对象）".to_string(),
            });
        }
        out.push((
            name.strip_suffix(CONVENTION_EXT)
                .unwrap_or(&name)
                .to_string(),
            path,
            value,
        ));
    }
    out.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(out)
}

/// 文件头 `task_kinds` 结构最小校验（设计稿 §2.3）：缺省/空 → 空表；列表 →
/// 逐项必须字符串；其他形态 → Parse 错误。深语义（对齐 G10 编译器）留消费侧。
fn extract_task_kinds(
    path: &Path,
    value: &serde_yaml::Value,
) -> Result<Vec<String>, ModeResourceError> {
    let reject = |reason: String| ModeResourceError::Parse {
        path: path.display().to_string(),
        reason,
    };
    match value.get(TASK_KINDS_FIELD) {
        None | Some(serde_yaml::Value::Null) => Ok(Vec::new()),
        Some(serde_yaml::Value::Sequence(items)) => items
            .iter()
            .map(|item| {
                item.as_str()
                    .map(str::to_string)
                    .ok_or_else(|| reject("task_kinds 条目必须是字符串".to_string()))
            })
            .collect(),
        Some(_) => Err(reject(format!("{TASK_KINDS_FIELD} 必须是字符串列表"))),
    }
}

/// 注册闸：把一个模式包的约定资源登记进注册表维度（fail-closed）。
///
/// 任一键已被注册（跨包同名/重复装载）或包内键自身重复 → `Err`，不产生部分
/// 插入。成功返回撤销 guard——revoke（disable/热重载收回）时精确移除本包
/// 全部键（禁用即同源消失，与工具/HTTP 路由维度同一套 M1 语义）。
pub fn register_mode_package_guarded(
    registry: &std::sync::Arc<crate::registry::CapabilityRegistryImpl>,
    package: ModePackageResources,
) -> Result<RegistrationGuard, String> {
    let plugin_id = package.plugin_id.clone();
    let mut keys: Vec<(String, bool)> = Vec::new(); // (键, 是否编排键)
    for entry in &package.agents {
        keys.push((entry.key.clone(), false));
    }
    for entry in &package.pipelines {
        keys.push((entry.key.clone(), true));
    }
    // 占用检查 + 包内去重（扫描层按目录枚举不会重复，防扫描面扩展漂移）。
    {
        let mut seen: HashSet<&str> = HashSet::new();
        for (key, is_pipeline) in &keys {
            if !seen.insert(key.as_str()) {
                return Err(format!(
                    "mode resource key duplicated within package {plugin_id}: '{key}'"
                ));
            }
            let taken = if *is_pipeline {
                registry.get_mode_pipeline(key).is_some()
            } else {
                registry.get_mode_agent(key).is_some()
            };
            if taken {
                return Err(format!(
                    "mode resource key conflict: '{key}' already registered (incoming plugin={plugin_id})"
                ));
            }
        }
    }
    registry.insert_mode_entries(&package);
    info!(
        plugin = %plugin_id,
        mode = %package.mode_id,
        agents = package.agents.len(),
        pipelines = package.pipelines.len(),
        "Mode package resources registered (convention-scan)"
    );
    let weak = std::sync::Arc::downgrade(registry);
    Ok(RegistrationGuard::new(move || {
        if let Some(reg) = weak.upgrade() {
            reg.remove_mode_keys_of(&plugin_id, &keys);
        }
    }))
}

/// 增量重扫入口（watcher 热发现接线）：把一个模式包的注册键表刷新为盘上扫描
/// 现状，返回是否发生了键表变化。
///
/// - 键表与扫描结果一致 → no-op（每轮 sync 的快路径，零 churn，不新增 guard）；
/// - 不一致 → 按快照移除旧键，再经 [`register_mode_package_guarded`] 登记新键表
///   （guard 入 plugin scope，禁用/卸载即同源消失，与工具/HTTP 路由维度同一套
///   M1 语义）；
/// - 扫描失败不进本函数（调用方负责告警并保留旧表）；注册失败（键冲突）→
///   回滚重登旧快照后报 `Err`，旧键表保留。
///
/// 重注册在 scope 里追加新 guard（scope 不支持单条撤销，旧 guard 的键名单在
/// 后续 scope 收回时按名单幂等移除，最多重复删已不在的键，无副作用）。
pub fn refresh_mode_package_guarded(
    registry: &std::sync::Arc<crate::registry::CapabilityRegistryImpl>,
    scopes: &crate::registry::PluginScopeRegistry,
    package: ModePackageResources,
) -> Result<bool, String> {
    let plugin_id = package.plugin_id.clone();
    // 快照该插件现注册条目（键表即真相源），用于相等比对与失败回滚；mode_id
    // 对齐入参——相等性只比条目本身（键/路径/内容），目录元数据漂移不触发重登。
    let old = ModePackageResources {
        plugin_id: plugin_id.clone(),
        mode_id: package.mode_id.clone(),
        agents: registry
            .list_mode_agents()
            .into_iter()
            .filter(|e| e.plugin_id == plugin_id)
            .collect(),
        pipelines: registry
            .list_mode_pipelines()
            .into_iter()
            .filter(|e| e.plugin_id == plugin_id)
            .collect(),
    };
    if old == package {
        return Ok(false);
    }
    let old_keys: Vec<(String, bool)> = old
        .agents
        .iter()
        .map(|e| (e.key.clone(), false))
        .chain(old.pipelines.iter().map(|e| (e.key.clone(), true)))
        .collect();
    registry.remove_mode_keys_of(&plugin_id, &old_keys);
    match register_mode_package_guarded(registry, package) {
        Ok(guard) => {
            scopes.scope_of(&plugin_id).track(guard);
            Ok(true)
        }
        Err(e) => {
            // 回滚：重登旧快照（重扫失败保留旧键表）。回滚自身也失败极罕见
            // （键被他人抢占），一并上报不静默。
            if !old.agents.is_empty() || !old.pipelines.is_empty() {
                match register_mode_package_guarded(registry, old) {
                    Ok(guard) => scopes.scope_of(&plugin_id).track(guard),
                    Err(restore_err) => return Err(format!("{e}; restore failed: {restore_err}")),
                }
            }
            Err(e)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_core::traits::CapabilityRegistry;
    use std::sync::Arc;

    /// 写一个模式包 fixture：plugin.json + 约定子目录内容。
    fn write_package(
        modes_root: &Path,
        mode_id: &str,
        version: &str,
        agents: &[(&str, &str)],
        pipelines: &[(&str, &str)],
    ) -> PathBuf {
        let dir = modes_root.join(mode_id);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("plugin.json"),
            format!(r#"{{"id":"{mode_id}","version":"{version}"}}"#),
        )
        .unwrap();
        for (name, content) in agents {
            std::fs::create_dir_all(dir.join("agents")).unwrap();
            std::fs::write(dir.join("agents").join(name), content).unwrap();
        }
        for (name, content) in pipelines {
            std::fs::create_dir_all(dir.join("pipelines")).unwrap();
            std::fs::write(dir.join("pipelines").join(name), content).unwrap();
        }
        dir
    }

    fn ids(items: &[&str]) -> HashSet<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn scan_registers_agent_and_pipeline_keys_with_task_kinds() {
        let tmp = tempfile::tempdir().unwrap();
        let modes = tmp.path().join("modes");
        write_package(
            &modes,
            "mode_writing",
            "1.0.0",
            &[
                ("novelist.yaml", "model_tier: large\n"),
                ("editor.yaml", "model_tier: medium\n"),
            ],
            &[(
                "chapter.yaml",
                "task_kinds:\n  - writing.chapter\n  - writing.outline\nsteps: []\n",
            )],
        );

        let packages = scan_mode_package_resources(&modes, None, &ids(&["mode_writing"])).unwrap();
        assert_eq!(packages.len(), 1);
        let pkg = &packages[0];
        assert_eq!(pkg.plugin_id, "mode_writing");
        assert_eq!(pkg.mode_id, "mode_writing");
        // agent 键 = mode_X/<stem>，按 stem 字典序
        assert_eq!(
            pkg.agents
                .iter()
                .map(|a| a.key.as_str())
                .collect::<Vec<_>>(),
            vec!["mode_writing/editor", "mode_writing/novelist"]
        );
        assert_eq!(pkg.agents[0].plugin_id, "mode_writing");
        assert!(pkg.agents[0].path.ends_with("agents/editor.yaml"));
        // 编排键 + task_kinds 暴露
        assert_eq!(pkg.pipelines.len(), 1);
        assert_eq!(pkg.pipelines[0].key, "mode_writing/chapter");
        assert_eq!(
            pkg.pipelines[0].task_kinds,
            vec!["writing.chapter", "writing.outline"]
        );
    }

    #[test]
    fn scan_missing_convention_dirs_is_zero_registration() {
        let tmp = tempfile::tempdir().unwrap();
        let modes = tmp.path().join("modes");
        // 现状种子包形态：只有 plugin.json/profile.yaml，无 agents/pipelines
        write_package(&modes, "mode_coding", "1.0.0", &[], &[]);

        let packages = scan_mode_package_resources(&modes, None, &ids(&["mode_coding"])).unwrap();
        assert!(packages.is_empty(), "无约定资源 → 零注册不报错");

        // 根本身缺席同样零注册
        let missing = tmp.path().join("not_deployed");
        assert!(
            scan_mode_package_resources(&missing, None, &ids(&["mode_coding"]))
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn scan_skips_packages_not_passed_or_not_enabled() {
        let tmp = tempfile::tempdir().unwrap();
        let modes = tmp.path().join("modes");
        write_package(&modes, "mode_ok", "1.0.0", &[("a.yaml", "x: 1\n")], &[]);
        // 不在可注册集合（G2 未过/被禁用）→ 跳过
        assert!(scan_mode_package_resources(&modes, None, &ids(&[]))
            .unwrap()
            .is_empty());

        // manifest 缺失/不可解析 = 未过 G2 → 跳过（不 fail 整轮扫描）
        let broken = modes.join("mode_broken");
        std::fs::create_dir_all(&broken).unwrap();
        std::fs::write(broken.join("plugin.json"), "not json {{{").unwrap();
        std::fs::create_dir_all(broken.join("agents")).unwrap();
        std::fs::write(broken.join("agents/x.yaml"), "x: 1\n").unwrap();
        assert!(
            scan_mode_package_resources(&modes, None, &ids(&["mode_broken", "mode_ok"]))
                .unwrap()
                .iter()
                .all(|p| p.mode_id != "mode_broken")
        );

        // 无 manifest 的散目录（如 modes/tests）同样跳过
        std::fs::create_dir_all(modes.join("tests")).unwrap();
        assert!(scan_mode_package_resources(&modes, None, &ids(&["tests"]))
            .unwrap()
            .is_empty());
    }

    #[test]
    fn scan_user_root_wins_over_factory_for_same_id() {
        let tmp = tempfile::tempdir().unwrap();
        let factory = tmp.path().join("factory-modes");
        let user = tmp.path().join("user-modes");
        write_package(
            &factory,
            "mode_x",
            "1.0.0",
            &[("f.yaml", "from: factory\n")],
            &[],
        );
        write_package(&user, "mode_x", "1.0.0", &[("u.yaml", "from: user\n")], &[]);

        let packages =
            scan_mode_package_resources(&factory, Some(&user), &ids(&["mode_x"])).unwrap();
        assert_eq!(packages.len(), 1, "同 id 双根只出一份（用户赢）");
        assert_eq!(
            packages[0]
                .agents
                .iter()
                .map(|a| a.key.as_str())
                .collect::<Vec<_>>(),
            vec!["mode_x/u"],
            "注册的是用户副本的约定资源"
        );
    }

    #[test]
    fn scan_duplicate_package_id_in_same_root_fails_closed() {
        // 同一模式根下两个目录声明同 id = 包制作错误（跨包同名），装载报错。
        let tmp = tempfile::tempdir().unwrap();
        let modes = tmp.path().join("modes");
        {
            let dir = modes.join("mode_a");
            std::fs::create_dir_all(&dir).unwrap();
            std::fs::write(
                dir.join("plugin.json"),
                r#"{"id":"same_id","version":"1.0.0"}"#,
            )
            .unwrap();
        }
        {
            let dir = modes.join("mode_b");
            std::fs::create_dir_all(&dir).unwrap();
            std::fs::write(
                dir.join("plugin.json"),
                r#"{"id":"same_id","version":"1.0.0"}"#,
            )
            .unwrap();
        }
        let err = scan_mode_package_resources(&modes, None, &ids(&["same_id"])).unwrap_err();
        assert!(matches!(err, ModeResourceError::Conflict { .. }), "{err}");
        assert!(err.to_string().contains("same_id"), "{err}");
    }

    #[test]
    fn scan_unparseable_or_non_mapping_yaml_fails_closed() {
        let tmp = tempfile::tempdir().unwrap();
        let modes = tmp.path().join("modes");
        let dir = write_package(&modes, "mode_x", "1.0.0", &[], &[]);

        // ① yaml 语法坏
        std::fs::create_dir_all(dir.join("agents")).unwrap();
        std::fs::write(dir.join("agents/bad.yaml"), "a: [broken\n  ::::").unwrap();
        let err = scan_mode_package_resources(&modes, None, &ids(&["mode_x"])).unwrap_err();
        assert!(matches!(err, ModeResourceError::Parse { .. }), "{err}");
        assert!(err.to_string().contains("bad.yaml"), "{err}");

        // ② 可解析但顶层不是映射
        std::fs::write(dir.join("agents/bad.yaml"), "- just\n- a list\n").unwrap();
        let err = scan_mode_package_resources(&modes, None, &ids(&["mode_x"])).unwrap_err();
        assert!(matches!(err, ModeResourceError::Parse { .. }), "{err}");

        // ③ 空文件（解析为 Null，非映射）同样拒
        std::fs::create_dir_all(dir.join("pipelines")).unwrap();
        std::fs::write(dir.join("pipelines/empty.yaml"), "").unwrap();
        std::fs::remove_file(dir.join("agents/bad.yaml")).unwrap();
        let err = scan_mode_package_resources(&modes, None, &ids(&["mode_x"])).unwrap_err();
        assert!(matches!(err, ModeResourceError::Parse { .. }), "{err}");
    }

    #[test]
    fn scan_pipeline_task_kinds_must_be_string_list() {
        let tmp = tempfile::tempdir().unwrap();
        let modes = tmp.path().join("modes");
        let dir = write_package(&modes, "mode_x", "1.0.0", &[], &[]);
        std::fs::create_dir_all(dir.join("pipelines")).unwrap();

        // 合法形态：缺省 / 显式空 / 字符串列表
        std::fs::write(dir.join("pipelines/no_field.yaml"), "steps: []\n").unwrap();
        std::fs::write(
            dir.join("pipelines/null_field.yaml"),
            "task_kinds:\nsteps: []\n",
        )
        .unwrap();
        std::fs::write(
            dir.join("pipelines/ok.yaml"),
            "task_kinds: [a.b, c.d]\nsteps: []\n",
        )
        .unwrap();
        let packages = scan_mode_package_resources(&modes, None, &ids(&["mode_x"])).unwrap();
        let kinds = |stem: &str| {
            packages[0]
                .pipelines
                .iter()
                .find(|p| p.key == format!("mode_x/{stem}"))
                .unwrap()
                .task_kinds
                .clone()
        };
        assert!(kinds("no_field").is_empty());
        assert!(kinds("null_field").is_empty());
        assert_eq!(kinds("ok"), vec!["a.b", "c.d"]);

        // 非法形态：标量 / 非字符串条目 → fail-closed
        std::fs::write(dir.join("pipelines/ok.yaml"), "task_kinds: writing\n").unwrap();
        let err = scan_mode_package_resources(&modes, None, &ids(&["mode_x"])).unwrap_err();
        assert!(err.to_string().contains("字符串列表"), "{err}");

        std::fs::write(dir.join("pipelines/ok.yaml"), "task_kinds: [a, 1]\n").unwrap();
        let err = scan_mode_package_resources(&modes, None, &ids(&["mode_x"])).unwrap_err();
        assert!(err.to_string().contains("字符串"), "{err}");
    }

    #[test]
    fn scan_hidden_files_and_non_yaml_ignored() {
        let tmp = tempfile::tempdir().unwrap();
        let modes = tmp.path().join("modes");
        let dir = write_package(&modes, "mode_x", "1.0.0", &[], &[]);
        std::fs::create_dir_all(dir.join("agents")).unwrap();
        std::fs::write(dir.join("agents/.hidden.yaml"), "x: 1\n").unwrap();
        std::fs::write(dir.join("agents/README.md"), "# doc\n").unwrap();
        // 契约即 *.yaml：.yml 不在约定面（静默忽略，不报错）
        std::fs::write(dir.join("agents/short.yml"), "x: 1\n").unwrap();

        let packages = scan_mode_package_resources(&modes, None, &ids(&["mode_x"])).unwrap();
        assert!(packages.is_empty(), "隐藏/非 yaml/非约定扩展名一律忽略");
    }

    // ── 注册闸 + registry 维度：可查 / 冲突 fail-closed / 撤销收回 ──

    fn sample_package(mode_id: &str, agent_key: &str, pipeline_key: &str) -> ModePackageResources {
        ModePackageResources {
            plugin_id: mode_id.to_string(),
            mode_id: mode_id.to_string(),
            agents: vec![ModeAgentEntry {
                key: format!("{mode_id}/{agent_key}"),
                plugin_id: mode_id.to_string(),
                path: PathBuf::from(format!("/pkg/{mode_id}/agents/{agent_key}.yaml")),
            }],
            pipelines: vec![ModePipelineEntry {
                key: format!("{mode_id}/{pipeline_key}"),
                plugin_id: mode_id.to_string(),
                path: PathBuf::from(format!("/pkg/{mode_id}/pipelines/{pipeline_key}.yaml")),
                task_kinds: vec!["t.one".to_string()],
            }],
        }
    }

    #[test]
    fn mode_resources_registered_queryable_and_revoked_by_guard() {
        let registry = Arc::new(crate::registry::CapabilityRegistryImpl::new());
        let guard =
            register_mode_package_guarded(&registry, sample_package("mode_x", "card", "main"))
                .unwrap();

        // 消费方取数路径：按键精确查 + 全量列表（按键字典序）
        let agent = registry.get_mode_agent("mode_x/card").unwrap();
        assert_eq!(agent.plugin_id, "mode_x");
        let pipeline = registry.get_mode_pipeline("mode_x/main").unwrap();
        assert_eq!(pipeline.task_kinds, vec!["t.one"]);
        assert!(registry.get_mode_agent("mode_x/absent").is_none());
        assert_eq!(registry.list_mode_agents().len(), 1);
        assert_eq!(registry.list_mode_pipelines().len(), 1);

        // guard revoke（禁用即同源消失）
        drop(guard);
        assert!(registry.get_mode_agent("mode_x/card").is_none());
        assert!(registry.get_mode_pipeline("mode_x/main").is_none());
        assert!(registry.list_mode_agents().is_empty());
    }

    #[test]
    fn mode_resource_key_conflict_fails_closed_without_partial_insert() {
        let registry = Arc::new(crate::registry::CapabilityRegistryImpl::new());
        // guard 必须绑定存活：guard drop = 注册收回（RAII），这正是被验证的语义
        let _guard =
            register_mode_package_guarded(&registry, sample_package("mode_x", "card", "main"))
                .unwrap();

        // 跨包同名键冲突：注册键已被占用 → Err（不部分插入）
        let mut clashing = sample_package("mode_y", "card", "other");
        clashing.agents[0].key = "mode_x/card".to_string();
        let err = match register_mode_package_guarded(&registry, clashing) {
            Err(err) => err,
            Ok(_) => panic!("键冲突必须被拒绝"),
        };
        assert!(
            err.contains("conflict") && err.contains("mode_x/card"),
            "{err}"
        );
        // 原注册完好，冲突方零残留
        assert_eq!(
            registry.get_mode_agent("mode_x/card").unwrap().plugin_id,
            "mode_x"
        );
        assert!(registry.get_mode_pipeline("mode_y/other").is_none());

        // 包内键自身重复 → Err
        let mut dup = sample_package("mode_z", "a", "main");
        dup.pipelines[0].key = "mode_z/a".to_string();
        let err = match register_mode_package_guarded(&registry, dup) {
            Err(err) => err,
            Ok(_) => panic!("包内键重复必须被拒绝"),
        };
        assert!(err.contains("duplicated"), "{err}");
        assert!(registry.get_mode_agent("mode_z/a").is_none());
    }

    #[test]
    fn clear_plugin_removes_mode_resources_of_that_plugin_only() {
        let registry = Arc::new(crate::registry::CapabilityRegistryImpl::new());
        let _g1 =
            register_mode_package_guarded(&registry, sample_package("mode_x", "card", "main"))
                .unwrap();
        let _g2 =
            register_mode_package_guarded(&registry, sample_package("mode_y", "card", "main"))
                .unwrap();

        registry.clear_plugin("mode_x");
        assert!(registry.get_mode_agent("mode_x/card").is_none());
        assert!(registry.get_mode_pipeline("mode_x/main").is_none());
        assert!(
            registry.get_mode_agent("mode_y/card").is_some(),
            "其他模式包资源不受牵连"
        );
        assert!(registry.get_mode_pipeline("mode_y/main").is_some());
    }

    /// 端到端形态（消费方取数路径预演）：扫描 fixture 包 → 注册闸登记 →
    /// 经 registry 维度读出（boot 装配同款调用序）。
    #[test]
    fn scan_then_register_end_to_end_matches_consumer_query_path() {
        let tmp = tempfile::tempdir().unwrap();
        let modes = tmp.path().join("modes");
        write_package(
            &modes,
            "mode_roleplay",
            "1.0.0",
            &[("alice.yaml", "name: alice\n")],
            &[("main.yaml", "task_kinds: [roleplay.chat]\n")],
        );

        let registry = Arc::new(crate::registry::CapabilityRegistryImpl::new());
        let packages = scan_mode_package_resources(&modes, None, &ids(&["mode_roleplay"])).unwrap();
        // guard 集中绑定存活（drop 即收回，这正是 M1 RAII 语义）
        let _guards: Vec<_> = packages
            .into_iter()
            .map(|package| register_mode_package_guarded(&registry, package).unwrap())
            .collect();
        let keys: Vec<String> = registry
            .list_mode_agents()
            .into_iter()
            .map(|a| a.key)
            .collect();
        assert_eq!(keys, vec!["mode_roleplay/alice"]);
        let pipelines = registry.list_mode_pipelines();
        assert_eq!(pipelines[0].key, "mode_roleplay/main");
        assert_eq!(pipelines[0].task_kinds, vec!["roleplay.chat"]);
    }

    // ── 增量重扫入口：no-op 快路径 / 键表同源增删 / 失败回滚 ──

    #[test]
    fn refresh_is_noop_when_unchanged_and_syncs_hot_add_and_delete() {
        let registry = Arc::new(crate::registry::CapabilityRegistryImpl::new());
        let scopes = crate::registry::PluginScopeRegistry::new();
        // boot 形态：基线 = 1 agent + 1 pipeline（guard 入 scope）
        let base = sample_package("mode_x", "card", "main");
        let guard = register_mode_package_guarded(&registry, base.clone()).unwrap();
        scopes.scope_of("mode_x").track(guard);
        let guards_after_boot = scopes.scope_of("mode_x").len();

        // 键表与盘上现状一致 → no-op：不 churn、不追加 guard
        let changed = refresh_mode_package_guarded(&registry, &scopes, base).unwrap();
        assert!(!changed, "一致时必须 no-op");
        assert_eq!(
            scopes.scope_of("mode_x").len(),
            guards_after_boot,
            "no-op 不得追加 guard（零 churn）"
        );

        // 热增 agents 文件 → 键出现（旧键保留）
        let mut grown = sample_package("mode_x", "card", "main");
        grown.agents.push(ModeAgentEntry {
            key: "mode_x/hot".to_string(),
            plugin_id: "mode_x".to_string(),
            path: PathBuf::from("/pkg/mode_x/agents/hot.yaml"),
        });
        assert!(refresh_mode_package_guarded(&registry, &scopes, grown).unwrap());
        assert!(registry.get_mode_agent("mode_x/hot").is_some());
        assert!(registry.get_mode_agent("mode_x/card").is_some());

        // 热删 pipeline 文件 → 键同源消失（其余键不受牵连）
        let mut shrunk = sample_package("mode_x", "card", "main");
        shrunk.pipelines.clear();
        assert!(refresh_mode_package_guarded(&registry, &scopes, shrunk).unwrap());
        assert!(registry.get_mode_pipeline("mode_x/main").is_none());
        assert!(registry.get_mode_agent("mode_x/card").is_some());
    }

    #[test]
    fn refresh_failure_rolls_back_and_keeps_old_table() {
        let registry = Arc::new(crate::registry::CapabilityRegistryImpl::new());
        let scopes = crate::registry::PluginScopeRegistry::new();
        // mode_z 现注册：agent mode_z/card
        let old = sample_package("mode_z", "card", "main");
        let guard = register_mode_package_guarded(&registry, old).unwrap();
        scopes.scope_of("mode_z").track(guard);

        // 他人占住 mode_z/shared（构造跨包同名冲突面）
        let mut squatter = sample_package("mode_y", "unused", "unused");
        squatter.agents[0].key = "mode_z/shared".to_string();
        squatter.pipelines.clear();
        let g2 = register_mode_package_guarded(&registry, squatter).unwrap();
        scopes.scope_of("mode_y").track(g2);

        // 重扫后 mode_z 盘上多了 shared（与 mode_y 注册键冲突）→ 注册失败须回滚
        let mut conflicting = sample_package("mode_z", "card", "main");
        conflicting.agents.push(ModeAgentEntry {
            key: "mode_z/shared".to_string(),
            plugin_id: "mode_z".to_string(),
            path: PathBuf::from("/pkg/mode_z/agents/shared.yaml"),
        });
        let err = match refresh_mode_package_guarded(&registry, &scopes, conflicting) {
            Err(e) => e,
            Ok(changed) => panic!("键冲突必须报错，got changed={changed}"),
        };
        assert!(err.contains("conflict"), "{err}");
        // 旧键表保留：card 还在、shared 未被 mode_z 抢走
        assert_eq!(
            registry.get_mode_agent("mode_z/card").unwrap().plugin_id,
            "mode_z"
        );
        assert!(registry
            .get_mode_agent("mode_z/shared")
            .is_some_and(|e| e.plugin_id == "mode_y"));
        assert!(registry.get_mode_pipeline("mode_z/main").is_some());
    }
}
