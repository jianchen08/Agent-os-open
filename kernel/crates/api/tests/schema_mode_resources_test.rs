//! schema 模式资源键表契约测试（ADR 2026-10-15-mode-registry-retirement）。
//!
//! `/api/v1/schema` 的 `mode_agents`/`mode_pipelines` 是冻结契约（键形
//! `mode_X/<stem>`，字段 key/plugin_id/path/task_kinds，消费方 = 前端 +
//! agent_manager `fetch_mode_agents`）；取数 = 按需聚合
//! （[`agentos_api::mode_view`]，活 manifests + 目录映射 + 盘上现状现算，
//! 零注册状态）。本文件锁：条目形状/按键字典序/序列化稳定性（ETag 前提）/
//! 启用与前缀过滤/热增文件即时生效。

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;

use agentos_api::routes::{schema_handler, AppState};
use agentos_core::traits::PluginManifest;
use axum::http::StatusCode;
use serde_json::{json, Value};

/// 最小合法模式包 manifest（system sidecar 形态）。
fn mode_manifest(id: &str) -> PluginManifest {
    serde_json::from_value(json!({
        "id": id, "name": id, "version": "1.0.0",
        "plugin_type": "system", "language": "python",
        "host_type": "sidecar", "entry": "python server.py",
        "capabilities": {}
    }))
    .expect("valid manifest")
}

/// 模式包磁盘 fixture（按需聚合的取数真源：约定子目录 + 约定文件）。
fn write_mode_package(
    root: &std::path::Path,
    mode_id: &str,
    agents: &[(&str, &str)],
    pipelines: &[(&str, &str)],
) -> PathBuf {
    let dir = root.join(mode_id);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(
        dir.join("plugin.json"),
        format!(
            r#"{{"id":"{mode_id}","name":"{mode_id}","version":"1.0.0",
                "plugin_type":"system","language":"python","host_type":"sidecar",
                "entry":"python server.py","capabilities":{{}}}}"#
        ),
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

/// 模式包 schema 集成态：manifest 在册 + 启用 + 目录映射指向磁盘 fixture。
async fn mode_schema_state(tmp: &tempfile::TempDir, mode_id: &str, enabled: bool) -> AppState {
    let mode_dir = write_mode_package(
        tmp.path(),
        mode_id,
        &[
            ("novelist.yaml", "model_tier: large\n"),
            ("editor.yaml", "model_tier: medium\n"),
        ],
        &[("chapter.yaml", "task_kinds: [writing.chapter]\nsteps: []\n")],
    );
    let mut dirs: HashMap<String, PathBuf> = HashMap::new();
    dirs.insert(mode_id.to_string(), mode_dir.clone());
    let _ = dirs; // 目录映射由 loader 活真源承载（B1），保留变量供调试断言
    let mut state = AppState {
        manifests: Arc::new(tokio::sync::RwLock::new(vec![mode_manifest(mode_id)])),
        plugin_loader: Some(agentos_api::test_env::discovered_plugin_loader(tmp.path()).await),
        ..AppState::new()
    };
    if enabled {
        state.enabled_plugin_ids =
            Arc::new(tokio::sync::RwLock::new(std::collections::HashSet::from([
                mode_id.to_string(),
            ])));
    }
    state
}

async fn schema_body(state: &AppState) -> Value {
    let resp = schema_handler(
        axum::extract::State(state.clone()),
        axum::http::HeaderMap::new(),
    )
    .await;
    assert_eq!(resp.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20)
        .await
        .unwrap();
    serde_json::from_slice(&bytes).unwrap()
}

