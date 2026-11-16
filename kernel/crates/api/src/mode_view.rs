//! 模式包约定资源视图——消费时按需聚合（ADR 2026-10-15-mode-registry-retirement）。
//!
//! 模式包（id 带 [`agentos_core::user_space::MODE_ID_PREFIX`] 前缀且启用的插件）
//! 的约定子目录 `agents/*.yaml`、`pipelines/*.yaml` 在 schema 装配时**现算**：
//! 遍历活 manifests + loader 活路径表解析包目录，逐包扫描约定目录产出键表。
//! 零注册状态、零同步机器（原 boot 注册 / watcher 增量重刷 / parity 特例随
//! mode_registry 注册状态机一并退役）。
//!
//! 键唯一性由结构保证：键 = `<plugin_id>/<stem>`，plugin_id 全局唯一（G2 注册
//! 闸保证 manifests 无重复 id，双根同 id 由 loader 裁决只留一份），同名冲突被
//! 键形结构排除，无需冲突执法。
//!
//! fail-closed 语义收敛（ADR 如实记录）：坏 yaml（不可解析 / 顶层非映射 /
//! task_kinds 非法）不再拒绝启动——按需聚合无启动时点，坏文件**跳过 + warn**
//! （schema 是全前端契约聚合面，单个模式包的坏文件不放大故障半径；声明面
//! 校验仍由 CI 车道 mode.yaml 对账闸拦截）。
//!
//! 先例：`agentos_engine::compiler::build_step_service_index`（消费时现算）。

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use agentos_core::traits::PluginManifest;
use tracing::warn;

/// 约定子目录名：模式私有 agent。
const AGENTS_DIR: &str = "agents";
/// 约定子目录名：模式内编排。
const PIPELINES_DIR: &str = "pipelines";
/// 约定文件扩展名（契约即 `*.yaml`，不含 `.yml`）。
const CONVENTION_EXT: &str = ".yaml";
/// 管道文件头字段：编排参与解析链②路由的任务型标注（列表值）。
const TASK_KINDS_FIELD: &str = "task_kinds";

/// 模式资源视图聚合：从活 manifests + 插件目录活真源现算键表。
///
/// 只聚合 id 带 [`MODE_ID_PREFIX`] 前缀、在 `enabled_ids` 内、且 `plugin_dirs`
/// 有目录映射的包；agent 条目 = `{key, plugin_id, path}`，编排条目额外带
/// `task_kinds`（文件头路由标注，深语义由消费侧解释），路径统一字符串化，
/// 全表按键字典序（HashMap 直序列化会让 ETag 内容寻址误变）。
/// 坏文件跳过 + warn（见模块注释），不产生部分形态条目。
pub fn aggregate_mode_resources(
    manifests: &[PluginManifest],
    enabled_ids: &HashSet<String>,
    plugin_dirs: &HashMap<String, PathBuf>,
) -> (Vec<serde_json::Value>, Vec<serde_json::Value>) {
    let mut agents: Vec<(String, serde_json::Value)> = Vec::new();
    let mut pipelines: Vec<(String, serde_json::Value)> = Vec::new();
    for manifest in manifests {
        let plugin_id = &manifest.id;
        if !plugin_id.starts_with(agentos_core::user_space::MODE_ID_PREFIX)
            || !enabled_ids.contains(plugin_id)
        {
            continue;
        }
        let Some(dir) = plugin_dirs.get(plugin_id) else {
            continue;
        };
        scan_convention_dir(&dir.join(AGENTS_DIR), plugin_id, false, &mut agents);
        scan_convention_dir(&dir.join(PIPELINES_DIR), plugin_id, true, &mut pipelines);
    }
    agents.sort_by(|a, b| a.0.cmp(&b.0));
    pipelines.sort_by(|a, b| a.0.cmp(&b.0));
    (
        agents.into_iter().map(|(_, v)| v).collect(),
        pipelines.into_iter().map(|(_, v)| v).collect(),
    )
}

