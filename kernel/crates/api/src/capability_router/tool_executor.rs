//! tool-executor 域处理器——tool_core 反向委托内核执行工具插件 sidecar
//! （sidecar 进程内没有 ToolRegistry）。从 capability_router.rs 按职责平移
//! （file-length 冻结件同刀拆，loader.rs 三分先例）。
//!
//! 治理三段：会话身份告警 → 内部元数据剥离 → 目标插件解析（fail-closed，
//! 含 BUG-37 调用路径注册表自愈）后经 invoker 执行；连续失败计入告警器。

use agentos_mcp::McpError;
use serde_json::{json, Value};
use tracing::warn;

use super::KernelCapabilityRouter;

impl KernelCapabilityRouter {
    /// tool-executor.invoke：tool_core 反向委托内核执行工具插件 sidecar。
    pub(super) async fn handle_tool_executor_invoke(
        &self,
        params: Value,
    ) -> Result<Value, McpError> {
        let tool_name = params
            .get("tool_name")
            .and_then(|v| v.as_str())
            .ok_or_else(|| McpError::Protocol {
                message: "tool-executor.invoke 缺少 tool_name 参数".to_string(),
            })?;
        let tool_args_raw = params.get("args").cloned().unwrap_or(json!({}));
        // 越权防护（治理）：0.2 工具调用应携带会话身份。会话身份由 tool_args_inject
        // 插件从 pipeline state 注入 session_id 到 args（state.session_id 来自
        // server.rs 构造的 initial_state），所有走 LLM 工具调用链的工具都带得上。
        // 缺失时告警不阻断——bash 等有状态工具插件侧用 _owner/session_id fallback
        // 链（bash/tool.py::_owner_from_inputs）做 pid 级越权兜底；此告警用于发现
        // 绕过 tool_args_inject 的调用方（如 hindsight 经 memory_read 直接调用）。
        let has_owner = tool_args_raw
            .get("_owner")
            .and_then(|v| v.as_str())
            .is_some_and(|s| !s.is_empty())
            || tool_args_raw
                .get("session_id")
                .and_then(|v| v.as_str())
                .is_some_and(|s| !s.is_empty());
        if !has_owner {
            warn!(
                "tool-executor.invoke 缺少会话身份（_owner/session_id）| tool={} | args_keys={:?}",
                tool_name,
                tool_args_raw
                    .as_object()
                    .map(|m| m.keys().cloned().collect::<Vec<_>>()),
            );
        }
        // 剥离纯内部元数据字段：_owner 是治理身份注入、_log_ctx 是日志上下文
        // （SDK 在 _handle_tools_call 也会 pop _log_ctx）、tenant_id 是内核多租户
        // 上下文（经 task_local 传递，不作为工具参数）。这些仅供内核/SDK 使用，
        // 不应透传给工具 handler。
        //
        // 注意：session_id / pipeline_id / task_id 必须保留——它们是工具在
        // injected_params 中显式声明的参数，由 tool_args_inject 插件从 pipeline
        // state 注入到 args；task/trigger 系工具（task_manage / trigger_setup /
        // trigger_review 等）依赖它们做权限校验与会话/管道/任务绑定。剥离它们会导致
        // sidecar 收到空值，报 MISSING_PIPELINE_ID / missing task_id 等。
        // 纯函数工具（file_read 等）不受影响：SDK 的 _filter_handler_kwargs
        // (agentos_plugin_sdk/server.py:54) 按 handler 签名过滤参数——无 **kwargs
        // 的工具自动丢弃这些字段，不会因 unexpected keyword argument 崩溃。
        let internal_keys = [
            "_owner",
            "_log_ctx",
            "tenant_id",
            "_call_context",
            "plugin_id",
        ];
        let mut tool_args = tool_args_raw;
        if let Some(obj) = tool_args.as_object_mut() {
            for k in internal_keys {
                obj.remove(k);
            }
        }
        // task_observability 任务 2：tool_core 在 params 级携带 _call_context
        // （前端路由键 call_id/pipeline_id/message_id/thread_id），合入 tool args
        // 透传给工具 sidecar——bash 等长任务工具据此经 frontend.emit 推
        // tool_progress 执行中进度。args 级的同名字段已在上方剥离（防伪造）。
        // 无 **kwargs 的纯函数工具由 SDK _filter_handler_kwargs 静默丢弃，无影响。
        if let Some(call_ctx) = params.get("_call_context") {
            if !call_ctx.is_null() {
                if let Some(obj) = tool_args.as_object_mut() {
                    obj.insert("_call_context".to_string(), call_ctx.clone());
                }
            }
        }
        // 解析目标插件：调用方可显式传 plugin_id（系统插件工具如 hindsight.recall
        // 不在 CapabilityRegistry——ADR 附录D① 只注册 tool 类插件工具给 LLM，反查
        // 必然失败）；缺省时从注册表反查 tool_name → plugin_id（tool_core 走 LLM
        // 工具链的既有路径）。反查失败 = 工具未注册，fail-closed 直接报错（不得用
        // "工具名当插件 ID"兜底——隐式契约"工具名==插件名才碰巧能用"会掩盖配置
        // 错误，报出误导性的 plugin not found）。
        let explicit_plugin_id = params
            .get("plugin_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty());
        // F-min 属地路由（设计文档 §53）：调用方可传 workspace（任务工作空间
        // 原样路径），同名多实例按属地解析目标插件（工作空间落在本机宿主
        // 领地内 → 该工具走宿主实例；否则云原生主实例）。未传/单实例 = 主
        // 实例直查，既有行为逐字节一致。
        let workspace = params.get("workspace").and_then(|v| v.as_str());
        let plugin_id = match explicit_plugin_id {
            Some(pid) => pid.to_string(),
            None => match self
                .registry
                .as_ref()
                .and_then(|r| r.resolve_tool_instance(tool_name, workspace))
            {
                Some(td) => td.plugin_id.clone(),
                None => {
                    // BUG-37 调用路径自愈（有界）：注册表条目可能因 G2 启动期
                    // 观测误剔等注册面丢失而缺席，且 watcher 对无文件变更的插件
                    // 不重扫——反查 miss 时先试一次"manifest 在册 → 重注册"，
                    // 命中则按重注册结果路由；仍未命中保持 fail-closed（禁用/
                    // 已删插件的既有拒绝语义不变）。
                    let healed = match self.tool_registry_heal.as_ref() {
                        Some(heal) => heal(tool_name).await,
                        None => None,
                    };
                    match healed {
                        Some(td) => {
                            tracing::info!(
                                target: "tool-executor",
                                tool = %tool_name,
                                plugin = %td.plugin_id,
                                "工具注册表反查 miss 经调用路径自愈恢复（重注册后路由）"
                            );
                            td.plugin_id.clone()
                        }
                        None => {
                            return Ok(json!({
                                "success": false,
                                "error": format!(
                                    "工具 {} 未注册（不在插件工具注册表；可能被 G2 注册闸净化或插件未启用；非注册表工具请显式传 plugin_id）",
                                    tool_name
                                ),
                            }));
                        }
                    }
                }
            },
        };
        // G2 注册门禁（ADR 2026-11-12）：校验态只拦截调用，不遮挡工具面——
        // 插件级（verify_incomplete/verify_failed）临时不可用，如实说明插件
        // 可能仍可用；工具级（sanitized/drift 且该工具被拒）声明与实现不一致。
        // 两态均不得到达 sidecar，复验通过（账本 ok）自动放行。
        if let Some(lookup) = self.shadowed_plugin_ids_lookup.as_ref() {
            if lookup().iter().any(|id| id == &plugin_id) {
                warn!(
                    target: "tool-executor",
                    plugin = %plugin_id,
                    tool = %tool_name,
                    error_code = super::ERR_TOOL_TEMPORARILY_UNAVAILABLE,
                    "tool-executor.invoke 被拦截：插件校验未完成或校验失败（G2 注册门禁，临时不可用）"
                );
                return Ok(json!({
                    "success": false,
                    "error_code": super::ERR_TOOL_TEMPORARILY_UNAVAILABLE,
                    "error": format!(
                        "{}: 工具 {tool_name} 所属插件 {plugin_id} 临时不可用——该插件的 G2 校验未完成\
                         （观测通道故障，插件本身可能仍可用）或已达校验失败终态。本次调用被临时拦截，\
                         未进入插件进程；修复插件后执行 POST /api/v1/plugins/validate-all（或禁用→启用）\
                         触发复验，通过后自动恢复。",
                        super::ERR_TOOL_TEMPORARILY_UNAVAILABLE
                    ),
                }));
            }
        }
        if let Some(lookup) = self.rejected_tool_names_lookup.as_ref() {
            if lookup()
                .iter()
                .any(|(pid, rejected_tool)| pid == &plugin_id && rejected_tool == tool_name)
            {
                warn!(
                    target: "tool-executor",
                    plugin = %plugin_id,
                    tool = %tool_name,
                    error_code = super::ERR_TOOL_DECLARATION_MISMATCH,
                    "tool-executor.invoke 被拦截：工具声明与实现不一致（G2 注册门禁，声明 schema 保留在面）"
                );
                return Ok(json!({
                    "success": false,
                    "error_code": super::ERR_TOOL_DECLARATION_MISMATCH,
                    "error": format!(
                        "{}: 工具 {tool_name}（插件 {plugin_id}）的声明与实现不一致\
                         （G2 注册闸判定：实现缺失或 schema 漂移）。该工具的声明 schema 仍保留在工具面，\
                         但调用被拒绝、未进入插件进程；修复 plugin.json 声明或插件实现后执行\
                         POST /api/v1/plugins/validate-all（或禁用→启用）复验，通过后自动恢复。",
                        super::ERR_TOOL_DECLARATION_MISMATCH
                    ),
                }));
            }
        }
        let invoker = self.invoker.as_ref().ok_or_else(|| McpError::Protocol {
            message: "tool-executor 未配置 invoker".to_string(),
        })?;
        tracing::info!(
            target: "tool-executor",
            plugin = %plugin_id,
            tool = %tool_name,
            "tool-executor.invoke 经路由进入工具执行"
        );
        match invoker.invoke_tool(&plugin_id, tool_name, &tool_args).await {
            Ok(result) => {
                // 工具连续失败告警：结果 success=false（参数校验失败/执行错误，
                // 非网络 Err）计入同工具连续失败计数，达到阈值即告警——防止「同一
                // 工具 100% 失败」在流水日志里被淹没。
                if !result.success {
                    if let Some(tracker) = &self.tool_failure_tracker {
                        let sample = result
                            .error
                            .clone()
                            .unwrap_or_else(|| result.data.to_string());
                        if let Some(alert) = tracker.record(tool_name, false, &sample) {
                            tracing::error!(
                                target: "tool-executor",
                                tool = %alert.tool_name,
                                consecutive = alert.consecutive,
                                since_secs = alert.since_secs,
                                "工具连续失败 {} 次（校验/执行错误，非网络类），请检查工具声明与实现: {}",
                                alert.consecutive,
                                alert.error_sample
                            );
                        }
                    }
                } else if let Some(tracker) = &self.tool_failure_tracker {
                    tracker.record(tool_name, true, "");
                }
                // 序列化失败 = ToolExecutionResult 信封无法编码（契约 bug 级异常）：
                // 以 success=false 占位信封返回（sidecar 侧拿到显式失败而非挂起），
                // 但必须 error 可见——伪造信封不允许静默。
                Ok(serde_json::to_value(result).unwrap_or_else(|e| {
                    tracing::error!(
                        target: "tool-executor",
                        tool = %tool_name,
                        plugin = %plugin_id,
                        error = %e,
                        "工具结果序列化失败，降级 success=false 占位信封返回"
                    );
                    json!({"success": false})
                }))
            }
            Err(e) => Ok(json!({
                "success": false,
                "error": format!("tool execution failed: {}", e.message),
            })),
        }
    }
}
