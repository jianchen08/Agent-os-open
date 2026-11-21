//! 管道阶段执行与收尾（stage_execute / stage_finalize / 响应提取）。

use super::compile_cache::compiled_for;
use super::compile_cache::live_plugin_ids;
use super::compile_cache::pinned_config_from_state;
use super::compile_cache::resolve_config_pin;
use super::compile_cache::PIPELINE_CONFIG_PIN_HASH_KEY;
use super::compile_cache::PIPELINE_CONFIG_PIN_NAME_KEY;
use super::engine_dispatch::emit_run_terminal_domain_events;
use super::engine_dispatch::terminal_event_state_payload;
use super::engine_dispatch::EngineOutcome;
use super::*;
#[allow(clippy::too_many_arguments)]
#[allow(clippy::too_many_lines)] // 存量吸收(2026-10-12 文件规模棘轮): chat 执行编排聚合,拆分随执行链批次
pub(crate) async fn stage_execute(
    state: &AppState,
    invoker: Arc<dyn agentos_core::traits::PluginInvoker>,
    project_root: PathBuf,
    tenant: TenantContext,
    store: Arc<dyn StorageBackend>,
    initial_state: serde_json::Value,
    agent_id: &str,
    message: &str,
    run_id: &str,
    user_id: &str,
    pipeline_config_id: Option<&str>,
) -> Result<serde_json::Value, EngineOutcome> {
    // runs 表退役（ADR 2026-09-18）：防御失败簿记所需坐标提前捕获（initial_state/
    // tenant 随后移入 executor）
    let defensive_bookkeeping: (String, String) = (
        initial_state
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        tenant.tenant_id.clone(),
    );

    // 轮次观察点桥接（一轮 = 一条消息）：session 未接线的部署形态不挂（仅引擎执行）。
    // 线程/管道坐标从 initial_state 取（WS 路径与注入路径同一来源）。
    let round_events = state.session.as_ref().map(|session| {
        let thread_id = initial_state
            .get("session_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .or_else(|| initial_state.get("thread_id").and_then(|v| v.as_str()))
            .unwrap_or("")
            .to_string();
        let route_id = initial_state
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| thread_id.clone());
        Arc::new(crate::ws_session::SessionRoundEvents::new(
            session.clone(),
            thread_id,
            route_id,
            user_id.to_string(),
            store.clone(),
            tenant.tenant_id.clone(),
        )) as Arc<dyn agentos_engine::RoundEvents>
    });

    // ── Pull 热加载（在 project_root 被 move 给 executor 之前算出 config_root）──
    // 管道编译产物选择收敛点：None = autonomous 热重载单例（默认会话现状路径，
    // D9 范围外不钉）；Some(id) = 版本化取用（D9 出生即钉：实例首次取用把
    // (name, config_hash) 钉进管道 state 持久键，后续轮次恒用钉住版本，PUT
    // 热更只对之后出生的实例生效）。
    let config_root = project_root.join("config");
    let pinned = pinned_config_from_state(&initial_state);
    let explicit_name = pipeline_config_id.filter(|s| !s.is_empty());
    let pinned_hash = match resolve_config_pin(
        explicit_name,
        pinned.as_ref().map(|(n, h)| (n.as_str(), h.as_str())),
    ) {
        Ok(h) => h,
        Err(e) => {
            warn!(pipeline_config = ?pipeline_config_id, error = %e, "Pipeline config pin rejected");
            return Err(EngineOutcome::terminated(format!(
                "[error] 管道配置加载失败: {e}"
            )));
        }
    };
    let compiled = match compiled_for(
        state,
        &config_root,
        pipeline_config_id,
        pinned_hash.as_deref(),
    )
    .await
    {
        Ok(c) => c,
        Err(e) => {
            warn!(pipeline_config = ?pipeline_config_id, error = %e, "Pipeline config select failed");
            return Err(EngineOutcome::terminated(format!(
                "[error] 管道配置加载失败: {e}"
            )));
        }
    };
    // P1-1 core 器官缺省声明化：管道配置 `initial_state:` 声明的键值在开轮前
    // 补种（缺省只补缺，已有值不覆盖——本轮/调用方值优先）。core_type 是纯
    // 语义迭代形态标志，未声明时以 llm_call 作语义缺省；core_plugin 缺省由
    // 配置声明（使用动态核心步骤的管道必须声明，内核零插件 id 知识）。
    let mut initial_state = initial_state;
    if let Some(obj) = initial_state.as_object_mut() {
        for (k, v) in &compiled.initial_state {
            if !obj.contains_key(k) {
                obj.insert(k.clone(), v.clone());
            }
        }
        if !obj.contains_key("core_type") {
            obj.insert("core_type".into(), serde_json::json!("llm_call"));
        }
    }
    // D9 出生即钉（写入面）：实例首次解析显式配置 → (name, config_hash) 落
    // 管道 state 持久键，两路写：
    // - pipeline_state 表直写（冷路径/重启恢复面）：project_state_snapshot 只
    //   投影 persistent_fields 声明键，内核自有簿记键走直写——record_run_start
    //   同通道；
    // - initial_state 点号键（热路径）：随 final_state 进 registry 快照，下轮
    //   merge_recovered_scalars 恢复。
    // 表写失败终止本轮（B3 同精神）：钉住簿记缺失 = 重启后版本钉住静默失效。
    if let (Some(name), None) = (explicit_name, pinned.as_ref()) {
        let mut pin_fields = serde_json::Map::new();
        pin_fields.insert(
            PIPELINE_CONFIG_PIN_NAME_KEY.to_string(),
            serde_json::json!(name),
        );
        pin_fields.insert(
            PIPELINE_CONFIG_PIN_HASH_KEY.to_string(),
            serde_json::json!(compiled.config_hash),
        );
        let pin_pipeline_id = initial_state
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if pin_pipeline_id.is_empty() {
            warn!("版本钉住落库跳过：initial_state 缺 pipeline_id（不可达防御）");
        } else if let Err(e) = store
            .upsert_state_fields(pin_pipeline_id, &tenant.tenant_id, &pin_fields)
            .await
        {
            warn!(
                pipeline_id = %pin_pipeline_id,
                error = %e,
                "版本钉住落库失败，终止本轮（重发可恢复）"
            );
            return Err(EngineOutcome::terminated(format!(
                "[error] 管道版本钉住写入失败: {e}（本轮未执行，请重发消息重试）"
            )));
        }
        if let Some(obj) = initial_state.as_object_mut() {
            for (k, v) in &pin_fields {
                obj.insert(k.clone(), v.clone());
            }
        }
    }
    let known_ids = live_plugin_ids(state).await;
    // P1-4 声明化：收集全部插件 manifest `state.volatile_keys` 声明并集注入
    // store（StorageBackend::set_declared_volatile_keys），checkpoint 落档时
    // 与内核自有键在同一剥离点（save_checkpoint）剥离；恢复合并侧
    // （stage_build_initial_state）消费同一并集跳过。
    let declared_volatile: Vec<String> = state
        .manifests
        .read()
        .await
        .iter()
        .filter_map(|m| m.state.as_ref())
        .flat_map(|s| s.volatile_keys.iter().cloned())
        .collect();
    store.set_declared_volatile_keys(&declared_volatile);
    let executor = agentos_engine::PipelineExecutor::new(
        invoker,
        project_root,
        tenant,
        known_ids.iter().cloned(),
        store.clone(),
        run_id.to_string(),
    )
    // 分层持久化：从所有插件 manifest 的 persistent_fields 声明收集并集。
    // 这些是需跨轮持久化的累计标量字段（如 track.total_tokens）。
    // messages 是系统字段（引擎固定投影），不依赖此集合。
    .with_persistent_fields(
        state
            .manifests
            .read()
            .await
            .iter()
            .flat_map(|m| m.persistent_fields.iter().cloned()),
    )
    // 监控 M2 分类（整改 K2 声明化）：从全部插件 manifest 的
    // `capabilities.metrics_class` 声明收集注入（未声明 = Other 兜底；
    // 非法值引擎装配点 warn 后按 Other 计）。
    .with_metrics_classes(state.manifests.read().await.iter().filter_map(|m| {
        m.capabilities
            .metrics_class
            .clone()
            .map(|declared| (m.id.clone(), declared))
    }))
    // on_pipeline_end 钩子插件收集（spill_guard 清理通道）：manifest 声明了该
    // 钩子的插件在 run 结束时收到 OnPipelineEnd（best-effort，见 executor）。
    .with_pipeline_end_hook_plugins(
        state
            .manifests
            .read()
            .await
            .iter()
            .filter(|m| {
                m.capabilities
                    .lifecycle_hooks
                    .contains(&agentos_core::traits::LifecycleHook::OnPipelineEnd)
            })
            .map(|m| m.id.clone()),
    )
    // 生命周期事件总线（观察路径）：run 开始/结束广播 OnPipelineStart/OnPipelineEnd
    // 给审计/指标订阅者。进程级单例（agentos-kernel 启动期 set_global 注册；
    // 未注册 None = 不发射，与域事件发射点同一降级语义）。
    .with_hook_bus(agentos_hooks::global());
    // 轮数硬上限接线（管道 YAML `max_rounds:` → 编译产物 → executor，ADR
    // 2026-09-11-engine-loop-cap）：None = 不注入（引擎缺省 200，旧 YAML 行为
    // 不变）；Some(0) 注入后由 run 入口 EngineError::Config 拒绝（零执行）。
    let executor = match compiled.max_rounds {
        Some(n) => executor.with_max_rounds(n),
        None => executor,
    };
    // 轮次观察点（None = 未接线）：chat 会话路径逐轮发流式事件。
    let executor = if let Some(ev) = round_events {
        executor.with_round_events(ev)
    } else {
        executor
    };

    // agent 配置唯一事实源 = agent_config_load 插件；工具面过滤 = tool_schema 插件
    // 经内核 tool-surface capability（K10 执行时契约）。引擎/内核零 agent 配置知识。
    info!(run_id = %run_id, agent_id = %agent_id, "Pipeline run started");

    // GAP-2：防御网路径的失败事件标签预捕获（run_compiled 会 move initial_state）。
    // 载荷复用终态剥离集——初始 state 携带出生契约键（task.*/lineage.*），
    // 订阅方据此派生失败通知；messages 等大字段已剥离，克隆成本 KB 级。
    let failed_emit_state = terminal_event_state_payload(&initial_state);

    // run 启动域事件：回合开始时广播 run.started，触发器（EVENT）与生命周期
    // 订阅者据此感知回合边界。标签与 run 终态事件同构（run_id/pipeline_id/
    // thread_id/session_id）。
    crate::plugin_lifecycle::broadcast_domain_event(
        state,
        "run.started",
        vec![
            ("run_id".to_string(), serde_json::json!(run_id)),
            (
                "pipeline_id".to_string(),
                initial_state
                    .get("pipeline_id")
                    .cloned()
                    .unwrap_or(serde_json::Value::Null),
            ),
            (
                "thread_id".to_string(),
                initial_state
                    .get("session_id")
                    .cloned()
                    .unwrap_or(serde_json::Value::Null),
            ),
        ],
    )
    .await;

    match executor.run_compiled(&compiled, initial_state).await {
        Ok(s) => Ok(s),
        Err(e) => {
            // error 级（run 失败是终态裁决事件；装机 2026-10-10 B74/B75/B76
            // 排障按 ERROR 检索不到本次级 WARN，失败原因等于没落日志）
            error!(run_id = %run_id, error = %e, "PipelineExecutor run failed");
            // GAP-2：run.failed 终态事件（引擎 Err 防御网；task.* 派生经预捕获标签）
            emit_run_terminal_domain_events(state, &failed_emit_state, run_id, true).await;
            // B2：引擎失败兜底——把 run 标记 failed + ended_at，避免永远卡 running、
            // 历史悬空。（PipelineExecutor::run 当前不返回 Err，这是防御网；崩溃留下的
            // running 孤儿由内核启动 reap_orphan_runs 清扫。）
            // runs 表退役（ADR 2026-09-18）：簿记 = state 运行键（按 pipeline_id 定位）
            let (failed_pipeline_id, bookkeeping_tenant) = &defensive_bookkeeping;
            if !failed_pipeline_id.is_empty() {
                let now = chrono::Utc::now().to_rfc3339();
                let mut fields = serde_json::Map::new();
                fields.insert("run_status".to_string(), serde_json::json!("failed"));
                fields.insert("run_ended_at".to_string(), serde_json::json!(now));
                if let Err(pe) = store
                    .upsert_state_fields(failed_pipeline_id, bookkeeping_tenant, &fields)
                    .await
                {
                    warn!(run_id = %run_id, error = %pe, "run_status(Failed) 簿记失败（继续）");
                }
            }
            Err(EngineOutcome::terminated(format!(
                "[engine-run-failed] {message}"
            )))
        }
    }
}