/// 扫描单个约定目录（目录缺席 = 零条目不报错），产出 schema 条目。
fn scan_convention_dir(
    convention_dir: &Path,
    plugin_id: &str,
    is_pipeline: bool,
    out: &mut Vec<(String, serde_json::Value)>,
) {
    let Ok(entries) = std::fs::read_dir(convention_dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let Some(name) = entry.file_name().to_str().map(str::to_string) else {
            continue;
        };
        if name.starts_with('.') || !name.ends_with(CONVENTION_EXT) || !path.is_file() {
            continue;
        }
        let Some(stem) = name.strip_suffix(CONVENTION_EXT).map(str::to_string) else {
            continue;
        };
        let key = format!("{plugin_id}/{stem}");
        let value = match read_yaml_mapping(&path) {
            Ok(v) => v,
            Err(reason) => {
                warn!(
                    target: "mode_view",
                    plugin = %plugin_id,
                    path = %path.display(),
                    reason = %reason,
                    "模式包约定文件非法，条目跳过（声明面由 CI mode.yaml 对账闸拦截）"
                );
                continue;
            }
        };
        if is_pipeline {
            let task_kinds = match extract_task_kinds(&path, &value) {
                Ok(k) => k,
                Err(reason) => {
                    warn!(
                        target: "mode_view",
                        plugin = %plugin_id,
                        path = %path.display(),
                        reason = %reason,
                        "模式包编排文件头非法，条目跳过"
                    );
                    continue;
                }
            };
            out.push((
                key.clone(),
                serde_json::json!({
                    "key": key,
                    "plugin_id": plugin_id,
                    "path": path.display().to_string(),
                    "task_kinds": task_kinds,
                }),
            ));
        } else {
            out.push((
                key.clone(),
                serde_json::json!({
                    "key": key,
                    "plugin_id": plugin_id,
                    "path": path.display().to_string(),
                }),
            ));
        }
    }
}

/// 读单个约定文件：可解析 + 顶层映射（原扫描层最小校验同语义）。
fn read_yaml_mapping(path: &Path) -> Result<serde_yaml::Value, String> {
    let text = std::fs::read_to_string(path).map_err(|e| e.to_string())?;
    let value: serde_yaml::Value = serde_yaml::from_str(&text).map_err(|e| e.to_string())?;
    if !value.is_mapping() {
        return Err("顶层必须是映射（对象）".to_string());
    }
    Ok(value)
}

