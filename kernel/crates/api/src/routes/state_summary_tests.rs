//! 声明化出口（ADR 2026-08-28）：内核运行域基线 + manifest `export_fields`
//! 声明并集。插件域字段（task.*/lineage.*/workspace/evaluation.* 等）经写入方
//! 插件声明出口；未声明 = 不出口（默认拒绝），基线键恒出口。

use super::*;

/// 构造带指定 export_fields 声明的测试 manifest。
fn export_manifest(fields: &[&str]) -> PluginManifest {
    PluginManifest {
        force_include_tools: Vec::new(),
        state: None,
        id: "test_plugin".to_string(),
        name: "test_plugin".to_string(),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::System,
        language: "python".to_string(),
        host_type: agentos_core::traits::HostType::Sidecar,
        host_group: None,
        entry: "python server.py".to_string(),
        capabilities: agentos_core::traits::ManifestCapabilities::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 100,
        mcp: None,
        lifecycle: None,
        native: None,
        restricted_capabilities: vec![],
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        persistent_fields: vec![],
        export_fields: fields.iter().map(|s| s.to_string()).collect(),
        aux_venvs: Vec::new(),
        provides: None,
    }
}

#[test]
fn test_summarize_exports_declared_task_and_lineage_fields() {
    // 任务域/血缘键由写入方插件声明后出口（原内核白名单语义迁入声明）
    let export = ExportFields::from_manifests(&[export_manifest(&[
        "task.goal",
        "task.status",
        "task.id",
        "lineage.parent_pipeline_id",
        "lineage.origin_session_id",
        "lineage.root",
    ])]);
    let state = json!({
        "pipeline_id": "p1",
        "task.goal": "喝水提醒",
        "task.status": "running",
        "task.id": "t1",
        "lineage.parent_pipeline_id": "pipe_parent",
        "lineage.origin_session_id": "sess_root",
        "lineage.root": true,
        "messages": [{"role": "user"}, {"role": "assistant"}],
    });
    let s = summarize_state(&state, &export);
    assert_eq!(s["pipeline_id"], "p1", "基线键恒出口");
    assert_eq!(s["task.goal"], "喝水提醒");
    assert_eq!(s["task.status"], "running");
    assert_eq!(s["task.id"], "t1");
    assert_eq!(s["lineage.parent_pipeline_id"], "pipe_parent");
    assert_eq!(s["lineage.origin_session_id"], "sess_root");
    assert_eq!(s["lineage.root"], true);
    // messages 仍只出口条数（大字段不出口的既有契约不变）
    assert!(s.get("messages").is_none());
    assert_eq!(s["message_count"], 2);
}

#[test]
fn test_summarize_default_denies_undeclared_plugin_fields() {
    // 默认拒绝性质：无任何声明（ExportFields::default）时插件域字段一律裁掉，
    // 基线键不受影响——插件声明是出口的唯一扩权通道。
    let export = ExportFields::default();
    let s = summarize_state(
        &json!({
            "pipeline_id": "p2",
            "ended": true,
            "task.goal": "g",
            "lineage.root": true,
            "workspace": "D:/ws",
        }),
        &export,
    );
    assert_eq!(s["pipeline_id"], "p2");
    assert_eq!(s["ended"], true);
    for k in [
        "task.goal",
        "lineage.root",
        "workspace",
        "task.owned.x.title",
    ] {
        assert!(s.get(k).is_none(), "未声明键 {k} 不应出口");
    }
}

