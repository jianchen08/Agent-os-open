//! boot sidecar 预热：把管道引用插件的宿主冷启动提前到启动后台窗口。
//!
//! 症状背景：sidecar 按调用懒 spawn，管道链（prepare/core/post 共 ~30 个
//! 宿主）串行执行——内核启动后的首次消息要为每个宿主付 spawn→MCP
//! initialize（秒级/个）的冷启动成本，实测首条消息 41.5s vs 第二条 1.9s。
//!
//! 预热集 = 编译管道引用的插件（[`CompiledPipeline::referenced_plugin_ids`]
//! 单一来源：步骤项 + Composite 池递归 + hooks 目标）∩ enabled sidecar。
//! 非管道插件（工具/服务等）保持纯懒加载——idle GC 治理不变：预热宿主
//! 长时间未用自动回收，懒 spawn 兜底；预热只是提前触发，不改变生命周期
//! 语义。Dynamic 项运行时才解析，不在预热集（同由懒 spawn 兜底）。

use std::sync::Arc;

use agentos_core::traits::{HostType, PluginManifest};
use agentos_engine::compiler::CompiledPipeline;
use agentos_invoker::PluginInvokerImpl;
use futures_util::stream::{self, StreamExt};
use tracing::{info, warn};

/// 预热并发度：宿主 spawn→initialize 每个秒级，4 路并发把 ~30 个管道插件
/// 的全量预热压在 ~10s 量级（boot 后台窗口，不与首条消息争串行带宽——
/// warmup 与调用路径共享 per-host single-flight 锁，并发触发不双 spawn）。
const WARMUP_CONCURRENCY: usize = 4;

/// 逃生门（仓库惯例：AGENTOS_* env 开关）：=1 时启动即跳过，回到纯懒加载。
pub fn warmup_disabled() -> bool {
    std::env::var("AGENTOS_DISABLE_SIDECAR_WARMUP").as_deref() == Ok("1")
}

/// 选预热目标：编译管道引用的插件 ∩ enabled sidecar manifests。
///
/// 纯函数。enabled 过滤由调用方保证（传 enabled_manifests 快照）；此处
/// 按 host_type（native 无进程模型）+ 管道引用集求交。返回保持传入顺序。
pub fn select_warmup_targets(
    pipeline: &CompiledPipeline,
    enabled_manifests: &[PluginManifest],
) -> Vec<PluginManifest> {
    let referenced = pipeline.referenced_plugin_ids();
    enabled_manifests
        .iter()
        .filter(|m| m.host_type == HostType::Sidecar && referenced.contains(&m.id))
        .cloned()
        .collect()
}

