//! 管道阶段：初始 state 构建 + overlay 合并 + 标量恢复。

use super::engine_dispatch::merge_message_execution_context;
use super::engine_dispatch::set_execution_context_path;
use super::*;
/// 阶段 1/1a/1a2：构造初始 state（含会话级/任务级 execution_context 注入）。
///
/// core 器官执行态缺省（core_type 语义标志 + core_plugin 插件选择）不在本函数
/// 种入——P1-1 声明化后由管道配置 `initial_state:` 声明、stage_execute
/// 消费编译产物补种（内核零插件 id 知识）；agent 配置加载已移出内核
/// （内核不读 config/agents/**，语义由管道输入插件 agent_config_load 按执行上下文
/// 键自持），管道配置步骤经 state 写面按步覆盖 core_type/core_plugin
/// （config/pipelines/autonomous.yaml）。自由 state overlay 在恢复合并之后
/// 应用（stage_recover_history）。
// 技术债（同上）：多参阶段函数，收尾时统一收敛参数结构体。
#[allow(clippy::too_many_arguments)]
pub(crate) async fn stage_build_initial_state(
    state: &AppState,
    store: &Arc<dyn StorageBackend>,
    message: &str,
    effective_pipeline_id: &str,
    thread_id: &str,
    message_id: &str,
    user_id: &str,
    execution_context: Option<&serde_json::Value>,
    run_id: &str,
) -> serde_json::Value {
    let mut initial_state = serde_json::json!({
        "message": message,
        "input": message,
        // 执行身份不是 per-run 注入键：agent 身份是管道 state 持久键 `agent.id`
        // （出生方经 chat.send_message state 透传落库），随本函数之后的热恢复
        // （registry）/冷恢复（pipeline_state 表）进入 state，供 agent_config_load
        // 等插件消费面读取——缺省主 agent 由消费面自持，派发不再携带/注入身份。
        "ended": false,
        "suspended": false,
        // 实际状态（内核持有，2026-09-03 双状态裁定）：run 生命周期唯一真值，
        // 起点恒为 running；收束由引擎经终态映射单点改写。插件对该键的写入在
        // merge 处拦截、overlay 写入被保留字拦截，恢复合并经 VOLATILE_RUN_KEYS
        // 剥离——上轮终态不会复活成本轮起点。
        "run_status": "running",
        // run_id：本轮 run 的轮询定位锚（批次 C 注入，llm_core 取消轮询经
        // pipeline-executor.get_run_status 查询它；跨轮被下一轮覆盖，属 per-run 键）。
        "run_id": run_id,
        // 流式推送路由键：
        // - pipeline_id：前端消息路由键（前端 handleStreamStart 据此匹配占位气泡）
        // - session_id：WS 连接路由键（内核 session.emit_stream 据此定位前端 WS 连接）
        //   必须是真实 thread_id（WS 握手时注册的），否则 chunk 推不到前端。
        // pipeline_id 用 effective 值覆盖：persist_run_start/end 从 state 读
        // pipeline_id 落库，统一语义后写入与查询键一致，空值不再产生孤儿数据。
        "pipeline_id": effective_pipeline_id,
        "session_id": thread_id,
        // user_id：触发器（trigger_setup）等工具在 0.2 需它做上下文绑定（触发时
        // 经 chat.send_message 回派发需要 user_id 解析 tenant）。tool_args_inject 据
        // 此注入到工具 args；空串时 tool_args_inject 自动跳过注入，不影响既有行为。
        "user_id": user_id,
        // assistant message_id：内核权威生成，sidecar 流式 chunk 携带它，
        // 前端 handleStreamChunk 据此把 chunk 路由到 stream_start 建立的占位气泡。
        "message_id": message_id,
    });

    // 1a. 会话级 execution_context 注入（声明驱动，ADR 2026-08-28）：遍历启用
    // 插件 contributes.thread_fields 的 x_metadata_key → x_execution_path 声明，
    // 把 thread metadata（会话创建时由前端按声明写入）的值按声明路径写入
    // execution_context。内核不认识 workspace/isolation 等具体键（归属
    // workspace_lifecycle / isolation 插件），缺省值同样归插件执行期兜底
    // （workspace_lifecycle：mode 未指定 → plain）——与前端表单组装
    // （SessionEditModal 按 x_execution_path 组装消息级上下文）同构。
    //
    // 任务级 execution_context（task_submit 提交）经消息级参数透传，整体覆盖
    // 会话级来源（1a2），优先级不变。
    match store.get_session(thread_id).await {
        Ok(Some(sess)) => {
            if let Some(meta) = sess.metadata {
                let enabled = state.enabled_plugin_ids.read().await.clone();
                let mut ec = serde_json::Map::new();
                for manifest in state.manifests.read().await.iter() {
                    if !enabled.contains(&manifest.id) {
                        continue;
                    }
                    let Some(fields) = manifest
                        .contributes
                        .as_ref()
                        .and_then(|c| c.get("thread_fields"))
                        .and_then(|v| v.as_array())
                    else {
                        continue;
                    };
                    for field in fields {
                        let (Some(meta_key), Some(path)) = (
                            field.get("x_metadata_key").and_then(|v| v.as_str()),
                            field.get("x_execution_path").and_then(|v| v.as_str()),
                        ) else {
                            continue;
                        };
                        // 只搬运非空字符串值（空值 = 未设置，默认语义归插件）
                        let Some(val) = meta
                            .get(meta_key)
                            .and_then(|v| v.as_str())
                            .filter(|s| !s.is_empty())
                        else {
                            continue;
                        };
                        set_execution_context_path(&mut ec, path, serde_json::json!(val));
                    }
                }
                if !ec.is_empty() {
                    if let Some(obj) = initial_state.as_object_mut() {
                        obj.insert(
                            "execution_context".to_string(),
                            serde_json::Value::Object(ec),
                        );
                    }
                }
            }
        }
        Ok(None) => {}
        Err(e) => {
            warn!(
                thread_id = %thread_id,
                error = %e,
                "execution_context 会话级注入失败：get_session 读不到会话（沿用默认）"
            );
        }
    }

    // 1a2. 消息级 execution_context 逐键并入（经 chat.send_message params 透传，
    // 任务执行器从 task.metadata 组装走同一通道）：消息级显式键 > 会话级同键，
    // 消息级未携带的键保留会话级种子（声明驱动兜底，整体替换会把会话级其余
    // 键丢掉）。非对象/空对象不注入。
    if let Some(ec) = execution_context {
        if ec.is_object() && !ec.as_object().map(|o| o.is_empty()).unwrap_or(true) {
            let base = initial_state
                .as_object_mut()
                .expect("initial_state 恒为对象")
                .entry("execution_context".to_string())
                .or_insert_with(|| serde_json::Value::Object(serde_json::Map::new()));
            merge_message_execution_context(base, ec);
        }
    }

    // 1a3（自由 state overlay）已移至 stage_recover_history 恢复合并之后应用
    // （B13① 域界定，ADR 2026-09-06）：恢复基线（registry 快照/checkpoint/
    // pipeline_state 表）合并会覆盖写已存在键，overlay 先应用会被残留值回写
    // 污染——显式派发指令必须优先于持久化基线。

    initial_state
}

