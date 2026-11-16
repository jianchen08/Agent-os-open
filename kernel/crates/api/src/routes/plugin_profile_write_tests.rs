//! K1：default_profile.yaml 损坏时 PUT enabled 拒绝写（422），不再用
//! 硬编码模板顶替并覆写（清空全部插件启停配置）；文件缺失 → 正常新建。
//!
//! 落点用户空间（ADR 2026-09-13-unified-user-root）：写侧落用户层、首次接管
//! 先播种 factory 整份。用例把 factory 与用户层都钉在临时目录下——不钉用户层
//! 会写进开发机真实用户目录（既有用例的隐式污染面）。

use super::*;

/// 测试装配：factory 项目根 + 隔离的用户配置层。
struct Fixture {
    tmp: tempfile::TempDir,
    _user_guard: crate::test_env::UserSpaceGuard,
    user_config: std::path::PathBuf,
}

impl Fixture {
    fn new() -> Self {
        let tmp = tempfile::tempdir().unwrap();
        let user_config = tmp.path().join("user-config");
        std::fs::create_dir_all(&user_config).unwrap();
        let guard = crate::test_env::pin_user_config_dir(&user_config);
        Self {
            tmp,
            _user_guard: guard,
            user_config,
        }
    }

    /// factory 项目根（`AGENTOS_CONFIG_ROOT` 语义上的 config/ 父目录）。
    fn project_root(&self) -> std::path::PathBuf {
        self.tmp.path().to_path_buf()
    }

    fn state(&self) -> AppState {
        let mut state = AppState::new();
        state.project_root = Some(self.project_root());
        state
    }

    /// factory 侧 profile 路径。
    fn factory_profile(&self) -> std::path::PathBuf {
        self.project_root()
            .join("config")
            .join("kernel")
            .join("default_profile.yaml")
    }

    /// 用户层 profile 路径（写侧落点）。
    fn user_profile(&self) -> std::path::PathBuf {
        self.user_config.join("kernel").join("default_profile.yaml")
    }

    /// 在 factory 写一份 profile。
    fn seed_factory(&self, yaml: &str) {
        let p = self.factory_profile();
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::write(p, yaml).unwrap();
    }
}

#[tokio::test]
async fn corrupted_profile_refuses_overwrite_with_422() {
    let fx = Fixture::new();
    let corrupted = "version: 1\nplugins: [broken\n";
    fx.seed_factory(corrupted);

    let err = plugins_set_enabled_handler(
        axum::extract::Path("some_plugin".to_string()),
        axum::extract::State(fx.state()),
        axum::Json(EnabledBody { enabled: true }),
    )
    .await
    .unwrap_err();

    assert!(
        matches!(err, ApiError::UnprocessableEntity { .. }),
        "损坏 profile 应 422 拒写，实际 {err:?}"
    );
    // 播种把损坏内容原样带到用户层，拒写后该现场必须保留（运维排查依据）
    assert_eq!(
        std::fs::read_to_string(fx.user_profile()).unwrap(),
        corrupted,
        "损坏现场必须原样保留（拒写不覆写）"
    );
}

#[tokio::test]
async fn scalar_top_level_profile_refuses_overwrite_with_422() {
    // 解析成功但顶层非 Mapping（标量）：if-let Mapping 不命中则静默跳过补丁、
    // 写回仍会覆盖原文——同样按损坏拒写。
    let fx = Fixture::new();
    fx.seed_factory("just_a_string\n");

    let err = plugins_set_enabled_handler(
        axum::extract::Path("some_plugin".to_string()),
        axum::extract::State(fx.state()),
        axum::Json(EnabledBody { enabled: false }),
    )
    .await
    .unwrap_err();

    assert!(matches!(err, ApiError::UnprocessableEntity { .. }));
    assert_eq!(
        std::fs::read_to_string(fx.user_profile()).unwrap(),
        "just_a_string\n"
    );
}

