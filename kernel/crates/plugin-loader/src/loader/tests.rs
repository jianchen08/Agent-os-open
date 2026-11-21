// @feature: FP-0.2.一 插件协议·plugin_check 契约校验引擎 | @ci: rust-test
//! loader 单元测试（发现 / 准入 / 双源 / 装载生命周期）。
use super::*;
use crate::test_env::{user_space_env_lock, UserRootGuard, UserSpaceEnvGuard};
use agentos_core::traits::{HostType, PluginVisibility, ProvidedCapabilityHost, StepCapability};
use std::fs;

/// 发现面收集原语（ADR 2026-10-13 拍平配套）：布局无关递归下钻——平铺一级、
/// 旧类目录二级、三级深嵌套同样命中；无 manifest 的目录只作路径下钻；纯
/// 文件不干扰；plugin.yaml 兜底同识别。
#[test]
fn collect_plugin_dirs_is_layout_agnostic_and_yaml_fallback() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path();
    // ① 平铺：root/flat/plugin.json
    fs::create_dir_all(root.join("flat")).unwrap();
    fs::write(root.join("flat/plugin.json"), r#"{"id":"flat"}"#).unwrap();
    // ② 二级：root/kinds/tools/nested/plugin.yaml
    fs::create_dir_all(root.join("kinds/tools/nested")).unwrap();
    fs::write(root.join("kinds/tools/nested/plugin.yaml"), "id: nested").unwrap();
    // ③ 三级深嵌套：root/a/b/c/deep/plugin.json
    fs::create_dir_all(root.join("a/b/c/deep")).unwrap();
    fs::write(root.join("a/b/c/deep/plugin.json"), r#"{"id":"deep"}"#).unwrap();
    // ④ 无 manifest 的目录（如 _libs）只下钻不命中；⑤ 散文件忽略
    fs::create_dir_all(root.join("_libs")).unwrap();
    fs::write(root.join("_libs/readme.txt"), "not a plugin").unwrap();
    fs::write(root.join("loose.txt"), "x").unwrap();

    let mut dirs = collect_plugin_dirs(root);
    dirs.sort();
    let names: Vec<String> = dirs
        .iter()
        .map(|d| d.file_name().unwrap().to_string_lossy().into_owned())
        .collect();
    assert_eq!(
        names,
        vec!["deep", "flat", "nested"],
        "三种布局同样命中；_libs 与散文件跳过"
    );
    // 性质断言：命中目录恰含 manifest（发现的定义面）
    assert!(dirs
        .iter()
        .all(|d| d.join("plugin.json").is_file() || d.join("plugin.yaml").is_file()));
}

fn create_test_plugin_dir(root: &Path, id: &str, plugin_type: &str) {
    create_test_plugin_dir_with_version(root, id, plugin_type, "1.0.0");
}

/// [`create_test_plugin_dir`] 的版本参数化变体（双源裁决测试用：
/// 用户新/资源新/版本相同各形状需要显式版本）。
fn create_test_plugin_dir_with_version(root: &Path, id: &str, plugin_type: &str, version: &str) {
    let dir = root.join(id);
    fs::create_dir_all(&dir).unwrap();
    // pipeline 类型插件需声明 invoke_entry（ADR 附录 D②，P6 discover 聚合校验）
    let invoke_entry_field = if plugin_type == "pipeline" {
        format!(",\n    \"invoke_entry\": \"{id}.execute\"")
    } else {
        String::new()
    };
    let manifest_json = format!(
        r#"{{
    "id": "{id}",
    "name": "Test Plugin {id}",
    "version": "{version}",
    "plugin_type": "{plugin_type}",
    "language": "rust",
    "host_type": "in_process",
    "entry": "test_plugin",
    "capabilities": {{}},
    "requires_services": [],
    "permissions": {{}},
    "priority": 100{invoke_entry_field}
}}"#
    );
    fs::write(dir.join("plugin.json"), manifest_json).unwrap();
}

/// 内存归一（唯一真源在 AppState.manifests）：loader 缓存只持轻量路径
/// 摘要，完整 manifest 按需读盘——磁盘 plugin.json 编辑后 get_manifest /
/// load 读到新内容（不经过 discover 重扫）。
#[tokio::test]
async fn test_manifest_cache_slim_reparses_disk_on_demand() {
    let builtin = tempfile::tempdir().unwrap();
    create_test_plugin_dir(builtin.path(), "slim_cache", "pipeline");

    let loader = PluginLoaderImpl::new(builtin.path(), None);
    loader.discover(&[]).await.unwrap();

    // discover 后缓存不持完整 manifest：改磁盘声明，get_manifest 应读到新值
    let manifest_path = builtin.path().join("slim_cache").join("plugin.json");
    let edited = std::fs::read_to_string(&manifest_path)
        .unwrap()
        .replace("Test Plugin slim_cache", "Edited Manifest Name");
    std::fs::write(&manifest_path, edited).unwrap();

    let m = loader
        .get_manifest("slim_cache")
        .expect("get_manifest 应可读");
    assert_eq!(m.name, "Edited Manifest Name", "按需读盘应读到磁盘最新声明");
    assert_eq!(m.id, "slim_cache");

    // load 路径同源：加载出的 manifest 也是按需读盘的当前磁盘内容
    let loaded = loader.load("slim_cache").await.expect("load 应成功");
    assert_eq!(loaded.manifest.name, "Edited Manifest Name");

    // get_plugin_dirs 走同一路径摘要缓存（根目录映射不受磁盘编辑影响）
    let dirs = loader.get_plugin_dirs();
    assert!(dirs.contains_key("slim_cache"), "目录映射应含该插件");
    assert_eq!(
        dirs["slim_cache"],
        builtin.path().join("slim_cache"),
        "目录映射应指向缓存路径的父目录"
    );
}

/// loader 缓存瘦身后的失败语义：manifest 读盘失败时 load 报显式错误、
/// get_manifest 按未发现处理（None + warn），不静默返回旧缓存。
#[tokio::test]
async fn test_manifest_cache_slim_failures_when_disk_unreadable() {
    let builtin = tempfile::tempdir().unwrap();
    create_test_plugin_dir(builtin.path(), "slim_gone", "pipeline");

    let loader = PluginLoaderImpl::new(builtin.path(), None);
    loader.discover(&[]).await.unwrap();

    let manifest_path = builtin.path().join("slim_gone").join("plugin.json");

    // ① 文件删除（IO 错误）：load 显式报 MANIFEST_READ，不静默降级
    std::fs::remove_file(&manifest_path).unwrap();
    let err = loader.load("slim_gone").await.expect_err("读盘失败应报错");
    assert_eq!(err.code.as_deref(), Some("MANIFEST_READ"));
    // get_manifest 按未发现处理（None）。warn 走 always-enabled 测试订阅者
    // 包裹：默认无订阅者时 tracing 按 callsite 缓存 never-interest，warn!
    // 的 format args 不会被求值（覆盖率盲区）；订阅者在场保证失败路径
    // 真实执行（断言仍是 None 语义，不 assert 日志内容）。
    let missing = tracing::subscriber::with_default(AlwaysSubscriber, || {
        loader.get_manifest("slim_gone").is_none()
    });
    assert!(missing);

    // ② 文件内容损坏（JSON/YAML 双解析失败）：同样按未发现处理
    std::fs::write(&manifest_path, "not a manifest: {{{").unwrap();
    let missing = tracing::subscriber::with_default(AlwaysSubscriber, || {
        loader.get_manifest("slim_gone").is_none()
    });
    assert!(missing, "损坏 manifest 应按未发现处理");
    assert!(loader.load("slim_gone").await.is_err());
}

