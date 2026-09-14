// @feature: FP-0.2.一 插件协议·plugin_check 契约校验引擎 | @ci: rust-test
//! plugin_check CLI 端到端：真实调用编译出的 bin，覆盖参数解析 / --root 扫描 /
//! --json / --deny 退出码 / 人类可读输出五条面。
//!
//! 为什么不放进 bin 内 `#[cfg(test)]`：bin 的 `main` 只能经进程调用（`std::process::exit`
//! 会杀掉测试进程），故用 `CARGO_BIN_EXE_plugin_check` 拿 cargo 注入的真实可执行路径，
//! 断可观察行为（stdout 文案 / 退出码）而非内部实现。
//!
//! 覆盖检查语义（必填字段 / output_schema / provides / native 预检）的逐条断言留在
//! bin 内 `mod tests` 的 `check_one` 单测；本文件只管 CLI 面。

use std::path::{Path, PathBuf};
use std::process::Command;

/// 真实 bin 路径（cargo 为集成测试注入）。
fn bin() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_plugin_check"))
}

/// 写一个合法 manifest 到 `<dir>/<id>/plugin.json`，返回 manifest 路径。
fn write_valid_manifest(dir: &Path, id: &str) -> PathBuf {
    let p = dir.join(id).join("plugin.json");
    std::fs::create_dir_all(p.parent().unwrap()).unwrap();
    std::fs::write(
        &p,
        format!(
            r#"{{
                "id":"{id}","name":"{id}","version":"1.0.0",
                "plugin_type":"tool","language":"python","host_type":"sidecar",
                "entry":"python server.py",
                "capabilities":{{"tools":[{{"name":"t1","description":"t1"}}]}}
            }}"#
        ),
    )
    .unwrap();
    p
}

struct Run {
    code: Option<i32>,
    stdout: String,
    stderr: String,
}

fn run(args: &[&str]) -> Run {
    let out = Command::new(bin())
        .args(args)
        .output()
        .expect("plugin_check 可执行（cargo 提供路径）");
    Run {
        code: out.status.code(),
        stdout: String::from_utf8_lossy(&out.stdout).to_string(),
        stderr: String::from_utf8_lossy(&out.stderr).to_string(),
    }
}

fn write_invalid_manifest(dir: &Path, id: &str) -> PathBuf {
    // 非 composite 且 entry 为空 → 必填校验失败
    let p = dir.join(id).join("plugin.json");
    std::fs::create_dir_all(p.parent().unwrap()).unwrap();
    std::fs::write(
        &p,
        format!(
            r#"{{
                "id":"{id}","name":"{id}","version":"1.0.0",
                "plugin_type":"tool","language":"python","host_type":"sidecar",
                "entry":"","capabilities":{{}}
            }}"#
        ),
    )
    .unwrap();
    p
}

/// 人类可读输出：合法插件 → [OK] 行 + 汇总行 + 退出码 0。
#[test]
fn human_readable_output_reports_ok_and_zero_exit() {
    let dir = tempfile::tempdir().unwrap();
    let p = write_valid_manifest(dir.path(), "cli_ok");
    let r = run(&[p.to_str().unwrap()]);

    assert_eq!(r.code, Some(0), "合法插件默认退出码 0");
    assert!(r.stdout.contains("[OK ] cli_ok"), "stdout: {}", r.stdout);
    assert!(
        r.stdout.contains("checked=1 violated=0"),
        "汇总行应含计数: {}",
        r.stdout
    );
    // checks 逐条列出（前导 "·"）
    assert!(r.stdout.contains('·'), "应列出 checks: {}", r.stdout);
}

/// 人类可读输出：非法插件 → [BAD] 行 + ✗ 错误行 + 无 --deny 仍退出 0。
#[test]
fn human_readable_output_reports_bad_without_deny() {
    let dir = tempfile::tempdir().unwrap();
    let p = write_invalid_manifest(dir.path(), "cli_bad");
    let r = run(&[p.to_str().unwrap()]);

    assert_eq!(r.code, Some(0), "无 --deny 时有错也退出 0（非 CI 模式）");
    assert!(r.stdout.contains("[BAD] cli_bad"), "stdout: {}", r.stdout);
    assert!(r.stdout.contains('✗'), "应列出错误: {}", r.stdout);
    assert!(r.stdout.contains("checked=1 violated=1"), "{}", r.stdout);
}

/// --deny：任一插件非法 → 退出码 1（CI 用）。
#[test]
fn deny_flag_exits_one_on_violation() {
    let dir = tempfile::tempdir().unwrap();
    let p = write_invalid_manifest(dir.path(), "cli_deny_bad");
    let r = run(&["--deny", p.to_str().unwrap()]);
    assert_eq!(r.code, Some(1), "--deny + 违规 → 退出 1");
}