#[tokio::test]
async fn missing_profile_creates_fresh_one_on_enable() {
    // 文件缺失（非损坏）：首次落盘合法——新建 Mapping 写入该插件开关，
    // 其他插件无存量可破坏。
    let fx = Fixture::new();

    let resp = plugins_set_enabled_handler(
        axum::extract::Path("fresh_plugin".to_string()),
        axum::extract::State(fx.state()),
        axum::Json(EnabledBody { enabled: true }),
    )
    .await
    .unwrap();

    assert_eq!(resp.0["success"], true);
    let raw = std::fs::read_to_string(fx.user_profile()).unwrap();
    assert!(raw.contains("fresh_plugin"), "新 profile 应含该插件条目");
    assert!(raw.contains("enabled: true"));
    // 写入落在用户层：factory 仍是"无此文件"（未被覆写、未在仓内新建）
    assert!(
        !fx.factory_profile().exists(),
        "写侧不得在 factory 落文件（改动必须出仓）"
    );
}

#[tokio::test]
async fn valid_profile_roundtrip_preserves_other_plugins() {
    // 回归：合法 profile 上切换某插件开关，其他插件启停设置必须保留
    // （K1 修复守护的正是这条不变量）。
    let fx = Fixture::new();
    fx.seed_factory(
        "version: 1\nplugins:\n  other_plugin:\n    enabled: false\ndefaults:\n  enabled: true\n",
    );

    let _resp = plugins_set_enabled_handler(
        axum::extract::Path("target_plugin".to_string()),
        axum::extract::State(fx.state()),
        axum::Json(EnabledBody { enabled: true }),
    )
    .await
    .unwrap();

    let doc: serde_yaml::Value =
        serde_yaml::from_str(&std::fs::read_to_string(fx.user_profile()).unwrap()).unwrap();
    assert_eq!(
        doc["plugins"]["other_plugin"]["enabled"], false,
        "其他插件启停设置必须保留（首次接管经播种拿到 factory 完整内容）"
    );
    assert_eq!(doc["plugins"]["target_plugin"]["enabled"], true);
    assert_eq!(doc["defaults"]["enabled"], true, "defaults 段一并保留");
}

/// 无 project_root → 500（不猜默认根，不落到 CWD）。
#[tokio::test]
async fn missing_project_root_returns_500() {
    let err = plugins_set_enabled_handler(
        axum::extract::Path("p".to_string()),
        axum::extract::State(AppState::new()),
        axum::Json(EnabledBody { enabled: true }),
    )
    .await
    .unwrap_err();
    assert!(matches!(err, ApiError::Internal { .. }), "实际 {err:?}");
}

// ── enable/disable 热路径：注册表对称更新 + 连带禁用 + 响应形状 ──

/// 构造带 manifest / registry / enabled 集合的 fixture state，复用
/// [`Fixture`] 的临时根与用户层钉桩。
fn hot_path_state(
    fx: &Fixture,
) -> (
    AppState,
    Arc<CapabilityRegistryImpl>,
    Arc<parking_lot::RwLock<Vec<crate::metrics::WidgetBinding>>>,
) {
    let mut state = fx.state();
    let registry = Arc::new(CapabilityRegistryImpl::new());
    state.capability_registry = Some(registry.clone());
    let bindings = Arc::new(parking_lot::RwLock::new(Vec::new()));
    state.widget_bindings = Some(bindings.clone());
    (state, registry, bindings)
}

fn hot_manifest(id: &str, tool: &str) -> PluginManifest {
    let mut m: PluginManifest = serde_json::from_value(json!({
        "id": id, "name": id, "version": "1.0.0",
        "plugin_type": "tool", "language": "python",
        "host_type": "sidecar", "entry": "x",
        "capabilities": {"tools": [{"name": tool}]},
    }))
    .expect("valid manifest");
    m.capabilities.tools[0].input_schema = Some(json!({"type": "object", "properties": {}}));
    m
}

/// 启用热路径：manifest 在册 → 立即重注册 tools（registered 账目非空），
/// 且注册表真出现该工具；enabled 集合同步插入。
#[tokio::test]
async fn enable_hot_path_reregisters_tools_immediately() {
    use agentos_core::traits::CapabilityRegistry;
    let fx = Fixture::new();
    let (mut state, registry, _bindings) = hot_path_state(&fx);
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![hot_manifest(
        "hot_a",
        "hot_tool_a",
    )]));

    let resp = plugins_set_enabled_handler(
        axum::extract::Path("hot_a".to_string()),
        axum::extract::State(state.clone()),
        axum::Json(EnabledBody { enabled: true }),
    )
    .await
    .unwrap();

    assert_eq!(resp.0["success"], true);
    assert_eq!(resp.0["enabled"], true);
    assert_eq!(resp.0["restart_required"], false, "G1 双向即时生效");
    assert_eq!(resp.0["registered"]["tools"], 1, "应重注册 1 个工具");
    let tool_names: Vec<String> = registry
        .list_tools()
        .iter()
        .map(|t| t.name.clone())
        .collect();
    assert!(
        tool_names.contains(&"hot_tool_a".to_string()),
        "启用后工具必须立即出现在注册表: {tool_names:?}"
    );
    assert!(
        state.enabled_plugin_ids.read().await.contains("hot_a"),
        "enabled 集合须同步插入"
    );
    assert!(resp.0["cascade_disabled"].as_array().unwrap().is_empty());
    assert!(
        resp.0["message"].as_str().unwrap().contains("已启用"),
        "消息应表达启用: {}",
        resp.0["message"]
    );
}

