//! 插件契约全量扫掠闸（2026-08-20）：把 `plugins/shared/**/plugin.json` 声明的
//! 全部工具/服务契约跑一遍"定义驱动校验器"的可消费性检查。
//!
//! 校验器原则（kernel_capabilities 模块）：定义详细到什么程度，就校验到什么
//! 程度——定义可以宽泛（无 pattern 的属性放行），但**定义本身必须能被执行器
//! 执行**：坏 pattern（regex 不可编译）在执行器里是 fail-closed 运行时错误，
//! 本闸把它提前到测试期抓红；顶层 type 非 object 的工具入参 schema（LLM 工具
//! 参数是命名参数包）同样提前红。
//!
//! 棘轮（只紧不松）：带参数面/带形态 pattern 的工具数、带契约的服务数有下限
//! ——删契约（退化回无 schema 裸参数）即红；补契约请顺手抬下限。
//! 盘点数字用 `cargo test --test plugin_contract_sweep -- --nocapture` 查看。

use serde_json::Value;
use std::path::{Path, PathBuf};

/// 仓库 plugins/shared 根（kernel/crates/api → 上溯三级 = 仓库根）。
fn plugins_shared_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../plugins/shared")
}

/// 收集全部 */*/plugin.json（排序保证确定性）。
fn manifest_files() -> Vec<PathBuf> {
    let mut out = Vec::new();
    let dir = plugins_shared_dir();
    for domain in ["system", "tools", "pipeline"] {
        let d = dir.join(domain);
        let Ok(entries) = std::fs::read_dir(&d) else {
            continue;
        };
        for e in entries.flatten() {
            let mf = e.path().join("plugin.json");
            if mf.is_file() {
                out.push(mf);
            }
        }
    }
    out.sort();
    out
}

/// 递归收集 schema 内全部 pattern 声明：(json 路径, pattern)。
fn collect_patterns(v: &Value, path: String, out: &mut Vec<(String, String)>) {
    match v {
        Value::Object(m) => {
            for (k, sub) in m {
                if k == "pattern" {
                    if let Some(p) = sub.as_str() {
                        out.push((format!("{path}/pattern"), p.to_string()));
                    }
                }
                collect_patterns(sub, format!("{path}/{k}"), out);
            }
        }
        Value::Array(a) => {
            for (i, sub) in a.iter().enumerate() {
                collect_patterns(sub, format!("{path}/{i}"), out);
            }
        }
        _ => {}
    }
}

/// 递归检查 type 值都在执行器支持集内（不支持的 type 在执行器里是契约自身错误）。
fn check_types_supported(v: &Value, path: String, errors: &mut Vec<String>) {
    const SUPPORTED: &[&str] = &[
        "object", "string", "boolean", "integer", "number", "array", "null",
    ];
    if let Value::Object(m) = v {
        for (k, sub) in m {
            if k == "type" {
                if let Some(t) = sub.as_str() {
                    if !SUPPORTED.contains(&t) {
                        errors.push(format!(
                            "{path}/type: 不支持的 type '{t}'（执行器关键字子集）"
                        ));
                    }
                }
            }
            check_types_supported(sub, format!("{path}/{k}"), errors);
        }
    } else if let Value::Array(a) = v {
        for (i, sub) in a.iter().enumerate() {
            check_types_supported(sub, format!("{path}/{i}"), errors);
        }
    }
}