/// --deny 但全绿 → 退出码 0（不得误报）。
#[test]
fn deny_flag_exits_zero_when_all_valid() {
    let dir = tempfile::tempdir().unwrap();
    let p = write_valid_manifest(dir.path(), "cli_deny_ok");
    let r = run(&["--deny", p.to_str().unwrap()]);
    assert_eq!(r.code, Some(0), "--deny + 全绿 → 退出 0");
}

/// --json：输出可解析 JSON，含 checked/violated/plugins 三键与插件详情。
#[test]
fn json_output_is_parseable_with_summary_and_plugins() {
    let dir = tempfile::tempdir().unwrap();
    let good = write_valid_manifest(dir.path(), "json_good");
    let bad = write_invalid_manifest(dir.path(), "json_bad");
    let r = run(&["--json", good.to_str().unwrap(), bad.to_str().unwrap()]);

    assert_eq!(r.code, Some(0));
    let v: serde_json::Value = serde_json::from_str(&r.stdout).expect("--json 应输出合法 JSON");
    assert_eq!(v["checked"], 2);
    assert_eq!(v["violated"], 1);
    let plugins = v["plugins"].as_array().expect("plugins 数组");
    assert_eq!(plugins.len(), 2);
    let ids: Vec<&str> = plugins
        .iter()
        .map(|p| p["plugin_id"].as_str().unwrap())
        .collect();
    assert_eq!(ids, vec!["json_good", "json_bad"], "逐个校验保序");
    assert_eq!(plugins[0]["valid"], true);
    assert_eq!(plugins[1]["valid"], false);
    assert!(
        plugins[0]["manifest_path"]
            .as_str()
            .unwrap()
            .contains("json_good"),
        "报告应带 manifest 路径"
    );
}

/// --root：递归扫描目录（含嵌套子目录），一次校验全部插件。
#[test]
fn root_scan_discovers_nested_manifests() {
    let dir = tempfile::tempdir().unwrap();
    // 模拟 plugins/shared/{system,tools}/<name>/plugin.json 嵌套布局
    write_valid_manifest(dir.path().join("shared/tools").as_path(), "nested_a");
    write_valid_manifest(dir.path().join("shared/system").as_path(), "nested_b");
    let r = run(&["--json", "--root", dir.path().to_str().unwrap()]);

    assert_eq!(r.code, Some(0));
    let v: serde_json::Value = serde_json::from_str(&r.stdout).unwrap();
    assert_eq!(
        v["checked"], 2,
        "嵌套目录下 manifest 均被发现: {}",
        r.stdout
    );
    assert_eq!(v["violated"], 0);
}

/// --root 扫描跳过 node_modules（第三方自带 plugin.json 非本仓插件）。
#[test]
fn root_scan_skips_node_modules() {
    let dir = tempfile::tempdir().unwrap();
    write_valid_manifest(dir.path(), "keep_me");
    write_valid_manifest(&dir.path().join("node_modules/pkg"), "third_party");
    let r = run(&["--json", "--root", dir.path().to_str().unwrap()]);

    let v: serde_json::Value = serde_json::from_str(&r.stdout).unwrap();
    assert_eq!(v["checked"], 1, "node_modules 应被跳过: {}", r.stdout);
    assert_eq!(v["plugins"][0]["plugin_id"], "keep_me");
}

/// plugin.yaml 形态也被 --root 发现（collect_manifests 扫两种文件名）。
#[test]
fn root_scan_finds_yaml_manifest() {
    let dir = tempfile::tempdir().unwrap();
    let p = dir.path().join("yaml_plugin/plugin.yaml");
    std::fs::create_dir_all(p.parent().unwrap()).unwrap();
    std::fs::write(
        &p,
        "id: yaml_plugin\nname: yaml_plugin\nversion: 1.0.0\n\
         plugin_type: tool\nlanguage: python\nhost_type: sidecar\n\
         entry: python server.py\ncapabilities:\n  tools:\n    - name: t1\n      description: t1\n",
    )
    .unwrap();
    let r = run(&["--json", "--root", dir.path().to_str().unwrap()]);

    assert_eq!(r.code, Some(0));
    let v: serde_json::Value = serde_json::from_str(&r.stdout).unwrap();
    assert_eq!(v["checked"], 1, "plugin.yaml 应被发现: {}", r.stdout);
    assert_eq!(v["plugins"][0]["plugin_id"], "yaml_plugin");
    assert_eq!(v["violated"], 0);
}