/// 阶段 3b/4：final_state 回写全局 registry + 提取响应。回写：final_state 含
/// 完整 messages 历史（LLM 插件 append 了 assistant 回复），按
/// (tenant_id, effective_pipeline_id) 常驻：下一轮热路径直接复用免 DB 查询，
/// 重启/内存丢失时冷路径从 messages 表恢复（见 stage_recover_history）。常驻
/// 副本剥离大对象：`tool_schemas`（每轮 prepare 链经 tool-surface capability
/// 重新拉取，跨轮无意义）与 `_` 前缀引擎私有 staging 键（`_full_tool_results`
/// 等 per-run 中间态，权威副本在工具缓存，见 ADR 2026-09-29-trace-mirror-dedup）；
/// messages 驻留是设计正确（管道持有队列省每轮拼装），不动。
pub(crate) async fn stage_finalize(
    final_state: &serde_json::Value,
    store: Option<&dyn StorageBackend>,
    tenant_id: &str,
    effective_pipeline_id: &str,
    thread_id: &str,
    agent_id: &str,
    run_id: &str,
) -> EngineOutcome {
    // 终态失败折算（控制状态键契约 ADR 2026-08-30，与引擎落库/域事件共用单点
    // RunStatus::from_control_state）：署名失败（dropped_tool_calls/timeout 等
    // FAILED_STOP_REASONS 命中，DSL 正常收束）曾硬编码 failed:false——失败 run
    // 以成功姿态外发，chat 面把虚报文本当真（BUG-45 机制根源）；挂起/取消非失败。
    let status = agentos_core::types::RunStatus::from_control_state(final_state);
    // 步骤失败聚合（ADR 2026-10-15-step-error-run-signature-aggregation）：插件
    // 步失败由引擎 warn+继续（ADR 2026-08-18），错误收集在 `_plugin_errors`——
    // 未署名完成轮零产出（raw_result 空，与响应提取同锚）折叠 failed；有产出
    // = 有痕完成（degraded）。控制状态署名优先，挂起/取消/署名失败轮不折叠。
    let plugin_errors = final_state
        .get("_plugin_errors")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    // 轮边界注入消费的队列 cmid（引擎 `_consumed_pending_cmids` 键，跨多次注入
    // 累积；下划线前缀键随 strip_non_resident_keys 剥离不进持久化）。
    let consumed_pending_cmids = final_state
        .get("_consumed_pending_cmids")
        .and_then(|v| v.as_array())
        .map(|a| {
            a.iter()
                .filter_map(|c| c.as_str().map(str::to_string))
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    let has_output = final_state
        .get("raw_result")
        .is_some_and(|v| v.as_str().is_some_and(|s| !s.is_empty()));
    let control_failed = matches!(status, agentos_core::types::RunStatus::Failed);
    let fold_failed = !control_failed
        && !has_output
        && !plugin_errors.is_empty()
        && status == agentos_core::types::RunStatus::Completed;
    let content = if control_failed {
        let reason = final_state
            .get("router.stop_reason")
            .and_then(|v| v.as_str())
            .unwrap_or("unknown");
        format!("本轮 run 失败（stop_reason: {reason}）")
    } else if fold_failed {
        step_error_fold::zero_output_content(&plugin_errors)
    } else {
        extract_response_content(final_state)
    };
    // A2：本轮最终 assistant 消息（完整持久形态，含 tool_calls/reasoning_content/seq），
    // WS 路径 new_message 携带它——前端与 DB 加载共用 mapper，冷热路径同构。
    let final_assistant = final_state
        .get("messages")
        .and_then(|v| v.as_array())
        .and_then(|msgs| {
            msgs.iter()
                .rev()
                .find(|m| m.get("role").and_then(|v| v.as_str()) == Some("assistant"))
                .cloned()
        });
    let failed = control_failed || fold_failed;
    // 已删除管道：收尾不回写内存注册表——删除已把 registry 条目逐出，回写
    // 会让 /pipelines/state 的 registry 读面重新长出幽灵行（DB 侧迟到写入
    // 已由 store 的删除墓碑写屏障丢弃）。
    if !effective_pipeline_id.is_empty()
        && !agentos_engine::deleted_pipelines::is_deleted(effective_pipeline_id)
    {
        let reg = agentos_session::global_registry();
        let mut persisted = final_state.clone();
        strip_non_resident_keys(&mut persisted);
        // 失败不进消息流（ADR 2026-10-27-pipeline-error-notification-channel，
        // 用户裁定，替代缺陷 ④-A 的失败回复直写契约）：失败是管道域事实，不
        // 是对话内容——合成 assistant 错误消息（synthetic=true）曾直写
        // message_slots 并入常驻快照，聊天流被错误气泡污染且刷新后历史悬置
        // 错误回复。失败事实的常驻面 = run_status / run_stop_reason /
        // run.plugin_errors（下方镜像键，管道域读面）+ run_failed WS 事件
        // （error 字段携带首条插件错误原文，通知卡弹层）；REST content 与
        // WS stream_error 照常携带失败终态文本。消息队列零写入 = 零回灌
        // （llm_core 出站装配天然见不到失败原因，旧 synthetic 剔除逻辑随之
        // 退役）。
        // 插件级失败常驻镜像（ADR 2026-10-17-task-birth-workspace-inclusion）：
        // `_plugin_errors` 是引擎私有 staging 键（`_` 前缀），strip 时随快照
        // 剥离——重启后插件级失败彻底失忆，启动对账（tasks/reconcile）与面板
        // 读面均无从知悉。镜像到常驻键 run.plugin_errors 并直写 pipeline_state
        // 表，冷启动后仍可读。非空才写：绝大多数 run 零插件失败，不增常驻体积。
        // 不改 ADR 2026-10-15 的折叠语义（有产出 + 步失败 = degraded 仍成立），
        // 本键只负责让失败事实不丢。
        if !plugin_errors.is_empty() {
            if let Some(obj) = persisted.as_object_mut() {
                obj.insert(
                    agentos_engine::RUN_PLUGIN_ERRORS_KEY.to_string(),
                    serde_json::Value::Array(plugin_errors.clone()),
                );
            }
            if let Some(store) = store {
                let mut fields = serde_json::Map::new();
                fields.insert(
                    agentos_engine::RUN_PLUGIN_ERRORS_KEY.to_string(),
                    serde_json::Value::Array(plugin_errors.clone()),
                );
                if let Err(pe) = store
                    .upsert_state_fields(effective_pipeline_id, tenant_id, &fields)
                    .await
                {
                    warn!(
                        pipeline_id = %effective_pipeline_id,
                        tenant_id = %tenant_id,
                        run_id = %run_id,
                        error = %pe,
                        "插件级失败常驻落库失败（继续；重启后该失败将不可见）"
                    );
                }
            }
        }
        if !reg.contains(tenant_id, effective_pipeline_id) {
            reg.get_or_init(
                tenant_id,
                effective_pipeline_id,
                thread_id,
                agent_id,
                persisted,
            );
        } else {
            reg.update_state(tenant_id, effective_pipeline_id, persisted);
        }
    }
    EngineOutcome {
        content,
        final_assistant,
        failed,
        // 有痕完成（步失败后自救产出）：调用方据此与干净完成区分；失败/干净轮恒 false。
        degraded: !fold_failed
            && !plugin_errors.is_empty()
            && status == agentos_core::types::RunStatus::Completed,
        // 插件错误收集（引擎 `_plugin_errors` 键；下划线前缀键不参与插件
        // state_updates 合并，仅引擎内部写入）。WS 路径据此发射 plugin_error。
        plugin_errors,
        consumed_pending_cmids,
    }
}

/// 常驻快照剥离：`tool_schemas`（每轮 prepare 链经 tool-surface capability
/// 重新拉取，跨轮无意义）与 `_` 前缀引擎私有 staging 键（`_full_tool_results`
/// 等 per-run 中间态，权威副本在工具缓存，见 ADR 2026-09-29-trace-mirror-dedup）。
/// messages 驻留是设计正确（管道持有队列省每轮拼装），不动。
pub(super) fn strip_non_resident_keys(state: &mut serde_json::Value) {
    if let Some(obj) = state.as_object_mut() {
        obj.remove("tool_schemas");
        let underscore_keys: Vec<String> =
            obj.keys().filter(|k| k.starts_with('_')).cloned().collect();
        for k in underscore_keys {
            obj.remove(&k);
        }
    }
}

/// 响应内容提取：只认 raw_result（LLM 本轮产出）。
///
/// A12：兜底不再把整份内部 state pretty-print 给客户端（内部结构/工具细节泄漏），
/// 固定返回 "pipeline finished"；完整 state 保留在服务端 tracing（debug 级）。
/// 不回退 state.message——那是用户输入原文，回退即把用户消息当回复回发
/// （前端表现为 assistant 气泡回显用户消息）。无回复轮次由调用方
/// （ws_session run_pipeline_round）按 final_assistant 缺失走 stream_error。
pub(super) fn extract_response_content(final_state: &serde_json::Value) -> String {
    if let Some(raw) = final_state
        .get("raw_result")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
    {
        raw.to_string()
    } else {
        debug!(
            target: "chat-response",
            state = ?final_state,
            "pipeline finished without raw_result（完整 state 仅留服务端日志）"
        );
        "pipeline finished".to_string()
    }
}

/// RFC 7396 JSON Merge Patch：把 patch 按序合并进 target。
/// 用于冷启动时按 step 级轨迹逐条 merge 回放，重建完整 state（**仅标量字段**）。
/// - patch 中值为对象：递归 merge（target 同 key 也为对象时）
/// - patch 中值为 null：从 target 删除该 key
/// - 否则：target[key] = patch[key]（整体替换）
///
/// messages **不参与回放**（零兼容）：消息队列真值在 message_slots 表（冷路径直读），
/// 轨迹里的 messages 实录只做审计/回退重建（任务 6），不走本函数。
pub(super) fn merge_patch(target: &mut serde_json::Value, patch: &serde_json::Value) {
    if let serde_json::Value::Object(patch_map) = patch {
        if !target.is_object() {
            *target = serde_json::Value::Object(serde_json::Map::new());
        }
        let target_obj = target.as_object_mut().expect("ensured object above");
        for (k, v) in patch_map {
            if v.is_null() {
                target_obj.remove(k);
            } else if k == "messages" {
                continue; // 队列真值在表，标量回放跳过
            } else if let Some(existing) = target_obj.get_mut(k) {
                if existing.is_object() && v.is_object() {
                    merge_patch(existing, v);
                } else {
                    *existing = v.clone();
                }
            } else {
                target_obj.insert(k.clone(), v.clone());
            }
        }
    } else {
        *target = patch.clone();
    }
}

/// /api/v1/chat POST 端点——通过管道引擎处理消息。
#[cfg(test)]
mod stage_finalize_residency_tests {
    use super::*;

    /// 常驻快照剥离 `_` 前缀引擎私有 staging 键（ADR 2026-09-29-trace-mirror-
    /// dedup）：`_full_tool_results` 等是 per-run 中间态（同 run 内被
    /// tool_cache_writer 消费，跨轮零消费——恢复链 merge_recovered_scalars 跳过
    /// `_` 键），不得随 final_state 滞留内存注册表。messages 驻留是设计正确
    /// （队列跨轮延续），不动；tool_schemas 剥离是既有契约。
    #[tokio::test]
    async fn residency_snapshot_strips_underscore_staging_keys_and_keeps_messages() {
        let reg = agentos_session::pipeline_state_registry::global_registry();
        let tenant = "tenant_finalize_us";
        let pid = "pipe_finalize_us";
        let final_state = serde_json::json!({
            "pipeline_id": pid,
            "run_status": "completed",
            "task.status": "done",
            "messages": [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a"},
            ],
            "_full_tool_results": [{"tool_name": "bash_execute", "data": "X".repeat(256)}],
            "_executed_tool_calls": [{"name": "bash_execute"}],
            "_plugin_errors": [],
            "tool_schemas": {"bash_execute": {"type": "object"}},
        });
        let _ = stage_finalize(
            &final_state,
            None,
            tenant,
            pid,
            "thread_fu",
            "agentos",
            "run_fu",
        )
        .await;

        let entry = reg.get(tenant, pid).expect("收尾应补建条目");
        let e = entry.read();
        assert!(
            e.state.get("_full_tool_results").is_none(),
            "全文 staging 键不得驻留"
        );
        assert!(
            e.state.get("_executed_tool_calls").is_none(),
            "执行记录 staging 键不得驻留"
        );
        assert!(
            e.state.get("_plugin_errors").is_none(),
            "引擎私有键不得驻留"
        );
        assert!(
            e.state.get("tool_schemas").is_none(),
            "tool_schemas 剥离是既有契约"
        );
        assert_eq!(e.state["task.status"], "done", "合法标量保留");
        assert_eq!(
            e.state["messages"].as_array().map(Vec::len),
            Some(2),
            "messages 队列驻留是设计正确，不动"
        );
        reg.remove(tenant, pid);
    }
}
