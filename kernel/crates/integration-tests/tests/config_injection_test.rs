//! 配置注入链路验证集成测试
//!
//! 验证配置从 YAML 文件 → JSON → MCP initialize params → 插件进程的完整链路。
//!
//! 测试覆盖 5 个功能点：
//! 1. plugin-loader 能加载 YAML 配置文件
//! 2. McpClient::initialize params 包含 config 字段
//! 3. invoker 在创建 MCP client 时加载并注入配置
//! 4. 无配置时降级为空 {}，不报错
//! 5. 配置热重载通知
//!
//! @feature: FP-0.2.CFG 配置系统与插件配置注入 | @vision: V3 可嵌入 | @ci: rust-test

use std::collections::HashMap;
use std::fs;
use std::sync::Arc;
use std::time::Duration;

use agentos_core::traits::{
    LoadedPlugin, PluginInvoker, PluginLoader, PluginManifest, PluginStatus, PluginType,
};
use agentos_mcp::McpClient;
use async_trait::async_trait;
use parking_lot::RwLock;
use serde_json::{json, Value};

// ── Mock MCP Server（内联 Python 脚本） ──

const MOCK_SERVER_SCRIPT: &str = r#"
import sys, json, os, time

RESULT_FILE = os.environ.get("VERIFY_RESULT_FILE", "/tmp/mcp_verify_result.json")

def write_result(data):
    with open(RESULT_FILE, "w") as f:
        json.dump(data, f, indent=2)

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except:
        continue
    method = req.get("method", "")
    if "id" not in req:
        if method == "notifications/on_config_change":
            write_result({"event": "config_change", "received_config": req.get("params", {}).get("config"), "timestamp": time.time()})
        elif method == "notifications/initialized":
            pass
        continue
    if method == "initialize":
        params = req.get("params", {})
        config = params.get("config", None)
        write_result({"event": "initialize", "received_config": config, "config_is_null": config is None, "timestamp": time.time()})
        resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {"listChanged": True}}, "serverInfo": {"name": "mock-server", "version": "1.0.0"}}}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()
    elif method == "tools/list":
        resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"tools": [{"name": "execute", "inputSchema": {"type": "object"}}]}}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()
    elif method == "tools/call":
        resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"success": True, "data": {"output": "ok"}}}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()
"#;

const SHELL_WRAPPER_TEMPLATE: &str = r#"#!/bin/bash
export VERIFY_RESULT_FILE="{result_file}"
# 跨平台 python 探测：Windows 上 python3 可能是 Store stub（静默失败），
# 优先 python3，不可用则回退 python。
if python3 -c "pass" 2>/dev/null; then PY=python3; else PY=python; fi
exec "$PY" -c '{script}'
"#;

fn create_mock_server_script(result_file: impl AsRef<str>) -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    let script_path = dir.path().join("mock_server.sh");
    let escaped_script = MOCK_SERVER_SCRIPT.replace("'", "'\\''");
    let script_content = SHELL_WRAPPER_TEMPLATE
        .replace("{result_file}", result_file.as_ref())
        .replace("{script}", &escaped_script);
    fs::write(&script_path, script_content).unwrap();

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mut perms = fs::metadata(&script_path).unwrap().permissions();
        perms.set_mode(0o755);
        fs::set_permissions(&script_path, perms).unwrap();
    }

    dir
}

