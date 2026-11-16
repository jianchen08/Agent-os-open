//! service-registry 域处理器——基础设施下沉内核后插件访问内核共享基础设施
//! 存储（execution-records/pipeline-summaries/memory 三域 → 现 messages/traces/
//! pipeline-runs 读面，对应 M1 内核存储层）。从 capability_router.rs 按职责
//! 平移（file-length 冻结件同刀拆，loader.rs 三分先例）。
//!
//! method 形如 `<域>.<op>`（如 execution-records.list）；store 未注入时统一
//! 返回 ERR_SERVICE_REGISTRY 前缀错误。

use agentos_core::traits::MessageQueryOpts;
use agentos_mcp::McpError;
use serde_json::Value;

use super::{KernelCapabilityRouter, ERR_SERVICE_REGISTRY};

/// service-registry 错误（带固定错误码前缀）。
fn srv_err(msg: String) -> McpError {
    McpError::Protocol {
        message: format!("[{ERR_SERVICE_REGISTRY}] service-registry: {msg}"),
    }
}

/// 从 params 对象构造 MessageQueryOpts（before_sequence/after_sequence/limit）。
fn parse_message_query_opts(params: &Value) -> MessageQueryOpts {
    MessageQueryOpts {
        before_sequence: params
            .get("before_sequence")
            .and_then(|v| v.as_u64())
            .map(|s| s as u32),
        after_sequence: params
            .get("after_sequence")
            .and_then(|v| v.as_u64())
            .map(|s| s as u32),
        limit: params
            .get("limit")
            .and_then(|v| v.as_u64())
            .map(|l| l as usize),
    }
}

impl KernelCapabilityRouter {
    /// 处理 service-registry.<域>.<op> 反向调用。
    ///
    /// method 形如 `execution-records.list`，先 split 出 (domain, op) 再分派。
    /// store 未注入时统一返回 ERR_SERVICE_REGISTRY。
    pub(super) async fn handle_service_registry(
        &self,
        method: &str,
        params: Value,
    ) -> Result<Value, McpError> {
        let store = self.store.as_ref().ok_or_else(|| McpError::Protocol {
            message: "service-registry disabled: kernel store not injected".to_string(),
        })?;
        // method = "<domain>.<op>"
        let (domain, op) = method.split_once('.').ok_or_else(|| McpError::Protocol {
            message: format!("invalid service-registry method (expect <domain>.<op>): {method}"),
        })?;
        match (domain, op) {
            // ── messages 域（按 pipeline_id 查对话消息，复盘/压缩块恢复用）──
            ("messages", "list") => {
                let pipeline_id = params
                    .get("pipeline_id")
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| srv_err("missing pipeline_id".into()))?;
                let opts = parse_message_query_opts(&params);
                let rows = store
                    .get_messages_by_pipeline(pipeline_id, opts)
                    .await
                    .map_err(|e| srv_err(format!("messages.list: {e}")))?;
                serde_json::to_value(rows).map_err(|e| srv_err(format!("encode: {e}")))
            }
            // ── traces 域（按 thread_id 查插件 state 变更轨迹，复盘骨架用）──
            ("traces", "list") => {
                let thread_id = params
                    .get("thread_id")
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| srv_err("missing thread_id".into()))?;
                let tenant_id = params
                    .get("tenant_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("default");
                let rows = store
                    .get_step_traces_by_thread(thread_id, tenant_id)
                    .await
                    .map_err(|e| srv_err(format!("traces.list: {e}")))?;
                serde_json::to_value(rows).map_err(|e| srv_err(format!("encode: {e}")))
            }
            // ── traces.list_by_pipeline：按 pipeline_id 直查单管道 step 轨迹 ──
            // pipeline_id 是执行态唯一坐标；绑真会话的任务管道落映射
            // (task→thread-xxx)，按 thread_id=pipeline_id 查恒空（task_manage
            // recent_activities 数据源）——本读面绕开会话映射直查。
            ("traces", "list_by_pipeline") => {
                let pipeline_id = params
                    .get("pipeline_id")
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| srv_err("missing pipeline_id".into()))?;
                let tenant_id = params
                    .get("tenant_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("default");
                let rows = store
                    .get_step_traces_by_pipeline(pipeline_id, tenant_id)
                    .await
                    .map_err(|e| srv_err(format!("traces.list_by_pipeline: {e}")))?;
                serde_json::to_value(rows).map_err(|e| srv_err(format!("encode: {e}")))
            }
            // ── pipeline-runs 域（runs 快照列表，调试中心会话/执行记录数据源）──
            // 与 GET /api/v1/pipelines/runs 同查询（list_pipelines_inner 四表联结），
            // 按 started_at 倒序；插件侧（channel_api）以此为「有执行记录的会话」清单。
            ("pipeline-runs", "list") => {
                let tenant_id = params
                    .get("tenant_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("default");
                let status = params
                    .get("status")
                    .and_then(|v| v.as_str())
                    .filter(|s| !s.is_empty());
                let limit = params
                    .get("limit")
                    .and_then(|v| v.as_u64())
                    .unwrap_or(100)
                    .min(500) as u32;
                let rows = store
                    .list_pipelines(tenant_id, status, limit)
                    .await
                    .map_err(|e| srv_err(format!("pipeline-runs.list: {e}")))?;
                serde_json::to_value(rows).map_err(|e| srv_err(format!("encode: {e}")))
            }
            // ── pipeline-runs.list_by_pipeline：按 pipeline_id 列该管道全部 run ──
            // 任务耗时起点数据源（task_manage elapsed_seconds：首 run created_at）。
            ("pipeline-runs", "list_by_pipeline") => {
                let pipeline_id = params
                    .get("pipeline_id")
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| srv_err("missing pipeline_id".into()))?;
                let tenant_id = params
                    .get("tenant_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("default");
                let rows = store
                    .list_runs_by_pipeline(pipeline_id, tenant_id)
                    .await
                    .map_err(|e| srv_err(format!("pipeline-runs.list_by_pipeline: {e}")))?;
                serde_json::to_value(rows).map_err(|e| srv_err(format!("encode: {e}")))
            }
            (domain, op) => Err(McpError::Protocol {
                message: format!("service-registry method not implemented: {domain}.{op}"),
            }),
        }
    }
}
