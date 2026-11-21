// @feature: FP-0.2.一 插件协议·plugin_check 契约校验引擎 | @ci: rust-test
//! 配置装载与受管目录测试（loader::tests 的后半段）。

use super::*;
use crate::loader::config_overlay::set_config_leaf;
#[tokio::test]
async fn test_load_config_mixed_yaml_and_yml() {
    let _user_space = factory_only_user_space();
    // 同一目录下同时有 .yaml 和 .yml 文件
    let config_dir = tempfile::tempdir().unwrap();

    fs::write(config_dir.path().join("long.yaml"), "type: yaml\n").unwrap();
    fs::write(config_dir.path().join("short.yml"), "type: yml\n").unwrap();

    let loader =
        PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

    let config = loader.load_config().await.unwrap();
    let obj = config.as_object().unwrap();

    assert_eq!(obj.len(), 2);
    assert!(obj.contains_key("long"));
    assert!(obj.contains_key("short"));
    assert_eq!(obj["long"]["type"], "yaml");
    assert_eq!(obj["short"]["type"], "yml");
}

/// 单个 YAML 文件解析失败时,不应连累整个 load_config 失败——
/// 应跳过该文件并继续加载其余文件(防御性降级 + 可观测 warn)。
///
/// 真实用例:config/templates/.agent_template_spec.yaml 是 Agent 配置模板
/// 文档(含 {placeholder}: 占位符 key),解析失败时若整体失败,所有插件
/// 都收不到配置(实证:0.2 内核启动后管道循环空转,每步 CONFIG_PARSE_ERROR)。
#[tokio::test]
async fn test_load_config_skips_unparseable_file_without_failing_others() {
    let _user_space = factory_only_user_space();
    let config_dir = tempfile::tempdir().unwrap();

    // 合法配置
    fs::write(config_dir.path().join("good.yaml"), "key: value\n").unwrap();
    // 非法 YAML(map key 用了占位符,serde_yaml 解析失败)
    fs::write(
        config_dir.path().join("bad.yaml"),
        "input_schema:\n  properties:\n    {placeholder}:\n      type: string\n",
    )
    .unwrap();

    let loader =
        PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

    // 关键:不返回 Err,而是跳过 bad.yaml 继续加载 good.yaml
    let config = loader.load_config().await.unwrap();
    let obj = config.as_object().unwrap();
    assert!(obj.contains_key("good"), "合法文件应正常加载");
    assert!(!obj.contains_key("bad"), "非法文件应被跳过,不让整体失败");
}

// ── 全量配置装载的 mtime 快照缓存（每次插件 spawn/工具调用都走
// load_config，装机实测 76 分钟 2415 次全量扫描构成分配 churn 洪峰）──

/// 缓存键语义（对外可观察契约）：指纹 = 文件集 × (路径, mtime, size)。
/// 指纹未变 → 返回快照（同长度同 mtime 原位改写不可检测）；真实改写
/// （mtime 必 bump）→ 下一次装载立见新值。watcher 语义保留：变更检测
/// 在每次 load 现场做，无 TTL 无延迟。
#[tokio::test]
async fn config_cache_hit_within_fingerprint_and_invalidates_on_real_edit() {
    let _user_space = factory_only_user_space();
    let config_dir = tempfile::tempdir().unwrap();
    let file = config_dir.path().join("svc.yaml");
    fs::write(&file, "k: one\n").unwrap();

    let loader =
        PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

    let first = loader.load_config().await.unwrap();
    assert_eq!(first["svc"]["k"], "one");

    // 同字节长度改写 + 显式还原 mtime：指纹不可检测 → 快照窗口内
    let mtime = fs::metadata(&file).unwrap().modified().unwrap();
    fs::write(&file, "k: two\n").unwrap();
    std::fs::File::options()
        .write(true)
        .open(&file)
        .unwrap()
        .set_modified(mtime)
        .unwrap();
    let second = loader.load_config().await.unwrap();
    assert_eq!(
        second["svc"]["k"], "one",
        "指纹未变应命中缓存（不重读内容）"
    );

    // 真实改写（长度与 mtime 双变）→ 必须失效重载
    fs::write(&file, "k: three-much-longer\n").unwrap();
    let third = loader.load_config().await.unwrap();
    assert_eq!(
        third["svc"]["k"], "three-much-longer",
        "mtime 变更必须失效重载"
    );
}

/// 文件集变更（新增/删除）必然改指纹：新文件进下一次装载、被删文件退出。
#[tokio::test]
async fn config_cache_invalidates_on_file_add_and_remove() {
    let _user_space = factory_only_user_space();
    let config_dir = tempfile::tempdir().unwrap();
    fs::write(config_dir.path().join("a.yaml"), "x: one\n").unwrap();
    let loader =
        PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

    let cfg = loader.load_config().await.unwrap();
    assert_eq!(cfg.as_object().unwrap().len(), 1);

    fs::write(config_dir.path().join("b.yaml"), "y: two\n").unwrap();
    let cfg = loader.load_config().await.unwrap();
    assert_eq!(
        cfg.as_object().unwrap().len(),
        2,
        "新增文件应进入下一次装载"
    );
    assert_eq!(cfg["b"]["y"], "two");

    fs::remove_file(config_dir.path().join("b.yaml")).unwrap();
    let cfg = loader.load_config().await.unwrap();
    assert_eq!(
        cfg.as_object().unwrap().len(),
        1,
        "删除文件应退出下一次装载"
    );
    assert!(cfg.get("b").is_none());
}

/// 用户覆盖层的变更同样经指纹失效：改写立见新值、删除回退 factory 值
/// （缓存不得把用户层冻结在旧快照——装机"改了没生效"排查依赖此语义）。
#[tokio::test]
async fn config_cache_invalidates_on_user_overlay_edit_and_removal() {
    let _lock = user_space_env_lock();
    let factory = tempfile::tempdir().unwrap();
    let user = tempfile::tempdir().unwrap();
    fs::write(factory.path().join("llm.yaml"), "model: factory\n").unwrap();
    let user_file = user.path().join("llm.yaml");
    fs::write(&user_file, "model: user-v1\n").unwrap();

    let _g = UserRootGuard::set(user.path());
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(factory.path());

    let cfg = loader.load_config().await.unwrap();
    assert_eq!(cfg["llm"]["model"], "user-v1");

    // 改写值取不同长度：同 mtime 同 size 的原位改写是指纹盲窗（loader.rs
    // 已如实声明），全量疾跑下两次写入可落进同一 mtime tick 而漏判（实测
    // flake）。size 参与指纹，长度必异即必失效，不依赖 mtime 粒度。
    fs::write(&user_file, "model: user-v2-rewritten\n").unwrap();
    let cfg = loader.load_config().await.unwrap();
    assert_eq!(
        cfg["llm"]["model"], "user-v2-rewritten",
        "用户层改写应立即可见"
    );

    fs::remove_file(&user_file).unwrap();
    let cfg = loader.load_config().await.unwrap();
    assert_eq!(
        cfg["llm"]["model"], "factory",
        "用户层删除应回退 factory 值"
    );
}

/// 多触发合并：同一 loader 上并发多次 load_config（spawn 洪峰形态），
/// 全部成功且结果一致——单份快照承载全部触发，无撕裂中间态。
#[tokio::test]
async fn config_cache_concurrent_loads_consistent() {
    let _user_space = factory_only_user_space();
    let config_dir = tempfile::tempdir().unwrap();
    fs::write(config_dir.path().join("a.yaml"), "x: one\n").unwrap();
    fs::create_dir_all(config_dir.path().join("sub")).unwrap();
    fs::write(config_dir.path().join("sub/b.yaml"), "y: two\n").unwrap();

    let loader = std::sync::Arc::new(
        PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path()),
    );
    let expected = loader.load_config().await.unwrap();

    let mut handles = Vec::new();
    for _ in 0..8 {
        let loader = loader.clone();
        handles.push(tokio::spawn(
            async move { loader.load_config().await.unwrap() },
        ));
    }
    for h in handles {
        assert_eq!(h.await.unwrap(), expected, "并发装载结果应与首载一致");
    }
}

/// 用户配置层：**文件级整体替换**（ADR 2026-09-13-unified-user-root）。
///
/// 覆盖语义的两组区分度输入：被接管的文件取用户值（整体，不合并 field），
/// 未被接管的文件保持 factory 值——后者是关键：用户层同目录存在别的文件时，
/// 不得因目录级整体替换而抹掉兄弟文件的 factory 值。
#[tokio::test]
async fn user_config_overlay_replaces_files_not_directories() {
    let _lock = user_space_env_lock();
    let factory = tempfile::tempdir().unwrap();
    let user = tempfile::tempdir().unwrap();
    // factory: models/llm.yaml + models/embedding.yaml（同目录兄弟文件）
    fs::create_dir_all(factory.path().join("models")).unwrap();
    fs::write(
        factory.path().join("models/llm.yaml"),
        "model: factory-model\nnested: {a: 1, b: 2}\n",
    )
    .unwrap();
    fs::write(
        factory.path().join("models/embedding.yaml"),
        "provider: factory-embed\n",
    )
    .unwrap();
    // 用户层：只接管 llm.yaml
    fs::create_dir_all(user.path().join("models")).unwrap();
    fs::write(user.path().join("models/llm.yaml"), "model: user-model\n").unwrap();

    let _g = UserRootGuard::set(user.path());
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(factory.path());
    let config = loader.load_config().await.unwrap();
    let models = config.get("models").unwrap().as_object().unwrap();

    // 接管生效：整文件替换（nested 不参与合并，用户文件没有该键就不该有）
    let llm = models.get("llm").unwrap().as_object().unwrap();
    assert_eq!(llm.get("model").unwrap().as_str().unwrap(), "user-model");
    assert!(
        !llm.contains_key("nested"),
        "整体替换：factory 的键不得渗进用户文件（合并即两处存值）"
    );
    // 兄弟文件不受牵连：未被接管的 embedding 仍读 factory
    assert_eq!(
        models
            .get("embedding")
            .unwrap()
            .get("provider")
            .unwrap()
            .as_str()
            .unwrap(),
        "factory-embed",
        "同目录兄弟文件必须保持 factory 值（防目录级整体替换抹掉）"
    );
}

