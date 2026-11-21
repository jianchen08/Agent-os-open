//! external MCP 受管插件目录（一行接入：清洗目录名 / 合成 manifest / 落盘 / 清理）。

use super::*;
/// mcp 配置名 → 受管插件目录名（`mcp_<清洗名>`）；清洗后为空返回 None。
pub(super) fn sanitize_mcp_dir_name(name: &str) -> Option<String> {
    let cleaned: String = name
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() {
                c.to_ascii_lowercase()
            } else {
                '_'
            }
        })
        .collect();
    let dir = format!("mcp_{cleaned}");
    if dir == "mcp_" {
        None
    } else {
        Some(dir)
    }
}

/// 构造 external MCP manifest（mcp:external 空声明 + mcp 块）。
/// 返回 (目录名, manifest JSON)——目录名 = mcp_<清洗名>。
pub(super) fn build_external_mcp_manifest(
    name: &str,
    spec: &serde_json::Value,
) -> Option<(String, serde_json::Value)> {
    let warn = |msg: String| {
        warn!(target: "plugin-loader", error = %msg, "external MCP 条目跳过");
    };
    let dir_name = sanitize_mcp_dir_name(name)?;
    let mut endpoint = serde_json::Map::new();
    let transport: &str;
    if let Some(cmd) = spec.get("command").and_then(|v| v.as_str()) {
        transport = "stdio";
        endpoint.insert("command".into(), serde_json::json!(cmd));
        if let Some(args) = spec.get("args") {
            endpoint.insert("args".into(), args.clone());
        }
        if let Some(env) = spec.get("env") {
            endpoint.insert("env".into(), env.clone());
        }
    } else if let Some(url) = spec.get("url").and_then(|v| v.as_str()) {
        transport = "streamable_http";
        endpoint.insert("url".into(), serde_json::json!(url));
        if let Some(headers) = spec.get("headers") {
            endpoint.insert("headers".into(), headers.clone());
        }
    } else {
        warn(format!("mcp {name:?} 缺 command/url"));
        return None;
    }
    let manifest = serde_json::json!({
        "id": dir_name,
        "name": format!("MCP {name}"),
        "description": "External MCP (config/mcp.json)",
        "version": "1.0.0",
        "plugin_type": "tool",
        "language": "external",
        "host_type": "sidecar",
        "entry": "mcp:external",
        "capabilities": {},
        "mcp": {"transport": transport, "endpoint": endpoint},
        "permissions": {},
        "priority": 30,
    });
    Some((dir_name, manifest))
}

/// 写受管 external MCP 插件目录：`.mcp-managed` 标记 + plugin.json（内容不变不重写）。
pub(super) fn write_managed_plugin_dir(target: &Path, manifest: &serde_json::Value) {
    if std::fs::create_dir_all(target).is_err() {
        warn!(target: "plugin-loader", dir = %target.display(), "external MCP 目录创建失败");
        return;
    }
    let marker = target.join(".mcp-managed");
    if std::fs::write(&marker, b"generated from config/mcp.json").is_err() {
        warn!(target: "plugin-loader", dir = %target.display(), "external MCP 标记写入失败");
        return;
    }
    let Ok(pretty) = serde_json::to_string_pretty(manifest) else {
        return;
    };
    let path = target.join("plugin.json");
    if std::fs::read_to_string(&path).is_ok_and(|existing| existing == pretty) {
        return; // 内容未变不重写（防 mtime 空转 watcher）
    }
    if let Err(e) = std::fs::write(&path, pretty) {
        warn!(target: "plugin-loader", path = %path.display(), error = %e, "external MCP manifest 写入失败");
    }
}

/// 受管目录清理原因（决定 WARN 文案——「配置已删」不得覆盖真实原因，否则
/// 误报会把排查引向不存在的删除动作，2026-11-20 mcp_windows_mcp 实证）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum PruneCause {
    /// 该根配置可读但条目已删（真·配置删除）。
    EntryRemoved,
    /// 该根配置文件本身已删除（NotFound）——删除信号，受管目录全清。
    ConfigFileDeleted,
}