/// 全开测试订阅者：`register_callsite` 返回 always-interest、`enabled` 恒真，
/// 事件/span 记录全部 no-op。仅供失败路径测试包裹用——不注入日志输出，
/// 只让 tracing 宏的 format args 被求值（默认无订阅者时 callsite 以
/// never-interest 缓存，错误路径的 warn 文案永不执行）。
struct AlwaysSubscriber;

impl tracing::Subscriber for AlwaysSubscriber {
    fn register_callsite(
        &self,
        _metadata: &'static tracing::Metadata<'static>,
    ) -> tracing::subscriber::Interest {
        tracing::subscriber::Interest::always()
    }
    fn enabled(&self, _metadata: &tracing::Metadata<'_>) -> bool {
        true
    }
    fn new_span(&self, _span: &tracing::span::Attributes<'_>) -> tracing::Id {
        tracing::Id::from_u64(1)
    }
    fn record(&self, _span: &tracing::Id, _values: &tracing::span::Record<'_>) {}
    fn record_follows_from(&self, _span: &tracing::Id, _follows: &tracing::Id) {}
    fn event(&self, _event: &tracing::Event<'_>) {}
    fn enter(&self, _span: &tracing::Id) {}
    fn exit(&self, _span: &tracing::Id) {}
}

/// manifest 未知字段 `deny_unknown_fields` 拒绝（fail-closed，不容忍
/// "声明了却不生效"——如已删除的 `capabilities.resources` 结构字段）。
/// 本测试断言携带未知字段的 manifest 被拒绝。
#[test]
fn test_manifest_with_unknown_field_is_rejected() {
    let manifest_json = r#"{
    "id": "legacy_res",
    "name": "Legacy Resource Plugin",
    "version": "1.0.0",
    "plugin_type": "pipeline",
    "language": "rust",
    "host_type": "in_process",
    "entry": "test_plugin",
    "invoke_entry": "legacy_res.execute",
    "capabilities": {
        "resources": [
            {"uri": "config://app", "name": "App Config", "mime_type": "application/json"}
        ]
    },
    "requires_services": [],
    "permissions": {},
    "priority": 100
}"#;
    let err = serde_json::from_str::<PluginManifest>(manifest_json)
        .expect_err("未知字段 capabilities.resources 必须拒绝，不再静默忽略");
    let msg = format!("{err:?}");
    assert!(
        msg.contains("resources"),
        "错误应指明被拒绝的未知字段: {msg}"
    );
}

/// 语料级校验（Phase 1 契约真实性回归）：真实仓库全部 plugin.json 必须毫发
/// 无伤通过严格反序列化（`deny_unknown_fields`）+ 必填字段校验。
///
/// 这是"校验器不要在真实数据上空转"的持续闸门——语料出现未知字段或缺
/// 必填（如顶层 `description` 未入 struct、`capabilities.resources` 这类
/// 非契约字段）即红灯（校验器真实生效）。
#[test]
fn real_corpus_all_manifests_pass_strict_parsing() {
    let manifest_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    let shared = manifest_dir.join("../../../plugins/shared");
    let mut count = 0usize;
    let mut walk = Vec::new();
    fn collect(dir: &std::path::Path, out: &mut Vec<std::path::PathBuf>) {
        // 跳过第三方/运行时产物目录：管理面只管插件声明，node_modules 下也有
        // 大量第三方依赖自带的 plugin.json（非本仓插件）。
        const SKIP: &[&str] = &[
            "node_modules",
            ".venv",
            "__pycache__",
            "dsh_plugins",
            "runtime",
        ];
        let entries = match std::fs::read_dir(dir) {
            Ok(e) => e,
            Err(e) => {
                panic!("无法读取 {}: {e}", dir.display());
            }
        };
        for entry in entries {
            let entry = entry.unwrap();
            let path = entry.path();
            if path.is_dir() {
                if !SKIP.contains(&path.file_name().and_then(|n| n.to_str()).unwrap_or("")) {
                    collect(&path, out);
                }
            } else if path.file_name().and_then(|n| n.to_str()) == Some("plugin.json") {
                out.push(path);
            }
        }
    }
    collect(&shared, &mut walk);
    assert!(
        walk.len() > 50,
        "应扫到真实插件语料 >50，实际 {}",
        walk.len()
    );
    for path in &walk {
        let text = std::fs::read_to_string(path)
            .unwrap_or_else(|e| panic!("读取 {} 失败: {e}", path.display()));
        let m: PluginManifest = serde_json::from_str(&text).unwrap_or_else(|e| {
            panic!(
                "真实语料 {} 未通过严格解析（校验器抓到未知字段/坏结构）: {e}",
                path.display()
            );
        });
        assert!(!m.id.is_empty() && !m.name.is_empty() && !m.version.is_empty());
        // output_schema 声明合法性（声明即校验）：真实工具若有畸形声明即红灯
        for t in &m.capabilities.tools {
            if let Some(out) = &t.output_schema {
                let msg = crate::registry::output_schema_error(out);
                assert!(
                    msg.is_none(),
                    "真实语料 {} 的 {} output_schema 声明不合法: {}",
                    path.display(),
                    t.name,
                    msg.unwrap()
                );
            }
        }
        // provides 服务注册检查：公告的方法必须有已声明工具（"服务未注册"）
        let unbacked = crate::registry::provides_methods_unbacked(&m);
        assert!(
                unbacked.is_empty(),
                "真实语料 {} 公告了未注册的服务（provides 方法无对应已声明工具）: {:?}——服务声明了但消费者调不到",
                path.display(),
                unbacked
            );
        count += 1;
    }
    assert!(count == walk.len(), "每份真实 manifest 都过严格解析");
}

/// 真实语料（含 approval 复活的 requires_services）在依赖闸（resolve_requires_services）
/// 上必须全部通过——服务面 = 插件声明面 + 内核内置能力面，否则 boot fail-fast 误伤。
#[test]
fn real_corpus_resolve_requires_services_passes() {
    let manifest_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    let shared = manifest_dir.join("../../../plugins/shared");
    let mut walk = Vec::new();
    fn collect(dir: &std::path::Path, out: &mut Vec<std::path::PathBuf>) {
        const SKIP: &[&str] = &[
            "node_modules",
            ".venv",
            "__pycache__",
            "dsh_plugins",
            "runtime",
        ];
        let Ok(entries) = std::fs::read_dir(dir) else {
            return;
        };
        for entry in entries {
            let path = entry.unwrap().path();
            if path.is_dir() {
                if !SKIP.contains(&path.file_name().and_then(|n| n.to_str()).unwrap_or("")) {
                    collect(&path, out);
                }
            } else if path.file_name().and_then(|n| n.to_str()) == Some("plugin.json") {
                out.push(path);
            }
        }
    }
    collect(&shared, &mut walk);
    let manifests: Vec<PluginManifest> = walk
        .iter()
        .map(|p| {
            let text = std::fs::read_to_string(p).unwrap();
            serde_json::from_str(&text).unwrap_or_else(|e| panic!("解析 {} 失败: {e}", p.display()))
        })
        .collect();
    let declared: Vec<&str> = manifests
        .iter()
        .filter(|m| !m.requires_services.is_empty())
        .map(|m| m.id.as_str())
        .collect();
    assert!(
        !declared.is_empty(),
        "应有真实插件声明 requires_services（approval 复活）"
    );
    crate::registry::resolve_requires_services(&manifests).unwrap_or_else(|e| {
        panic!("真实语料依赖闸不过（approval 等 requires_services 未满足→boot 会被拒）: {e}")
    });
}

#[test]
fn test_manifest_validation_valid() {
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
    let manifest = PluginManifest {
        force_include_tools: Vec::new(),
        state: None,
        id: "test".to_string(),
        name: "Test".to_string(),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::Pipeline,
        language: "rust".to_string(),
        host_type: HostType::InProcess,
        host_group: None,
        entry: "test_entry".to_string(),
        capabilities: Default::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 100,
        mcp: None,
        lifecycle: None,
        native: None,
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        restricted_capabilities: Vec::new(),
        enabled: None,
        activation: None,
        provides: None,
        visibility: Default::default(),
        persistent_fields: vec![],
        export_fields: vec![],
        aux_venvs: Vec::new(),
    };
    assert!(loader.validate_manifest(&manifest).is_ok());
}

/// 配置单一真值校验（2026-09-02 用户裁定）：引用形态（path 非空）禁止
/// fields 声明 default（manifest+文件两处存值 = 双真值错误）；内联形态
/// （path 空）带 default 合法；env 条目必须声明 path。
#[test]
fn test_manifest_validation_config_single_truth() {
    use agentos_core::traits::{ConfigFileMapping, EnvConfigField};

    fn base_manifest(id: &str) -> PluginManifest {
        PluginManifest {
            force_include_tools: Vec::new(),
            state: None,
            id: id.to_string(),
            name: "Single Truth".to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Pipeline,
            language: "rust".to_string(),
            host_type: HostType::InProcess,
            host_group: None,
            entry: "test_entry".to_string(),
            capabilities: Default::default(),
            requires_services: vec![],
            permissions: Default::default(),
            priority: 100,
            mcp: None,
            lifecycle: None,
            native: None,
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            restricted_capabilities: Vec::new(),
            enabled: None,
            activation: None,
            provides: None,
            visibility: Default::default(),
            persistent_fields: vec![],
            export_fields: vec![],
            aux_venvs: Vec::new(),
        }
    }

    fn field_with_default(name: &str) -> EnvConfigField {
        EnvConfigField {
            name: name.to_string(),
            label: "F".to_string(),
            field_type: "toggle".to_string(),
            required: false,
            description: None,
            extra: Some(
                serde_json::json!({"default": true})
                    .as_object()
                    .unwrap()
                    .clone(),
            ),
        }
    }

    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
    let mut manifest = base_manifest("single_truth");

    // ① 引用形态 + default → 双真值错误
    manifest.config_files = vec![ConfigFileMapping {
        id: "cfg".to_string(),
        path: "config/system/foo.yaml".to_string(),
        label: "F".to_string(),
        target: None,
        settings: None,
        fields: vec![field_with_default("enabled")],
    }];
    let err = loader.validate_manifest(&manifest).unwrap_err().to_string();
    assert!(err.contains("双真值"), "应报双真值错误: {err}");

    // ② 引用形态无 default（纯 schema）→ 合法
    manifest.config_files[0].fields = vec![EnvConfigField {
        name: "enabled".to_string(),
        label: "F".to_string(),
        field_type: "toggle".to_string(),
        required: false,
        description: None,
        extra: None,
    }];
    assert!(loader.validate_manifest(&manifest).is_ok());

    // ③ 内联形态（path 空）带 default → 合法
    manifest.config_files[0].path = String::new();
    manifest.config_files[0].fields = vec![field_with_default("enabled")];
    assert!(loader.validate_manifest(&manifest).is_ok());

    // ④ env 条目省 path → 报错（写入目标语义不可省）
    manifest.config_files[0].target = Some("env".to_string());
    manifest.config_files[0].fields = vec![];
    let err = loader.validate_manifest(&manifest).unwrap_err().to_string();
    assert!(err.contains("不可省略"), "env 条目应必须声明 path: {err}");
}

/// capabilities.steps 校验（管道步骤服务化提案 §3.1）：空 name 报错，
/// 且错误文案包含违规 plugin id。
#[test]
fn test_manifest_validation_steps_empty_name_rejected() {
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
    let mut manifest = PluginManifest {
        force_include_tools: Vec::new(),
        state: None,
        id: "steps_bad".to_string(),
        name: "Steps Bad".to_string(),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::Pipeline,
        language: "rust".to_string(),
        host_type: HostType::InProcess,
        host_group: None,
        entry: "test_entry".to_string(),
        capabilities: Default::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 100,
        mcp: None,
        lifecycle: None,
        native: None,
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        restricted_capabilities: Vec::new(),
        enabled: None,
        activation: None,
        provides: None,
        visibility: Default::default(),
        persistent_fields: vec![],
        export_fields: vec![],
        aux_venvs: Vec::new(),
    };
    manifest.capabilities.steps = vec![StepCapability {
        name: "  ".to_string(),
        description: None,
        input_schema: None,
    }];
    let err = loader
        .validate_manifest(&manifest)
        .expect_err("空 step name 必须拒绝");
    let msg = format!("{err}");
    assert!(
        msg.contains("steps_bad"),
        "错误文案应包含违规 plugin id: {msg}"
    );
    assert!(msg.contains("空 name"), "错误文案应说明原因: {msg}");
}

/// capabilities.steps 校验：同一 manifest 内 name 重复报错（全局唯一由
/// G10 编译期查重），文案含 plugin id 与重复名。
#[test]
fn test_manifest_validation_steps_duplicate_name_rejected() {
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
    let mut manifest = PluginManifest {
        force_include_tools: Vec::new(),
        state: None,
        id: "steps_dup".to_string(),
        name: "Steps Dup".to_string(),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::Pipeline,
        language: "rust".to_string(),
        host_type: HostType::InProcess,
        host_group: None,
        entry: "test_entry".to_string(),
        capabilities: Default::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 100,
        mcp: None,
        lifecycle: None,
        native: None,
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        restricted_capabilities: Vec::new(),
        enabled: None,
        activation: None,
        provides: None,
        visibility: Default::default(),
        persistent_fields: vec![],
        export_fields: vec![],
        aux_venvs: Vec::new(),
    };
    manifest.capabilities.steps = vec![
        StepCapability {
            name: "task.remind".to_string(),
            description: None,
            input_schema: None,
        },
        StepCapability {
            name: "task.remind".to_string(),
            description: Some("重复".to_string()),
            input_schema: None,
        },
    ];
    let err = loader
        .validate_manifest(&manifest)
        .expect_err("重复 step name 必须拒绝");
    let msg = format!("{err}");
    assert!(
        msg.contains("steps_dup"),
        "错误文案应包含违规 plugin id: {msg}"
    );
    assert!(
        msg.contains("task.remind"),
        "错误文案应指明重复的步骤名: {msg}"
    );
}

/// capabilities.steps 校验：合法声明（非空且唯一）通过。
#[test]
fn test_manifest_validation_steps_valid_ok() {
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
    let mut manifest = PluginManifest {
        force_include_tools: Vec::new(),
        state: None,
        id: "steps_ok".to_string(),
        name: "Steps Ok".to_string(),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::Pipeline,
        language: "rust".to_string(),
        host_type: HostType::InProcess,
        host_group: None,
        entry: "test_entry".to_string(),
        capabilities: Default::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 100,
        mcp: None,
        lifecycle: None,
        native: None,
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        restricted_capabilities: Vec::new(),
        enabled: None,
        activation: None,
        provides: None,
        visibility: Default::default(),
        persistent_fields: vec![],
        export_fields: vec![],
        aux_venvs: Vec::new(),
    };
    manifest.capabilities.steps = vec![
        StepCapability {
            name: "task.inject_params".to_string(),
            description: None,
            input_schema: None,
        },
        StepCapability {
            name: "task.remind".to_string(),
            description: None,
            input_schema: None,
        },
    ];
    assert!(
        loader.validate_manifest(&manifest).is_ok(),
        "合法 steps 声明应通过校验"
    );
}

#[test]
fn test_manifest_lifecycle_idle_timeout_parsed() {
    // 插件可在 plugin.json 声明 lifecycle.idle_timeout_secs 覆盖内核默认空闲卸载。
    let json = serde_json::json!({
        "id": "human_interaction_tool",
        "name": "Human Interaction",
        "version": "2.0.0",
        "plugin_type": "tool",
        "language": "python",
        "host_type": "sidecar",
        "entry": "python server.py",
        "capabilities": {},
        "lifecycle": { "idle_timeout_secs": 0 }
    });
    let m: PluginManifest = serde_json::from_value(json).expect("parse manifest");
    assert_eq!(
        m.lifecycle.expect("lifecycle present").idle_timeout_secs,
        Some(0)
    );

    let json2 = serde_json::json!({
        "id": "p", "name": "P", "version": "1.0.0",
        "plugin_type": "tool", "language": "python",
        "host_type": "sidecar", "entry": "python s.py",
        "capabilities": {}
    });
    let m2: PluginManifest = serde_json::from_value(json2).expect("parse manifest2");
    assert!(m2.lifecycle.is_none());
}

#[test]
fn test_manifest_validation_missing_id() {
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
    let manifest = PluginManifest {
        force_include_tools: Vec::new(),
        state: None,
        id: String::new(),
        name: "Test".to_string(),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::Pipeline,
        language: "rust".to_string(),
        host_type: HostType::InProcess,
        host_group: None,
        entry: "test_entry".to_string(),
        capabilities: Default::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 100,
        mcp: None,
        lifecycle: None,
        native: None,
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        restricted_capabilities: Vec::new(),
        enabled: None,
        activation: None,
        provides: None,
        visibility: Default::default(),
        persistent_fields: vec![],
        export_fields: vec![],
        aux_venvs: Vec::new(),
    };
    assert!(loader.validate_manifest(&manifest).is_err());
}

#[test]
fn test_manifest_validation_composite_empty_entry_ok() {
    // ADR ⑥: 组合插件 entry 可为空
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
    let manifest = PluginManifest {
        force_include_tools: Vec::new(),
        state: None,
        id: "composite_test".to_string(),
        name: "Composite".to_string(),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::Composite,
        language: "yaml".to_string(),
        host_type: HostType::InProcess,
        host_group: None,
        entry: String::new(), // 空entry
        capabilities: Default::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 100,
        mcp: None,
        lifecycle: None,
        native: None,
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        restricted_capabilities: Vec::new(),
        enabled: None,
        activation: None,
        provides: None,
        visibility: Default::default(),
        persistent_fields: vec![],
        export_fields: vec![],
        aux_venvs: Vec::new(),
    };
    assert!(loader.validate_manifest(&manifest).is_ok());
}

#[tokio::test]
async fn test_dual_root_scan() {
    let builtin = tempfile::tempdir().unwrap();
    let user = tempfile::tempdir().unwrap();

    create_test_plugin_dir(builtin.path(), "builtin_plugin", "pipeline");
    create_test_plugin_dir(user.path(), "user_plugin", "tool");

    let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()));
    let manifests = loader.discover(&[]).await.unwrap();

    assert_eq!(manifests.len(), 2);
    let ids: Vec<_> = manifests.iter().map(|m| m.id.clone()).collect();
    assert!(ids.contains(&"builtin_plugin".to_string()));
    assert!(ids.contains(&"user_plugin".to_string()));
}