/// 把自由 state overlay 并入 initial_state 顶层扁平键（GAP-1 阶段 1 合并点）。
///
/// 防御语义（调用方 chat_send_handler 已做保留字校验，此处纵深防御）：
/// - 引擎系统保留字（[`crate::kernel_capabilities::RESERVED_STATE_KEYS`]，
///   契约文件锚定的单一真值源清单）跳过，非 chat 入口的理论调用者无法覆写
///   message/pipeline_id 等系统字段；
/// - `lineage.*` 已存在时跳过——lineage 是引擎出生写入的保护字段（与 messages
///   同级），后续轮次 overlay 不可覆写（防伪造父/根）。
pub(crate) fn apply_state_overlay(
    initial_state: &mut serde_json::Value,
    overlay: &serde_json::Value,
) {
    let Some(src) = overlay.as_object() else {
        return;
    };
    let Some(obj) = initial_state.as_object_mut() else {
        return;
    };
    for (k, v) in src {
        if crate::kernel_capabilities::RESERVED_STATE_KEYS.contains(&k.as_str()) {
            continue;
        }
        if k.starts_with("lineage.") && obj.contains_key(k) {
            continue;
        }
        obj.insert(k.clone(), v.clone());
    }
}

/// 把恢复源（热路径 = registry 快照，冷路径 = checkpoint/traces/表重建）的标量键
/// 并入 initial_state 顶层扁平键，两条路径共用同一合并语义。
///
/// 跳过三类键：`messages`（队列真值由热/冷路径独立装配，不在此恢复）；内部
/// 下划线键、内核自有 per-run 键 [`agentos_engine::VOLATILE_RUN_KEYS`] 与插件
/// `state.volatile_keys` 声明键（per-run 指令与锚点/终止标志——快照残留会顶掉
/// 本轮新输入，旧 run_id 会顶掉取消轮询新锚，GAP-3 与批次 D 重跑标志同源）。
pub(super) fn merge_recovered_scalars(
    initial_state: &mut serde_json::Value,
    source: &serde_json::Value,
    declared_volatile: &std::collections::HashSet<String>,
) {
    let Some(src_obj) = source.as_object() else {
        return;
    };
    let Some(init_obj) = initial_state.as_object_mut() else {
        return;
    };
    for (k, v) in src_obj {
        if k == "messages" || k.starts_with('_') {
            continue;
        }
        if agentos_engine::VOLATILE_RUN_KEYS.contains(&k.as_str()) || declared_volatile.contains(k)
        {
            continue;
        }
        // execution_context：每轮声明组装键（1a 会话级 + 1a2 消息级在本函数
        // 之前先行合并），恢复源残留是历史轮组装产物——本轮已注入（键存在）
        // 则跳过，防上轮值顶掉本轮显式声明（消息级显式键 > 会话级默认 >
        // 恢复兜底，mode/isolation/workspace 全键同序）；本轮两级皆无时照常
        // 恢复（续跑连续性兜底：无透传轮沿用上轮执行上下文）。
        if k == "execution_context" && init_obj.contains_key(k) {
            continue;
        }
        init_obj.insert(k.clone(), v.clone());
    }
}

