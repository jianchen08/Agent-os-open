//! `session.*` 能力臂——sidecar 反调创建会话管道（日程承载兜底）。
//!
//! ADR 2026-11-03-pim-trigger-anchor：日程/事项的到点触发器由 pim 域自动
//! 注册并挂载到**创建事项的会话管道**；该会话不可得（已关/从未有会话）时
//! 由本臂兜底新建承载管道——触发器注入器（chat.send_message）要求管道挂
//! 真实会话，孤儿管道在派发面被既有守卫拒绝。
//!
//! 身份契约（同 `resume_pipeline` 刀3）：反调面身份取执行上下文注入
//! （PluginScopedRouter 桥 scope），无注入 user fail-fast 显式拒绝。
//!
//! [来源: docs/decisions/2026-11-03-pim-trigger-anchor.md]

use serde_json::{json, Value};

use super::KernelCapabilityRouter;
use agentos_mcp::McpError;

impl KernelCapabilityRouter {
    /// `session.create`：按注入身份新建会话 + 主管道，返回派发坐标。
    ///
    /// params：`title`（可选，承载管道命名建议）/ `agent_id`（可选，默认
    /// agentos）/ `metadata`（可选）。返回 `{"thread_id", "pipeline_ids",
    /// "active_pipeline_id"}`（PimTriggerSync 只消费管道坐标）。
    pub(super) async fn handle_session_create(&self, params: &Value) -> Result<Value, McpError> {
        // 反调面身份（刀3 同款）：执行上下文注入 user 必须在场——承载管道的
        // 会话归属/租户落库以此为准，空身份显式拒绝（与分发层「已删用户」
        // 文案区分）。
        let injected = agentos_tenant::current()
            .filter(|c| c.user_id.as_deref().is_some_and(|u| !u.is_empty()));
        let Some(ctx) = injected else {
            return Err(McpError::Protocol {
                message: "session.create 拒绝：调用链未携带用户身份（执行上下文无注入 \
                          user；承载管道归属必须可判）"
                    .to_string(),
            });
        };
        let user_id = ctx.user_id.clone().unwrap_or_default();
        let tenant_id = ctx.tenant_id;

        let outcome = crate::session_routes::create_session_core(
            self.store.as_ref(),
            self.session.as_ref(),
            &tenant_id,
            &user_id,
            params,
            self.domain_broadcaster.as_ref(),
        )
        .await
        .map_err(|message| McpError::Protocol { message })?;

        Ok(json!({
            "thread_id": outcome.thread_id,
            "pipeline_ids": [outcome.pipeline_id],
            "active_pipeline_id": outcome.pipeline_id,
        }))
    }
}