#[test]
fn test_summarize_exports_task_owned_prefix_and_workspace() {
    // task.owned.<id>.<field>（提交者管道自持的任务登记，键含动态管道 id）
    // 经 `task.owned.*` 前缀声明整段出口；workspace/ws_meta 精确键声明出口。
    let export = ExportFields::from_manifests(&[export_manifest(&[
        "task.owned.*",
        "task.submitted_by",
        "workspace",
        "ws_meta",
    ])]);
    let state = json!({
        "pipeline_id": "p3",
        "task.owned.ae7b430f.title": "AI行业近月发展调研",
        "task.owned.ae7b430f.status": "running",
        "task.submitted_by": "u1",
        "workspace": "D:/ws/copy_1",
        "ws_meta": {"path": "D:/ws/copy_1", "mode": "worktree"},
        "messages": [{"role": "user"}],
    });
    let s = summarize_state(&state, &export);
    assert_eq!(s["task.owned.ae7b430f.title"], "AI行业近月发展调研");
    assert_eq!(s["task.owned.ae7b430f.status"], "running");
    assert_eq!(s["task.submitted_by"], "u1");
    assert_eq!(s["workspace"], "D:/ws/copy_1");
    assert_eq!(s["ws_meta"]["mode"], "worktree");
    // 杂键仍裁掉（默认拒绝语义不变，防越权大字段出口）
    assert!(s.get("secret_field").is_none());
}

#[test]
fn test_summarize_omits_absent_declared_fields() {
    // 反向性质：无任务域字段的普通会话管道，摘要不得伪造 task.*/lineage.* 键
    let export = ExportFields::from_manifests(&[export_manifest(&[
        "task.goal",
        "task.status",
        "task.id",
        "lineage.parent_pipeline_id",
        "lineage.origin_session_id",
        "lineage.root",
    ])]);
    let s = summarize_state(&json!({"pipeline_id": "p2", "ended": true}), &export);
    assert_eq!(s["pipeline_id"], "p2");
    assert_eq!(s["ended"], true);
    for k in [
        "task.goal",
        "task.status",
        "task.id",
        "lineage.parent_pipeline_id",
        "lineage.origin_session_id",
        "lineage.root",
    ] {
        assert!(s.get(k).is_none(), "{k} 不应被伪造");
    }
}

#[test]
fn test_summarize_exports_evaluation_fields() {
    // 评估域键声明出口——pipeline-state.list 同数据源，评估执行器据此轮询
    // 回收评估结论（ADR 2026-08-24-eval-pipeline-state-keys 语义由声明承载）。
    let export = ExportFields::from_manifests(&[export_manifest(&[
        "evaluation.of_task",
        "evaluation.metric_id",
        "evaluation.detected_result",
    ])]);
    let state = json!({
        "pipeline_id": "evalPipe",
        "evaluation.of_task": "taskPipe1",
        "evaluation.metric_id": "semantic_check",
        "evaluation.detected_result": {"passed": true, "score": 88, "feedback": "结构完整"},
        "secret_field": "must-not-leak",
    });
    let s = summarize_state(&state, &export);
    assert_eq!(s["evaluation.of_task"], "taskPipe1");
    assert_eq!(s["evaluation.metric_id"], "semantic_check");
    assert_eq!(s["evaluation.detected_result"]["passed"], true);
    assert_eq!(s["evaluation.detected_result"]["score"], 88);
    assert!(s.get("secret_field").is_none());
}

#[test]
fn test_summarize_union_across_manifests() {
    // 并集语义：多个 manifest 各自声明，取并集出口。
    let export = ExportFields::from_manifests(&[
        export_manifest(&["task.goal"]),
        export_manifest(&["llm_model"]),
    ]);
    let s = summarize_state(
        &json!({"pipeline_id": "p7", "task.goal": "g", "llm_model": "k2"}),
        &export,
    );
    assert_eq!(s["task.goal"], "g");
    assert_eq!(s["llm_model"], "k2");
}

#[test]
fn test_summarize_cuts_retired_task_submit_params() {
    // 本守卫拒绝的历史参数名清单（task.priority/task.max_retries，
    // ADR 2026-08-24-task-submit-param-diet：执行层零消费者已移除）——
    // 收到即不出口，即使任务域其他键已声明。
    let export = ExportFields::from_manifests(&[export_manifest(&["task.goal"])]);
    let s = summarize_state(
        &json!({
            "pipeline_id": "p6",
            "task.priority": "high",
            "task.max_retries": 2,
            "task.goal": "g",
        }),
        &export,
    );
    assert!(s.get("task.priority").is_none(), "退役参数不应出口");
    assert!(s.get("task.max_retries").is_none(), "退役参数不应出口");
    assert_eq!(s["task.goal"], "g");
}

