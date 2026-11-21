// @feature: FP-0.2.一 插件协议 | @ci: rust-test
//! 指纹与 venv 命令解析（fingerprint/stale/venv/sidecar_command/invoke 失败路径）。

use super::*;
#[test]
fn test_compute_plugin_fingerprint_stable_for_unchanged_dir() {
    // 同一目录两次计算指纹应相同（mtime 不变）
    let dir = std::env::temp_dir().join("invoker_fp_test_stable");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("plugin.json"), b"{}").unwrap();
    std::fs::write(dir.join("server.py"), b"print(1)").unwrap();
    let manifest = make_sidecar_manifest("test_fp", "python server.py");
    let fp1 = compute_plugin_fingerprint(&dir, &manifest);
    let fp2 = compute_plugin_fingerprint(&dir, &manifest);
    assert_eq!(fp1, fp2, "未变更的目录指纹应稳定");
    let _ = std::fs::remove_dir_all(&dir);
}
#[test]
fn test_compute_plugin_fingerprint_changes_on_file_edit() {
    // 修改 server.py 内容（更新 mtime）后指纹应变化
    let dir = std::env::temp_dir().join("invoker_fp_test_change");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("server.py"), b"print(1)").unwrap();
    let manifest = make_sidecar_manifest("test_fp2", "python server.py");
    let fp1 = compute_plugin_fingerprint(&dir, &manifest);
    // 确保跨过 mtime 秒级精度边界
    std::thread::sleep(std::time::Duration::from_secs_f64(1.1));
    std::fs::write(dir.join("server.py"), b"print(2) # changed").unwrap();
    let fp2 = compute_plugin_fingerprint(&dir, &manifest);
    assert_ne!(fp1, fp2, "文件修改后指纹必须变化，否则热加载不会触发");
    let _ = std::fs::remove_dir_all(&dir);
}
#[tokio::test]
async fn test_is_plugin_stale_ttl_short_circuits() {
    // TTL 内（1s）重复检测不应过期：首次记录后立即再查应返回 false
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader.clone());
    let manifest = make_sidecar_manifest("stale_ttl", "python server.py");
    // 首次：写入指纹，返回 false
    let first = invoker.is_plugin_stale("stale_ttl", &manifest).await;
    assert!(!first, "首次检测不应判为过期");
    // TTL 内立即再查：应短路返回 false
    let second = invoker.is_plugin_stale("stale_ttl", &manifest).await;
    assert!(!second, "TTL 内应短路返回 false（不算过期）");
}
#[tokio::test]
async fn test_force_unload_via_trait_method() {
    // force_unload 是 trait 方法（trait 默认实现被 invoker 覆盖）。
    // 对未加载的插件调 force_unload 应返回 Ok（幂等，无 sidecar 可 kill）。
    let loader = Arc::new(MockLoader::new());
    let invoker: Arc<dyn PluginInvoker> = Arc::new(PluginInvokerImpl::new(loader.clone()));
    let result = invoker.force_unload("never_loaded_plugin").await;
    assert!(result.is_ok(), "force_unload 未加载插件应返回 Ok");
}
#[test]
fn test_touch_member_last_used_records_activity() {
    // touch_member_last_used 应在成员记账（member_last_used，键=plugin_id）写入当前时刻
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);
    assert!(
        invoker.member_last_used.read().is_empty(),
        "初始 member_last_used 应为空"
    );
    invoker.touch_member_last_used("plugin_a");
    invoker.touch_member_last_used("plugin_b");
    assert_eq!(
        invoker.member_last_used.read().len(),
        2,
        "touch 两个插件后 member_last_used 应有 2 条"
    );
    // 再次 touch 同一插件应更新（不新增）
    invoker.touch_member_last_used("plugin_a");
    assert_eq!(
        invoker.member_last_used.read().len(),
        2,
        "重复 touch 不应新增条目"
    );
}
#[tokio::test]
async fn test_discover_new_plugins_returns_via_trait() {
    // discover_new_plugins 是 trait 方法，转发到 loader.discover。
    // MockLoader.discover 返回 manifests 缓存里的全部（默认空）。
    let loader = Arc::new(MockLoader::new());
    let invoker: Arc<dyn PluginInvoker> = Arc::new(PluginInvokerImpl::new(loader));
    let result = invoker.discover_new_plugins().await;
    assert!(result.is_ok(), "discover_new_plugins 应返回 Ok");
    assert_eq!(result.unwrap().len(), 0, "空 MockLoader 应发现 0 个插件");
}
#[test]
fn test_venv_interpreter_layout_both_platforms() {
    // 纯路径选择逻辑：Windows 布局 .venv/Scripts/python.exe，
    // Unix 布局 .venv/bin/python——两分支必须在任意平台上都真实覆盖。
    let dir = std::path::Path::new("D:/proj/plugins/shared/demo");
    let win = venv_interpreter_layout(dir, true);
    assert_eq!(win, dir.join(".venv").join("Scripts").join("python.exe"));
    let unix = venv_interpreter_layout(dir, false);
    assert_eq!(unix, dir.join(".venv").join("bin").join("python"));
}
#[test]
fn test_find_venv_interpreter_detects_windows_layout() {
    // 假 .venv（Scripts/python.exe 占位文件）→ 探测命中返回绝对路径。
    let tmp = tempfile::tempdir().unwrap();
    let expected = fake_venv(tmp.path(), true);
    assert_eq!(find_venv_interpreter(tmp.path()), Some(expected));
}
#[test]
fn test_find_venv_interpreter_detects_unix_layout() {
    // Unix 布局（bin/python）也须可探测——跨平台分支不因运行平台而漏。
    let tmp = tempfile::tempdir().unwrap();
    let expected = fake_venv(tmp.path(), false);
    assert_eq!(find_venv_interpreter(tmp.path()), Some(expected));
}
#[test]
fn test_find_venv_interpreter_missing_venv_returns_none() {
    // 无 .venv → None（resolve_sidecar_command 层据此 fail-closed，单轨）。
    let tmp = tempfile::tempdir().unwrap();
    assert_eq!(find_venv_interpreter(tmp.path()), None);
    // .venv 目录存在但解释器缺失（半成品）同样 None，不误切。
    std::fs::create_dir_all(tmp.path().join(".venv")).unwrap();
    assert_eq!(find_venv_interpreter(tmp.path()), None);
}
#[test]
fn test_is_plain_python_command() {
    // 只有 PATH 裸 python/python3（含 Windows 可执行扩展）才被 venv 替换；
    // 绝对路径/其他解释器（node 等）不动。
    assert!(is_plain_python_command("python"));
    assert!(is_plain_python_command("python3"));
    assert!(is_plain_python_command("python.exe"));
    assert!(!is_plain_python_command("node"));
    assert!(!is_plain_python_command("D:/py/python.exe"));
    assert!(!is_plain_python_command("/usr/bin/python3"));
}
#[test]
fn test_resolve_sidecar_command_uses_venv_interpreter() {
    // 插件目录有 .venv → command 换成 venv 解释器绝对路径，args 原样保留。
    let tmp = tempfile::tempdir().unwrap();
    let interp = fake_venv(tmp.path(), true);

    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "demo_tool".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    let invoker = PluginInvokerImpl::new(loader);
    let manifest = make_sidecar_manifest("demo_tool", "python server.py");

    let (cmd, args) = invoker.resolve_sidecar_command(&manifest).unwrap();
    assert_eq!(cmd, interp.to_string_lossy().into_owned());
    assert_eq!(args, vec!["server.py"]);
}
#[test]
fn test_resolve_sidecar_command_unix_venv_layout() {
    // Unix 布局 venv 同样命中（find 的跨平台回退探测）。
    let tmp = tempfile::tempdir().unwrap();
    let interp = fake_venv(tmp.path(), false);

    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "demo_tool".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    let invoker = PluginInvokerImpl::new(loader);
    let manifest = make_sidecar_manifest("demo_tool", "python server.py");

    let (cmd, _) = invoker.resolve_sidecar_command(&manifest).unwrap();
    assert_eq!(cmd, interp.to_string_lossy().into_owned());
}
#[test]
fn test_resolve_sidecar_command_venv_without_pyproject_fails_closed() {
    // 回归闸：仅 .venv 无 pyproject
    // → Err（PYPROJECT_MISSING）而非回退裸 python——plain 轨已删，半契约状态
    // 必须早失败并给出修复指引（fail-closed）。
    let tmp = tempfile::tempdir().unwrap();
    let interp = if cfg!(windows) {
        tmp.path().join(".venv").join("Scripts").join("python.exe")
    } else {
        tmp.path().join(".venv").join("bin").join("python")
    };
    std::fs::create_dir_all(interp.parent().unwrap()).unwrap();
    std::fs::write(&interp, b"").unwrap();
    assert!(interp.is_file(), "测试前置：假 venv 解释器已就位");

    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "legacy_venv".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    let invoker = PluginInvokerImpl::new(loader);
    let manifest = make_sidecar_manifest("legacy_venv", "python server.py");

    let err = invoker
        .resolve_sidecar_command(&manifest)
        .expect_err("无 pyproject 的孤立 venv 必须失败（批 D 单轨 fail-closed）");
    assert_eq!(err.code.as_deref(), Some("PYPROJECT_MISSING"));
    assert!(
        err.message.contains("pyproject.toml") && err.message.contains("migrate_plugins_to_uv"),
        "错误信息必须含可读修复指引，实际: {}",
        err.message
    );
}
#[test]
fn test_resolve_sidecar_command_no_venv_fails_closed() {
    // 有 pyproject 无 .venv → Err（VENV_INTERPRETER_MISSING），错误含
    // `uv venv`/`uv sync` 重建指引——不再回退 PATH 裸 python。
    // 用户根钉到空目录：登记处兜底缺席的判定必须与机器环境无关（确定性）。
    let tmp = tempfile::tempdir().unwrap();
    std::fs::write(tmp.path().join("pyproject.toml"), b"[project]\n").unwrap();
    let empty_user = tempfile::tempdir().unwrap();
    let _guard = UserRootGuard::pin_to(empty_user.path());

    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "demo_tool".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    let invoker = PluginInvokerImpl::new(loader);
    let manifest = make_sidecar_manifest("demo_tool", "python server.py");

    let err = invoker
        .resolve_sidecar_command(&manifest)
        .expect_err("缺 .venv 解释器必须失败（批 D 单轨 fail-closed）");
    assert_eq!(err.code.as_deref(), Some("VENV_INTERPRETER_MISSING"));
    assert!(
        err.message.contains("uv venv") && err.message.contains("uv sync"),
        "错误信息必须含可读修复指引，实际: {}",
        err.message
    );
    // 半成品 .venv（目录存在但解释器缺失）同样失败——不误切也不兜底。
    std::fs::create_dir_all(tmp.path().join(".venv")).unwrap();
    let err2 = invoker
        .resolve_sidecar_command(&manifest)
        .expect_err("半成品 .venv（无解释器）必须失败");
    assert_eq!(err2.code.as_deref(), Some("VENV_INTERPRETER_MISSING"));
}
#[test]
fn test_venv_direct_interpreter_windows_trampoline_preserved() {
    // 跨平台语义契约：venv 解释器必须以能给出正确 sys.prefix/site-packages
    // 的形态 spawn——Windows trampoline 本身就满足（直跑 base 真身反而丢失
    // venv 身份）。完整可解析 pyvenv.cfg 夹具下断言跳板原样保留、零 env 注入。
    let tmp = tempfile::tempdir().unwrap();
    let (trampoline, _base) = fake_trampoline_venv(tmp.path(), "plugin-venvs/demo");
    let args = vec![
        "server.py".to_string(),
        "--port".to_string(),
        "8000".to_string(),
    ];

    let (cmd, args_out, env) =
        resolve_venv_direct_interpreter(&trampoline.to_string_lossy(), args.clone(), true);

    assert_eq!(
        cmd,
        trampoline.to_string_lossy(),
        "Windows trampoline 必须原样保留（真身替换丢失 venv sys.prefix，禁翻回）"
    );
    assert_eq!(args_out, args, "args 必须原样透传");
    assert_eq!(env, None, "不得注入任何 launcher 变量（Windows 不识别）");
}
#[test]
fn test_venv_direct_interpreter_unix_untouched() {
    // Unix venv python 是 symlink 单进程——恒原样，无 env 注入。
    let tmp = tempfile::tempdir().unwrap();
    let venv_py = tmp.path().join(".venv").join("bin").join("python");
    std::fs::create_dir_all(venv_py.parent().unwrap()).unwrap();
    std::fs::write(&venv_py, b"").unwrap();

    let (cmd, args_out, env) = resolve_venv_direct_interpreter(
        &venv_py.to_string_lossy(),
        vec!["server.py".into()],
        false,
    );

    assert_eq!(cmd, venv_py.to_string_lossy());
    assert_eq!(args_out, vec!["server.py".to_string()]);
    assert_eq!(env, None);
}
#[test]
fn test_venv_direct_interpreter_unresolvable_falls_back_identically() {
    // 性质断言：一切非 trampoline/不可解析形态——裸命令 / 无 Scripts 父目录 /
    // 缺 pyvenv.cfg / cfg 无 home / home 指向缺席解释器——输出与输入逐项恒等。
    let tmp = tempfile::tempdir().unwrap();
    let args = vec!["server.py".to_string()];
    let trampoline = tmp.path().join("v").join("Scripts").join("python.exe");

    let inputs = [
        "npx".to_string(),
        "C:/tools/standalone/python.exe".to_string(), // 无 Scripts 父目录
        trampoline.to_string_lossy().into_owned(),    // 无 pyvenv.cfg
    ];
    for input in inputs {
        let (cmd, a, env) = resolve_venv_direct_interpreter(&input, args.clone(), true);
        assert_eq!(
            (cmd.as_str(), a.as_slice(), env),
            (input.as_str(), &args[..], None),
            "不可解析输入必须恒等回落: {input}"
        );
    }

    // 有 pyvenv.cfg 但无 home 键 → 回落。
    std::fs::create_dir_all(trampoline.parent().unwrap()).unwrap();
    std::fs::write(&trampoline, b"").unwrap();
    std::fs::write(
        tmp.path().join("v").join("pyvenv.cfg"),
        "implementation = CPython\nversion = 3.12.0\n",
    )
    .unwrap();
    let (cmd, _, env) =
        resolve_venv_direct_interpreter(&trampoline.to_string_lossy(), args.clone(), true);
    assert_eq!(cmd, trampoline.to_string_lossy());
    assert_eq!(env, None);

    // home 指向的解释器不存在 → 回落。
    std::fs::write(
        tmp.path().join("v").join("pyvenv.cfg"),
        "home = D:\\no\\such\\base\n",
    )
    .unwrap();
    let (cmd, _, env) =
        resolve_venv_direct_interpreter(&trampoline.to_string_lossy(), args.clone(), true);
    assert_eq!(cmd, trampoline.to_string_lossy());
    assert_eq!(env, None);
}
#[test]
fn test_venv_direct_interpreter_for_spawn_passthrough_all_platforms() {
    // spawn 包装与直达解析同语义：任意平台上 trampoline 形态原样透传、
    // 非 trampoline 形态恒原样、零 env 注入（平台门收敛为同一恒等契约）。
    let tmp = tempfile::tempdir().unwrap();
    let (trampoline, _base) = fake_trampoline_venv(tmp.path(), "plugin-venvs/gated");

    let (cmd, _, env) = resolve_venv_direct_interpreter_for_spawn("npx", vec!["-y".into()]);
    assert_eq!(cmd, "npx");
    assert_eq!(env, None);

    let (cmd, args_out, env) = resolve_venv_direct_interpreter_for_spawn(
        &trampoline.to_string_lossy(),
        vec!["-m".into(), "server".into()],
    );
    assert_eq!(
        cmd,
        trampoline.to_string_lossy(),
        "spawn 期同样必须保留跳板（Windows 真身替换已废，禁翻回）"
    );
    assert_eq!(args_out, vec!["-m".to_string(), "server".to_string()]);
    assert_eq!(env, None);
}
#[test]
fn test_resolve_sidecar_command_falls_back_to_user_space_venv() {
    // 解析序②：插件目录 .venv 缺席（装机态打包排除）+ 登记处有解释器
    //（autoprovision 重定向产物）→ command = 登记处解释器绝对路径。
    let plugin_dir = tempfile::tempdir().unwrap();
    std::fs::write(plugin_dir.path().join("pyproject.toml"), b"[project]\n").unwrap();
    let user_root = tempfile::tempdir().unwrap();
    let _guard = UserRootGuard::pin_to(user_root.path());
    let registered = fake_registered_venv(user_root.path(), "host_context");

    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "host_context".to_string(),
        plugin_dir.path().to_string_lossy().into_owned(),
    );
    let invoker = PluginInvokerImpl::new(loader);
    let manifest = make_sidecar_manifest("host_context", "python server.py");

    let (cmd, args) = invoker.resolve_sidecar_command(&manifest).unwrap();
    assert_eq!(
        cmd,
        registered.to_string_lossy().into_owned(),
        "登记处解释器必须被命中（装机态兜底）"
    );
    assert_eq!(args, vec!["server.py"]);
}
#[test]
fn test_resolve_sidecar_command_plugin_dir_venv_wins_over_user_space() {
    // 解析序优先级：两级都有 → 插件目录 .venv 赢（纯 dev 路径零变化——
    // dev 机器即使用户空间残留登记项也绝不改变解析结果）。
    let plugin_dir = tempfile::tempdir().unwrap();
    let local = fake_venv(plugin_dir.path(), true);
    let user_root = tempfile::tempdir().unwrap();
    let _guard = UserRootGuard::pin_to(user_root.path());
    fake_registered_venv(user_root.path(), "demo_tool");

    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "demo_tool".to_string(),
        plugin_dir.path().to_string_lossy().into_owned(),
    );
    let invoker = PluginInvokerImpl::new(loader);
    let manifest = make_sidecar_manifest("demo_tool", "python server.py");

    let (cmd, _) = invoker.resolve_sidecar_command(&manifest).unwrap();
    assert_eq!(
        cmd,
        local.to_string_lossy().into_owned(),
        "插件目录 .venv 恒优先于登记处"
    );
}
#[test]
fn test_resolve_sidecar_command_user_space_key_traversal_rejected() {
    // 防御面：登记键含穿越形态（模拟 id 非法）→ 兜底直接 None，走 fail-closed，
    // 绝不按注入路径探测用户空间。
    let plugin_dir = tempfile::tempdir().unwrap();
    std::fs::write(plugin_dir.path().join("pyproject.toml"), b"[project]\n").unwrap();
    let user_root = tempfile::tempdir().unwrap();
    let _guard = UserRootGuard::pin_to(user_root.path());
    // 用户空间里恶意放置 ../ 形态可达位置的"venv"，合法键探测不得命中
    fake_registered_venv(user_root.path().parent().unwrap(), "evil");

    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "../evil".to_string(),
        plugin_dir.path().to_string_lossy().into_owned(),
    );
    let invoker = PluginInvokerImpl::new(loader);
    let manifest = make_sidecar_manifest("../evil", "python server.py");

    let err = invoker
        .resolve_sidecar_command(&manifest)
        .expect_err("非法键必须 fail-closed");
    assert_eq!(err.code.as_deref(), Some("VENV_INTERPRETER_MISSING"));
}
#[test]
fn test_resolve_sidecar_command_unknown_dir_fails_closed() {
    // loader 查不到插件目录（get_plugin_dir None）→ Err（
    // 原 keeps_plain 双轨语义已删——查不到目录就无法定位 venv，fail-closed）。
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);
    let manifest = make_sidecar_manifest("ghost", "python server.py");
    let err = invoker
        .resolve_sidecar_command(&manifest)
        .expect_err("未知插件目录的 Python sidecar 必须失败");
    assert_eq!(err.code.as_deref(), Some("PLUGIN_DIR_NOT_FOUND"));
}
#[test]
fn test_resolve_sidecar_command_explicit_interpreter_path_untouched() {
    // entry 首词带路径分隔符的显式解释器是刻意选择（is_plain_python_command
    // 不判）→ 原样返回，不替换也不 fail-closed。
    let loader = Arc::new(MockLoader::new());
    let invoker = PluginInvokerImpl::new(loader);
    let manifest = make_sidecar_manifest("ghost", "/usr/bin/python3 server.py");
    let (cmd, args) = invoker.resolve_sidecar_command(&manifest).unwrap();
    assert_eq!(cmd, "/usr/bin/python3");
    assert_eq!(args, vec!["server.py"]);
}
#[test]
fn test_resolve_sidecar_command_non_python_entry_not_replaced() {
    // entry 首词非 python（如 node server.js）→ 即使目录有 .venv 也不替换。
    let tmp = tempfile::tempdir().unwrap();
    fake_venv(tmp.path(), true);
    let loader = Arc::new(MockLoader::new());
    loader.plugin_dirs.write().insert(
        "demo_tool".to_string(),
        tmp.path().to_string_lossy().into_owned(),
    );
    let invoker = PluginInvokerImpl::new(loader);
    let manifest = make_sidecar_manifest("demo_tool", "node server.js");

    let (cmd, _) = invoker.resolve_sidecar_command(&manifest).unwrap();
    assert_eq!(cmd, "node");
}
#[test]
fn test_extract_mcp_content_is_error_without_text_uses_default() {
    // isError=true 但 content[0] 无 text 字段 → 用默认错误文案。
    let mcp_result = json!({
        "content": [{"type": "text"}],
        "isError": true
    });
    let extracted = extract_mcp_content(&mcp_result);
    assert_eq!(extracted["error"], "MCP tool returned isError=true");
}
#[test]
fn test_extract_mcp_content_is_error_without_content_uses_default() {
    // isError=true 且无 content 字段 → 用默认错误文案。
    let mcp_result = json!({"isError": true});
    let extracted = extract_mcp_content(&mcp_result);
    assert_eq!(extracted["error"], "MCP tool returned isError=true");
}
#[test]
fn test_extract_mcp_content_multiple_items_takes_first() {
    // content 多元素时取第一项（Python SDK 只产出单元素数组）。
    let mcp_result = json!({
        "content": [
            {"type": "text", "text": r#"{"first": true}"#},
            {"type": "text", "text": r#"{"second": true}"#}
        ],
        "isError": false
    });
    let extracted = extract_mcp_content(&mcp_result);
    assert_eq!(extracted["first"], true);
    assert!(extracted.get("second").is_none());
}
#[test]
fn test_extract_mcp_content_non_string_text_normalized_as_empty() {
    // text 不是字符串（如数字）→ 非内部 JSON 约定 → 标准协议归一：
    // text 项按字符串语义取值失败计空 → 空 output 成功信封（不裸透）。
    let mcp_result = json!({
        "content": [{"type": "text", "text": 12345}],
        "isError": false
    });
    let extracted = extract_mcp_content(&mcp_result);
    assert_eq!(extracted["success"], true);
    assert_eq!(extracted["output"], json!({}));
}
#[tokio::test]
async fn test_invoke_tool_sidecar_spawn_failure_returns_mcp_connect_failed() {
    // 入口命令不存在 → spawn 失败 → 降级为 MCP_CONNECT_FAILED 错误（不 panic、不卡死）。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_bad_entry_tool_manifest("tool_spawn_fail"));

    let invoker = PluginInvokerImpl::new(loader);
    let err = invoker
        .invoke_tool("tool_spawn_fail", "some_tool", &json!({"x": 1}))
        .await
        .unwrap_err();
    assert_eq!(err.code.as_deref(), Some("MCP_CONNECT_FAILED"));
}
#[tokio::test]
async fn test_invoke_pipeline_sidecar_spawn_failure_returns_mcp_connect_failed() {
    // pipeline 类型 sidecar：入口命令不存在 → MCP_CONNECT_FAILED。
    let loader = Arc::new(MockLoader::new());
    let mut m = make_pipeline_sidecar_manifest("pipe_spawn_fail", Some("ctx_build.execute"));
    m.entry = "definitely_missing_command_98765 --flag".to_string();
    loader.add_manifest(m);

    let invoker = PluginInvokerImpl::new(loader);
    let state = json!({});
    let ctx = PluginContext::new(
        &state,
        json!({}),
        TenantContext::new("t1", "s1"),
        Uuid::new_v4(),
        agentos_core::types::ContentLoader::new(
            Arc::new(MockStorage),
            "run1".to_string(),
            "main".to_string(),
        ),
    );
    let err = invoker
        .invoke_pipeline_plugin("pipe_spawn_fail", &ctx)
        .await
        .unwrap_err();
    assert_eq!(err.code.as_deref(), Some("MCP_CONNECT_FAILED"));
}
#[tokio::test]
async fn test_invoke_streamable_http_missing_endpoint_errors() {
    // transport=StreamableHttp 但无 endpoint → MCP_CONFIG_INVALID。
    let loader = Arc::new(MockLoader::new());
    let mut m = make_sidecar_manifest("http_no_ep", "unused entry");
    m.mcp = Some(agentos_core::traits::McpConfig {
        allowed_roots: Vec::new(),
        transport: agentos_core::traits::McpTransport::StreamableHttp,
        endpoint: None,
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    loader.add_manifest(m);

    let invoker = PluginInvokerImpl::new(loader);
    let err = invoker
        .invoke_tool("http_no_ep", "t", &json!({}))
        .await
        .unwrap_err();
    assert_eq!(err.code.as_deref(), Some("MCP_CONFIG_INVALID"));
}
#[tokio::test]
async fn test_invoke_streamable_http_endpoint_missing_url_errors() {
    // transport=StreamableHttp 且有 endpoint 但缺 url → MCP_CONFIG_INVALID。
    let loader = Arc::new(MockLoader::new());
    let mut m = make_sidecar_manifest("http_no_url", "unused entry");
    m.mcp = Some(agentos_core::traits::McpConfig {
        allowed_roots: Vec::new(),
        transport: agentos_core::traits::McpTransport::StreamableHttp,
        endpoint: Some(agentos_core::traits::McpEndpoint {
            url: None,
            ..Default::default()
        }),
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    loader.add_manifest(m);

    let invoker = PluginInvokerImpl::new(loader);
    let err = invoker
        .invoke_tool("http_no_url", "t", &json!({}))
        .await
        .unwrap_err();
    assert_eq!(err.code.as_deref(), Some("MCP_CONFIG_INVALID"));
}
#[tokio::test]
async fn test_invoke_stdio_external_bad_env_placeholder_errors() {
    // Stdio 外部命令的 env 含无法解析的 ${VAR} 占位 → MCP_CONFIG_INVALID
    // （在 spawn 前暴露，避免启动后 import error 卡死）。
    let loader = Arc::new(MockLoader::new());
    let mut m = make_sidecar_manifest("stdio_bad_env", "unused entry");
    let mut env = std::collections::HashMap::new();
    env.insert(
        "PLUGIN_HOME".to_string(),
        "${DEFINITELY_UNSET_VAR_XYZ}".to_string(),
    );
    m.mcp = Some(agentos_core::traits::McpConfig {
        allowed_roots: Vec::new(),
        transport: agentos_core::traits::McpTransport::Stdio,
        endpoint: Some(agentos_core::traits::McpEndpoint {
            command: Some("npx".to_string()),
            args: vec!["-y".to_string()],
            env,
            ..Default::default()
        }),
        idle_timeout_secs: 300,
        protocol_version: "2025-06-18".to_string(),
        request_timeout_secs: None,
    });
    loader.add_manifest(m);

    let invoker = PluginInvokerImpl::new(loader);
    let err = invoker
        .invoke_tool("stdio_bad_env", "t", &json!({}))
        .await
        .unwrap_err();
    assert_eq!(err.code.as_deref(), Some("MCP_CONFIG_INVALID"));
    assert!(
        err.message.contains("env 解析失败"),
        "错误应点名 env 解析失败: {}",
        err.message
    );
}
#[tokio::test]
async fn test_invoke_native_tool_without_loader_errors() {
    // B2：invoke_native_tool 已接通 execute 的 tool_call 约定字段——
    // 未注入 loader 时返回 NATIVE_LOADER_NOT_CONFIGURED（与 pipeline 路径一致），
    // 不再是旧的 NATIVE_TOOL_UNSUPPORTED 硬错误。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_inprocess_manifest("native_tool_plug"));

    let invoker = PluginInvokerImpl::new(loader);
    let err = invoker
        .invoke_tool("native_tool_plug", "some_tool", &json!({}))
        .await
        .unwrap_err();
    assert_eq!(err.code.as_deref(), Some("NATIVE_LOADER_NOT_CONFIGURED"));
}
#[tokio::test]
async fn test_invoke_native_tool_missing_artifact_errors() {
    // 注入了 native loader 但 manifest 缺 native.artifact → MISSING_NATIVE_ARTIFACT
    // （B2 后工具路径与 pipeline 路径同门：resolve artifact 是第一道校验）。
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_inprocess_manifest("native_tool_no_artifact"));

    let invoker =
        PluginInvokerImpl::new(loader).set_native_loader(Arc::new(NativePluginLoader::new()));
    let err = invoker
        .invoke_tool("native_tool_no_artifact", "some_tool", &json!({}))
        .await
        .unwrap_err();
    assert_eq!(err.code.as_deref(), Some("MISSING_NATIVE_ARTIFACT"));
}
#[tokio::test]
async fn test_invoke_native_tool_bad_artifact_errors() {
    // artifact 指向不存在的文件 → NATIVE_LOAD_FAILED（真实 NativePluginLoader 路径）。
    let loader = Arc::new(MockLoader::new());
    let mut m = make_inprocess_manifest("native_tool_bad_artifact");
    m.native = Some(agentos_core::traits::NativeArtifact {
        artifact: "definitely_missing_plugin.dll".to_string(),
        pre_dispatch: None,
    });
    loader.add_manifest(m);
    loader.plugin_dirs.write().insert(
        "native_tool_bad_artifact".to_string(),
        std::env::temp_dir().to_string_lossy().to_string(),
    );

    let invoker =
        PluginInvokerImpl::new(loader).set_native_loader(Arc::new(NativePluginLoader::new()));
    let err = invoker
        .invoke_tool("native_tool_bad_artifact", "some_tool", &json!({}))
        .await
        .unwrap_err();
    // resolve_artifact 双查后前置报 NOT_FOUND（比拖到 dlopen 的 LOAD_FAILED 更可读）
    assert_eq!(err.code.as_deref(), Some("NATIVE_ARTIFACT_NOT_FOUND"));
}
#[tokio::test]
async fn test_idle_timeout_secs_sync_manifest_never_unload() {
    // manifest 声明 Some(0) = 永不空闲卸载。
    let loader = Arc::new(MockLoader::new());
    let mut m = make_sidecar_manifest("never_idle", "python server.py");
    m.lifecycle = Some(agentos_core::traits::PluginLifecycle {
        idle_timeout_secs: Some(0),
    });
    loader.add_manifest(m);
    let invoker = PluginInvokerImpl::new(loader);
    assert_eq!(invoker.idle_timeout_secs_sync("never_idle"), 0);
}
#[tokio::test]
async fn test_idle_timeout_secs_sync_manifest_value() {
    let loader = Arc::new(MockLoader::new());
    let mut m = make_sidecar_manifest("custom_idle", "python server.py");
    m.lifecycle = Some(agentos_core::traits::PluginLifecycle {
        idle_timeout_secs: Some(42),
    });
    loader.add_manifest(m);
    let invoker = PluginInvokerImpl::new(loader);
    assert_eq!(invoker.idle_timeout_secs_sync("custom_idle"), 42);
}
#[tokio::test]
async fn test_idle_timeout_secs_sync_default() {
    // 未声明 lifecycle → 内核默认 300s。与 env 突变用例互斥（全局基线）。
    let _serial = ENV_SENSITIVE_LOCK.lock();
    let loader = Arc::new(MockLoader::new());
    loader.add_manifest(make_sidecar_manifest("default_idle", "python server.py"));
    let invoker = PluginInvokerImpl::new(loader);
    assert_eq!(
        invoker.idle_timeout_secs_sync("default_idle"),
        agentos_core::traits::default_idle_timeout()
    );
}
