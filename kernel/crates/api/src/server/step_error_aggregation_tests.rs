//! 步骤失败聚合进 run 终态署名（2026-10-15 P0 哨兵 failed:false 逃逸修复）。
//!
//! 契约（ADR 2026-10-15-step-error-run-signature-aggregation）：插件步失败走
//! 引擎 warn+继续（ADR 2026-08-18），错误收集在 `_plugin_errors`——未署名
//! 完成轮的出口不得再以成功姿态外发：
//! - 零产出（`raw_result` 空，与响应提取同锚）→ failed=true，文案携带真实
//!   错误原文（P0 实锤：llm_core MCP 失败零输出仍报 pipeline finished
//!   failed:false，调用方零感知——五包后LLM全断调查 §F4）；
//! - 有产出（后续步自救）→ failed=false + degraded=true（有痕完成）；
//! - 干净完成（无步失败）行为不变——零产出空完成是合法形态（纯工具轮）；
//! - 控制状态署名优先：看门狗 timeout 等词表署名不被聚合遮蔽；挂起/取消
//!   轮不折叠（挂起待恢复、取消是用户意图终态）。

use super::*;

/// 测试夹具：跑一次 stage_finalize 并清理 registry 常驻快照。
async fn finalize_and_clean(
    final_state: serde_json::Value,
    tenant: &str,
    pid: &str,
) -> EngineOutcome {
    let reg = agentos_session::global_registry();
    let outcome = stage_finalize(
        &final_state,
        None,
        tenant,
        pid,
        "thread_stepagg",
        "agentos",
        "run_stepagg",
    )
    .await;
    reg.remove(tenant, pid);
    outcome
}

/// (① P0 复现) 步骤 MCP 失败 + 零产出 → run 终态署名必须含失败语义。
/// 修前 failed:false + "pipeline finished" 假成功外发 = 红的根源。
#[tokio::test]
async fn step_failure_zero_output_must_not_report_success() {
    for (pid, errors) in [
        (
            "pipe_stepagg_zero_a",
            serde_json::json!([
                {
                    "plugin_id": "pipeline_llm_core",
                    "code": "PLUGIN_EXEC_FAILED",
                    "message": "MCP call failed: llm.complete_stream 工具执行失败: \
                                No module named 'litellm'",
                },
            ]),
        ),
        (
            "pipe_stepagg_zero_b",
            // invoker Err 形态（无 code，sidecar 不可达原文）
            serde_json::json!([
                {
                    "plugin_id": "resource_search_tool",
                    "message": "sidecar unreachable: transport closed",
                },
            ]),
        ),
    ] {
        let tenant = format!("tenant_stepagg_{pid}");
        let final_state = serde_json::json!({
            "pipeline_id": pid,
            "ended": true,
            "run_status": "completed",
            "_plugin_errors": errors,
        });
        let outcome = finalize_and_clean(final_state, &tenant, pid).await;

        assert!(
            outcome.failed,
            "步失败+零产出轮出口必须 failed=true（{pid}）: {outcome:?}"
        );
        assert!(
            !outcome.degraded,
            "失败轮不得同时标降级（degraded 只属有痕完成）: {outcome:?}"
        );
        assert_ne!(
            outcome.content, "pipeline finished",
            "零产出假成功兜底文案不得外发（{pid}）"
        );
    }
    // 文案性质断言：携带首条错误原文（真实归因），不做固定归因。
    let tenant = "tenant_stepagg_zero_c";
    let final_state = serde_json::json!({
        "pipeline_id": "pipe_stepagg_zero_c",
        "ended": true,
        "_plugin_errors": [
            {"plugin_id": "pipeline_llm_core", "message": "   "},
            {"plugin_id": "pipeline_llm_core",
             "message": "MCP protocol error: No module named 'litellm'"},
        ],
    });
    let outcome = finalize_and_clean(final_state, tenant, "pipe_stepagg_zero_c").await;
    assert!(
        outcome.content.contains("No module named 'litellm'"),
        "文案必须携带首条非空错误原文: {}",
        outcome.content
    );
}

/// (② 回归) 干净完成行为不变：有产出轮 content=原文；零产出轮（纯工具轮
/// 合法形态）failed=false 且 content 走 "pipeline finished" 兜底——零产出
/// 本身不是失败，缺步失败证据时不得折叠。
#[tokio::test]
async fn clean_completion_unchanged_including_legal_empty_output() {
    let tenant = "tenant_stepagg_clean";
    let with_output = finalize_and_clean(
        serde_json::json!({
            "pipeline_id": "pipe_stepagg_clean_a",
            "run_status": "completed",
            "raw_result": "正常回复",
            "messages": [{"role": "assistant", "content": "正常回复"}],
        }),
        tenant,
        "pipe_stepagg_clean_a",
    )
    .await;
    assert!(!with_output.failed);
    assert!(!with_output.degraded, "干净完成不得标降级: {with_output:?}");
    assert_eq!(with_output.content, "正常回复");

    let empty_output = finalize_and_clean(
        serde_json::json!({
            "pipeline_id": "pipe_stepagg_clean_b",
            "ended": true,
            "run_status": "completed",
        }),
        tenant,
        "pipe_stepagg_clean_b",
    )
    .await;
    assert!(
        !empty_output.failed,
        "零产出+无步失败=合法空完成（纯工具轮）: {empty_output:?}"
    );
    assert!(!empty_output.degraded);
    assert_eq!(empty_output.content, "pipeline finished");
}

