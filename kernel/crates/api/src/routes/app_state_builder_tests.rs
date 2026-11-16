//! AppState 注入面 builder 的装配契约：注入的字段真的落到 state 上，
//! 且未被注入的字段保持 [`AppState::new`] 缺省（`..Self::new()` 收敛
//! 语义）；schema 聚合对 kernel_capability_contracts 透传。

use super::*;
use agentos_core::traits::HostType;

fn manifest(id: &str, plugin_type: PluginType) -> PluginManifest {
    PluginManifest {
        force_include_tools: Vec::new(),
        state: None,
        id: id.to_string(),
        name: format!("{id} 名"),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type,
        language: "python".to_string(),
        host_type: HostType::Sidecar,
        host_group: None,
        entry: "python server.py".to_string(),
        capabilities: Default::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 100,
        mcp: None,
        lifecycle: None,
        native: None,
        restricted_capabilities: vec![],
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
        aux_venvs: Vec::new(),
        provides: None,
    }
}

/// 测试用 invoker：本模块只验证装配，不应真正被调用。
struct NeverInvoker;
#[async_trait::async_trait]
impl PluginInvoker for NeverInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        _plugin_id: &str,
        _ctx: &agentos_core::types::PluginContext<'a>,
    ) -> Result<agentos_core::types::PluginResult, agentos_core::types::PluginError> {
        unimplemented!("装配契约测试不触达调用")
    }
    async fn invoke_tool(
        &self,
        _plugin_id: &str,
        _tool_name: &str,
        _inputs: &serde_json::Value,
    ) -> Result<agentos_core::types::ToolExecutionResult, agentos_core::types::PluginError> {
        unimplemented!("装配契约测试不触达调用")
    }
    async fn send_lifecycle_hook(
        &self,
        _plugin_id: &str,
        _hook: agentos_core::traits::LifecycleHook,
        _ctx: &agentos_core::traits::HookContext,
    ) -> Result<(), agentos_core::types::PluginError> {
        Ok(())
    }
}

/// with_plugins：注入项全部落到 state；未注入项保持缺省
/// （manifest 列表/registry/invoker/store/project_root/enabled_plugin_ids
/// 各自可读回）。管道配置/公共 step 库不在此列：AppState 不持有 boot 快照
/// 副本，热路径 [`crate::server::load_and_compile_by_name`] 每次现读盘——
/// 本用例改写盘上配置后重载编译，产物必须反映新值（boot 副本语义下仍是旧值）。
#[test]
fn with_plugins_wires_all_injected_fields_and_keeps_defaults() {
    let registry = Arc::new(CapabilityRegistryImpl::new());
    let dir = tempfile::tempdir().unwrap();
    let store: Arc<dyn StorageBackend> =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let mut enabled = std::collections::HashSet::new();
    enabled.insert("on_plugin".to_string());

    let state = AppState::with_plugins(
        vec![manifest("m_plug", PluginType::Tool)],
        registry.clone(),
        Arc::new(NeverInvoker),
        store,
        dir.path().to_path_buf(),
        enabled,
    );

    assert_eq!(
        state.manifests.blocking_read().len(),
        1,
        "manifest 列表注入"
    );
    assert_eq!(state.manifests.blocking_read()[0].id, "m_plug");
    assert!(
        Arc::ptr_eq(state.capability_registry.as_ref().unwrap(), &registry),
        "registry 同一实例注入"
    );
    assert!(state.invoker.is_some());
    assert!(state.store.is_some());
    assert_eq!(state.project_root.as_deref(), Some(dir.path()));
    assert!(
        state
            .enabled_plugin_ids
            .blocking_read()
            .contains("on_plugin"),
        "enabled 集合注入"
    );
    // 未注入项保持缺省：db / session / http_handler / metrics 等
    assert!(state.db.is_none());
    assert!(state.session.is_none());
    assert!(state.http_handler.is_none());
    assert!(state.metrics.is_none());
    assert!(state.plugin_loader.is_none());

    // ── 现读盘语义：热路径改 config 即生效 ──
    // 用户空间钉桩：resolve_pipeline_config_path 先查用户层，不钉会旁路
    // 临时 YAML 读到宿主机真实用户空间（真机跑过一次即被播种）。
    let tmp = tempfile::tempdir().unwrap();
    let _user_guard = crate::test_env::pin_user_config_dir(&tmp.path().join("user-config"));
    let pipe_dir = tmp.path().join("config").join("pipelines");
    std::fs::create_dir_all(&pipe_dir).unwrap();
    let config_root = tmp.path().join("config");
    let hot_manifests = vec![manifest("probe_plugin", PluginType::Tool)];
    std::fs::write(
            pipe_dir.join("autonomous.yaml"),
            "name: hot_reload_a\nmax_rounds: 3\nloop_bodies:\n  - id: main\n    steps:\n      - id: core\n        steps:\n          - probe_plugin\n",
        )
        .unwrap();

    let v1 = crate::server::load_and_compile_by_name(&config_root, "autonomous", &hot_manifests)
        .expect("v1 配置应可加载编译");
    assert_eq!(v1.name, "hot_reload_a");
    assert_eq!(v1.max_rounds, Some(3));

    // 改盘即生效：同一名再加载编译，产物反映新盘面（无任何 boot 副本兜底）。
    std::fs::write(
            pipe_dir.join("autonomous.yaml"),
            "name: hot_reload_b\nmax_rounds: 7\nloop_bodies:\n  - id: main\n    steps:\n      - id: core\n        steps:\n          - probe_plugin\n",
        )
        .unwrap();
    let v2 = crate::server::load_and_compile_by_name(&config_root, "autonomous", &hot_manifests)
        .expect("改写后的配置应可加载编译");
    assert_eq!(v2.name, "hot_reload_b", "管道名现读盘");
    assert_eq!(v2.max_rounds, Some(7), "max_rounds 现读盘");
}