/// 文件头 `task_kinds` 结构最小校验：缺省/空 → 空表；列表 → 逐项必须字符串；
/// 其他形态 → Err（调用方跳过条目）。深语义（对齐 G10 编译器）留消费侧。
fn extract_task_kinds(path: &Path, value: &serde_yaml::Value) -> Result<Vec<String>, String> {
    match value.get(TASK_KINDS_FIELD) {
        None | Some(serde_yaml::Value::Null) => Ok(Vec::new()),
        Some(serde_yaml::Value::Sequence(items)) => items
            .iter()
            .map(|item| {
                item.as_str()
                    .map(str::to_string)
                    .ok_or_else(|| "task_kinds 条目必须是字符串".to_string())
            })
            .collect(),
        Some(_) => Err(format!("{TASK_KINDS_FIELD} 必须是字符串列表（{path:?}）")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    /// 写一个模式包 fixture：plugin.json（仅作布局真实感）+ 约定子目录内容。
    fn write_package(
        root: impl AsRef<Path>,
        mode_id: &str,
        agents: &[(&str, &str)],
        pipelines: &[(&str, &str)],
    ) -> PathBuf {
        let dir = root.as_ref().join(mode_id);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("plugin.json"),
            format!(r#"{{"id":"{mode_id}","version":"1.0.0"}}"#),
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

    /// 最小合法 manifest（必填字段取生产缺省形态，其余空缺）。
    fn manifest(id: &str) -> PluginManifest {
        PluginManifest {
            id: id.to_string(),
            name: id.to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: agentos_core::traits::PluginType::System,
            language: "python".to_string(),
            host_type: agentos_core::traits::HostType::Sidecar,
            host_group: None,
            entry: "python server.py".to_string(),
            capabilities: agentos_core::traits::ManifestCapabilities::default(),
            requires_services: vec![],
            permissions: Default::default(),
            priority: 100,
            persistent_fields: vec![],
            state: None,
            force_include_tools: vec![],
            export_fields: vec![],
            mcp: None,
            lifecycle: None,
            native: None,
            restricted_capabilities: vec![],
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            aux_venvs: vec![],
            ui_schema: None,
            contributes: None,
            http_endpoints: vec![],
            provides: None,
            enabled: None,
            activation: None,
        }
    }

    fn ids(items: &[&str]) -> HashSet<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    fn aggregate<'a>(
        manifests: impl IntoIterator<Item = &'a str>,
        enabled: &[&str],
        dirs: &HashMap<String, PathBuf>,
    ) -> (Vec<serde_json::Value>, Vec<serde_json::Value>) {
        let manifests: Vec<PluginManifest> = manifests.into_iter().map(manifest).collect();
        aggregate_mode_resources(&manifests, &ids(enabled), dirs)
    }

    #[test]
    fn aggregates_agent_and_pipeline_entries_in_contract_shape() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = write_package(
            tmp.path(),
            "mode_writing",
            &[
                ("novelist.yaml", "model_tier: large\n"),
                ("editor.yaml", "model_tier: medium\n"),
            ],
            &[(
                "chapter.yaml",
                "task_kinds:\n  - writing.chapter\n  - writing.outline\nsteps: []\n",
            )],
        );
        let mut dirs = HashMap::new();
        dirs.insert("mode_writing".to_string(), dir);

        let (agents, pipelines) = aggregate(["mode_writing"], &["mode_writing"], &dirs);

        // agent 条目：键/来源插件/定义路径透出，按键字典序
        assert_eq!(agents.len(), 2);
        let keys: Vec<_> = agents.iter().map(|a| a["key"].as_str().unwrap()).collect();
        assert_eq!(keys, vec!["mode_writing/editor", "mode_writing/novelist"]);
        assert_eq!(agents[1]["plugin_id"], "mode_writing");
        assert!(Path::new(agents[1]["path"].as_str().unwrap())
            .ends_with(Path::new("agents").join("novelist.yaml")));

        // 编排条目：额外透出 task_kinds
        assert_eq!(pipelines.len(), 1);
        assert_eq!(pipelines[0]["key"], "mode_writing/chapter");
        assert_eq!(
            pipelines[0]["task_kinds"],
            serde_json::json!(["writing.chapter", "writing.outline"])
        );

        // additive 结构序列化稳定：条目字段集合固定（schema ETag 内容寻址前提）
        for a in &agents {
            let mut names: Vec<_> = a.as_object().unwrap().keys().map(String::as_str).collect();
            names.sort_unstable();
            assert_eq!(names, vec!["key", "path", "plugin_id"], "{a}");
        }
        let mut names: Vec<_> = pipelines[0]
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        names.sort_unstable();
        assert_eq!(names, vec!["key", "path", "plugin_id", "task_kinds"]);
    }

    /// 退役机制的核心行为：键表 = 盘上现状的按需派生——热增/热删约定文件
    /// 无需任何注册/刷新机器，下次聚合即时同源（原 watcher 重扫特例的行为
    /// 由数据流天然获得）。
    #[test]
    fn hot_add_and_delete_reflect_on_next_aggregation_without_any_registry() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = write_package(
            tmp.path(),
            "mode_x",
            &[("base.yaml", "x: 1\n")],
            &[("main.yaml", "steps: []\n")],
        );
        let mut dirs = HashMap::new();
        dirs.insert("mode_x".to_string(), dir);
        let aggregate = || {
            let (agents, pipelines) = aggregate(["mode_x"], &["mode_x"], &dirs);
            let keys = |vs: &[serde_json::Value]| {
                vs.iter()
                    .map(|v| v["key"].as_str().unwrap().to_string())
                    .collect::<Vec<_>>()
            };
            (keys(&agents), keys(&pipelines))
        };

        let (agents, pipelines) = aggregate();
        assert_eq!(agents, vec!["mode_x/base"]);
        assert_eq!(pipelines, vec!["mode_x/main"]);

        // 热增 agents 文件 → 键出现（既有键不受牵连）
        std::fs::write(tmp.path().join("mode_x/agents").join("hot.yaml"), "x: 2\n").unwrap();
        let (agents, _) = aggregate();
        assert_eq!(agents, vec!["mode_x/base", "mode_x/hot"]);

        // 热删 pipeline 文件 → 键同源消失
        std::fs::remove_file(tmp.path().join("mode_x/pipelines/main.yaml")).unwrap();
        let (_, pipelines) = aggregate();
        assert!(pipelines.is_empty(), "热删文件后键必须消失: {pipelines:?}");
    }

    #[test]
    fn filters_by_mode_prefix_enabled_and_dir_mapping() {
        let tmp = tempfile::tempdir().unwrap();
        let mode_dir = write_package(tmp.path(), "mode_ok", &[("a.yaml", "x: 1\n")], &[]);
        // 非 mode_ 前缀：带 agents/ 约定子目录也不聚合（mode_ 前缀承担识别）
        let notmode = write_package(tmp.path(), "tools_helper", &[("a.yaml", "x: 1\n")], &[]);
        let mut dirs = HashMap::new();
        dirs.insert("mode_ok".to_string(), mode_dir);
        dirs.insert("tools_helper".to_string(), notmode);

        // 非前缀不聚合
        let (agents, _) = aggregate(
            ["mode_ok", "tools_helper"],
            &["mode_ok", "tools_helper"],
            &dirs,
        );
        assert_eq!(agents.len(), 1, "非 mode_ 前缀目录不聚合");
        assert_eq!(agents[0]["key"], "mode_ok/a");

        // disabled 不聚合
        let (agents, _) = aggregate(["mode_ok"], &[], &dirs);
        assert!(agents.is_empty(), "禁用包不聚合");

        // 无目录映射（loader 路径表缺席）不聚合
        let (agents, _) = aggregate(["mode_ok"], &["mode_ok"], &HashMap::new());
        assert!(agents.is_empty(), "目录映射缺席不聚合");

        // 多包聚合按键字典序
        let other = write_package(tmp.path(), "mode_a", &[("z.yaml", "x: 1\n")], &[]);
        dirs.insert("mode_a".to_string(), other);
        let (agents, _) = aggregate(["mode_ok", "mode_a"], &["mode_ok", "mode_a"], &dirs);
        let keys: Vec<_> = agents.iter().map(|a| a["key"].as_str().unwrap()).collect();
        assert_eq!(keys, vec!["mode_a/z", "mode_ok/a"], "跨包按键字典序");
    }

    /// 坏文件跳过 + 其余条目存活（不整轮失败——schema 聚合面故障隔离）。
    #[test]
    fn invalid_files_are_skipped_without_poisoning_other_entries() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = write_package(tmp.path(), "mode_x", &[("good.yaml", "x: 1\n")], &[]);
        std::fs::create_dir_all(dir.join("agents")).unwrap();
        // ① yaml 语法坏 ② 可解析但顶层非映射 ③ 空文件（Null 非映射）
        std::fs::write(dir.join("agents/bad_syntax.yaml"), "a: [broken\n  ::::").unwrap();
        std::fs::write(dir.join("agents/bad_shape.yaml"), "- just\n- a list\n").unwrap();
        std::fs::write(dir.join("agents/empty.yaml"), "").unwrap();
        // ④ 编排头 task_kinds 非法形态
        std::fs::create_dir_all(dir.join("pipelines")).unwrap();
        std::fs::write(
            dir.join("pipelines/bad_kinds.yaml"),
            "task_kinds: writing\n",
        )
        .unwrap();
        std::fs::write(dir.join("pipelines/good.yaml"), "task_kinds: [a.b]\n").unwrap();

        let mut dirs = HashMap::new();
        dirs.insert("mode_x".to_string(), dir.clone());
        let (agents, pipelines) = aggregate(["mode_x"], &["mode_x"], &dirs);

        let keys: Vec<_> = agents.iter().map(|a| a["key"].as_str().unwrap()).collect();
        assert_eq!(
            keys,
            vec!["mode_x/good"],
            "坏文件跳过，好条目存活: {keys:?}"
        );
        let pk: Vec<_> = pipelines
            .iter()
            .map(|p| p["key"].as_str().unwrap())
            .collect();
        assert_eq!(pk, vec!["mode_x/good"], "task_kinds 非法的编排条目跳过");

        // task_kinds 合法形态三分支：缺省 / 显式空 / 字符串列表
        std::fs::write(dir.join("pipelines/bad_kinds.yaml"), "steps: []\n").unwrap();
        std::fs::write(dir.join("pipelines/empty_kinds.yaml"), "task_kinds:\n").unwrap();
        let (_, pipelines) = aggregate(["mode_x"], &["mode_x"], &dirs);
        let kinds = |stem: &str| {
            pipelines
                .iter()
                .find(|p| p["key"] == format!("mode_x/{stem}"))
                .unwrap()["task_kinds"]
                .clone()
        };
        assert_eq!(kinds("good"), serde_json::json!(["a.b"]));
        assert_eq!(kinds("bad_kinds"), serde_json::json!([]), "缺省 = 空表");
        assert_eq!(kinds("empty_kinds"), serde_json::json!([]), "显式空 = 空表");
    }

    #[test]
    fn hidden_files_and_non_yaml_ignored_and_missing_dirs_yield_zero() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = write_package(tmp.path(), "mode_x", &[], &[]);
        std::fs::create_dir_all(dir.join("agents")).unwrap();
        std::fs::write(dir.join("agents/.hidden.yaml"), "x: 1\n").unwrap();
        std::fs::write(dir.join("agents/README.md"), "# doc\n").unwrap();
        // 契约即 *.yaml：.yml 不在约定面（静默忽略，不报错）
        std::fs::write(dir.join("agents/short.yml"), "x: 1\n").unwrap();

        let mut dirs = HashMap::new();
        dirs.insert("mode_x".to_string(), dir);
        let (agents, pipelines) = aggregate(["mode_x"], &["mode_x"], &dirs);
        assert!(agents.is_empty(), "隐藏/非 yaml/非约定扩展名一律忽略");
        assert!(pipelines.is_empty(), "约定目录缺席 = 零条目");
    }

    /// 双源同 id 用户赢：聚合按 loader 裁决后的目录映射取包目录——映射指向
    /// 用户副本目录时产出的是用户副本的约定资源（目录裁决在 loader，本层
    /// 只消费映射，零重复裁决）。
    #[test]
    fn follows_dir_mapping_adjudication_for_dual_source() {
        let tmp = tempfile::tempdir().unwrap();
        let factory = write_package(tmp.path().join("factory"), "mode_x", &[], &[]);
        std::fs::create_dir_all(factory.join("agents")).unwrap();
        std::fs::write(factory.join("agents/f.yaml"), "from: factory\n").unwrap();
        let user = write_package(tmp.path().join("user"), "mode_x", &[], &[]);
        std::fs::create_dir_all(user.join("agents")).unwrap();
        std::fs::write(user.join("agents/u.yaml"), "from: user\n").unwrap();

        // loader 双源裁决 = 用户赢：映射指向用户副本目录
        let mut dirs = HashMap::new();
        dirs.insert("mode_x".to_string(), user.clone());
        let (agents, _) = aggregate(["mode_x"], &["mode_x"], &dirs);
        let keys: Vec<_> = agents.iter().map(|a| a["key"].as_str().unwrap()).collect();
        assert_eq!(keys, vec!["mode_x/u"], "聚合产出用户副本的约定资源");
        assert!(Path::new(agents[0]["path"].as_str().unwrap()).starts_with(&user));
    }

    /// Arc 共享形态（routes.rs 经 Arc<PluginManifest> 切片聚合的生产同款）。
    #[test]
    fn aggregates_from_manifest_slices_shared_via_arc() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = write_package(tmp.path(), "mode_x", &[("a.yaml", "x: 1\n")], &[]);
        let manifests = Arc::new(vec![manifest("mode_x")]);
        let mut dirs = HashMap::new();
        dirs.insert("mode_x".to_string(), dir);
        let (agents, _) = aggregate_mode_resources(&manifests, &ids(&["mode_x"]), &dirs);
        assert_eq!(agents.len(), 1);
    }
}
