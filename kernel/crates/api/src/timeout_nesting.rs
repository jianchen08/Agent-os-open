// @feature: FP-0.2.〇 超时体系 L2 嵌套校验 | @ci: rust-test
//! L2 嵌套不变量装载期校验器（ADR 2026-11-05-timeout-taxonomy-and-merge；
//! 设计真值 docs/working/超时体系分类设计_20261105.md §5.2）。
//!
//! **静态面**（管道配置显式声明的步骤→服务调用，编译期静态可知）：校验外层
//! 声明 ≥ 内层声明，违反拦截报错（指名哪层包不住哪层）。接入点 =
//! [`crate::server::load_and_compile_by_name`]（启动期 fail-fast + 热重载
//! 拒新产物，G2 式拦截——矛盾在装载时炸而非运行 600s 后炸）。
//!
//! **动态面**（LLM 工具面，工具由模型运行期选定，无法编译期枚举）：由 L1
//! 让位机制承载（invoker `with_raisable_call_timeout` 可抬高 deadline +
//! 启动期回归断言用例 `raisable_shell_yields_to_inflight_declared_need`——
//! 新包裹层忘接让位即红），本器不做运行期猜测。
//!
//! 两类校验（数学事实：外层界 < 内层声明需求 ⇒ 内层界是死代码、运行期必炸）：
//!
//! 1. **显式收紧 vs 步骤目标声明需求**：step context 的
//!    `plugin_call_timeout_seconds` 是显式压制（不可让位——Fixed 臂），其
//!    管辖范围内（自身 Plugin 项 + 未重声明该键的复合子树）任意目标的声明
//!    需求超过收紧值 → 拦截。子步骤重声明该键 = 子树由子步骤自己的声明
//!    管辖（state 上下文合并语义：子覆盖父）。
//! 2. **声明壳 vs 所需服务提供方声明需求**：目标插件自报
//!    `capabilities.call_timeout_seconds`（声明壳）小于其 `requires_services`
//!    提供方的声明需求 → 拦截（壳的执行包含被调方的执行）。缺省壳（未自报）
//!    不在本检查面——缺省壳只管沉默者（L1），让位由运行期机制承担。
//!
//! 声明需求口径（与 M2 单声明派生单点同源）：[`PluginManifest::
//! declared_call_need_secs`]（`capabilities.call_timeout_seconds` ∪
//! `mcp.request_timeout_secs` 取最大）∪ 工具级 `timeout_ms` 最大值
//! （L2 的 "max(所含工具的 A3/A4 声明值)"）。

use std::collections::{HashMap, HashSet};

use agentos_core::traits::PluginManifest;
use agentos_core::types::{PipelineConfig, PipelineStep, StepLibrary};
use agentos_engine::compiler::StepServiceIndex;
use agentos_plugin_loader::ServiceSurface;