/// 清理配置中已删除的受管目录：仅限 `mcp_` 前缀且带 `.mcp-managed` 标记者。
/// `keep=None` = 该根受管目录全清（条目集为空或配置文件已删）。
/// `superseded` 里的目录名按「被用户根同名条目取代（单副本收敛）」记因，
/// 仅在 [`PruneCause::EntryRemoved`] 语义下生效。
pub(super) fn prune_managed_plugin_dirs(
    root: &Path,
    keep: Option<&std::collections::HashSet<String>>,
    superseded: &std::collections::HashSet<String>,
    cause: PruneCause,
) {
    let Ok(entries) = std::fs::read_dir(root) else {
        return;
    };
    for entry in entries.flatten() {
        let dir = entry.path();
        if !dir.is_dir() {
            continue;
        }
        let Some(name) = dir.file_name().and_then(|n| n.to_str()) else {
            continue;
        };
        if !name.starts_with("mcp_") || keep.is_some_and(|k| k.contains(name)) {
            continue;
        }
        if !dir.join(".mcp-managed").exists() {
            continue; // 非受管目录（手写插件同名前缀）不动
        }
        let message = match cause {
            PruneCause::ConfigFileDeleted => "external MCP 配置文件已删除，受管目录随删清理",
            PruneCause::EntryRemoved if superseded.contains(name) => {
                "external MCP 内置副本被用户根同名条目取代而清理（单副本收敛）"
            }
            PruneCause::EntryRemoved => "external MCP 目录已随配置删除而清理",
        };
        match std::fs::remove_dir_all(&dir) {
            Ok(()) => {
                warn!(target: "plugin-loader", dir = %dir.display(), message)
            }
            Err(e) => {
                warn!(target: "plugin-loader", dir = %dir.display(), error = %e, "external MCP 目录清理失败")
            }
        }
    }
}

// ── 标准 mcpServers 配置同步（external MCP 一行接入）──

#[cfg(test)]
mod mcp_sync_tests {
    use super::*;
    use agentos_core::traits::McpTransport;

    /// 辅助：建临时插件根 + config 目录，写 config/mcp.json。
    fn make_mcp_config_fixture(root: &Path, body: &str) -> PluginLoaderImpl {
        let plugins = root.join("plugins");
        let config = root.join("config");
        std::fs::create_dir_all(&config).unwrap();
        std::fs::write(config.join("mcp.json"), body).unwrap();
        PluginLoaderImpl::new(plugins, None)
    }

    #[tokio::test]
    async fn test_mcp_config_stdio_entry_generates_plugin_dir() {
        let root = tempfile::tempdir().unwrap();
        let loader = make_mcp_config_fixture(
            root.path(),
            r#"{"mcpServers":{"windows-mcp":{"command":"uvx","args":["windows-mcp","serve"]}}}"#,
        );
        let manifests = loader.discover(&[]).await.unwrap();
        let m = manifests
            .iter()
            .find(|m| m.id == "mcp_windows_mcp")
            .expect("生成的 external MCP manifest 应被发现");
        assert_eq!(m.entry, "mcp:external");
        assert!(m.capabilities.tools.is_empty(), "空声明 → 观测即声明");
        let mcp = m.mcp.as_ref().expect("mcp 块存在");
        assert_eq!(mcp.transport, McpTransport::Stdio);
        let endpoint = mcp.endpoint.as_ref().unwrap();
        assert_eq!(endpoint.command.as_deref(), Some("uvx"));
        assert_eq!(
            endpoint.args,
            vec!["windows-mcp".to_string(), "serve".to_string()]
        );
    }

    #[tokio::test]
    async fn test_mcp_config_http_entry_maps_streamable_http() {
        let root = tempfile::tempdir().unwrap();
        let loader = make_mcp_config_fixture(
            root.path(),
            r#"{"mcpServers":{"remote":{"url":"https://example.com/mcp"}}}"#,
        );
        let manifests = loader.discover(&[]).await.unwrap();
        let m = manifests
            .iter()
            .find(|m| m.id == "mcp_remote")
            .expect("http 条目生成 manifest");
        let mcp = m.mcp.as_ref().unwrap();
        assert_eq!(mcp.transport, McpTransport::StreamableHttp);
        assert_eq!(
            mcp.endpoint.as_ref().unwrap().url.as_deref(),
            Some("https://example.com/mcp")
        );
    }