/// with_contract_states / with_capability_handlers / with_plugin_scopes /
/// with_widget_bindings / with_plugin_loader / with_http_handler：注入即生效，
/// 且链式调用互不覆盖。
#[tokio::test]
async fn builder_chain_injects_each_field_independently() {
    struct NopHttpHandler;
    #[async_trait::async_trait]
    impl HttpHandleCapability for NopHttpHandler {
        async fn handle(
            &self,
            _request: agentos_core::traits::HttpHandleRequest,
        ) -> Result<agentos_core::traits::HttpHandleResponse, String> {
            Err("n/a".to_string())
        }
    }

    let states = Arc::new(crate::contract::ContractLedger::new());
    let handlers = Arc::new(agentos_mcp::CapabilityHandlerRegistry::new());
    let scopes = Arc::new(PluginScopeRegistry::new());
    let bindings = Arc::new(parking_lot::RwLock::new(Vec::new()));
    // loader 活真源 fixture：root/p1/plugin.json（discover 填充路径表）
    let tmp = tempfile::tempdir().unwrap();
    let p1 = tmp.path().join("p1");
    std::fs::create_dir_all(&p1).unwrap();
    std::fs::write(
        p1.join("plugin.json"),
        r#"{"id":"p1","name":"p1","version":"1.0.0","plugin_type":"tool",
                "language":"python","host_type":"sidecar","entry":"python server.py",
                "capabilities":{}}"#,
    )
    .unwrap();
    let loader = crate::test_env::discovered_plugin_loader(tmp.path()).await;

    let state = AppState::new()
        .with_contract_states(states.clone())
        .with_capability_handlers(handlers.clone())
        .with_plugin_scopes(scopes.clone())
        .with_widget_bindings(bindings.clone())
        .with_plugin_loader(loader)
        .with_http_handler(Arc::new(NopHttpHandler));

    assert!(Arc::ptr_eq(&state.contract_states, &states), "契约账本注入");
    assert!(
        state
            .capability_handlers
            .as_ref()
            .is_some_and(|h| Arc::ptr_eq(h, &handlers)),
        "能力 handler 注册表注入"
    );
    assert!(
        Arc::ptr_eq(&state.plugin_scopes, &scopes),
        "插件 scope 账本注入"
    );
    assert!(
        state
            .widget_bindings
            .as_ref()
            .is_some_and(|b| Arc::ptr_eq(b, &bindings)),
        "widget 绑定表注入"
    );
    assert!(
        state.plugin_dir("p1").is_some_and(|p| p.ends_with("p1")),
        "插件目录活真源注入（HTTP dispatcher 静态资源解析依据）"
    );
    assert!(state.http_handler.is_some(), "HTTP 插件处理器注入");
}

/// with_kernel_capability_contracts：注入的契约集合经 `/schema` 聚合原样
/// 透出（结构 additive：namespace + capabilities[].method/input_schema）。
#[tokio::test]
async fn schema_exposes_injected_kernel_capability_contracts() {
    let contract: crate::kernel_capabilities::KernelCapabilityContract =
        serde_json::from_value(serde_json::json!({
            "namespace": "chat",
            "description": "内核 chat 面",
            "capabilities": [{
                "method": "send_message",
                "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}}
            }]
        }))
        .expect("合法契约应反序列化");
    let state = AppState::new().with_kernel_capability_contracts(Arc::new(vec![contract]));

    let schema = build_schema(&state).await;
    assert_eq!(schema.kernel_capabilities.len(), 1, "注入的契约应透出");
    assert_eq!(schema.kernel_capabilities[0]["namespace"], "chat");
    assert_eq!(
        schema.kernel_capabilities[0]["capabilities"][0]["method"],
        "send_message"
    );

    // 未注入（缺省 None）→ 空列表，不伪造契约
    let bare = build_schema(&AppState::new()).await;
    assert!(bare.kernel_capabilities.is_empty());
}