/// --root 与显式 manifest 混用 → 退出码 2（用法错误）。
#[test]
fn root_and_explicit_manifest_conflict_exits_two() {
    let dir = tempfile::tempdir().unwrap();
    let p = write_valid_manifest(dir.path(), "mixed");
    let r = run(&["--root", dir.path().to_str().unwrap(), p.to_str().unwrap()]);
    assert_eq!(r.code, Some(2), "混用应报用法错误");
    assert!(r.stderr.contains("不能混用"), "stderr: {}", r.stderr);
}

/// 没有任何待校验目标 → 退出码 2 + 引导文案。
#[test]
fn no_targets_exits_two_with_guidance() {
    let r = run(&[]);
    assert_eq!(r.code, Some(2));
    assert!(
        r.stderr.contains("没有待校验的插件"),
        "stderr: {}",
        r.stderr
    );
}

/// 未知参数（以 - 开头）→ 退出码 2。
#[test]
fn unknown_flag_exits_two() {
    let r = run(&["--nope"]);
    assert_eq!(r.code, Some(2));
    assert!(r.stderr.contains("未知参数"), "stderr: {}", r.stderr);
}

/// --help / -h → 打印用法并退出 0（含全部三个开关说明）。
#[test]
fn help_prints_usage_and_exits_zero() {
    for flag in ["--help", "-h"] {
        let r = run(&[flag]);
        assert_eq!(r.code, Some(0), "{flag} 应退出 0");
        assert!(
            r.stdout.contains("plugin_check"),
            "{flag} stdout: {}",
            r.stdout
        );
        for needle in ["--root", "--json", "--deny"] {
            assert!(
                r.stdout.contains(needle),
                "{flag} 用法应说明 {needle}: {}",
                r.stdout
            );
        }
    }
}

/// 不可读 manifest 路径 → 报告 (unreadable) + valid=false（不 panic、不中止）。
#[test]
fn unreadable_manifest_reports_unreadable() {
    let dir = tempfile::tempdir().unwrap();
    let ghost = dir.path().join("nope/plugin.json");
    let r = run(&["--json", ghost.to_str().unwrap()]);

    assert_eq!(r.code, Some(0));
    let v: serde_json::Value = serde_json::from_str(&r.stdout).unwrap();
    assert_eq!(v["violated"], 1);
    assert_eq!(v["plugins"][0]["plugin_id"], "(unreadable)");
    assert!(
        v["plugins"][0]["errors"][0]
            .as_str()
            .unwrap()
            .contains("读取失败"),
        "应报读取失败: {}",
        r.stdout
    );
}

/// 损坏 manifest（JSON/YAML 双解析失败）→ (parse-failed) + 两类错误并列。
#[test]
fn corrupt_manifest_reports_parse_failed() {
    let dir = tempfile::tempdir().unwrap();
    let p = dir.path().join("corrupt/plugin.json");
    std::fs::create_dir_all(p.parent().unwrap()).unwrap();
    std::fs::write(&p, "not a manifest: {{{").unwrap();
    let r = run(&["--json", p.to_str().unwrap()]);

    let v: serde_json::Value = serde_json::from_str(&r.stdout).unwrap();
    assert_eq!(v["plugins"][0]["plugin_id"], "(parse-failed)");
    let errs = v["plugins"][0]["errors"].as_array().unwrap();
    assert_eq!(errs.len(), 2, "JSON 与 YAML 两条解析错误并列: {errs:?}");
    assert!(errs[0].as_str().unwrap().contains("JSON"));
    assert!(errs[1].as_str().unwrap().contains("YAML"));
}

/// native 产物缺失 → 违规（与 loader 预检同规则：裸名按平台补 cdylib 后缀）。
#[test]
fn missing_native_artifact_is_violation() {
    let dir = tempfile::tempdir().unwrap();
    let p = dir.path().join("nat/plugin.json");
    std::fs::create_dir_all(p.parent().unwrap()).unwrap();
    std::fs::write(
        &p,
        r#"{
            "id":"nat","name":"nat","version":"1.0.0",
            "plugin_type":"tool","language":"rust","host_type":"in_process",
            "entry":"x",
            "capabilities":{},
            "native":{"artifact":"nat"}
        }"#,
    )
    .unwrap();
    let r = run(&["--json", p.to_str().unwrap()]);

    let v: serde_json::Value = serde_json::from_str(&r.stdout).unwrap();
    assert_eq!(v["violated"], 1);
    let errs = v["plugins"][0]["errors"].as_array().unwrap();
    assert!(
        errs.iter()
            .any(|e| e.as_str().unwrap().contains("native artifact 缺失")),
        "应报产物缺失: {errs:?}"
    );
}