#[tokio::test]
async fn test_tier2_copy_wins_on_strictly_higher_version() {
    let tier1 = tempfile::tempdir().unwrap();
    let tier2 = tempfile::tempdir().unwrap();

    // 跨根同 id（生产由守卫拒启；loader 兜底归约 = version-max）：Tier2 副本
    // semver 严格更高 → 赢。
    create_test_plugin_dir_with_version(tier1.path(), "shared_plugin", "pipeline", "1.0.0");
    create_test_plugin_dir_with_version(tier2.path(), "shared_plugin", "tool", "1.1.0");

    let loader = PluginLoaderImpl::new(tier1.path(), Some(tier2.path().to_path_buf()));
    let manifests = loader.discover(&[]).await.unwrap();

    assert_eq!(manifests.len(), 1);
    assert_eq!(manifests[0].plugin_type, PluginType::Tool, "版本最高者赢");
}

/// 跨根同 id **平版**（版本相等）：包内（builtin/tier1）副本恒胜出，与扫描序
/// 无关。2026-11-08 装机实证（安装器把 tier2 用户副本刷成与包同版后）：平版
/// 胜者随扫描序漂移到用户副本 → spawn 拿无 .venv 的用户目录路径（provision
/// 契约排除 .venv，包内副本带 venv 交付）→ sidecar 全线 VENV_INTERPRETER_
/// MISSING + 合宿成员目录翻转致 _host 向上探测落空。裁决语义与升级覆盖规则
/// 同线：用户未改（版本未抬）→ 包内副本运行；改过（严格更高）→ 用户副本。
#[tokio::test]
async fn test_cross_root_tie_prefers_builtin_copy_order_independent() {
    let tier1 = tempfile::tempdir().unwrap();
    let tier2 = tempfile::tempdir().unwrap();

    create_test_plugin_dir_with_version(tier1.path(), "tied_plugin", "pipeline", "1.0.0");
    create_test_plugin_dir_with_version(tier2.path(), "tied_plugin", "pipeline", "1.0.0");

    let loader = PluginLoaderImpl::new(tier1.path(), Some(tier2.path().to_path_buf()));

    // 两种 root_paths 序（热路径 HashSet 序 lottery 的两种形态）：平版恒包内胜。
    let orders = [
        vec![tier1.path(), tier2.path()],
        vec![tier2.path(), tier1.path()],
    ];
    for roots in &orders {
        let root_strs: Vec<String> = roots
            .iter()
            .map(|p| p.to_string_lossy().to_string())
            .collect();
        let root_refs: Vec<&str> = root_strs.iter().map(|s| s.as_str()).collect();
        loader.discover(&root_refs).await.unwrap();
        let dir = loader.get_plugin_dir("tied_plugin").unwrap();
        assert!(
            std::path::Path::new(&dir).starts_with(tier1.path()),
            "平版必须包内副本胜出（roots 序: {roots:?}），got: {dir}"
        );
    }
}