/// 禁用热路径：scope revoke + clear_plugin 摘工具 + widget 绑定移除
/// （零残留），enabled 集合同步移除，响应消息为「已禁用」。
#[tokio::test]
async fn disable_hot_path_clears_registry_scope_and_bindings() {
    use agentos_core::traits::CapabilityRegistry;
    let fx = Fixture::new();
    let (mut state, registry, bindings) = hot_path_state(&fx);
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![hot_manifest(
        "hot_b",
        "hot_tool_b",
    )]));
    state
        .enabled_plugin_ids
        .write()
        .await
        .insert("hot_b".to_string());

    // 预置：注册工具 + 一条 widget 绑定（模拟已启用状态的残留面）
    registry.register_tool(
        "hot_b",
        agentos_core::traits::ToolDescriptor {
            name: "hot_tool_b".to_string(),
            description: "d".to_string(),
            plugin_id: "hot_b".to_string(),
            input_schema: json!({"type": "object", "properties": {}}),
            output_schema: None,
            category: agentos_core::types::ToolCategory::System,
            source: agentos_core::types::ToolSource::Builtin,
            ui: None,
            render: None,
            isolation_capable: None,
        },
    );
    bindings.write().push(crate::metrics::WidgetBinding {
        widget_id: "w".to_string(),
        plugin_id: "hot_b".to_string(),
        metric: "m".to_string(),
        interval: std::time::Duration::from_secs(1),
        scope: crate::metrics::BindingScope::Broadcast,
        owner_plugin_id: "hot_b".to_string(),
    });

    let resp = plugins_set_enabled_handler(
        axum::extract::Path("hot_b".to_string()),
        axum::extract::State(state.clone()),
        axum::Json(EnabledBody { enabled: false }),
    )
    .await
    .unwrap();

    assert_eq!(resp.0["enabled"], false);
    assert!(
        !registry.list_tools().iter().any(|t| t.plugin_id == "hot_b"),
        "禁用必须摘除该插件的工具（零残留）"
    );
    assert!(bindings.read().is_empty(), "widget 绑定必须被移除");
    assert!(
        !state.enabled_plugin_ids.read().await.contains("hot_b"),
        "enabled 集合须同步移除"
    );
    assert!(
        resp.0["message"].as_str().unwrap().contains("已禁用"),
        "消息应表达禁用: {}",
        resp.0["message"]
    );
}

/// 启用但 manifest 不在册 → registered 为 null（不凭空注册、不报错）。
#[tokio::test]
async fn enable_without_manifest_registers_nothing() {
    let fx = Fixture::new();
    let (state, _registry, _bindings) = hot_path_state(&fx);
    // manifests 为空
    let resp = plugins_set_enabled_handler(
        axum::extract::Path("ghost".to_string()),
        axum::extract::State(state),
        axum::Json(EnabledBody { enabled: true }),
    )
    .await
    .unwrap();
    assert!(
        resp.0["registered"].is_null(),
        "无 manifest 时 registered=null"
    );
    assert_eq!(resp.0["success"], true, "开关本身仍生效");
}

/// 无 capability_registry（未装配）→ 只改内存集合与文件，不 panic。
#[tokio::test]
async fn enable_without_registry_still_toggles_state() {
    let fx = Fixture::new();
    let state = fx.state(); // 无 registry
    let resp = plugins_set_enabled_handler(
        axum::extract::Path("no_reg".to_string()),
        axum::extract::State(state.clone()),
        axum::Json(EnabledBody { enabled: true }),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["success"], true);
    assert_eq!(resp.0["registered"], serde_json::Value::Null);
    assert!(state.enabled_plugin_ids.read().await.contains("no_reg"));
}