/// native 产物在场 → 通过并列出"native artifact 存在"检查项。
#[test]
fn present_native_artifact_passes_check() {
    let dir = tempfile::tempdir().unwrap();
    let pdir = dir.path().join("nat_ok");
    std::fs::create_dir_all(&pdir).unwrap();
    // 与平台命名规则一致地产出假产物（内容不参与静态校验）
    let artifact = agentos_plugin_loader::NativePluginLoader::platform_artifact_name("nat_ok");
    std::fs::write(pdir.join(&artifact), b"").unwrap();
    std::fs::write(
        pdir.join("plugin.json"),
        r#"{
            "id":"nat_ok","name":"nat_ok","version":"1.0.0",
            "plugin_type":"tool","language":"rust","host_type":"in_process",
            "entry":"x",
            "capabilities":{},
            "native":{"artifact":"nat_ok"}
        }"#,
    )
    .unwrap();
    let r = run(&["--json", pdir.join("plugin.json").to_str().unwrap()]);

    let v: serde_json::Value = serde_json::from_str(&r.stdout).unwrap();
    assert_eq!(v["violated"], 0, "产物在场应通过: {}", r.stdout);
    let checks = v["plugins"][0]["checks"].as_array().unwrap();
    assert!(
        checks
            .iter()
            .any(|c| c.as_str().unwrap().contains("native artifact 存在")),
        "应列出产物存在检查项: {checks:?}"
    );
}

/// 多 manifest 逐个校验且保序（显式路径形态）。
#[test]
fn multiple_explicit_manifests_checked_in_order() {
    let dir = tempfile::tempdir().unwrap();
    let a = write_valid_manifest(dir.path(), "order_a");
    let b = write_valid_manifest(dir.path(), "order_b");
    let c = write_invalid_manifest(dir.path(), "order_c");
    let r = run(&[
        "--json",
        c.to_str().unwrap(),
        a.to_str().unwrap(),
        b.to_str().unwrap(),
    ]);

    let v: serde_json::Value = serde_json::from_str(&r.stdout).unwrap();
    let ids: Vec<&str> = v["plugins"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| p["plugin_id"].as_str().unwrap())
        .collect();
    assert_eq!(
        ids,
        vec!["order_c", "order_a", "order_b"],
        "按命令行给出顺序校验"
    );
    assert_eq!(v["checked"], 3);
    assert_eq!(v["violated"], 1);
}

// ── 必填字段逐项拒绝（写合法值再删一个，锁"哪一项缺 → 哪条错误"）──

/// 逐个必填字段缺失 → 对应的独立错误文案（id/name/version/language）。
/// 表驱动：基线 manifest 合法，仅按用例注空一个字段。
#[test]
fn each_required_field_reports_its_own_error() {
    let cases: [(&str, &str, &str); 4] = [
        ("id", "id 为空（必填）", ""),
        ("name", "name 为空（必填）", ""),
        ("version", "version 为空（必填）", ""),
        ("language", "language 为空（必填）", ""),
    ];
    let dir = tempfile::tempdir().unwrap();
    for (field, expected, _) in cases {
        let mut m = serde_json::json!({
            "id": "req_field",
            "name": "req_field",
            "version": "1.0.0",
            "plugin_type": "tool",
            "language": "python",
            "host_type": "sidecar",
            "entry": "python server.py",
            "capabilities": {},
        });
        m[field] = serde_json::json!("");
        let p = dir.path().join(format!("req_{field}/plugin.json"));
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::write(&p, serde_json::to_string(&m).unwrap()).unwrap();

        let r = run(&["--json", p.to_str().unwrap()]);
        let v: serde_json::Value = serde_json::from_str(&r.stdout).unwrap();
        assert_eq!(v["violated"], 1, "{field} 置空应判非法: {}", r.stdout);
        let errs = v["plugins"][0]["errors"].as_array().unwrap();
        assert!(
            errs.iter().any(|e| e.as_str().unwrap() == expected),
            "{field} 置空应报「{expected}」: {errs:?}"
        );
    }
}

