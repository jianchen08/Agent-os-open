//! 仓库脚手架结构验证测试
//!
//! 验证 task_03 产出的 8 crate workspace 骨架结构完整性。
//! 对应 AC-02-1 ~ AC-02-5。

/// kernel/ workspace 根目录（CARGO_MANIFEST_DIR = kernel/crates/core/）
fn workspace_root() -> String {
    format!("{}/../../", env!("CARGO_MANIFEST_DIR"))
}

/// 项目根目录（0.2 仓库根）
fn project_root() -> String {
    format!("{}/../../../", env!("CARGO_MANIFEST_DIR"))
}

/// AC-02-1: Cargo.toml workspace 含 8 个 crate 骨架
#[test]
fn test_workspace_has_8_crates() {
    let cargo_toml = std::fs::read_to_string(format!("{}Cargo.toml", workspace_root()))
        .expect("kernel/Cargo.toml 不存在");
    let expected_crates = [
        "crates/core",
        "crates/config",
        "crates/plugin-loader",
        "crates/mcp",
        "crates/invoker",
        "crates/tenant",
        "crates/api",
        "crates/hooks",
    ];
    for crate_path in &expected_crates {
        assert!(
            cargo_toml.contains(crate_path),
            "Cargo.toml 缺少 workspace member: {}",
            crate_path
        );
    }
}

/// AC-02-1: 每个 crate 都有 Cargo.toml 和 lib.rs
#[test]
fn test_all_crates_have_skeleton() {
    let crate_names = [
        "core",
        "config",
        "plugin-loader",
        "mcp",
        "invoker",
        "tenant",
        "api",
        "hooks",
    ];
    for name in &crate_names {
        let cargo_path = format!("{}crates/{}/Cargo.toml", workspace_root(), name);
        let lib_path = format!("{}crates/{}/src/lib.rs", workspace_root(), name);
        let cargo_content = std::fs::read_to_string(&cargo_path)
            .unwrap_or_else(|_| panic!("缺少文件: {}", cargo_path));
        let _lib_content =
            std::fs::read_to_string(&lib_path).unwrap_or_else(|_| panic!("缺少文件: {}", lib_path));
        assert!(
            cargo_content.contains("[package]"),
            "{} 的 Cargo.toml 缺少 [package] 段",
            name
        );
    }
}

/// AC-02-1: workspace dependencies 完整
#[test]
fn test_workspace_dependencies_present() {
    let cargo_toml = std::fs::read_to_string(format!("{}Cargo.toml", workspace_root()))
        .expect("kernel/Cargo.toml 不存在");
    let expected_deps = [
        "tokio",
        "serde",
        "serde_json",
        "async-trait",
        "thiserror",
        "tracing",
        "uuid",
        "chrono",
    ];
    for dep in &expected_deps {
        assert!(
            cargo_toml.contains(dep),
            "workspace dependencies 缺少: {}",
            dep
        );
    }
}

/// AC-02-1: 所有 crate 依赖 agentos-core（除了 core 自身）
#[test]
fn test_crates_depend_on_core() {
    let dependent_crates = [
        "config",
        "plugin-loader",
        "mcp",
        "invoker",
        "tenant",
        "api",
        "hooks",
    ];
    for name in &dependent_crates {
        let cargo_path = format!("{}crates/{}/Cargo.toml", workspace_root(), name);
        let content = std::fs::read_to_string(&cargo_path)
            .unwrap_or_else(|_| panic!("缺少文件: {}", cargo_path));
        assert!(
            content.contains("agentos-core"),
            "crate {} 应依赖 agentos-core",
            name
        );
    }
}

/// AC-02-2: rust-toolchain.toml 存在且版本 ≥1.83
#[test]
fn test_rust_toolchain_exists() {
    let content = std::fs::read_to_string(format!("{}rust-toolchain.toml", project_root()))
        .expect("rust-toolchain.toml 不存在");
    assert!(
        content.contains("channel"),
        "rust-toolchain.toml 缺少 channel 字段"
    );
    let version_line = content
        .lines()
        .find(|l| l.contains("channel"))
        .expect("未找到 channel 行");
    let has_valid_version = version_line.contains("\"1.83")
        || version_line.contains("\"1.84")
        || version_line.contains("\"1.85")
        || version_line.contains("\"1.86")
        || version_line.contains("\"1.87")
        || version_line.contains("\"1.88")
        || version_line.contains("\"1.89")
        || version_line.contains("\"1.90")
        || version_line.contains("stable");
    assert!(
        has_valid_version,
        "rust-toolchain.toml 版本应 ≥1.83 或 stable，实际: {}",
        version_line
    );
}