fn read_result_file(path: impl AsRef<str>) -> Value {
    let path = path.as_ref();
    for _ in 0..50 {
        if let Ok(content) = fs::read_to_string(path) {
            if let Ok(val) = serde_json::from_str::<Value>(&content) {
                return val;
            }
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    panic!("Mock server did not write result file: {}", path);
}

fn clear_result_file(path: impl AsRef<str>) {
    let _ = fs::write(path.as_ref(), "");
}

/// 跨平台临时结果文件路径:用 `std::env::temp_dir()` 替代硬编码 `/tmp`。
///
/// 原 7 处 `let result_file = "/tmp/xxx.json"` 在 Windows 下因 `/tmp` 不存在,
/// mock server(python)写结果失败 → `read_result_file` panic,导致 7 个 FP/E2E
/// 用例在本地 Windows 全挂(CI Ubuntu 不受影响)。改用 temp_dir 后 Windows(Git
/// Bash + python)亦可跑通。
fn tmp_result_file(name: &str) -> String {
    std::env::temp_dir()
        .join(name)
        .to_string_lossy()
        .to_string()
}

struct MockLoader {
    manifests: RwLock<HashMap<String, PluginManifest>>,
    config_to_return: RwLock<Value>,
    load_config_call_count: RwLock<u32>,
}

impl MockLoader {
    fn new() -> Self {
        Self {
            manifests: RwLock::new(HashMap::new()),
            config_to_return: RwLock::new(json!({})),
            load_config_call_count: RwLock::new(0),
        }
    }

    fn add_manifest(&self, manifest: PluginManifest) {
        self.manifests.write().insert(manifest.id.clone(), manifest);
    }

    fn set_config(&self, config: Value) {
        *self.config_to_return.write() = config;
    }

    fn get_load_config_count(&self) -> u32 {
        *self.load_config_call_count.read()
    }
}

use agentos_core::types::PluginError;

#[async_trait]
impl PluginLoader for MockLoader {
    async fn discover(&self, _root_paths: &[&str]) -> Result<Vec<PluginManifest>, PluginError> {
        Ok(self.manifests.read().values().cloned().collect())
    }

    fn validate_manifest(&self, _manifest: &PluginManifest) -> Result<(), PluginError> {
        Ok(())
    }

    async fn load(&self, plugin_id: &str) -> Result<LoadedPlugin, PluginError> {
        let manifests = self.manifests.read();
        let manifest = manifests.get(plugin_id).ok_or_else(|| PluginError {
            message: format!("plugin not found: {}", plugin_id),
            code: Some("NOT_FOUND".to_string()),
            source: None,
        })?;
        Ok(LoadedPlugin {
            manifest: manifest.clone(),
            status: PluginStatus::Active,
            loaded_at: Some(chrono::Utc::now()),
        })
    }

    async fn unload(&self, _plugin_id: &str) -> Result<(), PluginError> {
        Ok(())
    }

    fn get_status(&self, _plugin_id: &str) -> PluginStatus {
        PluginStatus::Active
    }

    async fn load_config(&self) -> Result<Value, PluginError> {
        *self.load_config_call_count.write() += 1;
        Ok(self.config_to_return.read().clone())
    }
}

fn make_sidecar_manifest(id: &str, entry: &str) -> PluginManifest {
    PluginManifest {
        id: id.to_string(),
        name: format!("Test {}", id),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::Tool,
        pipeline_role: None,
        language: "python".to_string(),
        host_type: agentos_core::traits::HostType::Sidecar,
        host_group: None,
        entry: entry.to_string(),
        capabilities: Default::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 100,
        mcp: None,
        lifecycle: None,
        native: None,
        granted_capabilities: vec![],
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        persistent_fields: vec![],
        export_fields: vec![],
        provides: None,
    }
}

// ═══ FP1: plugin-loader 能加载 YAML 配置文件 ═══

#[tokio::test]
async fn fp1_load_config_reads_yaml_files() {
    let config_dir = tempfile::tempdir().unwrap();
    fs::write(
        config_dir.path().join("memory_storage.yaml"),
        "storage_backend: sqlite\ncache_size: 1000\n",
    )
    .unwrap();
    fs::write(
        config_dir.path().join("api_config.yaml"),
        "timeout: 30\nhost: localhost\n",
    )
    .unwrap();

    let loader = agentos_plugin_loader::PluginLoaderImpl::new("/tmp/nonexistent", None)
        .with_config_root(config_dir.path());

    let config = loader.load_config().await.unwrap();
    let obj = config.as_object().unwrap();

    assert_eq!(obj.len(), 2, "Should have 2 config entries");
    assert!(obj.contains_key("memory_storage"));
    assert!(obj.contains_key("api_config"));

    let mem = obj.get("memory_storage").unwrap();
    assert_eq!(mem["storage_backend"], "sqlite");
    assert_eq!(mem["cache_size"], 1000);

    let api = obj.get("api_config").unwrap();
    assert_eq!(api["timeout"], 30);
    assert_eq!(api["host"], "localhost");
}

// ═══ FP2: McpClient::initialize params 包含 config 字段 ═══

#[tokio::test]
#[cfg_attr(
    windows,
    ignore = "mock server 依赖 Unix bash+stdio,Windows 本地不可靠;CI(Ubuntu)覆盖"
)]
async fn fp2_initialize_includes_config_in_params() {
    let result_file = tmp_result_file("fp2_verify_result.json");
    clear_result_file(&result_file);

    let _script_dir = create_mock_server_script(&result_file);
    let script_path = _script_dir.path().join("mock_server.sh");

    let mut client = McpClient::new_stdio("bash", vec![script_path.to_string_lossy().to_string()]);
    client.connect().await.unwrap();

    let test_config = json!({
        "memory_storage": {"storage_backend": "sqlite", "cache_size": 1000},
        "api_config": {"timeout": 30}
    });
    let result = client.initialize(&test_config).await;
    assert!(result.is_ok(), "initialize should succeed");

    client.kill().await.unwrap();

    let received = read_result_file(&result_file);
    assert_eq!(received["event"], "initialize");

    let received_config = &received["received_config"];
    assert!(
        received["config_is_null"] == false,
        "config must NOT be null"
    );
    assert!(received_config.is_object(), "config should be JSON object");
    assert!(received_config["memory_storage"]["storage_backend"] == "sqlite");
    assert!(received_config["memory_storage"]["cache_size"] == 1000);
}