/// schema 的 routes 面按 plugin_id 排序且组内路由排序——输出确定性是
/// ETag 内容寻址的前提（HashMap 直序列化会让 304 协商失效）。
#[tokio::test]
async fn schema_routes_are_sorted_for_deterministic_etag() {
    let registry = Arc::new(CapabilityRegistryImpl::new());
    // guard 必须持有到断言结束：drop 即撤注册
    let mut guards = Vec::new();
    // 两个插件，注册顺序与字典序相反；每插件两条路由，路径顺序也相反
    for (pid, first, second) in [
        ("z_plugin", "/ext/z_plugin/b", "/ext/z_plugin/a"),
        ("a_plugin", "/ext/a_plugin/b", "/ext/a_plugin/a"),
    ] {
        for path in [first, second] {
            let (_desc, guard) = registry
                .register_http_route_guarded(
                    pid,
                    agentos_core::traits::HttpEndpoint {
                        route_id: format!("{pid}{path}"),
                        method: "GET".to_string(),
                        path: path.to_string(),
                        auth: None,
                        handler_capability: "http.handle".to_string(),
                        timeout_ms: None,
                        max_concurrency: None,
                        description: None,
                    },
                )
                .expect("命名空间内路径应注册成功");
            guards.push(guard);
        }
    }
    let mut state = AppState::new();
    state.capability_registry = Some(registry);
    let schema = build_schema(&state).await;
    let routes = schema.routes.as_object().expect("routes 面为对象");
    let keys: Vec<&String> = routes.keys().collect();
    assert_eq!(keys, vec!["a_plugin", "z_plugin"], "plugin 键字典序");
    let a_paths: Vec<&str> = routes["a_plugin"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["path"].as_str().unwrap())
        .collect();
    assert_eq!(
        a_paths,
        vec!["/ext/a_plugin/a", "/ext/a_plugin/b"],
        "组内路由按 route_id|method|path 排序"
    );

    // 相同内容重复聚合 → 相同字节（ETag 内容寻址前提）
    let schema2 = build_schema(&state).await;
    assert_eq!(
        serde_json::to_vec(&schema.routes).unwrap(),
        serde_json::to_vec(&schema2.routes).unwrap(),
        "同内容必须产出相同字节"
    );
}

/// json_value_type_name：六类 JSON 值各自命名（400 错误消息可读性契约）。
#[test]
fn json_value_type_name_covers_all_variants() {
    assert_eq!(json_value_type_name(&serde_json::Value::Null), "null");
    assert_eq!(json_value_type_name(&json!(true)), "bool");
    assert_eq!(json_value_type_name(&json!(1.5)), "number");
    assert_eq!(json_value_type_name(&json!("s")), "string");
    assert_eq!(json_value_type_name(&json!([1])), "array");
    assert_eq!(json_value_type_name(&json!({"k": 1})), "object");
}

/// consume_refresh_jti 的 db 故障路径：账本写入失败必须 fail-closed
/// （拒本次刷新），不因存储异常放行旧值复用。
#[test]
fn consume_refresh_jti_fails_closed_on_ledger_error() {
    // 只读库（无法建表）→ consume_refresh_jti 写入必失败
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("ro.db");
    let sqlite = Arc::new(agentos_engine::SqliteStore::open(db_path.to_str().unwrap()).unwrap());
    // 建同名表格挡住后续写入：把 consumed_refresh_jtis 建成只读视图
    sqlite
        .with_conn::<(), String>(|c| {
            c.execute_batch(
                "DROP TABLE consumed_refresh_jtis; \
                     CREATE VIEW consumed_refresh_jtis AS SELECT '' AS jti, 0 AS consumed_at;",
            )
            .map_err(|e| e.to_string())
        })
        .ok();
    let state = AppState::new().with_db(sqlite);
    if state.consume_refresh_jti("jti-fault-1") {
        // 视图可读但不可写时应当为 false；若驱动允许写入则退化为正常账本，
        // 此时断言第二次消费仍被拒（账本语义不变）。
        assert!(
            !state.consume_refresh_jti("jti-fault-1"),
            "账本异常或已消费都必须拒绝（fail-closed）"
        );
    }
}