/// 用户层不存在该文件时不接管；用户层完全没有时读 factory（回落）。
#[tokio::test]
async fn user_config_overlay_absent_falls_back_to_factory() {
    let _lock = user_space_env_lock();
    let factory = tempfile::tempdir().unwrap();
    let user = tempfile::tempdir().unwrap();
    fs::write(factory.path().join("solo.yaml"), "v: factory\n").unwrap();

    // ① 用户层目录为空
    let _g = UserRootGuard::set(user.path());
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(factory.path());
    assert_eq!(
        loader
            .load_config()
            .await
            .unwrap()
            .get("solo")
            .unwrap()
            .get("v")
            .unwrap()
            .as_str()
            .unwrap(),
        "factory"
    );

    // ② 用户层有其它文件但没接管 solo
    fs::write(user.path().join("other.yaml"), "v: user\n").unwrap();
    let loader2 = PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(factory.path());
    let cfg = loader2.load_config().await.unwrap();
    assert_eq!(
        cfg.get("solo").unwrap().get("v").unwrap().as_str().unwrap(),
        "factory",
        "存在性判定按文件而非目录"
    );
    assert_eq!(
        cfg.get("other")
            .unwrap()
            .get("v")
            .unwrap()
            .as_str()
            .unwrap(),
        "user"
    );
}

/// 用户层坏文件 fail-safe：告警跳过并**保留 factory 值**，不让插件静默拿空配置。
#[tokio::test]
async fn user_config_overlay_bad_file_keeps_factory_value() {
    let _lock = user_space_env_lock();
    let factory = tempfile::tempdir().unwrap();
    let user = tempfile::tempdir().unwrap();
    fs::write(factory.path().join("cfg.yaml"), "v: factory\n").unwrap();
    fs::write(
        user.path().join("cfg.yaml"),
        "input_schema:\n  properties:\n    {placeholder}:\n      type: string\n",
    )
    .unwrap();

    let _g = UserRootGuard::set(user.path());
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(factory.path());
    assert_eq!(
        loader
            .load_config()
            .await
            .unwrap()
            .get("cfg")
            .unwrap()
            .get("v")
            .unwrap()
            .as_str()
            .unwrap(),
        "factory",
        "坏的用户文件应告警跳过并保留 factory 值"
    );
}

/// 嵌套目录键路径：`agents/main/agentos.yaml` → 键 `agents.main.agentos`
/// （与注入侧 resolve_config_path 按 `/` 下钻同构）。
#[tokio::test]
async fn user_config_overlay_nested_paths_map_to_key_paths() {
    let _lock = user_space_env_lock();
    let factory = tempfile::tempdir().unwrap();
    let user = tempfile::tempdir().unwrap();
    fs::create_dir_all(factory.path().join("agents/main")).unwrap();
    fs::write(
        factory.path().join("agents/main/agentos.yaml"),
        "n: factory\n",
    )
    .unwrap();
    fs::create_dir_all(user.path().join("agents/main")).unwrap();
    fs::write(user.path().join("agents/main/agentos.yaml"), "n: user\n").unwrap();

    let _g = UserRootGuard::set(user.path());
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(factory.path());
    let cfg = loader.load_config().await.unwrap();
    assert_eq!(
        cfg["agents"]["main"]["agentos"]["n"].as_str().unwrap(),
        "user",
        "嵌套路径应按目录层级映射到键路径"
    );
}

/// 只读 factory 的用例专用：持锁 + 把用户配置层钉到一个**全新空目录**。
///
/// `load_config` 会把用户层文件并入结果（ADR 2026-09-13-unified-user-root），
/// 故"只断言 factory 内容"的用例必须让用户层为空——否则会读到开发机
/// `%APPDATA%/agentos/config/` 的残留（实测断言条数 2 却得 4）。
///
/// 返回值须绑定到变量（guard 存活到用例结束）。
pub(super) struct FactoryOnlyUserSpace {
    _lock: UserSpaceEnvGuard,
    _tmp: tempfile::TempDir,
    original_cfg: Option<String>,
}

impl Drop for FactoryOnlyUserSpace {
    fn drop(&mut self) {
        match self.original_cfg.take() {
            Some(v) => {
                std::env::set_var(agentos_core::user_space::USER_CONFIG_DIR_ENV, v);
            }
            None => std::env::remove_var(agentos_core::user_space::USER_CONFIG_DIR_ENV),
        }
    }
}

pub(super) fn factory_only_user_space() -> FactoryOnlyUserSpace {
    let tmp = tempfile::tempdir().unwrap();
    let lock = user_space_env_lock();
    let original_cfg = std::env::var(agentos_core::user_space::USER_CONFIG_DIR_ENV).ok();
    std::env::set_var(
        agentos_core::user_space::USER_CONFIG_DIR_ENV,
        tmp.path().join("user-config"),
    );
    FactoryOnlyUserSpace {
        _lock: lock,
        _tmp: tmp,
        original_cfg,
    }
}

/// 隐藏文件(以 `.` 开头)不应被当作配置加载——
/// Unix 惯例隐藏文件是元数据/文档,不是常规配置。
/// 真实用例:.agent_template_spec.yaml 是 Agent 配置规范文档。
#[tokio::test]
async fn test_load_config_skips_hidden_files() {
    let _user_space = factory_only_user_space();
    let config_dir = tempfile::tempdir().unwrap();

    fs::write(config_dir.path().join("visible.yaml"), "key: value\n").unwrap();
    fs::write(
        config_dir.path().join(".hidden.yaml"),
        "hidden_key: hidden_value\n",
    )
    .unwrap();

    let loader =
        PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

    let config = loader.load_config().await.unwrap();
    let obj = config.as_object().unwrap();
    assert!(obj.contains_key("visible"), "常规文件应加载");
    assert!(
        !obj.contains_key(".hidden") && !obj.contains_key("hidden"),
        "隐藏文件(.前缀)应被跳过"
    );
}

// ── P2-2 插件准入白名单 + SHA256 校验测试 ──

/// strict 模式下，非白名单插件应被拒绝。
#[tokio::test]
async fn test_allowlist_strict_rejects_unknown() {
    let builtin = tempfile::tempdir().unwrap();
    // 写一个插件，但它不在白名单里
    create_test_plugin_dir(builtin.path(), "unknown_plugin", "pipeline");

    // strict 模式 + 空白名单
    let allowlist = AllowlistConfig {
        mode: AllowlistMode::Strict,
        plugins: vec![],
        ..Default::default()
    };
    let loader = PluginLoaderImpl::new(builtin.path(), None).with_allowlist(allowlist);

    let manifests = loader.discover(&[]).await.unwrap();
    // 非白名单插件被 validate_manifest_internal 拒绝 → 不进入结果
    assert!(
        manifests.is_empty(),
        "strict mode should reject plugins not in allowlist"
    );
}

/// permissive 模式下，所有插件应放行（含非白名单插件）。
#[tokio::test]
async fn test_allowlist_permissive_allows_all() {
    let builtin = tempfile::tempdir().unwrap();
    create_test_plugin_dir(builtin.path(), "any_plugin_a", "pipeline");
    create_test_plugin_dir(builtin.path(), "any_plugin_b", "tool");

    // permissive 模式 + 空白名单（默认）
    let loader = PluginLoaderImpl::new(builtin.path(), None);

    let manifests = loader.discover(&[]).await.unwrap();
    assert_eq!(
        manifests.len(),
        2,
        "permissive mode should allow all plugins"
    );
    let ids: Vec<_> = manifests.iter().map(|m| m.id.clone()).collect();
    assert!(ids.contains(&"any_plugin_a".to_string()));
    assert!(ids.contains(&"any_plugin_b".to_string()));
}