#[test]
fn test_summarize_still_cuts_non_whitelisted_fields() {
    // 默认拒绝机制本身不变：声明之外的新键不出口
    let export = ExportFields::from_manifests(&[export_manifest(&["task.goal", "task.ended_at"])]);
    let s = summarize_state(
        &json!({
            "pipeline_id": "p3",
            "secret_blob": "x",
            "task.goal": "g"
        }),
        &export,
    );
    assert!(s.get("secret_blob").is_none(), "未声明字段仍裁剪");
    assert_eq!(s["task.goal"], "g");
    // raw_result（最终输出）属内核运行域基线，恒出口——复盘报告提取/任务树展示依赖
    let s2 = summarize_state(
        &json!({"pipeline_id": "p4", "raw_result": "复盘结论：x"}),
        &ExportFields::default(),
    );
    assert_eq!(s2["raw_result"], "复盘结论：x");
    // 终态回写的 task.ended_at 经声明出口（任务树展示完成时间）
    let s3 = summarize_state(
        &json!({"pipeline_id": "p5", "task.ended_at": "2026-08-16T09:00:00Z"}),
        &export,
    );
    assert_eq!(s3["task.ended_at"], "2026-08-16T09:00:00Z");
}

#[test]
fn test_summarize_exports_run_status_baseline() {
    // 实际状态内核持有（2026-09-03 双状态裁定）：可观测性唯一运行状态真值，
    // 属内核运行域基线键，无需插件 export_fields 声明即出口。
    let s = summarize_state(
        &json!({"pipeline_id": "p8", "run_status": "running", "secret": "x"}),
        &ExportFields::default(),
    );
    assert_eq!(s["run_status"], "running");
    assert!(s.get("secret").is_none(), "基线出口不放大未声明键");
}

#[test]
fn test_summarize_exports_run_stop_reason_baseline() {
    // 失败归因唯一真值载体随基线出口（2026-10-25 空回复耗尽排查实证：落表
    // 已写而出口缺席，面板只见 failed 无归因）。署名轮原样透传；未署名轮
    // 空串照出口（前端按非空渲染）；未声明插件键仍裁剪。
    let s = summarize_state(
        &json!({
            "pipeline_id": "p9",
            "run_status": "failed",
            "run_stop_reason": "empty_reply_exhausted",
            "secret": "x"
        }),
        &ExportFields::default(),
    );
    assert_eq!(s["run_stop_reason"], "empty_reply_exhausted");
    assert!(s.get("secret").is_none(), "基线出口不放大未声明键");

    let unsigned = summarize_state(
        &json!({"pipeline_id": "p10", "run_status": "cancelled", "run_stop_reason": ""}),
        &ExportFields::default(),
    );
    assert_eq!(unsigned["run_stop_reason"], "");
}

#[test]
fn test_summarize_exports_suspended_reason_baseline() {
    // 挂起归因唯一真值载体随基线出口（P-23 片1：写者是内核 drain 排空，
    // 无 manifest 可声明 export_fields——基线路径是唯一出口，不出口
    // 则面板只见 suspended 无归因）。
    let s = summarize_state(
        &json!({
            "pipeline_id": "p11",
            "run_status": "suspended",
            "suspended_reason": "memory watermark self-heal: private bytes 持续超阈值",
            "secret": "x"
        }),
        &ExportFields::default(),
    );
    assert_eq!(
        s["suspended_reason"], "memory watermark self-heal: private bytes 持续超阈值",
        "挂起归因随基线出口"
    );
    assert!(s.get("secret").is_none(), "基线出口不放大未声明键");

    // 非 drain 挂起行（DSL 等待/审批挂起）不写该键：键缺席不合成归因
    // 字段（面板归因行缺席回退现状）
    let no_reason = summarize_state(
        &json!({"pipeline_id": "p12", "run_status": "suspended"}),
        &ExportFields::default(),
    );
    assert!(
        no_reason.get("suspended_reason").is_none(),
        "键缺席不合成归因字段"
    );
}