#[cfg(test)]
mod merge_recovered_scalars_tests {
    use super::*;

    /// 2026-09-02 裁定：新消息到达 = 用户已响应交互，新一轮应从非对话态开始
    /// 直接运行——上轮挂起轮的对话/执行态（conversation_mode/core_type/
    /// core_plugin 及终止标志）不得经恢复合并复活。P1-4 后插件语义键经
    /// `state.volatile_keys` 声明进 declared_volatile（模拟 llm_core/human
    /// 插件声明的并集），内核自有键仍走 VOLATILE_RUN_KEYS。
    #[test]
    fn skips_volatile_run_keys_including_conversation_state() {
        let declared: std::collections::HashSet<String> = [
            "tool_schemas",
            "conversation_mode",
            "core_type",
            "core_plugin",
        ]
        .into_iter()
        .map(String::from)
        .collect();
        let mut initial = serde_json::json!({
            "message": "新消息",
            "run_id": "new-run",
            "suspended": false,
            "ended": false,
            "run_status": "running",
            "core_type": "llm_call",
            "core_plugin": "pipeline_llm_core",
        });
        // 恢复源 = 上一轮 final_state（交互挂起轮）：对话态 + 执行态 + 终止标志残留
        let source = serde_json::json!({
            "task.id": "t1",
            "workspace": "/ws",
            "conversation_mode": true,
            "core_type": "tool_execute",
            "core_plugin": "pipeline_tool_core",
            "suspended": true,
            "ended": true,
            "run_status": "completed",
            "run_id": "old-run",
        });
        merge_recovered_scalars(&mut initial, &source, &declared);
        // 持久键照常恢复
        assert_eq!(initial["task.id"], "t1");
        assert_eq!(initial["workspace"], "/ws");
        // 本轮执行态不被上轮覆盖（新 run 从 llm_call 开始；空转根因回归断言）
        assert!(
            initial.get("conversation_mode").is_none(),
            "对话等待态不得跨轮复活"
        );
        assert_eq!(initial["core_type"], "llm_call");
        assert_eq!(initial["core_plugin"], "pipeline_llm_core");
        // 终止标志、实际状态与新 run_id 保持本轮初始值
        assert_eq!(initial["suspended"], false);
        assert_eq!(initial["ended"], false);
        assert_eq!(initial["run_status"], "running");
        assert_eq!(initial["run_id"], "new-run");
    }