/// 白名单条目声明了 sha256，实际内容不匹配时应被拒绝。
#[tokio::test]
async fn test_sha256_mismatch_rejected() {
    let builtin = tempfile::tempdir().unwrap();
    let plugin_dir = builtin.path().join("hashed_plugin");
    fs::create_dir_all(&plugin_dir).unwrap();

    // manifest 文件（pipeline 类型声明 invoke_entry，P6 discover 聚合校验）
    let manifest_content = r#"{
    "id": "hashed_plugin",
    "name": "Hashed Plugin",
    "version": "1.0.0",
    "plugin_type": "pipeline",
    "language": "python",
    "host_type": "sidecar",
    "entry": "python3 server.py",
    "capabilities": {},
    "requires_services": [],
    "permissions": {},
    "priority": 100,
    "invoke_entry": "hashed_plugin.execute"
}"#;
    let manifest_path = plugin_dir.join("plugin.json");
    fs::write(&manifest_path, manifest_content).unwrap();
    // 入口文件
    fs::write(plugin_dir.join("server.py"), "# entry file\n").unwrap();

    // 第一性原理重算目录树规范化哈希：本插件仅两文件，排序相对路径为
    // (plugin.json, server.py)，sha256 输入 = 各 `路径\0字节\0` 顺序拼接。
    let manifest_bytes = fs::read(&manifest_path).unwrap();
    let entry_bytes = fs::read(plugin_dir.join("server.py")).unwrap();
    let mut hasher = Sha256::new();
    hasher.update(b"plugin.json");
    hasher.update([0u8]);
    hasher.update(&manifest_bytes);
    hasher.update([0u8]);
    hasher.update(b"server.py");
    hasher.update([0u8]);
    hasher.update(&entry_bytes);
    hasher.update([0u8]);
    let correct_hash = format!("{:x}", hasher.finalize());

    // 1) 正确哈希 → 放行
    let allowlist_ok = AllowlistConfig {
        mode: AllowlistMode::Permissive,
        plugins: vec![AllowlistEntry {
            id: "hashed_plugin".to_string(),
            sha256: correct_hash.clone(),
            ..Default::default()
        }],
        ..Default::default()
    };
    let loader_ok = PluginLoaderImpl::new(builtin.path(), None).with_allowlist(allowlist_ok);
    let manifests = loader_ok.discover(&[]).await.unwrap();
    assert_eq!(
        manifests.len(),
        1,
        "matching sha256 should allow the plugin"
    );
    assert_eq!(manifests[0].id, "hashed_plugin");

    // 2) 故意改坏哈希 → 拒绝
    // 翻转第一个 hex 字符以构造不匹配（保持合法 hex）
    let mut chars = correct_hash.chars();
    let first = chars.next().unwrap();
    let flipped = if first == '0' { '1' } else { '0' };
    let broken_hash = format!("{}{}", flipped, &correct_hash[1..]);
    let allowlist_bad = AllowlistConfig {
        mode: AllowlistMode::Permissive,
        plugins: vec![AllowlistEntry {
            id: "hashed_plugin".to_string(),
            sha256: broken_hash,
            ..Default::default()
        }],
        ..Default::default()
    };
    let loader_bad = PluginLoaderImpl::new(builtin.path(), None).with_allowlist(allowlist_bad);
    let manifests_bad = loader_bad.discover(&[]).await.unwrap();
    assert!(
        manifests_bad.is_empty(),
        "sha256 mismatch should reject the plugin"
    );
}

// ── 准入分级（2026-09-25）：危险前端能力 grants / 剥权 ──

/// 写一个带危险前端能力声明的 tool 插件（skin / client_styles 可选开关）。
fn write_frontend_plugin(root: &Path, id: &str, with_skin: bool, with_client_styles: bool) {
    let dir = root.join(id);
    fs::create_dir_all(&dir).unwrap();
    let themes = if with_skin {
        r#"[{"id": "t1", "name": "T1", "base": "dark", "skin": "s1"}]"#
    } else {
        "[]"
    };
    let styles = if with_client_styles {
        r#"[{"id": "s", "path": "/assets/x.css"}]"#
    } else {
        "[]"
    };
    let manifest = format!(
        r#"{{
    "id": "{id}",
    "name": "Frontend {id}",
    "version": "1.0.0",
    "plugin_type": "tool",
    "language": "python",
    "host_type": "sidecar",
    "entry": "python server.py",
    "capabilities": {{}},
    "contributes": {{"themes": {themes}, "client_styles": {styles}}}
}}"#
    );
    fs::write(dir.join("plugin.json"), manifest).unwrap();
}

/// 内容静态检测：themes[].skin 非空 → host_js；client_styles 非空数组 →
/// host_css；空串/空数组/缺失 → 不算声明。
#[test]
fn detect_frontend_dangerous_caps_by_manifest_content() {
    use serde_json::json;
    assert!(
        detect_frontend_dangerous_caps(None).is_empty(),
        "无 contributes 无危险能力"
    );
    assert!(
        detect_frontend_dangerous_caps(Some(&json!({"themes": []}))).is_empty(),
        "空 themes 无危险能力"
    );
    assert_eq!(
        detect_frontend_dangerous_caps(Some(&json!({"themes": [{"skin": "s1"}]}))),
        vec![CAP_HOST_JS],
        "skin 非空 → host_js"
    );
    assert!(
        detect_frontend_dangerous_caps(Some(&json!({"themes": [{"skin": ""}]}))).is_empty(),
        "空串 skin 不算声明"
    );
    assert_eq!(
        detect_frontend_dangerous_caps(Some(&json!({"client_styles": [{"id": "s"}]}))),
        vec![CAP_HOST_CSS],
        "非空 client_styles → host_css"
    );
    assert!(
        detect_frontend_dangerous_caps(Some(&json!({"client_styles": []}))).is_empty(),
        "空 client_styles 不算声明"
    );
    assert_eq!(
        detect_frontend_dangerous_caps(Some(
            &json!({"themes": [{"skin": "s1"}], "client_styles": [{"id": "s"}]})
        )),
        vec![CAP_HOST_JS, CAP_HOST_CSS],
        "双能力按词表序"
    );
}

/// 装机姿态（BuiltinFirst）：用户根插件未列入 allowlist → 危险能力全剥 +
/// restricted_capabilities 记录；内置根同款声明 → 原样（可信面不剥）。
#[tokio::test]
async fn builtin_first_strips_ungranted_dangerous_caps_for_user_plugins() {
    let builtin = tempfile::tempdir().unwrap();
    let user = tempfile::tempdir().unwrap();
    write_frontend_plugin(user.path(), "skin_user", true, true);
    write_frontend_plugin(builtin.path(), "skin_builtin", true, true);

    let allowlist = AllowlistConfig {
        mode: AllowlistMode::Permissive,
        plugins: vec![],
        deployed: true,
    };
    let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()))
        .with_allowlist(allowlist);
    let manifests = loader.discover(&[]).await.unwrap();

    let user_manifest = manifests
        .iter()
        .find(|m| m.id == "skin_user")
        .expect("user plugin admitted");
    assert_eq!(
        user_manifest.restricted_capabilities,
        vec![CAP_HOST_JS.to_string(), CAP_HOST_CSS.to_string()],
        "未授予应按词表序记录剥权"
    );
    let user_contributes = user_manifest.contributes.as_ref().unwrap();
    assert!(
        user_contributes["themes"][0].get("skin").is_none(),
        "skin 字段应被剥除"
    );
    assert!(
        user_contributes.get("client_styles").is_none(),
        "client_styles 应被整键剥除"
    );

    let builtin_manifest = manifests
        .iter()
        .find(|m| m.id == "skin_builtin")
        .expect("builtin plugin admitted");
    assert!(
        builtin_manifest.restricted_capabilities.is_empty(),
        "内置根插件不应剥权"
    );
    assert!(
        builtin_manifest
            .contributes
            .as_ref()
            .unwrap()
            .get("client_styles")
            .is_some(),
        "内置根 client_styles 应原样保留"
    );
}

/// allowlist 未部署（deployed=false，引导默认）→ 剥权面未启用全授予——
/// 根归属信任边界（ADR 2026-11-07 §2.1）只对已部署白名单生效。
#[tokio::test]
async fn allowlist_not_deployed_keeps_dangerous_caps() {
    let builtin = tempfile::tempdir().unwrap();
    let user = tempfile::tempdir().unwrap();
    write_frontend_plugin(user.path(), "skin_dev", true, true);

    let allowlist = AllowlistConfig {
        mode: AllowlistMode::Permissive,
        plugins: vec![],
        deployed: false,
    };
    let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()))
        .with_allowlist(allowlist);
    let manifests = loader.discover(&[]).await.unwrap();
    let manifest = manifests.iter().find(|m| m.id == "skin_dev").unwrap();
    assert!(
        manifest.restricted_capabilities.is_empty(),
        "白名单未部署应全授予"
    );
    assert!(
        manifest
            .contributes
            .as_ref()
            .unwrap()
            .get("client_styles")
            .is_some(),
        "未部署时 client_styles 应原样"
    );
}

/// allowlist 条目按 grants 部分授予：未授予项剥、已授予项保留。
#[tokio::test]
async fn allowlist_grants_selective() {
    let builtin = tempfile::tempdir().unwrap();
    let user = tempfile::tempdir().unwrap();
    write_frontend_plugin(user.path(), "skin_partial", true, true);

    let allowlist = AllowlistConfig {
        mode: AllowlistMode::Permissive,
        plugins: vec![AllowlistEntry {
            id: "skin_partial".to_string(),
            sha256: String::new(),
            grants: vec![CAP_HOST_CSS.to_string()],
        }],
        deployed: true,
    };
    let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()))
        .with_allowlist(allowlist);
    let manifests = loader.discover(&[]).await.unwrap();
    let manifest = manifests.iter().find(|m| m.id == "skin_partial").unwrap();
    assert_eq!(
        manifest.restricted_capabilities,
        vec![CAP_HOST_JS.to_string()],
        "只记录未授予项"
    );
    let contributes = manifest.contributes.as_ref().unwrap();
    assert!(
        contributes["themes"][0].get("skin").is_none(),
        "host_js 未授予应剥 skin"
    );
    assert!(
        contributes.get("client_styles").is_some(),
        "host_css 已授予应保留"
    );
}