#[tokio::test]
#[cfg_attr(
    windows,
    ignore = "mock server 依赖 Unix bash+stdio,Windows 本地不可靠;CI(Ubuntu)覆盖"
)]
async fn fp2_initialize_with_null_config() {
    let result_file = tmp_result_file("fp2b_verify_result.json");
    clear_result_file(&result_file);

    let _script_dir = create_mock_server_script(&result_file);
    let script_path = _script_dir.path().join("mock_server.sh");

    let mut client = McpClient::new_stdio("bash", vec![script_path.to_string_lossy().to_string()]);
    client.connect().await.unwrap();

    let result = client.initialize(&Value::Null).await;
    assert!(result.is_ok(), "initialize with Null should succeed");

    client.kill().await.unwrap();

    let received = read_result_file(&result_file);
    assert_eq!(received["event"], "initialize");
    assert!(received["config_is_null"] == true);
}

// ═══ FP3: invoker 在创建 MCP client 时加载并注入配置 ═══

#[tokio::test]
#[cfg_attr(
    windows,
    ignore = "mock server 依赖 Unix bash+stdio,Windows 本地不可靠;CI(Ubuntu)覆盖"
)]
async fn fp3_invoker_loads_and_injects_config() {
    let result_file = tmp_result_file("fp3_verify_result.json");
    clear_result_file(&result_file);

    let _script_dir = create_mock_server_script(&result_file);
    let script_path = _script_dir.path().join("mock_server.sh");

    let loader = Arc::new(MockLoader::new());
    let test_config = json!({
        "memory_storage": {"storage_backend": "postgres", "cache_size": 500},
        "api_config": {"timeout": 60}
    });
    loader.set_config(test_config);

    let mut manifest = make_sidecar_manifest(
        "test_plugin",
        &format!("bash {}", script_path.to_string_lossy()),
    );
    // P6 注入契约:只注入 manifest.config_files 声明的节(按 id 命名空间)。
    manifest.config_files = vec![
        agentos_core::traits::ConfigFileMapping {
            id: "memory_storage".to_string(),
            path: "memory_storage.yaml".to_string(),
            label: "Memory".to_string(),
            target: None,
            fields: vec![],
        },
        agentos_core::traits::ConfigFileMapping {
            id: "api_config".to_string(),
            path: "api_config.yaml".to_string(),
            label: "API".to_string(),
            target: None,
            fields: vec![],
        },
    ];
    loader.add_manifest(manifest);

    let invoker = agentos_invoker::PluginInvokerImpl::new(loader.clone());

    let result = invoker
        .invoke_tool("test_plugin", "execute", &json!({"input": "test"}))
        .await;

    assert!(
        result.is_ok(),
        "invoke_tool should succeed, got: {:?}",
        result.err()
    );
    assert!(
        loader.get_load_config_count() >= 1,
        "load_config called >= 1"
    );

    let received = read_result_file(&result_file);
    assert_eq!(received["event"], "initialize");

    let received_config = &received["received_config"];
    assert!(received["config_is_null"] == false);
    assert!(received_config["memory_storage"]["storage_backend"] == "postgres");
    assert!(received_config["memory_storage"]["cache_size"] == 500);
    assert!(received_config["api_config"]["timeout"] == 60);
}