    /// 非易变标量照常合并（合并语义不破坏续跑不丢键）。
    #[test]
    fn merges_persistent_scalars() {
        let mut initial = serde_json::json!({"message": "m"});
        let source = serde_json::json!({"task.id": "t9", "track.total_tokens": 42});
        merge_recovered_scalars(&mut initial, &source, &Default::default());
        assert_eq!(initial["task.id"], "t9");
        assert_eq!(initial["track.total_tokens"], 42);
    }

    /// 思考强度走声明面 overlay 通道（ADR-2026-11-17）：恢复合并在前、派发
    /// overlay 在后应用——本轮显式选择覆盖恢复值；本轮未带键（重发/回退不传参）
    /// 则沿用恢复值（管道上次选择）。对应旧实现里 `merge_recovered_scalars`
    /// 的 `thinking_strength` 特例，特例已随重复通道一并删除。
    #[test]
    fn declared_overlay_value_wins_recovered_value() {
        let source = serde_json::json!({
            "thinking_strength": "{\"reasoning_effort\":\"max\"}",
        });
        let mut fresh = serde_json::json!({"message": "m"});
        merge_recovered_scalars(&mut fresh, &source, &Default::default());
        apply_state_overlay(
            &mut fresh,
            &serde_json::json!({"thinking_strength": "{\"thinking\":{\"type\":\"disabled\"}}"}),
        );
        assert_eq!(
            fresh["thinking_strength"], "{\"thinking\":{\"type\":\"disabled\"}}",
            "本轮显式选择不得被恢复值覆盖"
        );
        let mut untouched = serde_json::json!({"message": "m"});
        merge_recovered_scalars(&mut untouched, &source, &Default::default());
        apply_state_overlay(&mut untouched, &serde_json::json!({}));
        assert_eq!(
            untouched["thinking_strength"], "{\"reasoning_effort\":\"max\"}",
            "本轮未选择 → 沿用恢复的管道上次选择"
        );
    }

    /// P1-4 声明化：恢复源残留的插件声明键同样不得复活（合并跳过声明键），
    /// 未声明键照常合并——跳过范围随声明增减。
    #[test]
    fn skips_declared_volatile_keys_from_plugins() {
        let declared: std::collections::HashSet<String> =
            ["tool_schemas"].into_iter().map(String::from).collect();
        let mut initial = serde_json::json!({"message": "m"});
        let source = serde_json::json!({"tool_schemas": [{"name": "x"}], "task.id": "t2"});
        merge_recovered_scalars(&mut initial, &source, &declared);
        assert!(
            initial.get("tool_schemas").is_none(),
            "声明键属 per-run 工具面，跨轮残留 = schema 滞留"
        );
        assert_eq!(initial["task.id"], "t2");
    }

    /// execution_context 是每轮声明组装键（1a 会话级 + 1a2 消息级先行注入），
    /// 恢复源残留是历史轮组装产物——本轮已注入时不得被快照整键顶掉
    /// （消息级显式键 > 会话级默认 > 恢复兜底，mode/isolation/workspace 全键同序）。
    #[test]
    fn execution_context_this_round_declaration_beats_recovered_snapshot() {
        let mut initial = serde_json::json!({
            "message": "m",
            "execution_context": {"mode": "coding"},
        });
        let source = serde_json::json!({
            "task.id": "t3",
            "execution_context": {"mode": "research"},
        });
        merge_recovered_scalars(&mut initial, &source, &Default::default());
        assert_eq!(
            initial["execution_context"]["mode"], "coding",
            "本轮显式 mode 不得被上轮残留覆盖"
        );
        assert_eq!(initial["task.id"], "t3", "其余持久键照常恢复");
    }