/// 播种副本（用户根同 id 同版本）不被剥权不打警告——剥权只对裁决胜者
/// 执行（2026-10-23 修正）：BuiltinFirst 持平内置赢，生效面=内置副本
/// （builtin 路径不走用户根判定），未胜出的播种副本早于裁决的剥权是
/// 纯噪声（mode_roleplay 48 次/日实证，功能从未丢失）。
#[tokio::test]
async fn seeded_user_copy_not_stripped_when_builtin_wins() {
    let builtin = tempfile::tempdir().unwrap();
    let user = tempfile::tempdir().unwrap();
    write_frontend_plugin(builtin.path(), "mode_seed", false, true);
    write_frontend_plugin(user.path(), "mode_seed", false, true);

    let allowlist = AllowlistConfig {
        mode: AllowlistMode::Permissive,
        plugins: vec![], // 无条目：若误剥，client_styles 必失
        deployed: true,
    };
    let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()))
        .with_allowlist(allowlist);
    let manifests = loader.discover(&[]).await.unwrap();
    let manifest = manifests.iter().find(|m| m.id == "mode_seed").unwrap();

    assert!(
        manifest.restricted_capabilities.is_empty(),
        "内置胜者不剥权，实际: {:?}",
        manifest.restricted_capabilities
    );
    let contributes = manifest.contributes.as_ref().unwrap();
    assert!(
        contributes.get("client_styles").is_some(),
        "client_styles 应完整保留（注册面从未被剥）"
    );
}

// ── P6 命名治理（ADR 附录 D③/D.5）：discover 启动期聚合校验 invoke_entry ──

/// 辅助：写一个 sidecar pipeline manifest（可指定 invoke_entry）。
fn write_pipeline_plugin(root: &Path, id: &str, invoke_entry: Option<&str>) {
    let dir = root.join(id);
    fs::create_dir_all(&dir).unwrap();
    let entry_field = match invoke_entry {
        Some(e) => format!(",\n    \"invoke_entry\": \"{e}\""),
        None => String::new(),
    };
    let manifest_json = format!(
        r#"{{
    "id": "{id}",
    "name": "Pipeline {id}",
    "version": "1.0.0",
    "plugin_type": "pipeline",
    "language": "python",
    "host_type": "sidecar",
    "entry": "python server.py",
    "capabilities": {{}}{entry_field}
}}"#
    );
    fs::write(dir.join("plugin.json"), manifest_json).unwrap();
}

/// pipeline 插件声明了 invoke_entry → discover 成功，manifest 原样返回。
#[tokio::test]
async fn test_discover_pipeline_with_invoke_entry_succeeds() {
    let builtin = tempfile::tempdir().unwrap();
    write_pipeline_plugin(builtin.path(), "good_pipe", Some("good_pipe.execute"));

    let loader = PluginLoaderImpl::new(builtin.path(), None);
    let manifests = loader.discover(&[]).await.unwrap();
    assert_eq!(manifests.len(), 1);
    assert_eq!(
        manifests[0].invoke_entry.as_deref(),
        Some("good_pipe.execute")
    );
}

/// 缺 invoke_entry 的 pipeline 插件 → discover 启动期聚合报错（不逐个 panic）。
/// 错误消息必须列出所有缺失的插件 id（聚合，不是第一个就崩）。
#[tokio::test]
async fn test_discover_pipeline_missing_invoke_entry_aggregates_errors() {
    let builtin = tempfile::tempdir().unwrap();
    // 两个 pipeline 插件都缺 invoke_entry——聚合报错应一次列出两者
    write_pipeline_plugin(builtin.path(), "bad_pipe_one", None);
    write_pipeline_plugin(builtin.path(), "bad_pipe_two", None);

    let loader = PluginLoaderImpl::new(builtin.path(), None);
    let result = loader.discover(&[]).await;
    assert!(result.is_err(), "missing invoke_entry must fail discover");
    let err = result.unwrap_err();
    assert_eq!(err.code.as_deref(), Some("MISSING_INVOKE_ENTRY"));
    // 聚合：两个缺失插件都在错误消息里
    assert!(
        err.message.contains("bad_pipe_one"),
        "error must aggregate all missing plugins, got: {}",
        err.message
    );
    assert!(
        err.message.contains("bad_pipe_two"),
        "error must aggregate all missing plugins (not fail on first), got: {}",
        err.message
    );
}

/// tool 类型插件不需要 invoke_entry（它用 capabilities.tools[]）；
/// system 类型暂保留 tools[] 不强制 invoke_entry（D.6 双语义待评估）。
#[tokio::test]
async fn test_discover_tool_plugin_does_not_require_invoke_entry() {
    let builtin = tempfile::tempdir().unwrap();
    create_test_plugin_dir(builtin.path(), "a_tool", "tool");
    create_test_plugin_dir(builtin.path(), "a_system", "system");

    let loader = PluginLoaderImpl::new(builtin.path(), None);
    let manifests = loader.discover(&[]).await.unwrap();
    // tool 和 system 都不要求 invoke_entry，discover 成功
    assert_eq!(manifests.len(), 2);
}

/// R7 F3：discover 落服务声明缓存——service_provider_ids 按点式条目
/// （ns.method）解析提供方（llm.complete_stream → llm_service），供 ensure
/// 声明面依赖闭包展开。缓存键 = 完整服务名；bare 命名空间不入键（调用面
/// 展开只认点式精确声明，bare 属注册闸存在性语义）。
#[tokio::test]
async fn test_discover_populates_service_provider_cache() {
    let builtin = tempfile::tempdir().unwrap();
    let pdir = builtin.path().join("llm_service");
    fs::create_dir_all(&pdir).unwrap();
    fs::write(
            pdir.join("plugin.json"),
            r#"{"id":"llm_service","name":"LLM Service","version":"1.0.0","plugin_type":"system","language":"python","host_type":"sidecar","entry":"python server.py","capabilities":{"services":[{"name":"llm.complete_stream","description":"streaming completion"}]}}"#,
        )
        .unwrap();

    let loader = PluginLoaderImpl::new(builtin.path(), None);
    loader.discover(&[]).await.unwrap();

    assert_eq!(
        loader.service_provider_ids("llm.complete_stream"),
        vec!["llm_service".to_string()],
        "点式服务条目必须解析出声明提供方"
    );
    assert!(
        loader.service_provider_ids("llm").is_empty(),
        "bare 命名空间不入缓存键（不参与调用面展开）"
    );
}

// ── GAP-4：env 声明自动生成（引用本身即声明）────────────────

fn env_coverage_manifest_json(declare: bool) -> String {
    let files = if declare {
        r#""config_files": [{
                "id": "api_keys", "label": "Keys", "path": ".env", "target": "env",
                "fields": [{"name": "SMITHERY_API_KEY", "label": "Smithery", "type": "secret", "required": true}]
            }],"#
    } else {
        ""
    };
    format!(
        r#"{{"id": "cov_plugin", "name": "Cov", "version": "1.0.0",
            "plugin_type": "tool", "language": "external", "host_type": "sidecar", "entry": "mcp:external", "capabilities": {{}}, "requires_services": [], "permissions": {{}}, "priority": 30,
            {files}
            "mcp": {{
                "transport": "streamable_http",
                "endpoint": {{
                    "url": "https://example.com/mcp",
                    "auth": {{"type": "api_key", "header_name": "Authorization", "value": "{value}"}}
                }}
            }}}}"#,
        files = files,
        value = "${SMITHERY_API_KEY}"
    )
}

#[test]
fn test_env_declarations_auto_generated_when_undeclared() {
    // 未声明引用不再拒绝：校验通过 + 自动生成 env 条目（引用即声明）
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
    let mut manifest: PluginManifest =
        serde_json::from_str(&env_coverage_manifest_json(false)).unwrap();
    let r = loader.validate_manifest_internal(&mut manifest, Path::new("(test)"));
    assert!(r.is_ok(), "未声明引用应自动生成而非拒绝：{r:?}");
    let entry = manifest
        .config_files
        .iter()
        .find(|f| f.target.as_deref() == Some("env"))
        .expect("应自动生成 target=env 条目");
    assert_eq!(entry.path, ".env", "env 条目写入目标（.env）不可省");
    let field = entry
        .fields
        .iter()
        .find(|f| f.name == "SMITHERY_API_KEY")
        .expect("引用的 var 应生成对应 field");
    assert_eq!(field.field_type, "secret", "生成字段走保守掩码默认");
    assert!(!field.required, "生成字段不强制必填");
}

#[test]
fn test_env_declarations_merge_into_existing_entry() {
    // 已有 env 条目声明了别的 var → 新引用合并进同一条目，不另建
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
    let json = env_coverage_manifest_json(false).replace(
            r#""mcp": {"#,
            r#""config_files": [{
                "id": "api_keys", "label": "Keys", "path": ".env", "target": "env",
                "fields": [{"name": "OTHER_KEY", "label": "Other", "type": "string", "required": true}]
            }],
            "mcp": {"#,
        );
    let mut manifest: PluginManifest = serde_json::from_str(&json).unwrap();
    let r = loader.validate_manifest_internal(&mut manifest, Path::new("(test)"));
    assert!(r.is_ok(), "{r:?}");
    let env_entries: Vec<_> = manifest
        .config_files
        .iter()
        .filter(|f| f.target.as_deref() == Some("env"))
        .collect();
    assert_eq!(env_entries.len(), 1, "应合并进既有 env 条目而非新建");
    let names: Vec<_> = env_entries[0]
        .fields
        .iter()
        .map(|f| f.name.as_str())
        .collect();
    assert!(names.contains(&"OTHER_KEY") && names.contains(&"SMITHERY_API_KEY"));
}

#[test]
fn test_env_declarations_declared_not_duplicated() {
    // 手写声明仍在 → 校验通过且不重复生成
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
    let mut manifest: PluginManifest =
        serde_json::from_str(&env_coverage_manifest_json(true)).unwrap();
    let r = loader.validate_manifest_internal(&mut manifest, Path::new("(test)"));
    assert!(r.is_ok(), "声明覆盖应通过：{r:?}");
    let env_fields: Vec<_> = manifest
        .config_files
        .iter()
        .filter(|f| f.target.as_deref() == Some("env"))
        .flat_map(|f| f.fields.iter().map(|fd| fd.name.as_str()))
        .collect();
    assert_eq!(
        env_fields
            .iter()
            .filter(|n| **n == "SMITHERY_API_KEY")
            .count(),
        1,
        "已声明的 var 不应重复生成"
    );
}