#[tokio::test]
#[cfg_attr(
    windows,
    ignore = "mock server 依赖 Unix bash+stdio,Windows 本地不可靠;CI(Ubuntu)覆盖"
)]
async fn fp3_config_change_triggers_reload() {
    let result_file = tmp_result_file("fp3b_verify_result.json");
    let _script_dir = create_mock_server_script(&result_file);
    let script_path = _script_dir.path().join("mock_server.sh");

    let loader = Arc::new(MockLoader::new());
    loader.set_config(json!({"version": "v1"}));

    let mut manifest = make_sidecar_manifest(
        "reload_plugin",
        &format!("bash {}", script_path.to_string_lossy()),
    );
    manifest.config_files = vec![agentos_core::traits::ConfigFileMapping {
        id: "version".to_string(),
        path: "version.yaml".to_string(),
        label: "Version".to_string(),
        target: None,
        fields: vec![],
    }];
    loader.add_manifest(manifest);

    let invoker = agentos_invoker::PluginInvokerImpl::new(loader.clone());

    clear_result_file(&result_file);
    let _ = invoker
        .invoke_tool("reload_plugin", "execute", &json!({}))
        .await;
    let received_v1 = read_result_file(&result_file);
    assert_eq!(received_v1["received_config"]["version"], "v1");

    loader.set_config(json!({"version": "v2"}));
    let count_before = loader.get_load_config_count();

    invoker.force_unload("reload_plugin").await.unwrap();
    clear_result_file(&result_file);
    let _ = invoker
        .invoke_tool("reload_plugin", "execute", &json!({}))
        .await;

    let count_after = loader.get_load_config_count();
    assert!(
        count_after > count_before,
        "load_config called again after reload"
    );

    let received_v2 = read_result_file(&result_file);
    assert_eq!(
        received_v2["received_config"]["version"], "v2",
        "After reload config should be v2"
    );
}

// ═══ FP4: 无配置时降级为空 {}，不报错 ═══

#[tokio::test]
async fn fp4_no_config_root_returns_empty() {
    let loader = agentos_plugin_loader::PluginLoaderImpl::new("/tmp/nonexistent", None);
    let config = loader.load_config().await.unwrap();
    assert_eq!(config, json!({}));
}

#[tokio::test]
async fn fp4_nonexistent_dir_returns_empty() {
    let loader = agentos_plugin_loader::PluginLoaderImpl::new("/tmp/nonexistent", None)
        .with_config_root("/tmp/no_such_dir_99999");
    let config = loader.load_config().await.unwrap();
    assert_eq!(config, json!({}));
}