/// 同 id 多候选（生产模式包拓扑：`modes/<mode_id>/plugin.json` 二级嵌套）：
/// root_paths 的迭代序不得决定胜者——version-max 归约单调，胜者与序解耦。
/// 热路径 discover_new_plugins 把多根父目录混装进每轮新建的 HashSet，序逐轮
/// 不稳定；非单调归约会让胜者逐轮翻转、源码目录随之翻转 → 代码指纹每轮必变
/// → 复验驱逐/重注册循环（/ext 路由间歇 503/504 根因，2026-09-18）。
#[tokio::test]
async fn test_same_id_winner_stable_regardless_of_root_order() {
    let tier1 = tempfile::tempdir().unwrap();
    let tier2 = tempfile::tempdir().unwrap();

    create_test_plugin_dir_with_version(&tier1.path().join("modes"), "mode_x", "pipeline", "1.0.0");
    create_test_plugin_dir_with_version(&tier2.path().join("modes"), "mode_x", "tool", "1.1.0");

    let loader = PluginLoaderImpl::new(tier1.path(), Some(tier2.path().to_path_buf()));

    // 两种 root_paths 序（模拟热路径 HashSet 逐轮序不稳定）：最高版本份恒赢。
    let orders = [
        vec![tier1.path().join("modes"), tier2.path().join("modes")],
        vec![tier2.path().join("modes"), tier1.path().join("modes")],
    ];
    for roots in &orders {
        let root_strs: Vec<String> = roots
            .iter()
            .map(|p| p.to_string_lossy().to_string())
            .collect();
        let root_refs: Vec<&str> = root_strs.iter().map(|s| s.as_str()).collect();
        let manifests = loader.discover(&root_refs).await.unwrap();
        assert_eq!(
            manifests.len(),
            1,
            "同 id 多候选必须合并为单 manifest（roots 序: {roots:?}）"
        );
        assert_eq!(
            manifests[0].plugin_type,
            PluginType::Tool,
            "version-max 必须选 1.1.0（roots 序: {roots:?}）"
        );
        // 源码目录稳定落在最高版本副本——代码指纹/复验驱逐的触发源。
        let dir = loader.get_plugin_dir("mode_x").unwrap();
        assert!(
            std::path::Path::new(&dir).starts_with(tier2.path()),
            "source dir 必须在最高版本副本（roots 序: {roots:?}），got: {dir}"
        );
    }
}