    #[tokio::test]
    async fn test_mcp_config_entry_removal_prunes_managed_dir() {
        let root = tempfile::tempdir().unwrap();
        let loader = make_mcp_config_fixture(
            root.path(),
            r#"{"mcpServers":{"gone":{"command":"uvx","args":["x"]}}}"#,
        );
        loader.discover(&[]).await.unwrap();
        let dir = root.path().join("plugins").join("mcp_gone");
        assert!(dir.join("plugin.json").exists(), "首次同步生成目录");
        assert!(dir.join(".mcp-managed").exists(), "受管标记存在");

        // 配置删条目 → 下一轮 discover 清目录
        let config = root.path().join("config").join("mcp.json");
        std::fs::write(&config, r#"{"mcpServers":{}}"#).unwrap();
        loader.discover(&[]).await.unwrap();
        assert!(!dir.exists(), "受管目录随配置删除被清理");
    }

    #[tokio::test]
    async fn test_mcp_config_prune_spares_unmanaged_dir() {
        let root = tempfile::tempdir().unwrap();
        let loader = make_mcp_config_fixture(root.path(), r#"{"mcpServers":{}}"#);
        // 手写同名前缀目录（无受管标记）不得被清
        let handmade = root.path().join("plugins").join("mcp_handmade");
        std::fs::create_dir_all(&handmade).unwrap();
        std::fs::write(handmade.join("plugin.json"), "{}").unwrap();

        loader.discover(&[]).await.unwrap();
        assert!(handmade.exists(), "非受管目录不动");
    }

    #[tokio::test]
    async fn test_mcp_config_malformed_json_keeps_managed_dirs() {
        // 契约：解析失败 → 跳过同步且不清目录（不因配置抖动销毁已装插件）。
        // 手编 mcp.json 打错一个逗号，下一轮 discover 不得删光受管插件目录。
        let root = tempfile::tempdir().unwrap();
        let loader = make_mcp_config_fixture(
            root.path(),
            r#"{"mcpServers":{"keep":{"command":"uvx","args":["x"]}}}"#,
        );
        loader.discover(&[]).await.unwrap();
        let dir = root.path().join("plugins").join("mcp_keep");
        assert!(dir.join(".mcp-managed").exists(), "首次同步生成受管目录");

        let config = root.path().join("config").join("mcp.json");
        std::fs::write(&config, r#"{"mcpServers":{ "broken":,,,}}"#).unwrap();
        loader.discover(&[]).await.unwrap();
        assert!(dir.exists(), "解析失败不得清受管目录");
    }

    #[tokio::test]
    async fn test_mcp_config_missing_mcp_servers_key_keeps_managed_dirs() {
        // 合法 JSON 但缺 mcpServers 键 = 形状坏，同解析失败口径：不清目录
        let root = tempfile::tempdir().unwrap();
        let loader = make_mcp_config_fixture(
            root.path(),
            r#"{"mcpServers":{"keep":{"command":"uvx","args":["x"]}}}"#,
        );
        loader.discover(&[]).await.unwrap();
        let dir = root.path().join("plugins").join("mcp_keep");

        let config = root.path().join("config").join("mcp.json");
        std::fs::write(&config, r#"{"other":1}"#).unwrap();
        loader.discover(&[]).await.unwrap();
        assert!(dir.exists(), "缺 mcpServers 键不得清受管目录");
    }

    #[tokio::test]
    async fn test_mcp_config_missing_file_is_noop() {
        let root = tempfile::tempdir().unwrap();
        let plugins = root.path().join("plugins");
        std::fs::create_dir_all(&plugins).unwrap();
        let loader = PluginLoaderImpl::new(plugins.clone(), None);
        let manifests = loader.discover(&[]).await.unwrap();
        assert!(manifests.is_empty());
        assert!(plugins.exists(), "无配置目录不动");
    }

    // ── 装机布局同构夹具组（启动优化 mcp_windows_mcp 复活链回归）──
    //
    // 装机几何：config_root == 用户配置目录（同一份 mcp.json 同时充当内置读与
    // 用户读），tier1/tier2 双插件根并存。实证：装机日志 kernel.log.2026-11-20
    // 「Config root: C:\Users\jc\AppData\Roaming\agentos\config」+ tier2 候选
    // path=C:\Users\jc\AppData\Roaming\agentos\plugins\mcp_windows_mcp。

    /// 装机布局同构夹具：config_root 与 user_root 同臂（user_root.parent()/config
    /// 即 config_root），tier1 根在 resources 子树。
    fn make_installed_layout_fixture(root: &Path, body: &str) -> PluginLoaderImpl {
        let config = root.join("config");
        let tier1 = root.join("resources").join("plugins").join("shared");
        let tier2 = root.join("plugins");
        std::fs::create_dir_all(&config).unwrap();
        std::fs::create_dir_all(&tier1).unwrap();
        std::fs::create_dir_all(&tier2).unwrap();
        std::fs::write(config.join("mcp.json"), body).unwrap();
        PluginLoaderImpl::new(tier1, Some(tier2)).with_config_root(config)
    }

    const WINDOWS_MCP_BODY: &str =
        r#"{"mcpServers":{"windows-mcp":{"command":"uvx","args":["windows-mcp","serve"]}}}"#;

    /// 正常配置组（行为不回归基线）：条目在 → 用户根副本生成并胜出（内置副本
    /// 被同名用户条目取代收敛单副本），manifest 在发现面可见。
    #[tokio::test]
    async fn test_installed_layout_normal_config_keeps_user_copy_discoverable() {
        let root = tempfile::tempdir().unwrap();
        let loader = make_installed_layout_fixture(root.path(), WINDOWS_MCP_BODY);
        let manifests = loader.discover(&[]).await.unwrap();
        let m = manifests
            .iter()
            .find(|m| m.id == "mcp_windows_mcp")
            .expect("正常配置下 external MCP 必须可被发现（不回归）");
        assert_eq!(m.entry, "mcp:external");
        let tier2_dir = root.path().join("plugins").join("mcp_windows_mcp");
        assert!(
            tier2_dir.join(".mcp-managed").exists(),
            "用户根受管副本在位"
        );
        let tier1_dir = root
            .path()
            .join("resources")
            .join("plugins")
            .join("shared")
            .join("mcp_windows_mcp");
        assert!(
            !tier1_dir.exists(),
            "内置副本被用户根同名条目取代：单副本收敛（生效面=用户副本，避免同 id 平版歧义）"
        );
    }

    /// 已删配置组：mcp.json 整文件删除（NotFound）= 配置删除信号 → 双根受管
    /// 目录全清、发现面无 manifest。装载/连接/spawn 全部以发现结果为唯一入口
    /// （无发现 → 无装载 → 无 stdio spawn → 无 uv sync）。
    #[tokio::test]
    async fn test_installed_layout_config_file_deleted_no_revival() {
        let root = tempfile::tempdir().unwrap();
        let loader = make_installed_layout_fixture(root.path(), WINDOWS_MCP_BODY);
        loader.discover(&[]).await.unwrap();
        let tier2_dir = root.path().join("plugins").join("mcp_windows_mcp");
        assert!(
            tier2_dir.join(".mcp-managed").exists(),
            "前置：正常态先生成"
        );

        let config_file = root.path().join("config").join("mcp.json");
        std::fs::remove_file(&config_file).unwrap();
        let manifests = loader.discover(&[]).await.unwrap();
        assert!(
            manifests.iter().all(|m| m.id != "mcp_windows_mcp"),
            "配置文件已删除：不得再被发现（否则启动期 spawn + uv sync 复活）"
        );
        assert!(!tier2_dir.exists(), "用户根受管副本随配置文件删除清理");
        let tier1_dir = root
            .path()
            .join("resources")
            .join("plugins")
            .join("shared")
            .join("mcp_windows_mcp");
        assert!(!tier1_dir.exists(), "内置根受管副本随配置文件删除清理");
    }

    /// 已删配置组（条目级）：同一文件改写为空 mcpServers 同样是删除信号——
    /// 双根全清且发现面无 manifest；与文件级删除构成两组有区分度的删除输入。
    #[tokio::test]
    async fn test_installed_layout_entry_removed_no_revival() {
        let root = tempfile::tempdir().unwrap();
        let loader = make_installed_layout_fixture(root.path(), WINDOWS_MCP_BODY);
        loader.discover(&[]).await.unwrap();

        let config_file = root.path().join("config").join("mcp.json");
        std::fs::write(&config_file, r#"{"mcpServers":{}}"#).unwrap();
        let manifests = loader.discover(&[]).await.unwrap();
        assert!(
            manifests.iter().all(|m| m.id != "mcp_windows_mcp"),
            "条目已删除：不得再被发现"
        );
        assert!(
            !root.path().join("plugins").join("mcp_windows_mcp").exists(),
            "用户根受管副本随条目删除清理"
        );
    }

    /// 配置抖动保护契约不回归：文件在但解析失败（半写/手编打错）不得销毁
    /// 受管目录——删除信号只认 NotFound/条目移除，不认解析失败。
    #[tokio::test]
    async fn test_installed_layout_broken_config_keeps_managed_dirs() {
        let root = tempfile::tempdir().unwrap();
        let loader = make_installed_layout_fixture(root.path(), WINDOWS_MCP_BODY);
        loader.discover(&[]).await.unwrap();
        let tier2_dir = root.path().join("plugins").join("mcp_windows_mcp");
        assert!(
            tier2_dir.join(".mcp-managed").exists(),
            "前置：正常态先生成"
        );

        let config_file = root.path().join("config").join("mcp.json");
        std::fs::write(&config_file, r#"{"mcpServers":{ "broken":,,,}}"#).unwrap();
        loader.discover(&[]).await.unwrap();
        assert!(tier2_dir.exists(), "解析失败=配置抖动，不得清受管目录");
    }
}