/// 装载期超时嵌套校验（L2 静态面）。违反返回 Err（指名哪层包不住哪层），
/// 调用方（load_and_compile_by_name）据既有装载错误路径拦截。
pub fn validate_timeout_nesting(
    pipeline: &PipelineConfig,
    library: &StepLibrary,
    manifests: &[PluginManifest],
    index: &StepServiceIndex,
) -> Result<(), String> {
    let plugin_by_id: HashMap<&str, &PluginManifest> =
        manifests.iter().map(|m| (m.id.as_str(), m)).collect();
    let surface = ServiceSurface::from_manifests(manifests);
    let plugin_ids: HashSet<&str> = plugin_by_id.keys().copied().collect();
    // 跨循环体统一步 id 集（对齐编译期 pool：复合引用可跨体命中①级）。
    let mut body_steps: HashMap<&str, &PipelineStep> = HashMap::new();
    for body in &pipeline.loop_bodies {
        for step in &body.steps {
            body_steps.insert(step.id.as_str(), step);
        }
    }

    // ── 检查 1：step 显式收紧 vs 其管辖目标的声明需求 ──
    for body in &pipeline.loop_bodies {
        for step in &body.steps {
            let Some(explicit_secs) = explicit_tighten_secs(step) else {
                continue;
            };
            let mut targets = Vec::new();
            let mut visiting = HashSet::new();
            collect_governed_targets(
                step,
                &body_steps,
                library,
                index,
                &plugin_ids,
                &mut targets,
                &mut visiting,
            );
            for target in targets {
                let Some(manifest) = plugin_by_id.get(target.as_str()) else {
                    continue;
                };
                if let Some(need) = declared_need_secs(manifest) {
                    if explicit_secs < need {
                        return Err(format!(
                            "循环体 '{}' / step '{}'：外层显式收紧 \
                             plugin_call_timeout_seconds={explicit_secs}s 包不住内层插件 \
                             '{target}' 的声明需求 {need}s（capabilities.call_timeout_seconds \
                             ∪ mcp.request_timeout_secs ∪ 工具级 timeout_ms 取最大）——\
                             显式收紧不可让位，矛盾装载时拦截（L2 嵌套不变量，\
                             ADR 2026-11-05-timeout-taxonomy-and-merge）；收紧值须 ≥ 内层声明",
                            body.id, step.id
                        ));
                    }
                }
            }
        }
    }

    // ── 检查 2：本管道引用目标的声明壳 vs 其所需服务提供方的声明需求 ──
    let mut referenced = Vec::new();
    let mut visiting = HashSet::new();
    for body in &pipeline.loop_bodies {
        for step in &body.steps {
            collect_governed_targets(
                step,
                &body_steps,
                library,
                index,
                &plugin_ids,
                &mut referenced,
                &mut visiting,
            );
        }
    }
    for target in &referenced {
        let Some(manifest) = plugin_by_id.get(target.as_str()) else {
            continue;
        };
        let Some(shell_secs) = manifest
            .capabilities
            .call_timeout_seconds
            .filter(|s| *s > 0)
        else {
            continue; // 缺省壳不在静态拦截面（L1 让位承担）
        };
        for item in &manifest.requires_services {
            for provider in surface.provider_ids(item) {
                let Some(provider_manifest) = plugin_by_id.get(provider.as_str()) else {
                    continue; // 内核内置服务（无 manifest）无声明需求可校验
                };
                if let Some(need) = declared_need_secs(provider_manifest) {
                    if shell_secs < need {
                        return Err(format!(
                            "插件 '{target}' 声明壳 call_timeout_seconds={shell_secs}s \
                             包不住其所需服务 '{item}' 提供方 '{provider}' 的声明需求 \
                             {need}s——壳的执行包含被调方的执行（L2 嵌套不变量，\
                             ADR 2026-11-05-timeout-taxonomy-and-merge）；抬高声明壳或 \
                             调整提供方声明",
                        ));
                    }
                }
            }
        }
    }
    Ok(())
}

/// step context 的显式收紧值（秒）：有限数且 ≥0 才算在场（负值/-1 按未配置，
/// 与 invoker `state_plugin_call_timeout` 解析单点同口径）。
fn explicit_tighten_secs(step: &PipelineStep) -> Option<u64> {
    step.context
        .get("plugin_call_timeout_seconds")
        .and_then(|v| v.as_f64())
        .filter(|v| v.is_finite() && *v >= 0.0)
        .map(|v| v as u64)
}

/// 插件声明需求（秒）：M2 单声明派生单点 ∪ 工具级 `timeout_ms` 最大值
/// （L2 的 "max(所含工具的 A3/A4 声明值)"）。全缺 = None（沉默者，缺省壳管辖）。
fn declared_need_secs(manifest: &PluginManifest) -> Option<u64> {
    let tool_max_secs = manifest
        .capabilities
        .tools
        .iter()
        .filter_map(|t| t.timeout_ms)
        .max()
        .map(|ms| ms / 1000)
        .filter(|s| *s > 0);
    match (manifest.declared_call_need_secs(), tool_max_secs) {
        (Some(a), Some(b)) => Some(a.max(b)),
        (a, b) => a.or(b),
    }
}

