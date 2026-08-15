// @feature: FP-0.2.CFG 配置系统与插件配置注入 | @vision: V3 可嵌入 | @ci: rust-test
//! F-CFG-1：`{{path:...}}` 外部文件引用的路径穿越防护。
//!
//! 审计发现：`resolve_path_refs` 直接 `project_root.join(raw_path)` 且无边界校验，
//! `{{path:../secret}}` 可读取 project_root 之外的任意文件并拼入配置。
//! 本测试锁定三条契约：穿越引用拒绝、绝对路径引用拒绝、正常引用不受影响。

use std::fs;

use agentos_config::ConfigLoader;
use tempfile::TempDir;

/// 构造 project/config 结构：
/// ```text
/// tmp/
///   ├── outside.md        <- 穿越逃逸目标（project_root 之外，存在）
///   ├── abs_target.md     <- 绝对路径逃逸目标（存在）
///   └── project/
///       └── config/       <- config_dir（project_root = config 的父目录）
///           └── rules/ok.md
/// ```
fn setup() -> TempDir {
    let tmp = TempDir::new().unwrap();
    let config_dir = tmp.path().join("project/config");
    fs::create_dir_all(config_dir.join("rules")).unwrap();
    fs::write(config_dir.join("rules/ok.md"), "OK-CONTENT").unwrap();
    fs::write(tmp.path().join("outside.md"), "TOP-SECRET").unwrap();
    fs::write(tmp.path().join("abs_target.md"), "ABS-SECRET").unwrap();
    tmp
}

#[test]
fn path_ref_rejects_traversal_escape() {
    let tmp = setup();
    let loader = ConfigLoader::new(tmp.path().join("project/config"), None);

    // `../outside.md` 解析后命中 project_root 之外的现有文件 → 必须拒绝
    let result = loader.resolve_path_refs("ref: {{path:../outside.md}}");
    assert!(
        result.is_err(),
        "穿越引用应被拒绝，实际读取到: {:?}",
        result.ok().filter(|s| s.contains("TOP-SECRET"))
    );

    // Windows 分隔符变体 `..\\outside.md` 同样拒绝
    let result_win = loader.resolve_path_refs("ref: {{path:..\\outside.md}}");
    assert!(result_win.is_err(), "`..\\` 变体也应被拒绝");
}

#[test]
fn path_ref_rejects_absolute_path() {
    let tmp = setup();
    let loader = ConfigLoader::new(tmp.path().join("project/config"), None);

    // 指向存在的绝对路径文件（Windows 用 `C:/...` 形式，Unix 用 `/tmp/...`）
    let abs = tmp.path().join("abs_target.md");
    let abs_str = abs.to_string_lossy().replace('\\', "/");
    let result = loader.resolve_path_refs(&format!("ref: {{{{path:{abs_str}}}}}"));
    assert!(
        result.is_err(),
        "绝对路径引用应被拒绝，实际读取到: {:?}",
        result.ok().filter(|s| s.contains("ABS-SECRET"))
    );
}

#[test]
fn path_ref_normal_refs_unaffected() {
    let tmp = setup();
    let loader = ConfigLoader::new(tmp.path().join("project/config"), None);

    // 正常相对引用（目录 + 扩展名、单文件，均相对 project_root）保持可用
    let dir = loader
        .resolve_path_refs("ref: {{path:config/rules|extensions=.md}}")
        .unwrap();
    assert!(dir.contains("OK-CONTENT"), "目录引用应正常读取: {dir:?}");

    let file = loader
        .resolve_path_refs("ref: {{path:config/rules/ok.md}}")
        .unwrap();
    assert!(file.contains("OK-CONTENT"), "单文件引用应正常读取");
}
