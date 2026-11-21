// @feature: FP-0.2.一 插件协议·private 机制 | @ci: rust-test
//! 批 2 回归：private 打标 + llm_service 合宿宿主形态退役（独立 sidecar 拉起）。
//!
//! 设计真值：
//! - `docs/working/LLM插件两类收敛_现状盘点与方案_20261118.md` §6.4-e / §7.1 批 2
//! - `docs/decisions/2026-11-18-llm-two-tier-plugin-convergence.md` 决策 5/6
//!
//! 判据（交付物内容指向）：
//! 1) llm_service / pipeline_llm_core manifest 声明 `visibility: private`；
//! 2) llm_service 不再声明 host_group —— invoker 合宿准入（`is_cohost_member`）
//!    判否 → 独占宿主键 `plugin:llm_service`、按自身 entry 拉起，host.py 组形态
//!    退役（host.py 本身通用，无成员路由特判，无需改动）；
//! 3) llm_service 的 `llm.complete_stream` 服务契约全程冻结（声明面不变）；
//! 4) 独立拉起的 venv 供给面切换：不再作为合宿成员被排除，进独立 sidecar 的
//!    venv 重建选集（装机态由内核 autoprovision 建 <USER_ROOT>/plugin-venvs/<id>）；
//! 5) /api/v1/plugins 出口 visibility 字段（前端插件管理页过滤 private 的数据源）。

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use agentos_api::routes::plugins_status_handler;
use agentos_api::routes::AppState;
use agentos_api::venv_provision::select_missing_venv_plugins;
use agentos_core::traits::{HostType, PluginManifest, PluginVisibility};
use axum::extract::State;
use tokio::sync::RwLock;

/// 仓库 plugins/shared 根（CARGO_MANIFEST_DIR = kernel/crates/api）。
fn repo_shared_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../plugins/shared")
}

/// 读取真实插件 manifest（与内核 discover 同一解析路径：serde deny_unknown_fields）。
fn repo_manifest(dir_name: &str) -> PluginManifest {
    let path = repo_shared_root().join(dir_name).join("plugin.json");
    let raw = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("读 {} 失败: {e}", path.display()));
    serde_json::from_str(&raw).unwrap_or_else(|e| panic!("解析 {} 失败: {e}", path.display()))
}

/// 最小合法 manifest JSON（public 默认 / 指定 host_group 的合宿对照）。
fn minimal_manifest_json(id: &str, host_group: Option<&str>) -> String {
    let group = host_group
        .map(|g| format!(r#", "host_group": "{g}""#))
        .unwrap_or_default();
    format!(
        r#"{{
            "id": "{id}",
            "name": "{id}",
            "version": "1.0.0",
            "plugin_type": "system",
            "language": "python",
            "host_type": "sidecar",
            "entry": "python server.py"{group},
            "capabilities": {{}}
        }}"#
    )
}

fn minimal_manifest(id: &str, host_group: Option<&str>) -> PluginManifest {
    serde_json::from_str(&minimal_manifest_json(id, host_group)).expect("最小 manifest 必须可解析")
}

#[test]
fn llm_service_is_private_and_no_longer_cohosted() {
    let manifest = repo_manifest("llm");

    assert_eq!(
        manifest.visibility,
        PluginVisibility::Private,
        "llm_service 必须声明 visibility: private（批 2 打标）"
    );
    assert_eq!(manifest.id, "llm_service", "id 契约冻结");
    assert!(
        manifest.host_group.is_none(),
        "llm_service 合宿宿主形态退役——manifest 不得再声明 host_group，实际 {:?}",
        manifest.host_group
    );
    assert!(
        !agentos_invoker::is_cohost_member(&manifest),
        "invoker 合宿准入必须判否 → 走独占宿主键 plugin:llm_service 独立拉起"
    );
    assert_eq!(manifest.host_type, HostType::Sidecar);
    assert_eq!(
        manifest.entry, "python server.py",
        "独立 sidecar 按自身 entry 拉起（不再经 _host/host.py 组包装）"
    );
    // 非外部 stdio 命令：build_mcp_client 走 build_sidecar_transport_client
    // （合宿分支只在 is_cohost_member 时命中，此处已判否 → 独占 spawn 分支）。
    let external_command = manifest
        .mcp
        .as_ref()
        .and_then(|c| c.endpoint.as_ref())
        .and_then(|e| e.command.as_ref());
    assert!(
        external_command.is_none(),
        "llm_service 不是外部 stdio 命令 MCP：{external_command:?}"
    );
}

#[test]
fn llm_service_service_contract_frozen_after_group_retirement() {
    let manifest = repo_manifest("llm");
    let names: Vec<&str> = manifest
        .capabilities
        .services
        .iter()
        .map(|s| s.name.as_str())
        .collect();
    for expected in [
        "llm.complete_stream",
        "llm.health_check",
        "llm.repair_json",
        "model.resolve",
        "http.handle",
    ] {
        assert!(
            names.contains(&expected),
            "服务契约冻结：缺少 {expected}（实际 {names:?}）"
        );
    }
}

