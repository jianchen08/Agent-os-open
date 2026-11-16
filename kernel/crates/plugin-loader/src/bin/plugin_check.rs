// @feature: FP-0.2.一 插件协议·plugin_check 契约校验引擎 | @ci: rust-test
//! `plugin_check` —— 插件契约校验引擎（闸0·开发期 / 闸1·注册期共用语义）。
//!
//! 用法：
//! ```text
//! plugin_check --root <plugins_dir>            # 扫描目录下所有 plugin.json/plugin.yaml
//! plugin_check <path-to-plugin.json> ...       # 逐个校验指定 manifest
//! plugin_check --json --deny --root <dir>      # JSON 输出 + 任一插件有错 → 退出码 1
//! ```
//!
//! 输出每插件一条契约报告，与闸2·观测 `PluginContractState` 同一赛义（静态结构 /
//! output_schema 声明合法性 / provides 未注册 / native 产物预检 / entry 必填）。
//!
//! 划分（与内核注册闸同源、不重复）：
//! - 本 bin 覆盖**静态结构 + 声明合法性**（作者/CI 在"还没进系统"前跑，可离线全量）；
//! - G2 声明↔实现一致 + 注册闸冒烟（需 sidecar 进程）属内核注册闸
//!   `g2_verify_and_sanitize`——本 bin 不重造；
//! - 静态函数 `output_schema_error` / `provides_methods_unbacked` 与注册闸**共用
//!   同一份**（一份逻辑，杜绝双份漂移，方案 §1.8）。

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use agentos_core::traits::{PluginManifest, PluginType};

/// 单插件静态校验结果（输出 JSON 模型）。
#[derive(serde::Serialize)]
struct CheckReport {
    plugin_id: String,
    manifest_path: String,
    valid: bool,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    errors: Vec<String>,
    checks: Vec<String>,
}

/// 服务声明闭包检查（T5.4）的语料级结果。
///
/// `ran=false` = 检查器不可用（仓外语料/无 python/检查器 fail-closed）——
/// 跳过不阻塞（运行时 router 对账仍兜底）；`violations` 按 `plugin=<目录>`
/// 归因（检查器违规行的机器可读键）。
struct CorpusScan {
    ran: bool,
    violations: HashMap<String, Vec<String>>,
}