/// 同 id 多候选归约（分级单副本模型，ADR 2026-11-07 §2.5 双源裁决退役）：
/// version-max、持平保留在位者——跨根双源已由启动守卫拒启，本套件只钉
/// 「同根畸形布局 / 守卫未及的热发现窗口」下归约的确定性与序无关性。
///
/// 实测沿革（BUG-55/R190）：用户空间陈旧副本曾靠「用户根恒赢」压过打包
/// 新版——该形态在分级模型下被守卫在启动期整体拒绝，版本闸不再存在于
/// 裁决层（移交基线对账）。

#[tokio::test]
async fn test_same_id_duplicate_higher_version_wins_order_independent() {
    let root = tempfile::tempdir().unwrap();
    // 同一根树内两份同 id（flat 1.0.0 + nested 1.1.0）：版本最高者赢，
    // 与 root_paths 迭代序无关（多候选逐对归约单调）。
    let flat = root.path().join("flat-layout");
    let nested = root.path().join("nested-group");
    create_test_plugin_dir_with_version(&flat, "dup_a", "pipeline", "1.0.0");
    create_test_plugin_dir_with_version(&nested, "dup_a", "pipeline", "1.1.0");

    let loader = PluginLoaderImpl::new(root.path(), None);
    let orders = [
        vec![flat.clone(), nested.clone()],
        vec![nested.clone(), flat.clone()],
    ];
    for roots in &orders {
        let root_strs: Vec<String> = roots
            .iter()
            .map(|p| p.to_string_lossy().to_string())
            .collect();
        let root_refs: Vec<&str> = root_strs.iter().map(|s| s.as_str()).collect();
        let manifests = loader.discover(&root_refs).await.unwrap();
        assert_eq!(
            manifests.len(),
            1,
            "同 id 多候选必须合并（roots 序: {roots:?}）"
        );
        assert_eq!(
            manifests[0].version, "1.1.0",
            "version-max：最高版本必须赢（roots 序: {roots:?}）"
        );
        let dir = loader.get_plugin_dir("dup_a").unwrap();
        assert!(
            std::path::Path::new(&dir).starts_with(&nested),
            "胜者 = 最高版本副本，与序无关（roots 序: {roots:?}），got: {dir}"
        );
    }
}

#[tokio::test]
async fn test_same_id_duplicate_tie_keeps_incumbent_within_one_scan() {
    let root = tempfile::tempdir().unwrap();
    let flat = root.path().join("flat-layout");
    let nested = root.path().join("nested-group");
    create_test_plugin_dir_with_version(&flat, "dup_tie", "pipeline", "1.0.0");
    create_test_plugin_dir_with_version(&nested, "dup_tie", "pipeline", "1.0.0");

    let loader = PluginLoaderImpl::new(root.path(), None);
    let roots = [
        flat.to_string_lossy().to_string(),
        nested.to_string_lossy().to_string(),
    ];
    let root_refs: Vec<&str> = roots.iter().map(|s| s.as_str()).collect();
    loader.discover(&root_refs).await.unwrap();

    // 持平（含 v 前缀宽容解析后持平）：在位者保留——单次扫描内确定即可
    // （分级模型下跨根同 id 由守卫拒启，此处只锁单根畸形布局不随扫描序
    // 逐轮翻面）。
    let dir_first = loader.get_plugin_dir("dup_tie").unwrap();
    loader.discover(&root_refs).await.unwrap();
    let dir_second = loader.get_plugin_dir("dup_tie").unwrap();
    assert_eq!(
        dir_first, dir_second,
        "同布局重复 discover 胜者必须稳定（无策略层翻面），got {dir_first} vs {dir_second}"
    );
}