/// AC-02-3: ci.yml 含 5 个 job
#[test]
fn test_ci_has_5_jobs() {
    let ci_content = std::fs::read_to_string(format!("{}.github/workflows/ci.yml", project_root()))
        .expect("ci.yml 不存在");
    let expected_jobs = [
        "rust-lint",
        "rust-build",
        "rust-test",
        "python-lint",
        "python-test",
    ];
    for job in &expected_jobs {
        assert!(
            ci_content.contains(&format!("{}:", job)),
            "ci.yml 缺少 job: {}",
            job
        );
    }
}

/// AC-02-3: ci.yml 的 rust-lint job 包含 fmt 和 clippy
#[test]
fn test_ci_rust_lint_has_fmt_and_clippy() {
    let ci_content = std::fs::read_to_string(format!("{}.github/workflows/ci.yml", project_root()))
        .expect("ci.yml 不存在");
    assert!(
        ci_content.contains("cargo fmt"),
        "ci.yml 的 rust-lint job 应包含 cargo fmt"
    );
    assert!(
        ci_content.contains("clippy"),
        "ci.yml 的 rust-lint job 应包含 cargo clippy"
    );
}

/// AC-02-3: ci.yml 的 rust-test job 包含 cargo test
#[test]
fn test_ci_rust_test_has_cargo_test() {
    let ci_content = std::fs::read_to_string(format!("{}.github/workflows/ci.yml", project_root()))
        .expect("ci.yml 不存在");
    assert!(
        ci_content.contains("cargo test"),
        "ci.yml 的 rust-test job 应包含 cargo test"
    );
}

/// AC-02-4: Swatinem/rust-cache 配置 shared-key: agentos-0.2
#[test]
fn test_ci_has_rust_cache_shared_key() {
    let ci_content = std::fs::read_to_string(format!("{}.github/workflows/ci.yml", project_root()))
        .expect("ci.yml 不存在");
    assert!(
        ci_content.contains("Swatinem/rust-cache"),
        "ci.yml 应使用 Swatinem/rust-cache action"
    );
    assert!(
        ci_content.contains("agentos-0.2"),
        "ci.yml 的 rust-cache 应配置 shared-key: agentos-0.2"
    );
}

/// AC-02-5: config/ 目录存在且包含 YAML 文件
#[test]
fn test_config_directory_exists() {
    let config_path = format!("{}config", project_root());
    let config_entries = std::fs::read_dir(&config_path).expect("config/ 目录不存在");
    let mut yaml_count = 0;
    for entry in config_entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            if let Ok(sub_entries) = std::fs::read_dir(&path) {
                for sub_entry in sub_entries.flatten() {
                    let sub_path = sub_entry.path();
                    if sub_path
                        .extension()
                        .is_some_and(|e| e == "yaml" || e == "yml")
                    {
                        yaml_count += 1;
                    } else if sub_path.is_dir() {
                        if let Ok(deep_entries) = std::fs::read_dir(&sub_path) {
                            for deep_entry in deep_entries.flatten() {
                                if deep_entry
                                    .path()
                                    .extension()
                                    .is_some_and(|e| e == "yaml" || e == "yml")
                                {
                                    yaml_count += 1;
                                }
                            }
                        }
                    }
                }
            }
        } else if path.extension().is_some_and(|e| e == "yaml" || e == "yml") {
            yaml_count += 1;
        }
    }
    assert!(
        yaml_count > 0,
        "config/ 目录应包含 YAML 配置文件，实际找到 {} 个",
        yaml_count
    );
}

/// 验证 Python SDK 目录结构存在
#[test]
fn test_python_sdk_scaffold_exists() {
    let pyproject =
        std::fs::read_to_string(format!("{}plugins/sdk/pyproject.toml", project_root()));
    assert!(pyproject.is_ok(), "plugins/sdk/pyproject.toml 应存在");
}

/// 验证所有 crate 的 crate name 命名规范（agentos- 前缀）
#[test]
fn test_crate_naming_convention() {
    let crate_dirs = [
        ("core", "agentos-core"),
        ("config", "agentos-config"),
        ("plugin-loader", "agentos-plugin-loader"),
        ("mcp", "agentos-mcp"),
        ("invoker", "agentos-invoker"),
        ("tenant", "agentos-tenant"),
        ("api", "agentos-api"),
        ("hooks", "agentos-hooks"),
    ];
    for (dir, expected_name) in &crate_dirs {
        let cargo_path = format!("{}crates/{}/Cargo.toml", workspace_root(), dir);
        let content = std::fs::read_to_string(&cargo_path)
            .unwrap_or_else(|_| panic!("缺少文件: {}", cargo_path));
        assert!(
            content.contains(&format!("name = \"{}\"", expected_name)),
            "crate {} 的 name 应为 {}",
            dir,
            expected_name
        );
    }
}