impl CorpusScan {
    fn skipped() -> Self {
        Self {
            ran: false,
            violations: HashMap::new(),
        }
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut root: Option<PathBuf> = None;
    let mut json = false;
    let mut deny = false;
    let mut manifests: Vec<PathBuf> = Vec::new();
    let mut it = args.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--root" => {
                root = it.next().map(PathBuf::from).or_else(|| {
                    eprintln!("--root 需要参数");
                    std::process::exit(2);
                });
            }
            "--json" => json = true,
            "--deny" => deny = true,
            "--help" | "-h" => {
                println!(
                    "plugin_check —— 插件契约静态校验（闸0/闸1 共用语义）\n\
                     \n\
                     usage:\n  \
                     \x20 plugin_check [--json] [--deny] --root <plugins_dir>\n  \
                     \x20 plugin_check [--json] [--deny] <plugin.json> [<plugin.json> ...]\n\
                     \n\
                     --root <dir>  扫描目录下所有 plugin.json/plugin.yaml\n\
                     --json        输出 JSON（默认人类可读）\n\
                     --deny        任一插件有错 → 退出码 1（CI 用）\n\
                     \n\
                     覆盖检查：必填字段 / entry（非 composite） / native 产物存在性 /\n\
                     \x20          tools output_schema 声明合法性 / provides 未注册方法 /\n\
                     \x20          服务声明闭包（委托 scripts/check_service_declarations.py，\n\
                     \x20          检查器不可用时跳过）/ 工具 schema 契约（委托\n\
                     \x20          scripts/check_tool_schema_contracts.py，同款跳过语义）"
                );
                std::process::exit(0);
            }
            other => {
                if other.starts_with('-') {
                    eprintln!("未知参数: {other}");
                    std::process::exit(2);
                }
                manifests.push(PathBuf::from(other));
            }
        }
    }

    // 收集待校验的 manifest 文件（--root 扫描）
    if let Some(ref root_dir) = root {
        if manifests.is_empty() {
            collect_manifests(root_dir, &mut manifests);
        } else {
            eprintln!("--root 与显式 manifest 路径不能混用");
            std::process::exit(2);
        }
    }
    if manifests.is_empty() {
        eprintln!("没有待校验的插件（需 --root 或显式 manifest 路径）");
        std::process::exit(2);
    }

    let mut reports: Vec<CheckReport> = Vec::new();
    // 语料级契约检查（T5.4 安装期）：服务声明闭包 + 工具 schema 契约，各委托
    // Python 检查器全仓扫一次，违规按插件归因合并分发（单子进程每检查器，
    // 非逐插件重复扫）。任一 ran 时每个报告都有该侧结论行（零违规也是结论）；
    // 未 ran 的侧整体跳过。
    let svc = contract_checker_scan(
        SERVICE_DECL_CHECKER,
        "AGENTOS_SERVICE_DECL_CHECKER",
        root.as_deref(),
        manifests.first(),
    );
    let tool_schema = contract_checker_scan(
        TOOL_SCHEMA_CONTRACT_CHECKER,
        "AGENTOS_TOOL_SCHEMA_CONTRACT_CHECKER",
        root.as_deref(),
        manifests.first(),
    );
    let mut corpus_violations = svc.violations.clone();
    for (plugin, lines) in &tool_schema.violations {
        corpus_violations
            .entry(plugin.clone())
            .or_default()
            .extend(lines.clone());
    }
    let corpus_ran = svc.ran || tool_schema.ran;
    for path in &manifests {
        let dir_key = path
            .parent()
            .and_then(|p| p.file_name())
            .map(|n| n.to_string_lossy().into_owned());
        let viol: Option<&[String]> = if corpus_ran {
            Some(
                dir_key
                    .as_deref()
                    .and_then(|k| corpus_violations.get(k))
                    .map(|v| v.as_slice())
                    .unwrap_or(&[]),
            )
        } else {
            None
        };
        reports.push(check_one(path, viol));
    }
    if !svc.ran {
        eprintln!("提示: 服务声明闭包检查跳过（检查器或 python 不可用；运行时对账兜底）");
    }
    if !tool_schema.ran {
        eprintln!("提示: 工具 schema 契约检查跳过（检查器或 python 不可用；CI 闸执法）");
    }

    if json {
        println!(
            "{}",
            serde_json::to_string_pretty(&serde_json::json!({
                "checked": reports.len(),
                "violated": reports.iter().filter(|r| !r.valid).count(),
                "plugins": reports,
            }))
            .unwrap_or_else(|e| format!("JSON 序列化失败: {e}"))
        );
    } else {
        for r in &reports {
            let status = if r.valid { "OK " } else { "BAD" };
            println!("[{status}] {}  <- {}", r.plugin_id, r.manifest_path);
            for c in &r.checks {
                println!("      · {c}");
            }
            for e in &r.errors {
                println!("      ✗ {e}");
            }
        }
        let violated = reports.iter().filter(|r| !r.valid).count();
        println!("\nchecked={} violated={}", reports.len(), violated);
    }

    if deny && reports.iter().any(|r| !r.valid) {
        std::process::exit(1);
    }
}

/// 扫描目录：递归发现 root 下所有 plugin.json/plugin.yaml（与 loader::discover
/// 同布局——插件可嵌套在 plugins/shared/{system,tools,pipeline}/<name>/ 等任意深度）。
fn collect_manifests(root: &Path, out: &mut Vec<PathBuf>) {
    for name in ["plugin.json", "plugin.yaml"] {
        let p = root.join(name);
        if p.exists() {
            out.push(p);
        }
    }
    let Ok(entries) = std::fs::read_dir(root) else {
        eprintln!("无法读取目录 {}（{root:?}）", root.display());
        std::process::exit(2);
    };
    let mut sub_dirs: Vec<PathBuf> = entries
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.is_dir())
        .filter(|p| p.file_name().is_some_and(|n| n != "node_modules"))
        .collect();
    sub_dirs.sort(); // 确定性输出（深度优先，字典序）
    for dir in sub_dirs {
        collect_manifests(&dir, out);
    }
}

/// 语料级契约检查器定位锚名（仓根相对）：服务声明闭包 + 工具 schema 契约。
const SERVICE_DECL_CHECKER: &str = "scripts/check_service_declarations.py";
const TOOL_SCHEMA_CONTRACT_CHECKER: &str = "scripts/check_tool_schema_contracts.py";