/// 用户侧同 id 两副本（平铺 1.1.0 + modes 残留 1.0.0）：版本最高者赢，
/// 与序无关——modes 残留形态在守卫通过后的窗口内仍不得靠扫描序翻面。
#[tokio::test]
async fn test_same_id_multi_source_version_max_beats_modes_residual() {
    let user = tempfile::tempdir().unwrap();
    let user_flat = user.path().join("flat-layout");
    let user_modes = user.path().join("modes");
    create_test_plugin_dir_with_version(&user_flat, "mode_side", "pipeline", "1.1.0");
    create_test_plugin_dir_with_version(&user_modes, "mode_side", "pipeline", "1.0.0");

    let tier1 = tempfile::tempdir().unwrap();
    let loader = PluginLoaderImpl::new(tier1.path(), Some(user.path().to_path_buf()));

    let orders = [
        vec![user_flat.clone(), user_modes.clone()],
        vec![user_modes.clone(), user_flat.clone()],
    ];
    for roots in &orders {
        let root_strs: Vec<String> = roots
            .iter()
            .map(|p| p.to_string_lossy().to_string())
            .collect();
        let root_refs: Vec<&str> = root_strs.iter().map(|s| s.as_str()).collect();
        loader.discover(&root_refs).await.unwrap();
        let dir = loader.get_plugin_dir("mode_side").unwrap();
        assert!(
            std::path::Path::new(&dir).starts_with(&user_flat),
            "version-max 必须选平铺 1.1.0（roots 序: {roots:?}），got: {dir}"
        );
    }
}

/// Tier2 根仍是「Tier1 没有的 id」装载面：第三方/用户自有 id 照常从
/// Tier2 根装载（安装面语义不变，与策略无关）。
#[tokio::test]
async fn test_user_only_id_still_loads_from_tier2_root() {
    let tier1 = tempfile::tempdir().unwrap();
    let tier2 = tempfile::tempdir().unwrap();
    create_test_plugin_dir(tier1.path(), "tier1_only", "system");
    create_test_plugin_dir(tier2.path(), "third_party", "tool");

    let loader = PluginLoaderImpl::new(tier1.path(), Some(tier2.path().to_path_buf()));
    loader.discover(&[]).await.unwrap();

    let dir = loader.get_plugin_dir("third_party").unwrap();
    assert!(
        std::path::Path::new(&dir).starts_with(tier2.path()),
        "Tier1 没有的 id 必须从 Tier2 根装载，got: {dir}"
    );
    assert!(loader.get_plugin_dir("tier1_only").is_some());
}

/// 跨根同 id 在 loader 层仍可机械归约（version-max），但生产形态由启动
/// 守卫拒启——本测试钉 loader 兜底行为不回退成扫描序敏感。
#[tokio::test]
async fn test_cross_root_same_id_falls_to_version_max_not_scan_order() {
    let tier1 = tempfile::tempdir().unwrap();
    let tier2 = tempfile::tempdir().unwrap();
    create_test_plugin_dir_with_version(tier1.path(), "both_roots", "system", "1.0.0");
    create_test_plugin_dir_with_version(tier2.path(), "both_roots", "system", "2.0.0");

    let loader = PluginLoaderImpl::new(tier1.path(), Some(tier2.path().to_path_buf()));
    let orders = [
        vec![],
        vec![tier1.path().to_string_lossy().to_string()],
        vec![tier2.path().to_string_lossy().to_string()],
    ];
    for roots in &orders {
        let root_refs: Vec<&str> = roots.iter().map(|s| s.as_str()).collect();
        let manifests = loader.discover(&root_refs).await.unwrap();
        assert_eq!(manifests.len(), 1);
        assert_eq!(
            manifests[0].version, "2.0.0",
            "双根兜底归约恒取最高版本（Tier2 2.0.0），roots={roots:?}"
        );
    }
}

#[tokio::test]
async fn test_load_and_unload() {
    let builtin = tempfile::tempdir().unwrap();
    create_test_plugin_dir(builtin.path(), "test_load", "pipeline");

    let loader = PluginLoaderImpl::new(builtin.path(), None);
    loader.discover(&[]).await.unwrap();

    // 初始状态：Discovered
    assert_eq!(loader.get_status("test_load"), PluginStatus::Discovered);

    // 加载
    let loaded = loader.load("test_load").await.unwrap();
    assert_eq!(loaded.status, PluginStatus::Active);
    assert_eq!(loader.get_status("test_load"), PluginStatus::Active);

    // 重复加载返回已加载的
    let loaded2 = loader.load("test_load").await.unwrap();
    assert_eq!(loaded2.status, PluginStatus::Active);

    // 卸载
    loader.unload("test_load").await.unwrap();
    assert_eq!(loader.get_status("test_load"), PluginStatus::Discovered);
}

#[tokio::test]
async fn test_get_plugin_dir_returns_correct_path() {
    let builtin = tempfile::tempdir().unwrap();
    create_test_plugin_dir(builtin.path(), "dir_test_plugin", "pipeline");

    let loader = PluginLoaderImpl::new(builtin.path(), None);
    loader.discover(&[]).await.unwrap();

    let plugin_dir = loader.get_plugin_dir("dir_test_plugin");
    assert!(
        plugin_dir.is_some(),
        "get_plugin_dir should return Some for discovered plugin"
    );
    let dir = plugin_dir.unwrap();
    assert!(
        dir.ends_with("dir_test_plugin"),
        "plugin dir should end with plugin id, got: {dir}"
    );
}

#[tokio::test]
async fn test_get_plugin_dir_nonexistent_returns_none() {
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
    // 未 discover 的插件应返回 None
    assert!(loader.get_plugin_dir("nonexistent_plugin").is_none());
}

#[tokio::test]
async fn test_get_plugin_dir_existing() {
    let builtin = tempfile::tempdir().unwrap();
    create_test_plugin_dir(builtin.path(), "dir_test", "pipeline");

    let loader = PluginLoaderImpl::new(builtin.path(), None);
    loader.discover(&[]).await.unwrap();

    let plugin_dir = loader.get_plugin_dir("dir_test");
    assert!(
        plugin_dir.is_some(),
        "get_plugin_dir should return Some for discovered plugin"
    );
    let dir = plugin_dir.unwrap();
    assert!(
        dir.ends_with("dir_test"),
        "plugin dir should end with plugin id, got: {dir}"
    );
}