#[test]
fn test_env_coverage_default_syntax_exempt() {
    // ${VAR:-def} 带默认值（可选凭据）豁免：不生成声明条目
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
    let json =
        env_coverage_manifest_json(false).replace("${SMITHERY_API_KEY}", "${OPTIONAL_KEY:-}");
    let mut manifest: PluginManifest = serde_json::from_str(&json).unwrap();
    assert!(loader
        .validate_manifest_internal(&mut manifest, Path::new("(test)"))
        .is_ok());
    assert!(
        !manifest
            .config_files
            .iter()
            .any(|f| f.target.as_deref() == Some("env")),
        "带默认值的引用不应生成声明"
    );
}

// ── state.reads 读面声明（schema 收录阶段）：合法透传 / 非法告警忽略 ──

fn state_reads_manifest_json(reads_entries: &str) -> String {
    format!(
        r#"{{"id": "reads_plugin", "name": "Reads", "version": "1.0.0",
            "plugin_type": "tool", "language": "external", "host_type": "sidecar",
            "entry": "mcp:external", "capabilities": {{}}, "requires_services": [],
            "permissions": {{}}, "priority": 30,
            "state": {{"volatile_keys": ["k.volatile"], "reads": [{reads_entries}]}}}}"#
    )
}

/// 三种合法形态（键名 / `messages` 全量 / `messages_tail:N`）解析成功并
/// 原样透传到运行时 manifest 视图。
#[test]
fn test_state_reads_valid_entries_pass_through() {
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
    let mut manifest: PluginManifest = serde_json::from_str(&state_reads_manifest_json(
        r#""task.status", "messages", "messages_tail:50""#,
    ))
    .unwrap();
    let r = loader.validate_manifest_internal(&mut manifest, Path::new("(test)"));
    assert!(r.is_ok(), "合法 reads 条目应通过校验：{r:?}");
    assert_eq!(
        manifest.state.as_ref().unwrap().reads,
        vec!["task.status", "messages", "messages_tail:50"],
        "合法条目应原样透传（不增不删不改写）"
    );
}

/// `messages_tail:N` 格式非法（N 非正整数）→ G2 装载告警并忽略该条目
/// （warn 不拒载），同 manifest 其余合法条目不受影响。
#[test]
fn test_state_reads_invalid_messages_tail_warns_and_ignored() {
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
    let mut manifest: PluginManifest = serde_json::from_str(&state_reads_manifest_json(
            r#""task.status", "messages_tail:x", "messages_tail:0", "messages_tail:", "messages_tail:1.5", "messages_tail:50""#,
        ))
        .unwrap();
    // warn 走 always-enabled 测试订阅者包裹（默认无订阅者时 warn! 文案
    // 不求值）；断言仍是语义结果（不拒载 + 非法条目被忽略），不断言日志
    let r = tracing::subscriber::with_default(AlwaysSubscriber, || {
        loader.validate_manifest_internal(&mut manifest, Path::new("(test)"))
    });
    assert!(
        r.is_ok(),
        "非法 messages_tail 条目应告警忽略而非拒载：{r:?}"
    );
    assert_eq!(
        manifest.state.as_ref().unwrap().reads,
        vec!["task.status", "messages_tail:50"],
        "非法条目（非数字/零/空/非整数）被忽略，合法邻居条目保留"
    );
}

/// 向后兼容：旧 manifest 的 state 段无 reads（或整段无 state）→ 校验全绿，
/// reads 缺省为空。
#[test]
fn test_state_reads_absent_backward_compat() {
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
    let legacy_state = r#"{"id": "legacy_state", "name": "L", "version": "1.0.0",
            "plugin_type": "tool", "language": "external", "host_type": "sidecar",
            "entry": "mcp:external", "capabilities": {},
            "state": {"volatile_keys": ["k.volatile"]}}"#;
    let mut m1: PluginManifest = serde_json::from_str(legacy_state).unwrap();
    assert!(loader
        .validate_manifest_internal(&mut m1, Path::new("(test)"))
        .is_ok());
    assert!(
        m1.state.as_ref().unwrap().reads.is_empty(),
        "旧 state 段（只有 volatile_keys）reads 缺省为空"
    );

    let no_state = r#"{"id": "no_state", "name": "N", "version": "1.0.0",
            "plugin_type": "tool", "language": "external", "host_type": "sidecar",
            "entry": "mcp:external", "capabilities": {}}"#;
    let mut m2: PluginManifest = serde_json::from_str(no_state).unwrap();
    assert!(loader
        .validate_manifest_internal(&mut m2, Path::new("(test)"))
        .is_ok());
    assert!(m2.state.is_none(), "无 state 段不受影响");
}

/// 契约闸门 2.5：native 产物预检——artifact 缺失 → 加载期明确报错（不是拖到
/// load/调用期才炸）。
#[test]
fn test_native_artifact_precheck_missing_rejected() {
    let dir = tempfile::tempdir().unwrap();
    let plugin_dir = dir.path().join("p_native");
    std::fs::create_dir_all(&plugin_dir).unwrap();
    let json_path = plugin_dir.join("plugin.json");
    std::fs::write(
        &json_path,
        r#"{
                "id": "p_native", "name": "p_native", "version": "1.0.0",
                "plugin_type": "tool", "language": "rust",
                "host_type": "in_process", "entry": "p_native",
                "capabilities": {},
                "native": { "artifact": "libp_native.so" }
            }"#,
    )
    .unwrap();
    let loader = PluginLoaderImpl::new(dir.path(), None);
    let mut manifest: PluginManifest =
        serde_json::from_str(&std::fs::read_to_string(&json_path).unwrap()).unwrap();
    let err = loader.validate_manifest_internal(&mut manifest, &json_path);
    let msg = err.expect_err("native artifact 缺失应被拒").to_string();
    assert!(msg.contains("native artifact 缺失"), "{msg}");
}

/// 契约闸门 2.5：native 产物预检——artifact 存在 → 通过。
#[test]
fn test_native_artifact_precheck_existing_accepted() {
    let dir = tempfile::tempdir().unwrap();
    let plugin_dir = dir.path().join("p_native");
    std::fs::create_dir_all(&plugin_dir).unwrap();
    let json_path = plugin_dir.join("plugin.json");
    std::fs::write(
        &json_path,
        r#"{
                "id": "p_native", "name": "p_native", "version": "1.0.0",
                "plugin_type": "tool", "language": "rust",
                "host_type": "in_process", "entry": "p_native",
                "capabilities": {},
                "native": { "artifact": "libp_native.so" }
            }"#,
    )
    .unwrap();
    std::fs::write(plugin_dir.join("libp_native.so"), b"not-really-so").unwrap();
    let loader = PluginLoaderImpl::new(dir.path(), None);
    let mut manifest: PluginManifest =
        serde_json::from_str(&std::fs::read_to_string(&json_path).unwrap()).unwrap();
    let r = loader.validate_manifest_internal(&mut manifest, &json_path);
    assert!(r.is_ok(), "native artifact 存在应通过：{r:?}");
}

/// allowlist 生产接线：缺文件 → 默认 permissive 空白名单（不误伤）；
/// yaml 合法 → 如实载入 mode/条目。
#[test]
fn load_allowlist_missing_file_defaults_to_permissive() {
    let dir = tempfile::tempdir().unwrap();
    let cfg = load_allowlist_file(&dir.path().join("no_allowlist.yaml"));
    assert_eq!(cfg.mode, AllowlistMode::Permissive);
    assert!(cfg.plugins.is_empty());
}

#[test]
fn load_allowlist_parses_yaml() {
    let dir = tempfile::tempdir().unwrap();
    let p = dir.path().join("plugin_allowlist.yaml");
    std::fs::write(
        &p,
        "mode: strict\nplugins:\n  - id: foo\n    sha256: abc123\n",
    )
    .unwrap();
    let cfg = load_allowlist_file(&p);
    assert_eq!(cfg.mode, AllowlistMode::Strict);
    assert_eq!(cfg.plugins.len(), 1);
    assert_eq!(cfg.plugins[0].id, "foo");
    assert_eq!(cfg.plugins[0].sha256, "abc123");
}

/// K2：解析失败（文件存在但损坏）→ fail-closed：strict 空名单（所有插件
/// 在发现期被拒），不再静默降为 permissive 全放行。文件缺失仍 permissive
/// （文档化默认，见 load_allowlist_missing_file_defaults_to_permissive）。
#[test]
fn load_allowlist_corrupt_file_fails_closed_to_strict_empty() {
    let dir = tempfile::tempdir().unwrap();
    let p = dir.path().join("plugin_allowlist.yaml");
    std::fs::write(&p, "mode: [broken\n  ::::").unwrap();
    let cfg = load_allowlist_file(&p);
    assert_eq!(
        cfg.mode,
        AllowlistMode::Strict,
        "损坏文件必须 fail-closed 为 strict"
    );
    assert!(cfg.plugins.is_empty(), "strict 空名单 = 全部插件拒载");
}