    /// 同序反例：本轮会话级种子也优先于恢复源（作用域是整键，不限消息级）。
    #[test]
    fn execution_context_session_seed_beats_recovered_snapshot() {
        let mut initial = serde_json::json!({
            "message": "m",
            "execution_context": {"workspace": {"source_path": "会话级目录"}},
        });
        let source = serde_json::json!({
            "execution_context": {
                "workspace": {"source_path": "项目目录", "mode": "worktree"},
                "isolation": {"level": "isolated"},
            },
        });
        merge_recovered_scalars(&mut initial, &source, &Default::default());
        assert_eq!(
            initial["execution_context"],
            serde_json::json!({"workspace": {"source_path": "会话级目录"}}),
            "本轮会话级种子原样保留，恢复源不得整键覆写"
        );
    }

    /// 本轮两级皆无（1a 无声明值且消息未携带）：恢复值照常生效——续跑
    /// 连续性兜底（无透传轮沿用上轮执行上下文，防工作区漂移）。
    #[test]
    fn execution_context_recovered_when_round_has_no_declaration() {
        let mut initial = serde_json::json!({"message": "m"});
        let source = serde_json::json!({
            "execution_context": {"isolation": {"level": "isolated"}},
        });
        merge_recovered_scalars(&mut initial, &source, &Default::default());
        assert_eq!(
            initial["execution_context"]["isolation"]["level"], "isolated",
            "本轮无声明时恢复值兜底生效"
        );
    }
}

#[cfg(test)]
mod apply_state_overlay_tests {
    use super::*;

    /// 实际状态内核持有（2026-09-03 双状态裁定）：overlay（chat.send_message
    /// state 参数）命中保留字 run_status 一律跳过——实际状态只出自引擎折算。
    #[test]
    fn overlay_cannot_write_kernel_held_run_status() {
        let mut initial = serde_json::json!({"run_status": "running", "pipeline_id": "p1"});
        let overlay = serde_json::json!({
            "run_status": "failed",
            "task.goal": "合法注入",
        });
        apply_state_overlay(&mut initial, &overlay);
        assert_eq!(initial["run_status"], "running", "保留字拦截 overlay 直写");
        assert_eq!(initial["task.goal"], "合法注入", "非保留键照常并入");
    }

    /// agent.id 消息级覆盖（WS agent_id 透传 2026-09-25）：overlay 在恢复合并
    /// 之后应用——帧显式携带的执行身份覆盖既有持久键（每条消息可切换人格），
    /// 原本无该键时照常写入（管道后续轮次沿用最后写入值）。
    #[test]
    fn overlay_agent_id_overrides_recovered_identity() {
        // 既有身份（热/冷恢复写入 initial_state 后的形态）被消息级覆盖。
        let mut initial = serde_json::json!({"agent.id": "old_persona", "run_status": "running"});
        apply_state_overlay(
            &mut initial,
            &serde_json::json!({"agent.id": "new_persona"}),
        );
        assert_eq!(
            initial["agent.id"], "new_persona",
            "消息级显式身份覆盖恢复基线"
        );
        // 管道原本无身份键（会话类管道）→ 写入生效。
        let mut fresh = serde_json::json!({"run_status": "running"});
        apply_state_overlay(&mut fresh, &serde_json::json!({"agent.id": "persona_a"}));
        assert_eq!(fresh["agent.id"], "persona_a");
    }

    /// 通道守卫：agent.id 不得进入 overlay 保留字与 per-run 挥发键——否则
    /// WS agent_id 透传（router 合成单键 overlay）与 task_birth 出生键会被
    /// 静默拦截，执行身份通道断裂（回归锚 2026-09-25）。
    #[test]
    fn agent_dot_id_is_neither_reserved_nor_volatile() {
        assert!(
            !crate::kernel_capabilities::RESERVED_STATE_KEYS.contains(&"agent.id"),
            "agent.id 是身份持久键，不得列为 overlay 保留字"
        );
        assert!(
            !agentos_engine::VOLATILE_RUN_KEYS.contains(&"agent.id"),
            "agent.id 跨轮沿用，不得列为 per-run 挥发键"
        );
    }
}