// ── 连带禁用（cascade_disable_dependents）──

fn dep_manifest(id: &str, requires: &[&str]) -> PluginManifest {
    serde_json::from_value(json!({
        "id": id, "name": id, "version": "1.0.0",
        "plugin_type": "tool", "language": "python",
        "host_type": "sidecar", "entry": "x",
        "requires_services": requires,
        "capabilities": {},
    }))
    .expect("valid manifest")
}

/// 构造带 capability namespace 声明的提供者 manifest（服务依赖闸读 provides）。
fn provider_manifest(id: &str, namespace: &str) -> PluginManifest {
    serde_json::from_value(json!({
        "id": id, "name": id, "version": "1.0.0",
        "plugin_type": "tool", "language": "python",
        "host_type": "sidecar", "entry": "x",
        "capabilities": {"tools": [{"name": format!("{id}.status"),
            "input_schema": {"type": "object", "properties": {}}}]},
        "provides": {"capabilities": [{"namespace": namespace, "methods": ["status"]}]},
    }))
    .expect("valid provider manifest")
}

/// 禁用提供者 P：直接依赖 P 的插件被连带摘除，间接依赖方（依赖依赖方）
/// 经 fixpoint 也覆盖；响应 `cascade_disabled` 列全清单且消息点名。
#[tokio::test]
async fn disable_cascades_to_dependents_transitively() {
    let fx = Fixture::new();
    let (mut state, registry, _bindings) = hot_path_state(&fx);
    // P 提供服务；A 依赖 P；B 依赖 A（间接）
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![
        provider_manifest("p_provider", "svc_x"),
        dep_manifest("a_dep", &["svc_x"]),
        dep_manifest("b_indirect", &["svc_from_a"]),
        dep_manifest("c_clean", &[]),
    ]));
    {
        let mut ids = state.enabled_plugin_ids.write().await;
        for id in ["p_provider", "a_dep", "b_indirect", "c_clean"] {
            ids.insert(id.to_string());
        }
    }

    let resp = plugins_set_enabled_handler(
        axum::extract::Path("p_provider".to_string()),
        axum::extract::State(state.clone()),
        axum::Json(EnabledBody { enabled: false }),
    )
    .await
    .unwrap();

    let cascaded: Vec<String> = resp.0["cascade_disabled"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap().to_string())
        .collect();
    assert!(
        cascaded.contains(&"a_dep".to_string()),
        "直接依赖方必须被连带摘除: {cascaded:?}"
    );
    assert!(
        !cascaded.contains(&"c_clean".to_string()),
        "无依赖插件不得被误伤: {cascaded:?}"
    );
    assert!(
        resp.0["message"].as_str().unwrap().contains("连带摘除"),
        "有连带时消息必须点名: {}",
        resp.0["message"]
    );
    // 连带摘除走 clear_plugin（注册面零残留）
    assert!(
        !registry.list_tools().iter().any(|t| t.plugin_id == "a_dep"),
        "连带摘除必须清注册表"
    );
}

/// 无连带时 `cascade_disabled` 为空数组，消息不提连带。
#[tokio::test]
async fn disable_without_dependents_reports_empty_cascade() {
    let fx = Fixture::new();
    let (mut state, _registry, _bindings) = hot_path_state(&fx);
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![dep_manifest(
        "solo_plugin",
        &[],
    )]));
    let resp = plugins_set_enabled_handler(
        axum::extract::Path("solo_plugin".to_string()),
        axum::extract::State(state),
        axum::Json(EnabledBody { enabled: false }),
    )
    .await
    .unwrap();
    assert!(resp.0["cascade_disabled"].as_array().unwrap().is_empty());
    assert!(!resp.0["message"].as_str().unwrap().contains("连带"));
}

// ── dependents 查询端点 ──