/// K2 端到端：解析失败的 allowlist 接入 loader 后，白名单外插件被拒载
/// （fail-closed 的实际效果，非仅配置对象形态）。
#[tokio::test]
async fn corrupt_allowlist_rejects_plugin_load() {
    let builtin = tempfile::tempdir().unwrap();
    create_test_plugin_dir(builtin.path(), "some_plugin", "tool");

    let cfg_dir = tempfile::tempdir().unwrap();
    let allowlist_path = cfg_dir.path().join("plugin_allowlist.yaml");
    std::fs::write(&allowlist_path, "mode: [broken").unwrap();
    let allowlist = load_allowlist_file(&allowlist_path);

    let loader = PluginLoaderImpl::new(builtin.path(), None).with_allowlist(allowlist);
    let discovered = loader.discover(&[]).await.unwrap();
    assert!(
        discovered.is_empty(),
        "strict 空名单下损坏 allowlist 应拒载所有插件（fail-closed）"
    );
}

// ── scan_root 分支补充：YAML manifest / 解析失败跳过 / 校验失败跳过 ──

/// plugin.yaml 形态：JSON 解析失败后回落 YAML 解析（同一份 discover 路径
/// 支持两种 manifest 文件名与两种语法）。
#[tokio::test]
async fn scan_root_parses_yaml_manifest() {
    let builtin = tempfile::tempdir().unwrap();
    let dir = builtin.path().join("yaml_plugin");
    fs::create_dir_all(&dir).unwrap();
    fs::write(
        dir.join("plugin.yaml"),
        "id: yaml_plugin\nname: YAML Plugin\nversion: 1.0.0\n\
             plugin_type: tool\nlanguage: python\nhost_type: sidecar\n\
             entry: python server.py\ncapabilities: {}\n",
    )
    .unwrap();

    let loader = PluginLoaderImpl::new(builtin.path(), None);
    let discovered = loader.discover(&[]).await.unwrap();
    assert_eq!(discovered.len(), 1, "plugin.yaml 应被发现");
    assert_eq!(discovered[0].id, "yaml_plugin");
    assert_eq!(discovered[0].name, "YAML Plugin");
}

/// 目录里既无 plugin.json 也无 plugin.yaml → 跳过（不报错、不产出伪条目）。
#[tokio::test]
async fn scan_root_skips_dir_without_manifest() {
    let builtin = tempfile::tempdir().unwrap();
    fs::create_dir_all(builtin.path().join("empty_dir")).unwrap();
    // 顶层散落文件也不是插件目录（read_dir 只下钻目录）
    fs::write(builtin.path().join("stray.txt"), "x").unwrap();
    create_test_plugin_dir(builtin.path(), "real_plugin", "tool");

    let loader = PluginLoaderImpl::new(builtin.path(), None);
    let discovered = loader.discover(&[]).await.unwrap();
    assert_eq!(discovered.len(), 1, "只发现真插件目录");
    assert_eq!(discovered[0].id, "real_plugin");
}

/// 损坏 manifest（JSON/YAML 双解析失败）→ 跳过该插件但不阻断同 root 其他插件。
#[tokio::test]
async fn scan_root_skips_unparseable_and_keeps_others() {
    let builtin = tempfile::tempdir().unwrap();
    let bad = builtin.path().join("broken_plugin");
    fs::create_dir_all(&bad).unwrap();
    fs::write(bad.join("plugin.json"), "not json {{{").unwrap();
    create_test_plugin_dir(builtin.path(), "good_plugin", "tool");

    let loader = PluginLoaderImpl::new(builtin.path(), None);
    let discovered = loader
        .discover(&[])
        .await
        .expect("损坏插件不应让 discover 失败");
    assert_eq!(discovered.len(), 1, "坏插件跳过，好插件保留");
    assert_eq!(discovered[0].id, "good_plugin");
}

/// 校验失败（name 为空）→ 跳过该插件（warn 留痕），同 root 其他插件照常发现。
#[tokio::test]
async fn scan_root_skips_validation_failure_and_keeps_others() {
    let builtin = tempfile::tempdir().unwrap();
    let invalid = builtin.path().join("nameless");
    fs::create_dir_all(&invalid).unwrap();
    fs::write(
        invalid.join("plugin.json"),
        r#"{
                "id":"nameless","name":"","version":"1.0.0",
                "plugin_type":"tool","language":"python","host_type":"sidecar",
                "entry":"python server.py","capabilities":{}
            }"#,
    )
    .unwrap();
    create_test_plugin_dir(builtin.path(), "valid_peer", "tool");

    let loader = PluginLoaderImpl::new(builtin.path(), None);
    let discovered = loader.discover(&[]).await.unwrap();
    assert_eq!(discovered.len(), 1, "校验失败插件跳过");
    assert_eq!(discovered[0].id, "valid_peer");
}

/// 不存在的 root → scan_root 直接返回空（不报 IO 错）——双根扫描里
/// builtin root 未部署时的正常路径。
#[tokio::test]
async fn discover_missing_root_returns_empty_without_error() {
    let missing = tempfile::tempdir().unwrap().path().join("not_deployed");
    let loader = PluginLoaderImpl::new(&missing, None);
    let discovered = loader.discover(&[]).await.expect("不存在的 root 不应报错");
    assert!(discovered.is_empty());
}

// ── validate_manifest_internal 必填字段逐项（id/name/version/language）──

/// 逐个必填字段缺失 → 对应 reason（表驱动；entry 校验另有专测）。
#[test]
fn validate_manifest_reports_each_missing_required_field() {
    let dir = tempfile::tempdir().unwrap();
    let manifest_path = {
        let p = dir.path().join("p.json");
        fs::write(
            &p,
            r#"{
                    "id":"p","name":"P","version":"1.0.0",
                    "plugin_type":"tool","language":"python","host_type":"sidecar",
                    "entry":"python server.py","capabilities":{}
                }"#,
        )
        .unwrap();
        p
    };
    let loader = PluginLoaderImpl::new(dir.path(), None);
    let base: PluginManifest =
        serde_json::from_str(&fs::read_to_string(&manifest_path).unwrap()).unwrap();

    type ManifestEdit = fn(&mut PluginManifest);
    let cases: [(&str, ManifestEdit); 4] = [
        ("id is required", |m| m.id.clear()),
        ("name is required", |m| m.name.clear()),
        ("version is required", |m| m.version.clear()),
        ("language is required", |m| m.language.clear()),
    ];
    for (expected, mutate) in cases {
        let mut m = base.clone();
        mutate(&mut m);
        let err = loader
            .validate_manifest_internal(&mut m, &manifest_path)
            .expect_err("缺必填字段应拒绝");
        let msg = format!("{err:?}");
        assert!(msg.contains(expected), "应报「{expected}」: {msg}");
    }
}

/// language 缺失时的 plugin_id 归属：错误应带被拒插件 id（非 "(unknown)"）。
/// 对照 id 缺失用例（此时只能报 (unknown)）。
#[test]
fn validate_manifest_error_carries_plugin_id() {
    let dir = tempfile::tempdir().unwrap();
    let manifest_path = {
        let p = dir.path().join("p.json");
        fs::write(
            &p,
            r#"{
                    "id":"who_am_i","name":"N","version":"1.0.0",
                    "plugin_type":"tool","language":"","host_type":"sidecar",
                    "entry":"python server.py","capabilities":{}
                }"#,
        )
        .unwrap();
        p
    };
    let loader = PluginLoaderImpl::new(dir.path(), None);
    let mut m: PluginManifest =
        serde_json::from_str(&fs::read_to_string(&manifest_path).unwrap()).unwrap();
    let err = loader
        .validate_manifest_internal(&mut m, &manifest_path)
        .unwrap_err();
    let msg = format!("{err:?}");
    assert!(msg.contains("who_am_i"), "错误应带插件 id: {msg}");

    // id 缺失：无从归属，报 (unknown)
    let mut no_id = m.clone();
    no_id.id.clear();
    let err = loader
        .validate_manifest_internal(&mut no_id, &manifest_path)
        .unwrap_err();
    assert!(
        format!("{err:?}").contains("(unknown)"),
        "id 缺失时归属为 (unknown): {err:?}"
    );
}

/// state.reads 非法条目（messages_tail 非正整数）→ 忽略该条目但整体放行
/// （单条声明错误不拒载）；合法条目原样保留。
#[test]
fn validate_manifest_ignores_invalid_state_reads_entries() {
    let dir = tempfile::tempdir().unwrap();
    let manifest_path = dir.path().join("p.json");
    fs::write(
            &manifest_path,
            r#"{
                "id":"reads_mix","name":"N","version":"1.0.0",
                "plugin_type":"tool","language":"python","host_type":"sidecar",
                "entry":"python server.py","capabilities":{},
                "state":{"reads":["messages","messages_tail:0","messages_tail:abc","messages_tail:5"]}
            }"#,
        )
        .unwrap();
    let loader = PluginLoaderImpl::new(dir.path(), None);
    let mut m: PluginManifest =
        serde_json::from_str(&fs::read_to_string(&manifest_path).unwrap()).unwrap();
    loader
        .validate_manifest_internal(&mut m, &manifest_path)
        .expect("非法读取条目应被忽略而非拒载");

    let reads = &m.state.as_ref().unwrap().reads;
    assert!(
        reads.contains(&"messages".to_string()),
        "普通键保留: {reads:?}"
    );
    assert!(
        reads.contains(&"messages_tail:5".to_string()),
        "正整数保留: {reads:?}"
    );
    assert!(
        !reads.iter().any(|r| r.contains(":0") || r.contains(":abc")),
        "0 与非数字条目应被剔除: {reads:?}"
    );
}

// ── scan_root 的 IO 错误路径：root 存在但不可读 ──

