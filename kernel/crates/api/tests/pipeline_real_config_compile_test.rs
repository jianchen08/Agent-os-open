// @feature: FP-0.2.CFG 管道配置 | @vision: V6 可即用 | @ci: rust-test
//! 真实仓库配置编译回归（③级接线 ADR 2026-10-08-step-service-level3-wiring）。
//!
//! 走真仓库 `config/pipelines/*.yaml` × `plugins/shared/**/plugin.json` 全量
//! manifests：编译失败即红——管道引用步骤服务名（如 todo.fold/todo.extract）
//! 而提供插件缺失/manifest 错误/索引重名时，此处最先暴露，不必等内核启动。

use std::path::PathBuf;

use agentos_api::server::load_and_compile_by_name;
use agentos_api::test_env::pin_user_root;
use agentos_core::traits::PluginManifest;

/// 仓根（CARGO_MANIFEST_DIR = kernel/crates/api）。
fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("..")
}

/// 钉桩空临时用户空间。用户空间解析用户层优先（ADR 2026-09-13-unified-user-root）：
/// 不钉桩会读到宿主机装机版旧种子副本（如 %APPDATA%\agentos\config\pipelines），
/// 车道结果随机器漂移；钉空根使解析回落 factory——本车道语义=仓内 factory
/// 配置 × 全量 manifests，必须确定性。
fn pin_factory_user_space() -> (tempfile::TempDir, agentos_api::test_env::UserSpaceGuard) {
    let tmp = tempfile::TempDir::new().expect("临时用户空间");
    let guard = pin_user_root(tmp.path());
    (tmp, guard)
}

/// 收集 plugins/shared 下全部 plugin.json 为 PluginManifest（跳过 .venv/
/// node_modules/__pycache__/.ai_workspaces 等非插件目录——与生产 watcher
/// 发iegel/discovery 的豁免语义一致）。
fn collect_shared_manifests() -> Vec<PluginManifest> {
    let root = repo_root().join("plugins").join("shared");
    let mut manifests = Vec::new();
    let mut stack = vec![root.clone()];
    while let Some(dir) = stack.pop() {
        let entries = match std::fs::read_dir(&dir) {
            Ok(entries) => entries,
            Err(_) => continue,
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                let name = entry.file_name().to_string_lossy().to_string();
                let skip = name.starts_with('.')
                    || name == "node_modules"
                    || name == "__pycache__"
                    || name == ".ai_workspaces";
                if !skip {
                    stack.push(path);
                }
            } else if entry.file_name() == "plugin.json" {
                let raw = std::fs::read_to_string(&path)
                    .unwrap_or_else(|e| panic!("读取 {path:?} 失败: {e}"));
                let manifest: PluginManifest = serde_json::from_str(&raw)
                    .unwrap_or_else(|e| panic!("解析 {path:?} 失败: {e}"));
                manifests.push(manifest);
            }
        }
    }
    manifests
}

#[test]
fn real_config_autonomous_compiles_with_shared_manifests() {
    let (_us_root, _us) = pin_factory_user_space();
    let root = repo_root();
    let config_root = root.join("config");
    let manifests = collect_shared_manifests();
    assert!(
        manifests.iter().any(|m| m.id == "pipeline_todo_track"),
        "todo_track 插件应被收录（③级 todo.fold/todo.extract 的提供方）"
    );
    let compiled = load_and_compile_by_name(&config_root, "autonomous", &manifests)
        .expect("autonomous.yaml 应以全量 manifests 编译通过（含 todo.fold/todo.extract ③级命中）");
    assert_eq!(compiled.name, "autonomous");
}

#[test]
fn real_config_roleplay_compiles_with_shared_manifests() {
    let (_us_root, _us) = pin_factory_user_space();
    let root = repo_root();
    let config_root = root.join("config");
    let manifests = collect_shared_manifests();
    let compiled = load_and_compile_by_name(&config_root, "roleplay", &manifests)
        .expect("roleplay.yaml 应以全量 manifests 编译通过");
    assert_eq!(compiled.name, "roleplay");
}