#[tokio::test]
async fn test_get_plugin_dir_nonexistent() {
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
    // 未 discover 时返回 None
    assert!(loader.get_plugin_dir("no_such_plugin").is_none());
}

#[tokio::test]
async fn test_load_nonexistent_plugin() {
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
    let result = loader.load("nonexistent").await;
    assert!(result.is_err());
}

#[tokio::test]
async fn test_unload_not_loaded() {
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
    let result = loader.unload("not_loaded").await;
    assert!(result.is_err());
}

#[test]
fn test_manifest_with_requires_content() {
    // ADR ⑦: requires_content 字段
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
    let manifest = PluginManifest {
        force_include_tools: Vec::new(),
        state: None,
        id: "memory_read".to_string(),
        name: "Memory Read".to_string(),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::Pipeline,
        language: "rust".to_string(),
        host_type: HostType::InProcess,
        host_group: None,
        entry: "memory_read".to_string(),
        capabilities: Default::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 100,
        mcp: None,
        lifecycle: None,
        native: None,
        requires_content: Some(2),
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        restricted_capabilities: Vec::new(),
        enabled: None,
        activation: None,
        provides: None,
        visibility: Default::default(),
        persistent_fields: vec![],
        export_fields: vec![],
        aux_venvs: Vec::new(),
    };
    assert!(loader.validate_manifest(&manifest).is_ok());
    assert_eq!(manifest.requires_content, Some(2));
}

#[test]
fn test_manifest_parses_provides_capabilities() {
    // M4: plugin.json 的 provides.capabilities 声明应被正确反序列化。
    // 验证：声明了 provides 的 manifest，provides 字段非 None 且内容正确。
    let json = r#"{
            "id": "human_interaction_service",
            "name": "Human Interaction Service",
            "version": "1.0.0",
            "plugin_type": "system",
            "language": "python",
            "host_type": "sidecar",
            "entry": "python server.py",
            "capabilities": {"tools": [], "lifecycle_hooks": ["on_load"]},
            "provides": {
                "capabilities": [
                    {
                        "namespace": "human-interaction",
                        "methods": ["create_choice", "wait_for_choice", "respond", "cancel"],
                        "host": "in-process"
                    }
                ]
            }
        }"#;
    let manifest: PluginManifest = serde_json::from_str(json).unwrap();
    let provides = manifest.provides.expect("provides 应被解析");
    assert_eq!(provides.capabilities.len(), 1);
    let cap = &provides.capabilities[0];
    assert_eq!(cap.namespace, "human-interaction");
    assert_eq!(
        cap.methods,
        vec!["create_choice", "wait_for_choice", "respond", "cancel"]
    );
    assert_eq!(cap.host, ProvidedCapabilityHost::InProcess);
}

#[test]
fn test_manifest_without_provides_defaults_to_none() {
    // 向后兼容：旧 plugin.json 不含 provides 字段时，解析为 None，不报错。
    let json = r#"{
            "id": "legacy_plugin",
            "name": "Legacy",
            "version": "1.0.0",
            "plugin_type": "tool",
            "language": "python",
            "host_type": "sidecar",
            "entry": "python server.py",
            "capabilities": {"tools": [{"name": "legacy_tool"}]}
        }"#;
    let manifest: PluginManifest = serde_json::from_str(json).unwrap();
    assert!(manifest.provides.is_none(), "无 provides 字段应解析为 None");
}

#[test]
fn test_manifest_provides_defaults_host_to_inprocess() {
    // host 字段缺省时（#[serde(default)])应为 InProcess。
    let json = r#"{
            "id": "p1", "name": "P1", "version": "1.0.0",
            "plugin_type": "system", "language": "python",
            "host_type": "sidecar", "entry": "s.py",
            "capabilities": {"tools": []},
            "provides": {"capabilities": [{"namespace": "my-cap", "methods": ["do"]}]}
        }"#;
    let manifest: PluginManifest = serde_json::from_str(json).unwrap();
    let cap = &manifest.provides.unwrap().capabilities[0];
    assert_eq!(
        cap.host,
        ProvidedCapabilityHost::InProcess,
        "host 缺省应为 InProcess"
    );
}

// ── 配置加载测试 ──

#[tokio::test]
async fn test_load_config_no_config_root_returns_empty() {
    let _user_space = factory_only_user_space();
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
    let config = loader.load_config().await.unwrap();
    assert_eq!(config, serde_json::json!({}));
}

#[tokio::test]
async fn test_load_config_nonexistent_dir_returns_empty() {
    let _user_space = factory_only_user_space();
    let loader =
        PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root("/tmp/no_such_dir");
    let config = loader.load_config().await.unwrap();
    assert_eq!(config, serde_json::json!({}));
}

#[tokio::test]
async fn test_load_config_reads_yaml_files() {
    let _user_space = factory_only_user_space();
    let config_dir = tempfile::tempdir().unwrap();

    // 写入两个 YAML 配置文件
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

    let loader =
        PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

    let config = loader.load_config().await.unwrap();
    let obj = config.as_object().unwrap();

    assert_eq!(obj.len(), 2);
    assert!(obj.contains_key("memory_storage"));
    assert!(obj.contains_key("api_config"));

    // 验证 YAML 内容正确解析
    let mem = obj.get("memory_storage").unwrap();
    assert_eq!(mem["storage_backend"], "sqlite");
    assert_eq!(mem["cache_size"], 1000);
}

#[tokio::test]
async fn test_load_config_handles_nested_dirs() {
    let _user_space = factory_only_user_space();
    let config_dir = tempfile::tempdir().unwrap();
    let sub_dir = config_dir.path().join("isolation");
    fs::create_dir_all(&sub_dir).unwrap();

    // 根目录 YAML
    fs::write(
        config_dir.path().join("memory_storage.yaml"),
        "backend: sqlite\n",
    )
    .unwrap();
    // 子目录 YAML
    fs::write(
        sub_dir.join("isolation_config.yaml"),
        "level: strict\ncontainers: 5\n",
    )
    .unwrap();

    let loader =
        PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

    let config = loader.load_config().await.unwrap();
    let obj = config.as_object().unwrap();

    assert_eq!(obj.len(), 2);
    assert!(obj.contains_key("memory_storage"));
    assert!(obj.contains_key("isolation"));

    // 子目录合并为嵌套对象
    let isolation = obj.get("isolation").unwrap().as_object().unwrap();
    assert_eq!(isolation.len(), 1);
    assert!(isolation.contains_key("isolation_config"));
    assert_eq!(isolation["isolation_config"]["level"], "strict");
}

#[tokio::test]
async fn test_load_config_ignores_non_yaml_files() {
    let _user_space = factory_only_user_space();
    let config_dir = tempfile::tempdir().unwrap();

    fs::write(config_dir.path().join("config.yaml"), "key: value\n").unwrap();
    fs::write(config_dir.path().join("readme.md"), "# Not a config\n").unwrap();
    fs::write(
        config_dir.path().join("data.json"),
        "{\"key\": \"value\"}\n",
    )
    .unwrap();

    let loader =
        PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

    let config = loader.load_config().await.unwrap();
    let obj = config.as_object().unwrap();

    // 只有 YAML 文件被收录
    assert_eq!(obj.len(), 1);
    assert!(obj.contains_key("config"));
    assert!(!obj.contains_key("readme"));
    assert!(!obj.contains_key("data"));
}