/// 全仓语料跑一次语料级契约检查器，违规按 `plugin=<目录>` 归因。
///
/// 检查器定位：`AGENTOS_<NAME>_CHECKER` 环境变量优先，否则从扫描根
/// （--root 或首个 manifest 的祖先）向上找仓根 `scripts/` 锚。python 按
/// `python` → `python3` 解析。任何基建缺位 → skipped（不阻塞其他检查）；
/// 检查器 exit 2（fail-closed）同样降级为跳过并留痕——CI 静态闸直跑检查器，
/// fail-closed 在那层执法。
fn contract_checker_scan(
    checker_relpath: &str,
    env_override: &str,
    root: Option<&Path>,
    any_manifest: Option<&PathBuf>,
) -> CorpusScan {
    let anchor_root = root
        .map(Path::to_path_buf)
        .or_else(|| any_manifest.map(|p| p.parent().unwrap_or(p).to_path_buf()));
    let Some(start) = anchor_root else {
        return CorpusScan::skipped();
    };
    let checker: PathBuf = match std::env::var(env_override) {
        Ok(v) if !v.is_empty() => PathBuf::from(v),
        _ => {
            let mut found = None;
            let mut cur = Some(start.as_path());
            while let Some(dir) = cur {
                let candidate = dir.join(checker_relpath);
                if candidate.is_file() {
                    found = Some(candidate);
                    break;
                }
                cur = dir.parent();
            }
            match found {
                Some(c) => c,
                None => return CorpusScan::skipped(),
            }
        }
    };
    let Some(mut cmd) = python_command() else {
        return CorpusScan::skipped();
    };
    cmd.arg(&checker);
    if let Some(root_dir) = root {
        cmd.arg("--scan-root").arg(root_dir);
    }
    let output = match cmd.output() {
        Ok(o) => o,
        Err(_) => return CorpusScan::skipped(),
    };
    if output.status.code() == Some(2) {
        // 检查器 fail-closed（内建表残缺/manifest 不可解析）——CI 闸层执法，此处留痕跳过
        eprintln!("提示: 服务声明检查器 fail-closed（exit 2），本工具跳过该检查");
        return CorpusScan::skipped();
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let mut violations: HashMap<String, Vec<String>> = HashMap::new();
    for line in text.lines() {
        let Some(rest) = line.strip_prefix("VIOLATION: plugin=") else {
            continue;
        };
        let (plugin, _tail) = rest.split_once(' ').unwrap_or((rest, ""));
        if plugin.is_empty() {
            continue;
        }
        violations
            .entry(plugin.to_string())
            .or_default()
            .push(line.to_string());
    }
    CorpusScan {
        ran: true,
        violations,
    }
}

/// 解析 python 解释器（`python` → `python3` 回落）；都不可用返回 None。
fn python_command() -> Option<std::process::Command> {
    for exe in ["python", "python3"] {
        let mut cmd = std::process::Command::new(exe);
        cmd.env("PYTHONIOENCODING", "utf-8");
        // 探活：--version 快速失败即换下一个候选
        if cmd.arg("--version").output().is_ok() {
            cmd = std::process::Command::new(exe);
            cmd.env("PYTHONIOENCODING", "utf-8");
            return Some(cmd);
        }
    }
    None
}

/// 对单个 manifest 文件跑静态校验，产出契约报告。
///
/// `service_violations` = T5.4 服务声明闭包检查器归因到本插件的违规行
/// （None = 检查器未运行，跳过不阻塞）。
fn check_one(manifest_path: &Path, service_violations: Option<&[String]>) -> CheckReport {
    let content = match std::fs::read_to_string(manifest_path) {
        Ok(c) => c,
        Err(e) => {
            return CheckReport {
                plugin_id: "(unreadable)".to_string(),
                manifest_path: manifest_path.display().to_string(),
                valid: false,
                errors: vec![format!("读取失败: {e}")],
                checks: Vec::new(),
            }
        }
    };
    let plugin_dir = manifest_path.parent().unwrap_or_else(|| Path::new("."));

    // 严格反序列化（deny_unknown_fields）：未知/遗留字段 → 解析失败即拒载（fail-closed）
    let manifest: PluginManifest = match serde_json::from_str(&content) {
        Ok(m) => m,
        Err(json_err) => match serde_yaml::from_str(&content) {
            Ok(m) => m,
            Err(yaml_err) => {
                return CheckReport {
                    plugin_id: "(parse-failed)".to_string(),
                    manifest_path: manifest_path.display().to_string(),
                    valid: false,
                    errors: vec![
                        format!("manifest JSON 解析失败: {json_err}"),
                        format!("manifest YAML 解析失败: {yaml_err}"),
                    ],
                    checks: vec!["无法反序列化（含未知/遗留字段拒绝——fail-closed）".to_string()],
                }
            }
        },
    };

    let mut errors: Vec<String> = Vec::new();
    let mut checks: Vec<String> = Vec::new();

    // 1. 必填字段
    if manifest.id.is_empty() {
        errors.push("id 为空（必填）".to_string());
    }
    if manifest.name.is_empty() {
        errors.push("name 为空（必填）".to_string());
    }
    if manifest.version.is_empty() {
        errors.push("version 为空（必填）".to_string());
    }
    if manifest.language.is_empty() {
        errors.push("language 为空（必填）".to_string());
    }
    if manifest.plugin_type != PluginType::Composite && manifest.entry.is_empty() {
        errors.push("entry 为空（非 composite 必填）".to_string());
    }
    if errors.is_empty() {
        checks.push(format!(
            "必填字段：id/name/version/language/entry 齐备（type={:?}, host={:?}）",
            manifest.plugin_type, manifest.host_type
        ));
    }

    // 2. native 产物预检（与 loader validate_manifest_internal 同规则：裸名按平台
    // 补 cdylib 后缀，与真实加载路径一致——否则 cdylib 形态插件（`<id>_native`）
    // 声明会因磁盘上是 `..._native.dll` 而被误判缺失）
    if let Some(native) = &manifest.native {
        let artifact_path = plugin_dir.join(
            agentos_plugin_loader::NativePluginLoader::platform_artifact_name(&native.artifact),
        );
        if artifact_path.exists() {
            checks.push(format!("native artifact 存在: {}", artifact_path.display()));
        } else {
            errors.push(format!(
                "native artifact 缺失: {}（cdylib 产物未构建或路径声明有误）",
                artifact_path.display()
            ));
        }
    }

    // 3. tools output_schema 声明合法性（与注册闸 reject_malformed_output_schemas 同一函数）
    let mut bad_tools = Vec::new();
    for t in &manifest.capabilities.tools {
        if let Some(out) = &t.output_schema {
            if let Some(msg) = agentos_plugin_loader::output_schema_error(out) {
                bad_tools.push(format!("{}({msg})", t.name));
            }
        }
    }
    if bad_tools.is_empty() {
        checks.push(format!(
            "output_schema 声明合法（{} 工具）",
            manifest.capabilities.tools.len()
        ));
    } else {
        errors.push(format!(
            "output_schema 声明不合法（声明即校验，fail-closed）: {}",
            bad_tools.join(", ")
        ));
    }

    // 4. provides 服务未注册检查（与注册闸同一函数）
    let unbacked = agentos_plugin_loader::provides_methods_unbacked(&manifest);
    if unbacked.is_empty() {
        checks.push("provides 公告的方法均有声明工具".to_string());
    } else {
        errors.push(format!(
            "provides 公告的方法未声明工具（消费者将调不到）: {}",
            unbacked.join(", ")
        ));
    }

    // 5. 服务声明闭包（T5.4 安装期，资源注册收敛方案 §二 第二层）：
    // 调用点 ⊆ requires_services ∪ KERNEL_PROVIDED_SERVICES——检查器
    // （scripts/check_service_declarations.py）归因到本插件的违规行逐条入 errors。
    if let Some(viol) = service_violations {
        if viol.is_empty() {
            checks.push(
                "服务声明闭包：调用点 ⊆ requires_services ∪ 内建表（检查器 0 违规）".to_string(),
            );
        } else {
            errors.extend(viol.iter().cloned());
        }
    }

    // 生成排序稳定的 checks/errors（确定性输出）
    checks.sort();
    errors.sort();

    CheckReport {
        plugin_id: manifest.id.clone(),
        manifest_path: manifest_path.display().to_string(),
        valid: errors.is_empty(),
        errors,
        checks,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn write_manifest(dir: &Path, id: &str, body: &str) -> PathBuf {
        let p = dir.join(id).join("plugin.json");
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        let mut f = std::fs::File::create(&p).unwrap();
        f.write_all(body.as_bytes()).unwrap();
        p
    }

    #[test]
    fn valid_manifest_reports_ok() {
        let dir = tempfile::tempdir().unwrap();
        let p = write_manifest(
            dir.path(),
            "p_ok",
            r#"{
                "id":"p_ok","name":"p_ok","version":"1.0.0",
                "plugin_type":"tool","language":"python","host_type":"sidecar",
                "entry":"python server.py",
                "capabilities":{"tools":[{"name":"t1","description":"t1"}]}
            }"#,
        );
        let r = check_one(&p, None);
        assert!(r.valid, "期望通过: {:?}", r.errors);
        assert!(!r.checks.is_empty());
        assert_eq!(r.plugin_id, "p_ok");
    }

    #[test]
    fn stale_unknown_field_rejected() {
        // PluginManifest deny_unknown_fields：遗留/未知顶层字段 → 解析失败即拒载
        let dir = tempfile::tempdir().unwrap();
        let p = write_manifest(
            dir.path(),
            "p_stale",
            r#"{
                "id":"p_stale","name":"p_stale","version":"1.0.0",
                "plugin_type":"tool","language":"python","host_type":"sidecar",
                "entry":"python server.py",
                "error_policy_legacy":"retry"
            }"#,
        );
        let r = check_one(&p, None);
        assert!(!r.valid, "未知字段应拒载: {:?}", r.checks);
        assert!(
            r.errors
                .iter()
                .any(|e| e.contains("解析失败") || e.contains("unknown")),
            "{:?}",
            r.errors
        );
    }

    #[test]
    fn malformed_output_schema_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let p = write_manifest(
            dir.path(),
            "p_badschema",
            r#"{
                "id":"p_badschema","name":"p_badschema","version":"1.0.0",
                "plugin_type":"tool","language":"python","host_type":"sidecar",
                "entry":"python server.py",
                "capabilities":{"tools":[{
                    "name":"t1","description":"t1",
                    "output_schema":{"type":"not-a-real-type"}
                }]}
            }"#,
        );
        let r = check_one(&p, None);
        assert!(!r.valid);
        assert!(
            r.errors.iter().any(|e| e.contains("output_schema")),
            "{:?}",
            r.errors
        );
    }

    #[test]
    fn missing_entry_rejected_for_non_composite() {
        let dir = tempfile::tempdir().unwrap();
        let p = write_manifest(
            dir.path(),
            "p_noentry",
            r#"{
                "id":"p_noentry","name":"p_noentry","version":"1.0.0",
                "plugin_type":"tool","language":"python","host_type":"sidecar",
                "capabilities":{},
                "entry":""
            }"#,
        );
        let r = check_one(&p, None);
        assert!(!r.valid);
        assert!(
            r.errors.iter().any(|e| e.contains("entry 为空")),
            "{:?}",
            r.errors
        );
    }

    #[test]
    fn provides_unbacked_rejected() {
        // provides 公告的方法无对应已声明工具 → 服务声明了但没注册（fail-closed）
        let dir = tempfile::tempdir().unwrap();
        let p = write_manifest(
            dir.path(),
            "p_unbacked",
            r#"{
                "id":"p_unbacked","name":"p_unbacked","version":"1.0.0",
                "plugin_type":"system","language":"python","host_type":"sidecar",
                "entry":"python server.py",
                "provides":{"capabilities":[{"name":"ghost_svc","methods":["run"]}]}
            }"#,
        );
        let r = check_one(&p, None);
        assert!(!r.valid);
        assert!(
            r.errors.iter().any(|e| e.contains("provides")),
            "{:?}",
            r.errors
        );
    }

    #[test]
    fn service_declaration_violations_injected() {
        // T5.4 安装期闭包：检查器归因到本插件的违规行逐条入 errors →
        // --deny 语义（任一 error 即 invalid）覆盖服务声明面。
        let dir = tempfile::tempdir().unwrap();
        let p = write_manifest(
            dir.path(),
            "p_svc",
            r#"{
                "id":"p_svc","name":"p_svc","version":"1.0.0",
                "plugin_type":"tool","language":"python","host_type":"sidecar",
                "entry":"python server.py",
                "capabilities":{"tools":[{"name":"t1","description":"t1"}]}
            }"#,
        );
        let clean = check_one(&p, Some(&[]));
        assert!(clean.valid, "0 违规不应红: {:?}", clean.errors);
        assert!(
            clean.checks.iter().any(|c| c.contains("服务声明闭包")),
            "应有闭包通过 check 行: {:?}",
            clean.checks
        );
        let viol = vec![
            "VIOLATION: plugin=p_svc file=shared/p_svc/server.py:1 调用未声明能力 `ghost`"
                .to_string(),
        ];
        let bad = check_one(&p, Some(&viol));
        assert!(!bad.valid, "归因违规应红: {:?}", bad.errors);
        assert_eq!(bad.errors, viol);
    }
}