#[test]
fn pipeline_llm_core_is_private_and_keeps_llm_group() {
    let manifest = repo_manifest("llm_core");

    assert_eq!(
        manifest.visibility,
        PluginVisibility::Private,
        "pipeline_llm_core 必须声明 visibility: private（批 2 打标）"
    );
    assert_eq!(
        manifest.host_group.as_deref(),
        Some("llm"),
        "批 2 只退役 llm_service 单成员合宿，llm_core 的 llm 组保持"
    );
    assert!(
        agentos_invoker::is_cohost_member(&manifest),
        "llm_core 仍在合宿组内（装箱判定不变）"
    );
}

#[tokio::test]
async fn plugins_payload_exposes_visibility_field() {
    let private_manifest = repo_manifest("llm");
    let public_manifest = minimal_manifest("public_tool", None);

    let mut state = AppState::new();
    state.manifests = Arc::new(RwLock::new(vec![private_manifest, public_manifest.clone()]));
    state.enabled_plugin_ids = Arc::new(RwLock::new(
        [String::from("llm_service"), public_manifest.id.clone()]
            .into_iter()
            .collect::<HashSet<_>>(),
    ));

    let axum::Json(payload) = plugins_status_handler(State(state)).await;
    let items = payload.as_array().expect("插件列表应为数组");

    let find = |id: &str| {
        items
            .iter()
            .find(|item| item["plugin_id"] == id)
            .unwrap_or_else(|| panic!("{id} 应在插件状态列表"))
    };
    assert_eq!(
        find("llm_service")["visibility"],
        serde_json::json!("private"),
        "private 插件如实出口 visibility=private（前端插件管理页过滤的数据源）"
    );
    assert_eq!(
        find(public_manifest.id.as_str())["visibility"],
        serde_json::json!("public"),
        "缺省可见的插件出口 visibility=public（旧 manifest 宽容读面）"
    );
}

/// 环境变量守卫（AGENTOS_PLUGIN_VENVS_DIR 指向空目录，隔离真实用户空间 venv）。
struct VenvsDirGuard {
    prev: Option<String>,
}

impl VenvsDirGuard {
    fn isolate(dir: &Path) -> Self {
        let prev = std::env::var(agentos_core::user_space::PLUGIN_VENVS_DIR_ENV).ok();
        std::env::set_var(agentos_core::user_space::PLUGIN_VENVS_DIR_ENV, dir);
        Self { prev }
    }
}

impl Drop for VenvsDirGuard {
    fn drop(&mut self) {
        match self.prev.take() {
            Some(v) => std::env::set_var(agentos_core::user_space::PLUGIN_VENVS_DIR_ENV, v),
            None => std::env::remove_var(agentos_core::user_space::PLUGIN_VENVS_DIR_ENV),
        }
    }
}

/// 独立拉起的 venv 供给面切换：批 2 前 llm_service 是合宿成员（select 恒排除，
/// 运行期只认 _host 共享 venv）；去 host_group 后进独立 sidecar venv 重建选集。
#[test]
#[serial_test::serial]
fn group_retirement_moves_llm_service_into_solo_venv_selection() {
    let tmp = tempfile::tempdir().unwrap();
    let _venvs = VenvsDirGuard::isolate(&tmp.path().join("empty-venvs"));

    let plugin_dir = tmp.path().join("llm");
    std::fs::create_dir_all(&plugin_dir).unwrap();
    std::fs::write(plugin_dir.join("pyproject.toml"), "[project]\nname = 'x'\n").unwrap();

    let solo = repo_manifest("llm");
    let mut grouped = solo.clone();
    grouped.id = "llm_service_grouped".to_string();
    grouped.host_group = Some("light".to_string());
    let grouped_dir = tmp.path().join("grouped");
    std::fs::create_dir_all(&grouped_dir).unwrap();
    std::fs::write(
        grouped_dir.join("pyproject.toml"),
        "[project]\nname = 'y'\n",
    )
    .unwrap();

    let manifests = vec![solo, grouped];
    let dirs: HashMap<String, PathBuf> = [
        ("llm_service".to_string(), plugin_dir),
        ("llm_service_grouped".to_string(), grouped_dir),
    ]
    .into_iter()
    .collect();

    let selected = select_missing_venv_plugins(&manifests, &dirs);
    let ids: Vec<&str> = selected.iter().map(|(id, _)| id.as_str()).collect();
    assert!(
        ids.contains(&"llm_service"),
        "去 host_group 后 llm_service 必须进独立 venv 重建选集（实际 {ids:?}）"
    );
    assert!(
        !ids.contains(&"llm_service_grouped"),
        "对照：仍声明 host_group 的插件照旧被排除（`_host` 共享 venv 供给）"
    );
}