#[test]
fn all_plugin_contracts_are_executor_consumable() {
    let manifests = manifest_files();
    assert!(
        manifests.len() >= 40,
        "扫掠插件数异常偏低（{}）：plugins/shared 布局变更或清单丢失",
        manifests.len()
    );

    let (mut tools, mut tools_props, mut tools_pattern, mut tools_output) =
        (0u32, 0u32, 0u32, 0u32);
    let (mut services, mut services_input) = (0u32, 0u32);

    for mf in &manifests {
        let tag = mf
            .parent()
            .and_then(|p| p.file_name())
            .and_then(|n| n.to_str())
            .unwrap_or("<?>")
            .to_string();
        let raw = std::fs::read_to_string(mf)
            .unwrap_or_else(|e| panic!("[{tag}] plugin.json 读取失败: {e}"));
        let m: Value = serde_json::from_str(&raw)
            .unwrap_or_else(|e| panic!("[{tag}] plugin.json 解析失败: {e}"));

        let mut patterns: Vec<(String, String)> = Vec::new();
        let mut type_errors: Vec<String> = Vec::new();

        for t in m
            .get("capabilities")
            .and_then(|c| c.get("tools"))
            .and_then(|t| t.as_array())
            .into_iter()
            .flatten()
        {
            tools += 1;
            let name = t.get("name").and_then(|v| v.as_str()).unwrap_or("<?>");
            let ins = t.get("input_schema");
            let outs = t.get("output_schema");
            // 工具入参是命名参数包：声明了顶层 type 就必须是 object
            if let Some(ty) = ins.and_then(|s| s.get("type")).and_then(|v| v.as_str()) {
                assert_eq!(
                    ty, "object",
                    "[{tag}] 工具 {name} input_schema.type 应为 object（命名参数包），实际 {ty}"
                );
            }
            if ins
                .and_then(|s| s.get("properties"))
                .and_then(|p| p.as_object())
                .is_some_and(|p| !p.is_empty())
            {
                tools_props += 1;
            }
            if outs.is_some_and(|o| o.get("properties").is_some()) {
                tools_output += 1;
            }
            let mut tool_patterns = Vec::new();
            if let Some(schema) = ins {
                collect_patterns(
                    schema,
                    format!("tools.{name}.input_schema"),
                    &mut tool_patterns,
                );
                check_types_supported(
                    schema,
                    format!("tools.{name}.input_schema"),
                    &mut type_errors,
                );
                // required 元素必须是字符串
                if let Some(req) = schema.get("required").and_then(|r| r.as_array()) {
                    for r in req {
                        assert!(
                            r.is_string(),
                            "[{tag}] 工具 {name} required 含非字符串元素: {r}"
                        );
                    }
                }
            }
            if let Some(schema) = outs {
                collect_patterns(
                    schema,
                    format!("tools.{name}.output_schema"),
                    &mut tool_patterns,
                );
                check_types_supported(
                    schema,
                    format!("tools.{name}.output_schema"),
                    &mut type_errors,
                );
            }
            if tool_patterns.iter().any(|(p, _)| p.starts_with("tools.")) {
                // input_schema 内带 pattern 的工具计入形态棘轮
                let has_input_pattern = ins
                    .and_then(|s| s.get("properties"))
                    .and_then(|p| p.as_object())
                    .is_some_and(|props| props.values().any(|v| v.get("pattern").is_some()));
                if has_input_pattern {
                    tools_pattern += 1;
                }
            }
            patterns.extend(tool_patterns);
        }

        for s in m
            .get("capabilities")
            .and_then(|c| c.get("services"))
            .and_then(|s| s.as_array())
            .into_iter()
            .flatten()
        {
            services += 1;
            let ns = s
                .get("namespace")
                .and_then(|v| v.as_str())
                .unwrap_or("<ns?>");
            let method = s.get("method").and_then(|v| v.as_str()).unwrap_or("<m?>");
            if let Some(schema) = s.get("input_schema").filter(|v| !v.is_null()) {
                services_input += 1;
                collect_patterns(
                    schema,
                    format!("services.{ns}.{method}.input_schema"),
                    &mut patterns,
                );
                check_types_supported(
                    schema,
                    format!("services.{ns}.{method}.input_schema"),
                    &mut type_errors,
                );
            }
        }

        // 全部声明 pattern 必须可被 regex 编译——执行器遇到坏 pattern 是运行时
        // fail-closed 错误，本闸提前到扫掠期。
        for (path, p) in &patterns {
            regex::Regex::new(p)
                .unwrap_or_else(|e| panic!("[{tag}] {path} 声明的 pattern 无法编译（{p}）: {e}"));
        }
        assert!(
            type_errors.is_empty(),
            "[{tag}] 契约使用了执行器不支持的 type: {type_errors:?}"
        );
    }

    // 棘轮（2026-08-20 L2 补齐后基线：47 插件 / 58 工具，56/58 声明 input_schema
    // （唯二缺 = widget_demo 死演示工具，无 Python 实现不造伪），53 带参数面 /
    // 2 空参声明 / 2 带形态 pattern / 88 服务）
    // 2026-08-25 收紧：hot_swap 整目录下线（-1）+ lsp.completion/supported_languages
    // 死工具删声明（-2）→ 带参数面 53→50。
    assert!(
        tools_props >= 50,
        "带参数面工具数退化: {tools_props} < 50（L2 补齐基线，2026-08-25 收紧）"
    );
    assert!(
        tools_pattern >= 2,
        "带形态 pattern 的工具数退化: {tools_pattern} < 2（基线 trigger_setup/task_submit）"
    );
    assert!(
        services_input >= 88,
        "带契约服务数退化: {services_input} < 88"
    );
    // output_schema 棘轮：builtin×4（常量接线）+ tts_generate + 既有 bash/
    // enhanced_search/spill_retrieve/demo = 11。剩余 ~47 个的补齐是 2026-08-15
    // "存量 output_schema 缓补"挂账债的延续，需按各工具返回形状 AUTHOR（非同步）。
    // 2026-08-25 收紧：hot_swap 下线（-1）→ 11→10。
    assert!(
        tools_output >= 10,
        "带 output_schema 的工具数退化: {tools_output} < 10"
    );

    eprintln!(
        "插件契约盘点: plugins={} tools={} with_input_props={} with_pattern={} with_output={} services={} with_input={}",
        manifests.len(), tools, tools_props, tools_pattern, tools_output, services, services_input
    );
}