#[tokio::test]
#[cfg_attr(
    windows,
    ignore = "mock server 依赖 Unix bash+stdio,Windows 本地不可靠;CI(Ubuntu)覆盖"
)]
async fn fp4_invoker_works_with_empty_config() {
    let result_file = tmp_result_file("fp4_verify_result.json");
    clear_result_file(&result_file);

    let _script_dir = create_mock_server_script(&result_file);
    let script_path = _script_dir.path().join("mock_server.sh");

    let loader = Arc::new(MockLoader::new());
    let manifest = make_sidecar_manifest(
        "empty_config_plugin",
        &format!("bash {}", script_path.to_string_lossy()),
    );
    loader.add_manifest(manifest);

    let invoker = agentos_invoker::PluginInvokerImpl::new(loader);

    let result = invoker
        .invoke_tool("empty_config_plugin", "execute", &json!({}))
        .await;
    assert!(
        result.is_ok(),
        "invoke_tool with empty config should succeed"
    );

    let received = read_result_file(&result_file);
    assert_eq!(received["event"], "initialize");
    let config = &received["received_config"];
    assert!(
        config.is_object() && config.as_object().map(|o| o.is_empty()).unwrap_or(false),
        "config should be empty object {{}}, got: {}",
        config
    );
}

// ═══ E2E: YAML → load_config → initialize params ═══

#[tokio::test]
#[cfg_attr(
    windows,
    ignore = "mock server 依赖 Unix bash+stdio,Windows 本地不可靠;CI(Ubuntu)覆盖"
)]
async fn e2e_full_config_injection_chain() {
    let result_file = tmp_result_file("e2e_verify_result.json");
    clear_result_file(&result_file);

    let config_dir = tempfile::tempdir().unwrap();
    fs::write(
        config_dir.path().join("memory_storage.yaml"),
        "storage_backend: redis\ncache_size: 2048\n",
    )
    .unwrap();
    fs::write(
        config_dir.path().join("api_config.yaml"),
        "timeout: 45\nhost: 0.0.0.0\n",
    )
    .unwrap();

    let _script_dir = create_mock_server_script(&result_file);
    let script_path = _script_dir.path().join("mock_server.sh");

    let plugin_dir = tempfile::tempdir().unwrap();
    let plugin_entry_dir = plugin_dir.path().join("e2e_plugin");
    fs::create_dir_all(&plugin_entry_dir).unwrap();
    fs::write(
        plugin_entry_dir.join("plugin.json"),
        format!(
            r#"{{"id":"e2e_plugin","name":"E2E","version":"1.0.0","plugin_type":"tool","language":"python","host_type":"sidecar","entry":"bash {}","capabilities":{{}},"requires_services":[],"permissions":{{}},"priority":100,"config_files":[{{"id":"memory_storage","path":"memory_storage.yaml","label":"Memory"}},{{"id":"api_config","path":"api_config.yaml","label":"API"}}]}}"#,
            script_path.to_string_lossy()
        ),
    ).unwrap();

    let real_loader = Arc::new(
        agentos_plugin_loader::PluginLoaderImpl::new(plugin_dir.path(), None)
            .with_config_root(config_dir.path()),
    );
    eprintln!(
        "[e2e-debug] plugin_dir={:?} exists={} entries={:?}",
        plugin_dir.path(),
        plugin_dir.path().exists(),
        std::fs::read_dir(plugin_dir.path())
            .map(|rd| rd
                .flatten()
                .map(|e| e.path().to_string_lossy().to_string())
                .collect::<Vec<_>>())
            .map_err(|e| e.to_string())
    );
    real_loader.discover(&[]).await.unwrap();

    let real_invoker = agentos_invoker::PluginInvokerImpl::new(real_loader);

    let result = real_invoker
        .invoke_tool("e2e_plugin", "execute", &json!({"input": "e2e_test"}))
        .await;

    assert!(
        result.is_ok(),
        "E2E invoke_tool should succeed, error: {:?}",
        result.err()
    );

    let received = read_result_file(&result_file);
    assert_eq!(received["event"], "initialize");
    let received_config = &received["received_config"];
    assert!(received["config_is_null"] == false);
    assert!(received_config["memory_storage"]["storage_backend"] == "redis");
    assert!(received_config["memory_storage"]["cache_size"] == 2048);
    assert!(received_config["api_config"]["timeout"] == 45);
}