/// root 是文件而非目录（read_dir 必然失败）→ LoaderError::Io 上抛，
/// 不静默返回空（配置面故障必须可见）。
#[tokio::test]
async fn scan_root_io_error_is_isolated_to_that_root() {
    let dir = tempfile::tempdir().unwrap();
    let not_a_dir = dir.path().join("regular_file");
    fs::write(&not_a_dir, "x").unwrap();

    // 坏根（文件而非目录）与好根并存：坏根读失败只记 warn 不阻断，
    // 好根插件照常发现——单根失败不得瘫痪整次发现。
    let good = tempfile::tempdir().unwrap();
    create_test_plugin_dir(good.path(), "survives_bad_root", "tool");

    let loader = PluginLoaderImpl::new(&not_a_dir, Some(good.path().to_path_buf()));
    let discovered = loader
        .discover(&[not_a_dir.to_str().unwrap()])
        .await
        .expect("单根读失败不应让 discover 整体失败");
    assert_eq!(discovered.len(), 1, "好根插件应照常发现");
    assert_eq!(discovered[0].id, "survives_bad_root");
}

/// root 下某插件目录的 manifest 不可读（Windows 下用目录占位同名路径
/// 制造读失败）→ 该 root 扫描报 IO 错（不产出部分结果）。
#[tokio::test]
async fn scan_root_manifest_read_failure_isolated_to_that_root() {
    let bad = tempfile::tempdir().unwrap();
    // plugin.json 位置是目录 → read_to_string 必然失败
    let plugin_dir = bad.path().join("unreadable_manifest");
    fs::create_dir_all(plugin_dir.join("plugin.json")).unwrap();

    let good = tempfile::tempdir().unwrap();
    create_test_plugin_dir(good.path(), "peer_ok", "tool");

    let loader = PluginLoaderImpl::new(bad.path(), Some(good.path().to_path_buf()));
    let discovered = loader
        .discover(&[])
        .await
        .expect("单根 manifest 读失败不应让 discover 整体失败");
    assert_eq!(discovered.len(), 1, "用户根插件照常发现");
    assert_eq!(discovered[0].id, "peer_ok");
}

/// entry 为空且非 composite → 明确拒绝（entry is required）。
#[test]
fn validate_manifest_non_composite_empty_entry_rejected() {
    let dir = tempfile::tempdir().unwrap();
    let manifest_path = dir.path().join("p.json");
    fs::write(
        &manifest_path,
        r#"{
                "id":"no_entry","name":"N","version":"1.0.0",
                "plugin_type":"tool","language":"python","host_type":"sidecar",
                "entry":"","capabilities":{}
            }"#,
    )
    .unwrap();
    let loader = PluginLoaderImpl::new(dir.path(), None);
    let mut m: PluginManifest =
        serde_json::from_str(&fs::read_to_string(&manifest_path).unwrap()).unwrap();
    let err = loader
        .validate_manifest_internal(&mut m, &manifest_path)
        .expect_err("非 composite 空 entry 应拒绝");
    assert!(
        format!("{err:?}").contains("entry is required for non-composite"),
        "文案: {err:?}"
    );
}

/// native 声明但 manifest 路径无父目录 → 明确拒绝（无法解析相对产物路径）。
/// 用相对文件名作为 source_path 制造 parent() == Some("") 的边界；
/// 空路径 parent 为 None 才触发该分支，故取 `""`。
#[test]
fn validate_manifest_native_without_parent_dir_rejected() {
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
    let mut m: PluginManifest = serde_json::from_str(
        r#"{
                "id":"no_parent","name":"N","version":"1.0.0",
                "plugin_type":"tool","language":"rust","host_type":"in_process",
                "entry":"x","capabilities":{},
                "native":{"artifact":"no_parent"}
            }"#,
    )
    .unwrap();
    let err = loader
        .validate_manifest_internal(&mut m, Path::new(""))
        .expect_err("空 source_path（无父目录）应拒绝 native 预检");
    assert!(
        format!("{err:?}").contains("无父目录"),
        "文案应指明无父目录: {err:?}"
    );
}

// ── 目录树规范化哈希（compute_plugin_sha256）的边界分支 ──

/// 确定性：同一棵树重复计算结果一致（枚举序不进摘要——路径排序兜底）。
#[test]
fn tree_hash_is_deterministic() {
    let dir = tempfile::tempdir().unwrap();
    fs::write(dir.path().join("plugin.json"), b"{}").unwrap();
    fs::write(dir.path().join("server.py"), b"print('hi')").unwrap();
    let loader = PluginLoaderImpl::new(dir.path(), None);
    let manifest_path = dir.path().join("plugin.json");
    let first = loader.compute_plugin_sha256(&manifest_path).unwrap();
    let second = loader.compute_plugin_sha256(&manifest_path).unwrap();
    assert_eq!(first, second, "同一棵树两次计算应一致");
}

/// 内容变更 → 摘要变化（热篡改检测的判定基础）。
#[test]
fn tree_hash_changes_when_file_content_changes() {
    let dir = tempfile::tempdir().unwrap();
    fs::write(dir.path().join("plugin.json"), b"{}").unwrap();
    fs::write(dir.path().join("server.py"), b"print('hi')").unwrap();
    let loader = PluginLoaderImpl::new(dir.path(), None);
    let manifest_path = dir.path().join("plugin.json");
    let before = loader.compute_plugin_sha256(&manifest_path).unwrap();
    fs::write(dir.path().join("server.py"), b"print('bye')").unwrap();
    let after = loader.compute_plugin_sha256(&manifest_path).unwrap();
    assert_ne!(before, after, "文件字节变更应改变摘要");
}

/// 排除面：.venv* / __pycache__ / *.pyc / node_modules / logs 不进摘要
/// （环境噪声，不属于"插件源"；增删这些文件不改变摘要）。
#[test]
fn tree_hash_excludes_env_noise() {
    let dir = tempfile::tempdir().unwrap();
    fs::write(dir.path().join("plugin.json"), b"{}").unwrap();
    let loader = PluginLoaderImpl::new(dir.path(), None);
    let manifest_path = dir.path().join("plugin.json");
    let before = loader.compute_plugin_sha256(&manifest_path).unwrap();

    fs::create_dir_all(dir.path().join(".venv-hindsight")).unwrap();
    fs::write(
        dir.path().join(".venv-hindsight").join("pyvenv.cfg"),
        b"junk",
    )
    .unwrap();
    fs::create_dir_all(dir.path().join("__pycache__")).unwrap();
    fs::write(
        dir.path()
            .join("__pycache__")
            .join("server.cpython-312.pyc"),
        b"junk",
    )
    .unwrap();
    fs::write(dir.path().join("server.pyc"), b"junk").unwrap();
    fs::create_dir_all(dir.path().join("node_modules")).unwrap();
    fs::write(dir.path().join("node_modules").join("x.js"), b"junk").unwrap();
    fs::create_dir_all(dir.path().join("logs")).unwrap();
    fs::write(dir.path().join("logs").join("run.log"), b"junk").unwrap();
    let after = loader.compute_plugin_sha256(&manifest_path).unwrap();
    assert_eq!(before, after, "排除面文件增删不应改变摘要");
}

/// 相对路径入摘要：同字节文件放在不同路径下 → 摘要不同（防换位伪装）。
#[test]
fn tree_hash_binds_relative_paths() {
    let dir_a = tempfile::tempdir().unwrap();
    fs::create_dir_all(dir_a.path().join("a")).unwrap();
    fs::write(dir_a.path().join("a").join("x.py"), b"same").unwrap();
    let dir_b = tempfile::tempdir().unwrap();
    fs::create_dir_all(dir_b.path().join("b")).unwrap();
    fs::write(dir_b.path().join("b").join("x.py"), b"same").unwrap();

    let loader = PluginLoaderImpl::new(dir_a.path(), None);
    let hash_a = loader
        .compute_plugin_sha256(&dir_a.path().join("a"))
        .unwrap();
    let hash_b = loader
        .compute_plugin_sha256(&dir_b.path().join("b"))
        .unwrap();
    assert_ne!(hash_a, hash_b, "同字节不同相对路径应产生不同摘要");
}

/// 符号链接不进摘要（逃逸面，与 /ext 静态服务同口径）。
#[cfg(unix)]
#[test]
fn tree_hash_skips_symlinks() {
    let dir = tempfile::tempdir().unwrap();
    fs::write(dir.path().join("plugin.json"), b"{}").unwrap();
    std::os::unix::fs::symlink("/etc/hostname", dir.path().join("evil.py")).unwrap();
    let loader = PluginLoaderImpl::new(dir.path(), None);
    let hash = loader
        .compute_plugin_sha256(&dir.path().join("plugin.json"))
        .unwrap();
    // 只含 plugin.json 一个文件：与手工两文件摘要不同（symlink 未进摘要）
    let mut hasher = Sha256::new();
    hasher.update(b"plugin.json");
    hasher.update([0u8]);
    hasher.update(b"{}");
    hasher.update([0u8]);
    assert_eq!(hash, format!("{:x}", hasher.finalize()), "symlink 应被跳过");
}

/// 哈希比对：等长不同内容判不等（常量时间比较的差异分支）。
/// 长度不同同样判不等。
#[test]
fn hash_compare_detects_length_and_content_differences() {
    assert!(!secure_eq("abcd", "abce"), "同长不同内容应不等");
    assert!(secure_eq("abcd", "abcd"), "自身相等");
    assert!(!secure_eq("abcd", "abc"), "长度不同应不等");
    assert!(secure_eq("", ""), "空串相等");
}

// ── 用户配置层叠加的单元素语义（apply_user_config_overlay / set_config_leaf）──