/// (③ 语义锁定) 步失败但后续步自救产出 → completed + degraded=true
/// （ADR 折算语义二选一之「degraded=有痕」）：run 可用、有痕可辨。
#[tokio::test]
async fn self_heal_completion_is_degraded_with_trace() {
    let tenant = "tenant_stepagg_heal";
    for (pid, raw) in [
        ("pipe_stepagg_heal_a", "自救后的回复"),
        ("pipe_stepagg_heal_b", "另一轮自救产出"),
    ] {
        let final_state = serde_json::json!({
            "pipeline_id": pid,
            "ended": true,
            "run_status": "completed",
            "raw_result": raw,
            "_plugin_errors": [
                {"plugin_id": "resource_search_tool", "message": "tool timeout"},
                {"plugin_id": "memory_store", "message": "write rejected"},
            ],
        });
        let outcome = finalize_and_clean(final_state, tenant, pid).await;
        assert!(
            !outcome.failed,
            "自救产出轮不是失败终态（{pid}）: {outcome:?}"
        );
        assert!(
            outcome.degraded,
            "步失败痕必须以 degraded 显式化（{pid}）: {outcome:?}"
        );
        assert_eq!(outcome.content, raw, "自救轮外发真实产出原文（{pid}）");
        assert!(
            !outcome.plugin_errors.is_empty(),
            "错误收集照常外发（WS plugin_error 事件源）"
        );
    }
}

/// (④ 共存不遮蔽) 控制状态署名优先于步失败折叠：
/// - 看门狗 timeout 署名在场 → 走署名失败分支（文案带 stop_reason，非折叠文案）；
/// - 单次超时信封（STEP_CALL_TIMEOUT）+ 有产出 → 步级继续语义不变（戊组
///   completed 面），api 出口=有痕完成；
/// - 挂起/取消轮不折叠（from_control_state 挂起优先、取消是用户意图终态）。
#[tokio::test]
async fn aggregation_does_not_shadow_control_state_signatures() {
    let tenant = "tenant_stepagg_coex";

    // 看门狗署名优先：零产出+步失败同时在场，文案仍走 stop_reason 分支。
    let watchdog = finalize_and_clean(
        serde_json::json!({
            "pipeline_id": "pipe_stepagg_coex_a",
            "ended": true,
            "router.stop_reason": "timeout",
            "_plugin_errors": [
                {"plugin_id": "p_hang", "code": "STEP_CALL_TIMEOUT",
                 "message": "单次插件调用超时"},
            ],
        }),
        tenant,
        "pipe_stepagg_coex_a",
    )
    .await;
    assert!(watchdog.failed);
    assert!(!watchdog.degraded);
    assert!(
        watchdog.content.contains("timeout"),
        "署名失败轮文案走 stop_reason 分支: {}",
        watchdog.content
    );

    // 单次超时信封 + 后续步产出：run 继续推进语义（戊组 b01638298）在出口
    // 折算为有痕完成——信封留在 plugin_errors，不被折叠改写为失败。
    let step_timeout = finalize_and_clean(
        serde_json::json!({
            "pipeline_id": "pipe_stepagg_coex_b",
            "ended": true,
            "run_status": "completed",
            "raw_result": "超时后自救产出",
            "_plugin_errors": [
                {"plugin_id": "p_hang", "code": "STEP_CALL_TIMEOUT",
                 "message": "单次插件调用超时"},
            ],
        }),
        tenant,
        "pipe_stepagg_coex_b",
    )
    .await;
    assert!(
        !step_timeout.failed,
        "步级超时+产出轮非失败: {step_timeout:?}"
    );
    assert!(
        step_timeout.degraded,
        "步级超时痕必须可辨: {step_timeout:?}"
    );

    // 挂起轮不折叠（等待恢复，非终态）。
    let suspended = finalize_and_clean(
        serde_json::json!({
            "pipeline_id": "pipe_stepagg_coex_c",
            "suspended": true,
            "run_status": "suspended",
            "_plugin_errors": [
                {"plugin_id": "p_x", "message": "step failed before suspend"},
            ],
        }),
        tenant,
        "pipe_stepagg_coex_c",
    )
    .await;
    assert!(!suspended.failed, "挂起轮非失败终态: {suspended:?}");

    // 取消轮不折叠（取消署名优先于步失败痕）。
    let cancelled = finalize_and_clean(
        serde_json::json!({
            "pipeline_id": "pipe_stepagg_coex_d",
            "ended": true,
            "router.stop_reason": "user_requested",
            "_plugin_errors": [
                {"plugin_id": "p_x", "message": "step failed before cancel"},
            ],
        }),
        tenant,
        "pipe_stepagg_coex_d",
    )
    .await;
    assert!(!cancelled.failed, "取消轮非失败终态: {cancelled:?}");
    assert!(
        !cancelled.degraded,
        "非完成终态不得标降级（degraded 只属完成轮有痕）: {cancelled:?}"
    );
}