/// 收集 step 显式收紧的管辖目标（插件 id 集）：自身 Plugin 项 + 复合引用子树；
/// 子步骤重声明 `plugin_call_timeout_seconds` = 子树由子步骤自己管辖（state
/// 上下文合并语义：子覆盖父），从本步骤管辖集剔除。动态模板项（`{{...}}`）
/// 运行期才定名，静态面跳过。
fn collect_governed_targets(
    step: &PipelineStep,
    body_steps: &HashMap<&str, &PipelineStep>,
    library: &StepLibrary,
    index: &StepServiceIndex,
    plugin_ids: &HashSet<&str>,
    out: &mut Vec<String>,
    visiting: &mut HashSet<String>,
) {
    for item in &step.steps {
        let name = item.name();
        if name.contains("{{") {
            continue;
        }
        // ③级命中（步骤服务索引）与 ④级直引（插件名）→ 原子插件目标。
        if let Some(resolved) = index.resolve(name) {
            out.push(resolved.plugin_id.clone());
            continue;
        }
        if plugin_ids.contains(name) {
            out.push(name.to_string());
            continue;
        }
        // ①/②级命中（跨体 step id / 公共 step 库）→ 复合递归。
        let child = body_steps
            .get(name)
            .copied()
            .or_else(|| library.steps.get(name));
        if let Some(child) = child {
            if visiting.insert(name.to_string()) {
                if !child.context.contains_key("plugin_call_timeout_seconds") {
                    collect_governed_targets(
                        child, body_steps, library, index, plugin_ids, out, visiting,
                    );
                }
                visiting.remove(name);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_core::traits::{ManifestCapabilities, ToolCapability};
    use agentos_core::types::{LoopBody, StepItem};
    use agentos_engine::compiler::build_step_service_index;
    use std::collections::HashMap;

    fn manifest(
        id: &str,
        call_timeout: Option<u64>,
        request_timeout: Option<u64>,
    ) -> PluginManifest {
        let mut m = agentos_core::traits::PluginManifest {
            restricted_capabilities: Vec::new(),
            force_include_tools: Vec::new(),
            state: None,
            id: id.to_string(),
            name: id.to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: agentos_core::traits::PluginType::Pipeline,
            language: "rust".to_string(),
            host_type: agentos_core::traits::HostType::InProcess,
            host_group: None,
            entry: "e".to_string(),
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
            enabled: None,
            activation: None,
            provides: None,
            visibility: Default::default(),
            persistent_fields: vec![],
            aux_venvs: Vec::new(),
            export_fields: vec![],
        };
        m.capabilities.call_timeout_seconds = call_timeout;
        if let Some(secs) = request_timeout {
            m.mcp = Some(
                serde_json::from_value(serde_json::json!({
                    "transport": "stdio", "request_timeout_secs": secs
                }))
                .unwrap(),
            );
        }
        m
    }

    fn bare(name: &str) -> StepItem {
        StepItem::Bare(name.to_string())
    }

    fn step(id: &str, items: Vec<StepItem>) -> PipelineStep {
        PipelineStep {
            id: id.to_string(),
            steps: items,
            when: None,
            context: HashMap::new(),
            routes: Vec::new(),
            loop_config: None,
        }
    }

    fn body(id: &str, steps: Vec<PipelineStep>) -> LoopBody {
        LoopBody {
            id: id.to_string(),
            steps,
            exit_routes: Vec::new(),
            while_cond: None,
            run_on_error: false,
        }
    }

    fn pipeline(bodies: Vec<LoopBody>) -> PipelineConfig {
        PipelineConfig {
            name: "t".to_string(),
            loop_bodies: bodies,
            checkpoint: Default::default(),
            initial_state: Default::default(),
            max_rounds: None,
        }
    }

    fn validate(bodies: Vec<LoopBody>, manifests: Vec<PluginManifest>) -> Result<(), String> {
        let library = StepLibrary::default();
        let index = build_step_service_index(&manifests).expect("索引构建");
        let config = pipeline(bodies);
        validate_timeout_nesting(&config, &library, &manifests, &index)
    }

    /// 三向·① 违反拦截：显式收紧 30s 包不住声明 1800s 的目标 → Err 指名双方。
    #[test]
    fn explicit_tighter_than_declared_target_intercepts() {
        let err = validate(
            vec![body(
                "main",
                vec![{
                    let mut s = step("llm", vec![bare("p_big")]);
                    s.context.insert(
                        "plugin_call_timeout_seconds".to_string(),
                        serde_json::json!(30),
                    );
                    s
                }],
            )],
            vec![manifest("p_big", Some(1800), None)],
        )
        .expect_err("违反必须拦截");
        assert!(err.contains("p_big"), "指名内层: {err}");
        assert!(err.contains("30s"), "指名外层值: {err}");
        assert!(err.contains("1800s"), "指名内层值: {err}");
        assert!(err.contains("plugin_call_timeout_seconds"), "指名键: {err}");
    }

    /// 三向·② 合规放行：收紧 ≥ 声明（边界相等）→ Ok。
    #[test]
    fn explicit_equal_or_greater_passes() {
        validate(
            vec![body(
                "main",
                vec![{
                    let mut s = step("llm", vec![bare("p_big")]);
                    s.context.insert(
                        "plugin_call_timeout_seconds".to_string(),
                        serde_json::json!(1800),
                    );
                    s
                }],
            )],
            vec![manifest("p_big", Some(1800), None)],
        )
        .expect("边界相等合规");
        validate(
            vec![body(
                "main",
                vec![{
                    let mut s = step("llm", vec![bare("p_big")]);
                    s.context.insert(
                        "plugin_call_timeout_seconds".to_string(),
                        serde_json::json!(3600),
                    );
                    s
                }],
            )],
            vec![manifest("p_big", Some(1800), None)],
        )
        .expect("收紧更宽合规");
    }

    /// 三向·③ 显式收紧缺席 + 内层声明 → 放行（L1 让位承担，缺省壳不进静态面）；
    /// 沉默目标 + 显式收紧 → 放行（无声明需求可违反）。
    #[test]
    fn absent_tighten_and_silent_target_pass() {
        // 缺省壳（未收紧）+ 声明内层：让位由运行期 L1 机制承担，静态面放行。
        validate(
            vec![body("main", vec![step("llm", vec![bare("p_big")])])],
            vec![manifest("p_big", Some(86_400), None)],
        )
        .expect("缺省壳交 L1 让位");
        // 显式收紧 + 沉默目标：无声明需求，无矛盾可拦。
        let mut s = step("x", vec![bare("p_silent")]);
        s.context.insert(
            "plugin_call_timeout_seconds".to_string(),
            serde_json::json!(1),
        );
        validate(
            vec![body("main", vec![s])],
            vec![manifest("p_silent", None, None)],
        )
        .expect("沉默目标无声明需求");
    }

    /// 复合传递：父步骤显式收紧管辖复合子树（子未重声明）→ 违反指名孙子目标。
    #[test]
    fn explicit_tighten_governs_transitive_composite_targets() {
        let mut parent = step("group", vec![bare("child")]);
        parent.context.insert(
            "plugin_call_timeout_seconds".to_string(),
            serde_json::json!(30),
        );
        let err = validate(
            vec![body(
                "main",
                vec![parent, step("child", vec![bare("p_big")])],
            )],
            vec![manifest("p_big", Some(1800), None)],
        )
        .expect_err("复合子树目标必须被父收紧管辖");
        assert!(err.contains("p_big"), "{err}");
    }

    /// 子步骤重声明收紧键 = 子树由子步骤管辖，父声明不再越管（state 上下文
    /// 合并语义：子覆盖父）——父收紧对子树目标放行，子收紧对子树目标拦截。
    #[test]
    fn child_redeclared_tighten_cuts_parent_scope() {
        let mut parent = step("group", vec![bare("child")]);
        parent.context.insert(
            "plugin_call_timeout_seconds".to_string(),
            serde_json::json!(30),
        );
        let mut child = step("child", vec![bare("p_big")]);
        child.context.insert(
            "plugin_call_timeout_seconds".to_string(),
            serde_json::json!(3600),
        );
        validate(
            vec![body("main", vec![parent, child])],
            vec![manifest("p_big", Some(1800), None)],
        )
        .expect("子重声明后父收紧不再管辖子树");
    }

    /// 检查 2：目标声明壳 < 其 requires_services 提供方声明需求 → 拦截指名
    /// 服务与提供方；缺省壳（未自报）不进本检查面。
    #[test]
    fn declared_shell_smaller_than_provider_need_intercepts() {
        let mut consumer = manifest("p_consumer", Some(600), None);
        consumer.requires_services = vec!["svc.method".to_string()];
        let mut provider = manifest("p_provider", None, None);
        provider.provides = Some(agentos_core::traits::ProvidesCapabilities {
            capabilities: vec![agentos_core::traits::ProvidedCapability {
                namespace: "svc".to_string(),
                methods: vec!["method".to_string()],
                host: agentos_core::traits::ProvidedCapabilityHost::Sidecar,
                tool_prefix: None,
                protocol_roles: Vec::new(),
            }],
        });
        provider.capabilities.call_timeout_seconds = Some(3600);
        let err = validate(
            vec![body("main", vec![step("s", vec![bare("p_consumer")])])],
            vec![consumer, provider.clone()],
        )
        .expect_err("声明壳必须盖住所需服务提供方声明需求");
        assert!(err.contains("p_consumer"), "{err}");
        assert!(err.contains("p_provider"), "{err}");
        assert!(err.contains("svc.method"), "{err}");

        // 缺省壳（未自报 call_timeout_seconds）不进本检查面。
        let mut silent_consumer = manifest("p_silent_consumer", None, None);
        silent_consumer.requires_services = vec!["svc.method".to_string()];
        validate(
            vec![body(
                "main",
                vec![step("s", vec![bare("p_silent_consumer")])],
            )],
            vec![silent_consumer, provider],
        )
        .expect("缺省壳交 L1 让位");
    }

    /// 工具级 timeout_ms 进声明需求口径（L2 "max(所含工具的 A3/A4 声明值)"）。
    #[test]
    fn tool_level_timeout_counts_into_declared_need() {
        let mut target = manifest("p_tools", None, None);
        target.capabilities.tools = vec![ToolCapability {
            name: "t1".to_string(),
            description: None,
            input_schema: None,
            output_schema: None,
            category: None,
            ui: None,
            render: None,
            isolation_capable: None,
            smoke: None,
            timeout_ms: Some(3_600_000),
        }];
        let mut s = step("x", vec![bare("p_tools")]);
        s.context.insert(
            "plugin_call_timeout_seconds".to_string(),
            serde_json::json!(30),
        );
        let err = validate(vec![body("main", vec![s])], vec![target])
            .expect_err("工具级声明必须进需求口径");
        assert!(err.contains("3600s"), "{err}");
    }

    /// ManifestCapabilities 直引自检（构造面字段冻结防漂移）。
    #[test]
    fn manifest_capabilities_shape() {
        let caps = ManifestCapabilities::default();
        assert!(caps.tools.is_empty());
        assert_eq!(caps.call_timeout_seconds, None);
    }
}