/// `set_config_leaf` 的分段写入契约：空段返回 false（不写）；
/// 单段直接替换；中间段缺失时新建；同名标量挡路时保留原值返回 false。
#[test]
fn set_config_leaf_creates_missing_layers_and_refuses_scalar_shadow() {
    let mut map = serde_json::Map::new();

    assert!(
        !set_config_leaf(&mut map, &[], serde_json::json!(1)),
        "空段不写"
    );
    assert!(
        set_config_leaf(&mut map, &["a".to_string()], serde_json::json!("v")),
        "单段直接写入"
    );
    assert_eq!(map["a"], serde_json::json!("v"));

    assert!(
        set_config_leaf(
            &mut map,
            &["d".to_string(), "e".to_string()],
            serde_json::json!(2)
        ),
        "中间段缺失时新建空对象"
    );
    assert_eq!(map["d"]["e"], serde_json::json!(2));

    // 同名标量挡路（a 已是字符串，不能再作目录层级）→ 不覆盖、返回未接管
    assert!(
        !set_config_leaf(
            &mut map,
            &["a".to_string(), "child".to_string()],
            serde_json::json!(3)
        ),
        "标量挡路不得强拆（保留 factory 值）"
    );
    assert_eq!(map["a"], serde_json::json!("v"), "原标量保持不变");

    // 已有对象子树：只替换末段，兄弟键保留（目录层不整体清空）
    assert!(set_config_leaf(
        &mut map,
        &["d".to_string(), "e".to_string()],
        serde_json::json!(9)
    ));
    assert!(set_config_leaf(
        &mut map,
        &["d".to_string(), "sibling".to_string()],
        serde_json::json!(8)
    ));
    assert_eq!(map["d"]["e"], serde_json::json!(9), "末段被文件级替换");
    assert_eq!(map["d"]["sibling"], serde_json::json!(8), "兄弟键不受影响");
}

/// 用户层文件与 factory 同路径：整体替换（不逐字段合并）；未被接管的
/// factory 兄弟文件保留；用户层返回的接管计数如实反映文件数。
#[tokio::test]
async fn user_config_overlay_replaces_file_wholesale_and_counts() {
    let _lock = user_space_env_lock();
    let factory = tempfile::tempdir().unwrap();
    let user = tempfile::tempdir().unwrap();
    // factory 的 llm.yaml 有 a/b 两键；用户层只有 a —— 文件级接管应丢掉 b
    fs::write(
        factory.path().join("llm.yaml"),
        "a: factory\nextra: keep-me-only-in-factory\n",
    )
    .unwrap();
    fs::write(factory.path().join("peer.yaml"), "p: factory\n").unwrap();
    fs::write(user.path().join("llm.yaml"), "a: user\n").unwrap();

    let _g = UserRootGuard::set(user.path());
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(factory.path());
    let cfg = loader.load_config().await.unwrap();
    assert_eq!(cfg["llm"]["a"], "user", "用户层文件整体取代 factory");
    assert!(
        cfg["llm"].get("extra").is_none(),
        "文件级替换不是字段级合并——factory 独有键随文件被取代"
    );
    assert_eq!(cfg["peer"]["p"], "factory", "未接管的兄弟文件保持 factory");

    // 无用户层文件时接管计数为 0（用户目录存在但空）
    let empty_user = tempfile::tempdir().unwrap();
    let _g2 = UserRootGuard::set(empty_user.path());
    let loader2 = PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(factory.path());
    let cfg2 = loader2.load_config().await.unwrap();
    assert_eq!(cfg2["llm"]["a"], "factory", "空用户层 = 全 factory");
    assert_eq!(cfg2["llm"]["extra"], "keep-me-only-in-factory");
}

/// 用户层**不可读**目录（read_dir 失败）→ overlay 整体跳过并返回 0，
/// factory 全量保留（不半途写入部分键）。
#[tokio::test]
async fn user_config_overlay_unreadable_dir_skips_whole() {
    let _lock = user_space_env_lock();
    let factory = tempfile::tempdir().unwrap();
    fs::write(factory.path().join("cfg.yaml"), "v: factory\n").unwrap();

    // 用文件路径冒充用户配置目录：is_dir() 为假 → 直接返回 0（第一道门）
    let as_file = tempfile::tempdir().unwrap();
    let fake = as_file.path().join("not-a-dir");
    fs::write(&fake, "x").unwrap();
    let _g = UserRootGuard::set(&fake);
    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(factory.path());
    let cfg = loader.load_config().await.unwrap();
    assert_eq!(cfg["cfg"]["v"], "factory", "非目录用户层 = 未接管");
}

// ── 配置加载的错误映射与 canonicalize 回落 ──

/// `with_config_root` 对不存在路径回落原始路径（不报错），并照常可用；
/// 配置根不存在时 `load_config` 返回空对象（不抛错）。
#[tokio::test]
async fn with_config_root_missing_path_falls_back_and_loads_empty() {
    let dir = tempfile::tempdir().unwrap();
    let missing = dir.path().join("no-such-config-dir");
    let loader = tracing::subscriber::with_default(AlwaysSubscriber, || {
        PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(&missing)
    });
    let cfg = loader.load_config().await.unwrap();
    assert_eq!(cfg, serde_json::json!({}), "不存在的配置根返回空对象");
}

/// 记录 WARN 事件数的 subscriber：读失败跳过必须可观测（不是静默丢配置）。
struct WarnCountingSubscriber(std::sync::Arc<std::sync::atomic::AtomicUsize>);

impl tracing::Subscriber for WarnCountingSubscriber {
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
    fn event(&self, event: &tracing::Event<'_>) {
        if *event.metadata().level() == tracing::Level::WARN {
            self.0.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        }
    }
    fn enter(&self, _span: &tracing::Id) {}
    fn exit(&self, _span: &tracing::Id) {}
}

/// 配置目录里出现无法读取的条目（非 UTF-8 字节的 .yaml 文件）→ 该文件
/// 告警跳过，其余文件照常加载（不静默丢配置，也不因单个坏文件连累全目录）。
///
/// 回归锚（2026-11-13 装机事故）：asr.yaml 被写成非法 UTF-8，单文件读失败
/// 曾让整个 load_config 返回 Err，所有插件被注入空配置，LLM 回退硬编码
/// gpt-4。读失败与解析失败同语义（warn + 跳过），目录遍历失败才传播。
#[tokio::test]
async fn load_config_unreadable_file_skipped_with_warning_others_load() {
    let _user_space = factory_only_user_space();
    let dir = tempfile::tempdir().unwrap();
    // 非 UTF-8 内容 → read_to_string 必然失败（InvalidData）
    fs::write(dir.path().join("broken.yaml"), [0xff_u8, 0xfe]).unwrap();
    fs::write(dir.path().join("ok.yaml"), "v: 1\n").unwrap();

    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(dir.path());
    let warn_count = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let guard = tracing::subscriber::set_default(WarnCountingSubscriber(warn_count.clone()));
    let cfg = loader
        .load_config()
        .await
        .expect("单个不可读文件不应让整体装载失败");
    drop(guard);

    assert_eq!(cfg["ok"]["v"], 1, "正常文件应照常加载");
    assert!(cfg.get("broken").is_none(), "不可读文件不得产出键");
    assert_eq!(
        warn_count.load(std::sync::atomic::Ordering::SeqCst),
        1,
        "跳过必须可观测（恰一条 warn）"
    );
}

/// 目录中单个非法 UTF-8 文件 + 正常 YAML（含子目录）→ 正常文件仍被加载，
/// 子目录递归不受影响。
#[tokio::test]
async fn load_config_invalid_utf8_does_not_block_other_files() {
    let _user_space = factory_only_user_space();
    let dir = tempfile::tempdir().unwrap();
    fs::write(dir.path().join("broken.yaml"), [0xff_u8, 0xfe]).unwrap();
    fs::write(dir.path().join("good.yaml"), "a: 1\n").unwrap();
    let sub = dir.path().join("model");
    fs::create_dir(&sub).unwrap();
    fs::write(sub.join("nested.yaml"), "b: 2\n").unwrap();

    let loader = PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(dir.path());
    let cfg = loader
        .load_config()
        .await
        .expect("非法 UTF-8 文件不得让整体装载失败");

    assert_eq!(cfg["good"]["a"], 1, "同目录正常文件应加载");
    assert_eq!(cfg["model"]["nested"]["b"], 2, "子目录正常文件应递归加载");
    assert!(cfg.get("broken").is_none(), "非法文件不得产出键");
}

/// 目录缺失时 sha256 计算传播 IO 错误（不静默当空树）。
#[test]
fn compute_plugin_sha256_propagates_missing_dir() {
    let dir = tempfile::tempdir().unwrap();
    let loader = PluginLoaderImpl::new(dir.path(), None);
    let ghost_path = dir.path().join("ghost").join("plugin.json");
    let err = loader
        .compute_plugin_sha256(&ghost_path)
        .expect_err("目录缺失应报 IO 错误");
    assert!(
        matches!(err, LoaderError::Io { .. }),
        "应为 Io 变体，实际: {err:?}"
    );
}

/// `set_config_leaf` 的空段与既有对象层组合：目录层已存在时递归只动末段；
/// 末段键已存在时被替换（不追加）。
#[test]
fn set_config_leaf_replaces_existing_leaf() {
    let mut map = serde_json::Map::new();
    map.insert(
        "cfg".to_string(),
        serde_json::json!({"keep": 1, "target": "old"}),
    );
    assert!(set_config_leaf(
        &mut map,
        &["cfg".to_string(), "target".to_string()],
        serde_json::json!("new")
    ));
    assert_eq!(map["cfg"]["target"], "new");
    assert_eq!(map["cfg"]["keep"], 1, "兄弟键在递归路径下同样保留");
}