#[tokio::test]
async fn schema_mode_resources_from_live_manifests_with_stable_serialization() {
    let tmp = tempfile::tempdir().unwrap();
    let state = mode_schema_state(&tmp, "mode_writing", true).await;
    let body = schema_body(&state).await;

    // agent 条目：键/来源插件/定义路径透出，按键字典序
    let agents = body["mode_agents"].as_array().unwrap();
    assert_eq!(agents.len(), 2);
    let keys: Vec<_> = agents.iter().map(|a| a["key"].as_str().unwrap()).collect();
    assert_eq!(keys, vec!["mode_writing/editor", "mode_writing/novelist"]);
    assert_eq!(agents[1]["plugin_id"], "mode_writing");
    assert!(std::path::Path::new(agents[1]["path"].as_str().unwrap())
        .ends_with(std::path::Path::new("agents").join("novelist.yaml")));

    // 编排条目：额外透出 task_kinds
    let pipelines = body["mode_pipelines"].as_array().unwrap();
    assert_eq!(pipelines.len(), 1);
    assert_eq!(pipelines[0]["key"], "mode_writing/chapter");
    assert_eq!(pipelines[0]["plugin_id"], "mode_writing");
    assert!(std::path::Path::new(pipelines[0]["path"].as_str().unwrap())
        .ends_with(std::path::Path::new("pipelines").join("chapter.yaml")));
    assert_eq!(pipelines[0]["task_kinds"], json!(["writing.chapter"]));

    // additive 结构序列化稳定：条目字段集合固定（不随聚合内容增删漂移），
    // 且两次聚合产出字节一致（schema ETag 内容寻址的前提）。
    for a in agents {
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
    assert_eq!(
        names,
        vec!["key", "path", "plugin_id", "task_kinds"],
        "{}",
        pipelines[0]
    );
    let first = serde_json::to_vec(&schema_body(&state).await).unwrap();
    let second = serde_json::to_vec(&schema_body(&state).await).unwrap();
    assert_eq!(first, second, "同内容聚合必须产出相同字节");

    // 热增约定文件 → 下次聚合键即时出现（按需派生，无需注册/重启/刷新）。
    std::fs::write(
        tmp.path().join("mode_writing/agents").join("hot.yaml"),
        "model_tier: small\n",
    )
    .unwrap();
    let body = schema_body(&state).await;
    let keys: Vec<_> = body["mode_agents"]
        .as_array()
        .unwrap()
        .iter()
        .map(|a| a["key"].as_str().unwrap())
        .collect();
    assert_eq!(
        keys,
        vec![
            "mode_writing/editor",
            "mode_writing/hot",
            "mode_writing/novelist"
        ],
        "热增文件无需重启即入键表"
    );
}

#[tokio::test]
async fn schema_mode_resources_filtered_by_enabled_and_prefix() {
    let tmp = tempfile::tempdir().unwrap();

    // disabled 模式包（manifest 在册、未启用）→ 不聚合（禁用即同源消失）
    let state = mode_schema_state(&tmp, "mode_disabled", false).await;
    let body = schema_body(&state).await;
    assert!(
        body["mode_agents"].as_array().unwrap().is_empty(),
        "禁用包不聚合（空数组非 null，消费方按数组取数）"
    );
    assert!(body["mode_pipelines"].as_array().unwrap().is_empty());

    // manifests 为空（裸 AppState 测试态）→ 同样空集合
    let body = schema_body(&AppState::new()).await;
    assert!(body["mode_agents"].as_array().unwrap().is_empty());
    assert!(body["mode_pipelines"].as_array().unwrap().is_empty());

    // 非 mode_ 前缀插件带 agents/ 约定子目录 → 不聚合（mode_ 前缀承担识别）
    write_mode_package(tmp.path(), "tools_helper", &[("x.yaml", "x: 1\n")], &[]);
    let state = AppState {
        manifests: Arc::new(tokio::sync::RwLock::new(vec![mode_manifest(
            "tools_helper",
        )])),
        plugin_loader: Some(agentos_api::test_env::discovered_plugin_loader(tmp.path()).await),
        enabled_plugin_ids: Arc::new(tokio::sync::RwLock::new(std::collections::HashSet::from([
            "tools_helper".to_string(),
        ]))),
        ..AppState::new()
    };
    let body = schema_body(&state).await;
    assert!(
        body["mode_agents"].as_array().unwrap().is_empty(),
        "非 mode_ 前缀目录不聚合"
    );
}