/// 空/空白必填字段不得借"非空即合法"漏检：仅 id 非空时其余三项各自报错。
#[test]
fn multiple_missing_required_fields_are_all_reported() {
    let dir = tempfile::tempdir().unwrap();
    let p = dir.path().join("multi_missing/plugin.json");
    std::fs::create_dir_all(p.parent().unwrap()).unwrap();
    std::fs::write(
        &p,
        r#"{
            "id":"multi_missing","name":"","version":"","language":"",
            "plugin_type":"tool","host_type":"sidecar","entry":"","capabilities":{}
        }"#,
    )
    .unwrap();
    let r = run(&["--json", p.to_str().unwrap()]);

    let v: serde_json::Value = serde_json::from_str(&r.stdout).unwrap();
    let errs: Vec<&str> = v["plugins"][0]["errors"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| e.as_str().unwrap())
        .collect();
    for expected in [
        "name 为空（必填）",
        "version 为空（必填）",
        "language 为空（必填）",
        "entry 为空（非 composite 必填）",
    ] {
        assert!(
            errs.contains(&expected),
            "应并列报出「{expected}」: {errs:?}"
        );
    }
}

/// provides 公告的方法无对应声明工具 → 违规（与注册闸同一函数语义）。
#[test]
fn provides_unbacked_method_is_violation() {
    let dir = tempfile::tempdir().unwrap();
    let p = dir.path().join("unbacked/plugin.json");
    std::fs::create_dir_all(p.parent().unwrap()).unwrap();
    std::fs::write(
        &p,
        r#"{
            "id":"unbacked","name":"unbacked","version":"1.0.0",
            "plugin_type":"system","language":"python","host_type":"sidecar",
            "entry":"python server.py",
            "capabilities":{},
            "provides":{"capabilities":[
                {"namespace":"my-ns","methods":["do_thing"],"host":"sidecar"}
            ]}
        }"#,
    )
    .unwrap();
    let r = run(&["--json", p.to_str().unwrap()]);

    let v: serde_json::Value = serde_json::from_str(&r.stdout).unwrap();
    assert_eq!(v["violated"], 1);
    let errs = v["plugins"][0]["errors"].as_array().unwrap();
    let joined = errs
        .iter()
        .map(|e| e.as_str().unwrap())
        .collect::<Vec<_>>()
        .join("\n");
    assert!(
        joined.contains("provides") && joined.contains("my_ns.do_thing"),
        "应报未背书方法（namespace 连字符转下划线拼接）: {joined}"
    );
}

/// 对照：provides 方法有对应声明工具（`<prefix>.<method>`）→ 通过。
#[test]
fn provides_backed_method_passes_check() {
    let dir = tempfile::tempdir().unwrap();
    let p = dir.path().join("backed/plugin.json");
    std::fs::create_dir_all(p.parent().unwrap()).unwrap();
    std::fs::write(
        &p,
        r#"{
            "id":"backed","name":"backed","version":"1.0.0",
            "plugin_type":"system","language":"python","host_type":"sidecar",
            "entry":"python server.py",
            "capabilities":{"tools":[{"name":"my_ns.do_thing","description":"d"}]},
            "provides":{"capabilities":[
                {"namespace":"my-ns","methods":["do_thing"],"host":"sidecar"}
            ]}
        }"#,
    )
    .unwrap();
    let r = run(&["--json", p.to_str().unwrap()]);

    let v: serde_json::Value = serde_json::from_str(&r.stdout).unwrap();
    assert_eq!(v["violated"], 0, "有背书应通过: {}", r.stdout);
    let checks = v["plugins"][0]["checks"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c.as_str().unwrap())
        .collect::<Vec<_>>()
        .join("\n");
    assert!(
        checks.contains("provides 公告的方法均有声明工具"),
        "应列出 provides 检查项: {checks}"
    );
}

/// output_schema 非法 → 违规且错误文案指向该工具名（对照：合法 schema 通过）。
#[test]
fn malformed_output_schema_is_violation_with_tool_name() {
    let dir = tempfile::tempdir().unwrap();
    let p = dir.path().join("badschema/plugin.json");
    std::fs::create_dir_all(p.parent().unwrap()).unwrap();
    std::fs::write(
        &p,
        r#"{
            "id":"badschema","name":"badschema","version":"1.0.0",
            "plugin_type":"tool","language":"python","host_type":"sidecar",
            "entry":"python server.py",
            "capabilities":{"tools":[
                {"name":"tool_a","description":"d","output_schema":{"type":"not-a-real-type"}}
            ]}
        }"#,
    )
    .unwrap();
    let r = run(&["--json", p.to_str().unwrap()]);

    let v: serde_json::Value = serde_json::from_str(&r.stdout).unwrap();
    assert_eq!(v["violated"], 1);
    let errs = v["plugins"][0]["errors"].as_array().unwrap();
    let joined = errs
        .iter()
        .map(|e| e.as_str().unwrap())
        .collect::<Vec<_>>()
        .join("\n");
    assert!(
        joined.contains("output_schema") && joined.contains("tool_a"),
        "错误应定位到具体工具: {joined}"
    );
}