#[tokio::test]
async fn test_load_config_yml_extension() {
    let _user_space = factory_only_user_space();
    let config_dir = tempfile::tempdir().unwrap();

    fs::write(config_dir.path().join("short.yml"), "name: test\n").unwrap();

    let loader =
        PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

    let config = loader.load_config().await.unwrap();
    let obj = config.as_object().unwrap();

    assert_eq!(obj.len(), 1);
    assert!(obj.contains_key("short"));
}

#[tokio::test]
async fn test_load_config_empty_yaml_dir() {
    let _user_space = factory_only_user_space();
    let config_dir = tempfile::tempdir().unwrap();
    // 空目录

    let loader =
        PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

    let config = loader.load_config().await.unwrap();
    assert_eq!(config, serde_json::json!({}));
}

#[tokio::test]
async fn test_load_config_deep_nested_dirs() {
    let _user_space = factory_only_user_space();
    // 深层嵌套（≥ 2 层目录）
    let config_dir = tempfile::tempdir().unwrap();
    let deep_dir = config_dir.path().join("system").join("subsystem");
    fs::create_dir_all(&deep_dir).unwrap();

    fs::write(
        config_dir.path().join("root_config.yaml"),
        "root_key: root_value\n",
    )
    .unwrap();
    fs::write(deep_dir.join("deep_config.yaml"), "deep_key: deep_value\n").unwrap();

    let loader =
        PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

    let config = loader.load_config().await.unwrap();
    let obj = config.as_object().unwrap();

    assert_eq!(obj.len(), 2);
    assert!(obj.contains_key("root_config"));
    assert!(obj.contains_key("system"));

    // 第二层
    let system = obj.get("system").unwrap().as_object().unwrap();
    assert!(system.contains_key("subsystem"));

    // 第三层
    let subsystem = system.get("subsystem").unwrap().as_object().unwrap();
    assert!(subsystem.contains_key("deep_config"));
    assert_eq!(subsystem["deep_config"]["deep_key"], "deep_value");
}

#[tokio::test]
async fn test_load_config_same_stem_across_dirs() {
    let _user_space = factory_only_user_space();
    // 同名文件跨目录：根目录和子目录都有同名 yaml
    let config_dir = tempfile::tempdir().unwrap();
    let sub_dir = config_dir.path().join("sub");
    fs::create_dir_all(&sub_dir).unwrap();

    // 根目录有 config.yaml
    fs::write(config_dir.path().join("config.yaml"), "from: root\n").unwrap();
    // 子目录也有 config.yaml
    fs::write(sub_dir.join("config.yaml"), "from: sub\n").unwrap();

    let loader =
        PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

    let config = loader.load_config().await.unwrap();
    let obj = config.as_object().unwrap();

    // 根目录的 config.yaml 作为顶层 key
    assert!(obj.contains_key("config"));
    assert_eq!(obj["config"]["from"], "root");

    // 子目录的 config.yaml 在 sub 子对象下（不会覆盖）
    assert!(obj.contains_key("sub"));
    let sub = obj.get("sub").unwrap().as_object().unwrap();
    assert!(sub.contains_key("config"));
    assert_eq!(sub["config"]["from"], "sub");
}

#[tokio::test]
async fn test_load_config_empty_subdir_not_collected() {
    let _user_space = factory_only_user_space();
    // 空子目录不应出现在结果中
    let config_dir = tempfile::tempdir().unwrap();
    let empty_sub = config_dir.path().join("empty_dir");
    fs::create_dir_all(&empty_sub).unwrap();

    fs::write(config_dir.path().join("config.yaml"), "key: value\n").unwrap();

    let loader =
        PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

    let config = loader.load_config().await.unwrap();
    let obj = config.as_object().unwrap();

    // 空子目录不应出现
    assert_eq!(obj.len(), 1);
    assert!(obj.contains_key("config"));
    assert!(!obj.contains_key("empty_dir"));
}

use self::managed::factory_only_user_space;

/// 写一个只关心 visibility 的最小 manifest（其余字段取可装载最小值）。
fn write_visibility_manifest(root: &Path, id: &str, visibility: Option<&str>) {
    let dir = root.join(id);
    fs::create_dir_all(&dir).unwrap();
    let visibility_field = visibility
        .map(|v| format!(",\n    \"visibility\": \"{v}\""))
        .unwrap_or_default();
    fs::write(
        dir.join("plugin.json"),
        format!(
            r#"{{
    "id": "{id}",
    "name": "Test Plugin {id}",
    "version": "1.0.0",
    "plugin_type": "system",
    "language": "rust",
    "host_type": "in_process",
    "entry": "test_plugin",
    "capabilities": {{}},
    "priority": 100{visibility_field}
}}"#
        ),
    )
    .unwrap();
}

/// visibility 契约（批 2 private 机制，ADR 2026-11-18 决策 5）：private 是
/// 可见性属性不是能力属性——私有点位在装载面不过滤，照常发现/可加载；
/// 缺省字段（旧 manifest）按公开宽容读。
#[tokio::test]
async fn test_visibility_private_discovered_and_legacy_defaults_public() {
    let root = tempfile::tempdir().unwrap();
    write_visibility_manifest(root.path(), "private_one", Some("private"));
    write_visibility_manifest(root.path(), "legacy_one", None);

    let loader = PluginLoaderImpl::new(root.path(), None);
    let manifests = loader.discover(&[]).await.unwrap();

    assert_eq!(manifests.len(), 2, "private 声明不得在装载面被过滤");
    let find = |id: &str| {
        manifests
            .iter()
            .find(|m| m.id == id)
            .unwrap_or_else(|| panic!("{id} 应被发现"))
    };
    assert_eq!(find("private_one").visibility, PluginVisibility::Private);
    assert_eq!(
        find("legacy_one").visibility,
        PluginVisibility::Public,
        "无 visibility 的旧 manifest = 默认可见（宽容读）"
    );
}

/// 字段只增不删：未知取值（未来内核新增的可见性档）落在当前内核对不得
/// 拒载/跳过整个插件——按公开处理，插件照常装载。
#[tokio::test]
async fn test_visibility_unknown_value_does_not_skip_plugin() {
    let root = tempfile::tempdir().unwrap();
    write_visibility_manifest(root.path(), "future_one", Some("internal"));

    let loader = PluginLoaderImpl::new(root.path(), None);
    let manifests = loader.discover(&[]).await.unwrap();

    assert_eq!(manifests.len(), 1, "未知 visibility 取值不得导致插件被跳过");
    assert_eq!(manifests[0].visibility, PluginVisibility::Public);
}

mod managed;