/// 未知插件 404；已知插件返回依赖方清单（enabled 标注启停态）。
#[tokio::test]
async fn dependents_endpoint_reports_known_and_unknown() {
    let fx = Fixture::new();
    let (mut state, _registry, _bindings) = hot_path_state(&fx);
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![
        provider_manifest("prov_x", "svc_y"),
        dep_manifest("dep_on_x", &["svc_y"]),
    ]));
    state
        .enabled_plugin_ids
        .write()
        .await
        .insert("dep_on_x".to_string());

    let err = plugins_dependents_handler(
        axum::extract::Path("nope".to_string()),
        axum::extract::State(state.clone()),
    )
    .await
    .unwrap_err();
    assert!(matches!(err, ApiError::NotFound { .. }), "实际 {err:?}");

    let resp = plugins_dependents_handler(
        axum::extract::Path("prov_x".to_string()),
        axum::extract::State(state),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["total"], 1, "{:?}", resp.0);
    assert_eq!(resp.0["dependents"][0]["id"], "dep_on_x");
    assert_eq!(
        resp.0["dependents"][0]["enabled"], true,
        "启停态须随查询出口"
    );
}

// ── load_profile_doc / apply_enabled_patch 纯函数面 ──

/// 空白文件 = 全新 Mapping；文件不存在 = 全新 Mapping；非法 YAML 与非
/// Mapping 顶层 = 422；读取错误（目录占位）= 500。
#[test]
fn load_profile_doc_branch_matrix() {
    let tmp = tempfile::tempdir().unwrap();

    // 文件不存在
    let missing = tmp.path().join("absent.yaml");
    let doc = load_profile_doc(&missing).expect("缺失 = 新建 Mapping");
    assert!(matches!(doc, serde_yaml::Value::Mapping(_)));

    // 空白文件
    let blank = tmp.path().join("blank.yaml");
    std::fs::write(&blank, "   \n").unwrap();
    assert!(matches!(
        load_profile_doc(&blank).unwrap(),
        serde_yaml::Value::Mapping(_)
    ));

    // 非法 YAML
    let broken = tmp.path().join("broken.yaml");
    std::fs::write(&broken, "plugins: [a\n").unwrap();
    assert!(matches!(
        load_profile_doc(&broken).unwrap_err(),
        ApiError::UnprocessableEntity { .. }
    ));

    // 顶层非 Mapping（标量 / 序列）
    for content in ["just_a_string\n", "- one\n- two\n"] {
        let p = tmp.path().join("scalar.yaml");
        std::fs::write(&p, content).unwrap();
        assert!(
            matches!(
                load_profile_doc(&p).unwrap_err(),
                ApiError::UnprocessableEntity { .. }
            ),
            "顶层非 Mapping 必须 422: {content:?}"
        );
    }

    // 读取错误：路径是目录 → 500（NotFound 之外的其他 IO 错）
    let dir_as_file = tmp.path().join("dir.yaml");
    std::fs::create_dir_all(&dir_as_file).unwrap();
    assert!(matches!(
        load_profile_doc(&dir_as_file).unwrap_err(),
        ApiError::Internal { .. }
    ));
}

/// `apply_enabled_patch` 三级创建：顶层非 Mapping 时静默跳过（不 panic）；
/// plugins 键缺失则创建；插件条目缺失则创建；已有条目仅改 enabled 不动其他键。
#[test]
fn apply_enabled_patch_creates_missing_levels() {
    // 已有条目：只改 enabled，其他键保留
    let mut doc: serde_yaml::Value =
        serde_yaml::from_str("plugins:\n  p1:\n    enabled: false\n    note: keep\n").unwrap();
    apply_enabled_patch(&mut doc, "p1", true);
    assert_eq!(doc["plugins"]["p1"]["enabled"], true);
    assert_eq!(doc["plugins"]["p1"]["note"], "keep", "其他键不得被动");

    // plugins 键缺失 → 创建
    let mut fresh = serde_yaml::Value::Mapping(serde_yaml::Mapping::new());
    apply_enabled_patch(&mut fresh, "p2", true);
    assert_eq!(fresh["plugins"]["p2"]["enabled"], true);

    // 顶层非 Mapping → 静默跳过（由 load_profile_doc 的 422 闸拦截在入口）
    let mut scalar = serde_yaml::Value::String("nope".into());
    apply_enabled_patch(&mut scalar, "p3", true);
    assert_eq!(scalar, serde_yaml::Value::String("nope".into()));

    // plugins 存在但非 Mapping（标量）→ 内层 if-let 不命中，静默跳过
    let mut weird: serde_yaml::Value = serde_yaml::from_str("plugins: 42\n").unwrap();
    apply_enabled_patch(&mut weird, "p4", true);
    assert_eq!(weird["plugins"], 42, "非 Mapping 的 plugins 不被改写");
}