/// 发射 boot 后台预热任务（fire-and-forget：不阻塞启动/HTTP 就绪，预热
/// 进行中即可正常服务——single-flight 保证 warmup 与首条消息对同一宿主
/// 只 spawn 一次）。单插件失败仅 warn 跳过（懒 spawn 运行期兜底），汇总
/// ok/failed 计数收口日志。
pub fn spawn_pipeline_sidecar_warmup(
    invoker: Arc<PluginInvokerImpl>,
    pipeline: Arc<CompiledPipeline>,
    enabled_manifests: Vec<PluginManifest>,
) {
    if warmup_disabled() {
        info!(target: "sidecar-warmup", "AGENTOS_DISABLE_SIDECAR_WARMUP=1 → 跳过 boot 预热（纯懒加载）");
        return;
    }
    let targets = select_warmup_targets(&pipeline, &enabled_manifests);
    let total = targets.len();
    if total == 0 {
        info!(target: "sidecar-warmup", "管道无 sidecar 引用，boot 预热空集跳过");
        return;
    }
    // 预分配先行（§合宿装箱正确性）：合宿宿主的 --members 与组指纹取分配表
    // 实时快照——先分配完全体目标再并发 warmup，每个组宿主一次 spawn 即装载
    // 完整成员集；若边分配边 spawn，先到成员把宿主以单成员集定型，后到成员
    // 在指纹 TTL 内被快速路径短路复用（宿主里没有该成员，预热假成功）。
    for m in &targets {
        invoker.preassign_host_key(m);
    }
    info!(
        target: "sidecar-warmup",
        total,
        concurrency = WARMUP_CONCURRENCY,
        "Boot sidecar 预热启动（管道引用的 sidecar 宿主提前 spawn 进缓存）"
    );
    tokio::spawn(async move {
        let results: Vec<(String, Result<(), agentos_core::types::PluginError>)> =
            stream::iter(targets)
                .map(|m| {
                    let inv = invoker.clone();
                    async move {
                        let id = m.id.clone();
                        (id, inv.warmup_sidecar(&m).await)
                    }
                })
                .buffer_unordered(WARMUP_CONCURRENCY)
                .collect()
                .await;
        let mut failed = Vec::new();
        for (id, result) in &results {
            if let Err(e) = result {
                warn!(
                    target: "sidecar-warmup",
                    plugin = %id,
                    error = %e.message,
                    "boot 预热单插件失败（跳过，懒 spawn 运行期兜底）"
                );
                failed.push(id.clone());
            }
        }
        let ok = results.len() - failed.len();
        info!(
            target: "sidecar-warmup",
            ok,
            failed = failed.len(),
            "Boot sidecar 预热完成"
        );
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_core::traits::PluginType;
    use agentos_core::types::{LoopBody, PipelineConfig, PipelineStep, StepItem};
    use std::collections::{HashMap, HashSet};

    fn manifest(id: &str, host_type: HostType) -> PluginManifest {
        PluginManifest {
            id: id.to_string(),
            name: format!("Test {id}"),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Pipeline,
            pipeline_role: None,
            language: "python".to_string(),
            host_type,
            host_group: None,
            entry: "server.py".to_string(),
            capabilities: Default::default(),
            requires_services: vec![],
            permissions: Default::default(),
            priority: 100,
            mcp: None,
            lifecycle: None,
            native: None,
            granted_capabilities: vec![],
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
            provides: None,
            persistent_fields: vec![],
        }
    }

    fn single_step_pipeline(plugin: &str) -> CompiledPipeline {
        let config = PipelineConfig {
            name: "p".into(),
            loop_bodies: vec![LoopBody {
                id: "main".into(),
                steps: vec![PipelineStep {
                    id: "s".into(),
                    steps: vec![StepItem::Bare(plugin.into())],
                    when: None,
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                }],
                while_cond: None,
                exit_routes: vec![],
                run_on_error: false,
            }],
            checkpoint: Default::default(),
        };
        let plugin_ids: HashSet<String> = [plugin].iter().map(|s| s.to_string()).collect();
        agentos_engine::compiler::compile_pipeline(
            &config,
            &agentos_core::types::StepLibrary::default(),
            &plugin_ids,
        )
        .expect("单步管道编译成功")
    }

    /// 正例 + 排除项一组覆盖：管道引用的 sidecar 选中；未引用的 sidecar 与
    /// 引用集外的 native 排除；性质断言结果 ⊆ 引用集。
    #[test]
    fn select_warmup_targets_filters_by_reference_and_host_type() {
        let pipeline = single_step_pipeline("alpha");
        let enabled = vec![
            manifest("alpha", HostType::Sidecar),
            manifest("unused_tool", HostType::Sidecar),
            // native 插件即使同名引用也无进程可预热（select 按引用+sidecar 双过滤；
            // 此处以未引用的 native 验证 host_type 排除面）
            manifest("native_one", HostType::InProcess),
        ];
        let targets = select_warmup_targets(&pipeline, &enabled);
        let ids: Vec<&str> = targets.iter().map(|m| m.id.as_str()).collect();
        assert_eq!(ids, vec!["alpha"], "只选管道引用的 sidecar: {ids:?}");
    }

    /// 空管道（无引用）→ 空集（空集短路，不发射任务）。
    #[test]
    fn select_warmup_targets_empty_reference_yields_empty() {
        let config = PipelineConfig {
            name: "p".into(),
            loop_bodies: vec![LoopBody {
                id: "main".into(),
                steps: vec![],
                while_cond: None,
                exit_routes: vec![],
                run_on_error: false,
            }],
            checkpoint: Default::default(),
        };
        let pipeline = agentos_engine::compiler::compile_pipeline(
            &config,
            &agentos_core::types::StepLibrary::default(),
            &HashSet::new(),
        )
        .expect("空管道编译成功");
        let enabled = vec![manifest("alpha", HostType::Sidecar)];
        assert!(select_warmup_targets(&pipeline, &enabled).is_empty());
    }
}
