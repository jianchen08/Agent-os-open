//! pipeline-executor 域处理器——runs 表状态簿记（审批挂起/恢复、复盘轮询、
//! 任务删除）。从 capability_router.rs 按职责平移（file-length 冻结件同刀拆，
//! loader.rs 三分先例）：分发骨架留母文件，域处理器落子模块。
//!
//! 新引擎执行流由 state.suspended 插件机制控制，此处仅同步 runs 表状态供
//! 查询/恢复语义；resume_pipeline 额外经 PipelineResumerFn 拉起续跑轮
//! （任务执行统一走 chat.send_message → PipelineExecutor）。

use serde_json::{json, Value};

use super::KernelCapabilityRouter;
use agentos_mcp::McpError;

impl KernelCapabilityRouter {
    /// pipeline-executor 域：runs 表状态簿记（审批挂起/恢复、复盘轮询、
    /// 任务删除）；新引擎执行流由 state.suspended 插件机制控制，此处仅
    /// 同步 runs 表状态供查询/恢复语义。None = 未命中（落兜底）。
    pub(super) async fn dispatch_pipeline_executor(
        &self,
        method: &str,
        params: &Value,
    ) -> Option<Result<Value, McpError>> {
        match method {
            "suspend" => Some(self.handle_pipeline_executor_suspend(params.clone()).await),
            "resume" => Some(self.handle_pipeline_executor_resume(params.clone()).await),
            // GAP-1 统一：按管道挂起/恢复（task_manage stop/resume 映射）
            "suspend_pipeline" => Some(
                self.handle_pipeline_executor_suspend_pipeline(params.clone())
                    .await,
            ),
            "resume_pipeline" => Some(
                self.handle_pipeline_executor_resume_pipeline(params.clone())
                    .await,
            ),
            "get_run_status" => Some(
                self.handle_pipeline_executor_get_run_status(params.clone())
                    .await,
            ),
            "wait_run_stop" => Some(
                self.handle_pipeline_executor_wait_run_stop(params.clone())
                    .await,
            ),
            "is_pipeline_viewed" => Some(
                self.handle_pipeline_executor_is_pipeline_viewed(params.clone())
                    .await,
            ),
            "delete_pipeline" => Some(
                self.handle_pipeline_executor_delete_pipeline(params.clone())
                    .await,
            ),
            _ => None,
        }
    }

    /// pipeline-executor.suspend：按 run_id 挂起。已挂起直接返回当前句柄（幂等）。
    async fn handle_pipeline_executor_suspend(&self, params: Value) -> Result<Value, McpError> {
        let run_id = params
            .get("run_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| McpError::Protocol {
                message: "suspend 缺少 run_id 参数".to_string(),
            })?;
        let store = self.store.as_ref().ok_or_else(|| McpError::Protocol {
            message: "suspend disabled: kernel store not injected".to_string(),
        })?;
        let run = store
            .get_run(run_id)
            .await
            .map_err(|e| McpError::Protocol {
                message: format!("suspend 失败: {e}"),
            })?;
        // 已终态（completed/failed）的 run 不再挂起，直接返回当前句柄（幂等）
        if run.status == agentos_core::types::RunStatus::Suspended {
            return Ok(json!({
                "status": "suspended",
                "run_id": run.run_id,
                "branch_id": run.current_branch,
                "seq": run.current_seq,
            }));
        }
        // runs 表退役（ADR 2026-09-18）：簿记 = state 运行键回写。
        let Some(pipeline_id) = run.pipeline_id.clone() else {
            return Err(McpError::Protocol {
                message: format!("suspend 失败: run 无管道归属（run_id={run_id}）"),
            });
        };
        store
            .upsert_state_fields(
                &pipeline_id,
                &run.tenant_id,
                &json!({ "run_status": "suspended" })
                    .as_object()
                    .unwrap()
                    .clone(),
            )
            .await
            .map_err(|e| McpError::Protocol {
                message: format!("suspend 失败: {e}"),
            })?;
        // 落标即唤醒 wait_run_stop 等待者（事件驱动取消，与 dispatch_stop 同链）。
        crate::run_signal::RunSignalHub::global().signal(run_id);
        // 挂起凭据落库（审批挂起恢复链写侧）：approval 插件 suspend 时携带
        // 交互 request_id（形参名 approval_id 兼容），落 state 标量键
        // `suspend_request_id` 供 interaction_response 按 request_id 反查唤醒
        // （find_suspended_run_by_request_id）。失败降级 warn 不回滚挂起。
        let request_id = params
            .get("request_id")
            .and_then(|v| v.as_str())
            .or_else(|| params.get("approval_id").and_then(|v| v.as_str()))
            .filter(|s| !s.is_empty());
        if let (Some(db), Some(req_id)) = (self.sqlite.as_ref(), request_id) {
            let mut fields = serde_json::Map::new();
            fields.insert("suspend_request_id".to_string(), json!(req_id));
            if let Err(e) = db.upsert_state_fields(&pipeline_id, &run.tenant_id, &fields) {
                tracing::warn!(
                    run_id = %run_id,
                    request_id = %req_id,
                    error = %e,
                    "suspend 落挂起凭据失败（interaction_response 将反查不到此 run）"
                );
            }
        }
        // 返回完整 handle，sidecar resume 时需回传全部字段
        Ok(json!({
            "status": "suspended",
            "run_id": run.run_id,
            "branch_id": run.current_branch,
            "seq": run.current_seq,
        }))
    }

    /// pipeline-executor.resume：按 run_id 恢复为 Running。
    async fn handle_pipeline_executor_resume(&self, params: Value) -> Result<Value, McpError> {
        // resume 凭据 = suspend 返回的完整 handle JSON（run_id + branch_id + seq）。
        // sidecar 在 suspend 时拿到 handle，resume 时回传完整字段。
        let run_id = params
            .get("run_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| McpError::Protocol {
                message: "resume 缺少 run_id 参数".to_string(),
            })?;
        let store = self.store.as_ref().ok_or_else(|| McpError::Protocol {
            message: "resume disabled: kernel store not injected".to_string(),
        })?;
        let run = store
            .get_run(run_id)
            .await
            .map_err(|e| McpError::Protocol {
                message: format!("resume 失败: {e}"),
            })?;
        let Some(pipeline_id) = run.pipeline_id.clone() else {
            return Err(McpError::Protocol {
                message: format!("resume 失败: run 无管道归属（run_id={run_id}）"),
            });
        };
        store
            .upsert_state_fields(
                &pipeline_id,
                &run.tenant_id,
                &json!({ "run_status": "running" })
                    .as_object()
                    .unwrap()
                    .clone(),
            )
            .await
            .map_err(|e| McpError::Protocol {
                message: format!("resume 失败: {e}"),
            })?;
        // 恢复即清挂起凭据：陈旧 suspend_request_id 留在 state 会让后续按凭据
        // 的反查误判该 run 仍在等审批。
        // 同批清挂起归因（P-23 片1）：陈旧 suspended_reason 残留会让恢复
        // 后的行被误读为仍处该次排空挂起。
        if let Some(db) = self.sqlite.as_ref() {
            let mut fields = serde_json::Map::new();
            fields.insert("suspend_request_id".to_string(), Value::Null);
            fields.insert("suspended_reason".to_string(), Value::Null);
            if let Err(e) = db.upsert_state_fields(&pipeline_id, &run.tenant_id, &fields) {
                tracing::warn!(
                    run_id = %run_id,
                    error = %e,
                    "resume 清挂起凭据/归因失败（凭据或归因残留）"
                );
            }
        }
        Ok(json!({"status": "resumed", "run_id": run_id}))
    }

    /// pipeline-executor.suspend_pipeline：挂起该管道最新非终态 run
    /// （task_manage stop 映射；GAP-1 统一）。幂等：无匹配 run 返回 ok。
    async fn handle_pipeline_executor_suspend_pipeline(
        &self,
        params: Value,
    ) -> Result<Value, McpError> {
        let pipeline_id = params
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| McpError::Protocol {
                message: "suspend_pipeline 缺少 pipeline_id 参数".to_string(),
            })?;
        let store = self.store.as_ref().ok_or_else(|| McpError::Protocol {
            message: "suspend_pipeline disabled: kernel store not injected".to_string(),
        })?;
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let runs = store
            .list_runs_by_pipeline(pipeline_id, &tenant_id)
            .await
            .map_err(|e| McpError::Protocol {
                message: format!("suspend_pipeline 查询失败: {e}"),
            })?;
        let target = runs.into_iter().find(|r| {
            r.status == agentos_core::types::RunStatus::Running
                || r.status == agentos_core::types::RunStatus::Suspended
        });
        match target {
            Some(run) if run.status == agentos_core::types::RunStatus::Suspended => {
                Ok(json!({"status": "suspended", "pipeline_id": pipeline_id, "run_id": run.run_id}))
            }
            Some(run) => {
                store
                    .upsert_state_fields(
                        pipeline_id,
                        &run.tenant_id,
                        &json!({ "run_status": "suspended" })
                            .as_object()
                            .unwrap()
                            .clone(),
                    )
                    .await
                    .map_err(|e| McpError::Protocol {
                        message: format!("suspend_pipeline 失败: {e}"),
                    })?;
                // 落标即唤醒 wait_run_stop 等待者（事件驱动取消；任务停止与
                // 会话停止同链，llm 流式中断不再依赖插件轮询间隔）。
                crate::run_signal::RunSignalHub::global().signal(&run.run_id);
                Ok(json!({"status": "suspended", "pipeline_id": pipeline_id, "run_id": run.run_id}))
            }
            None => Ok(json!({"status": "suspended", "pipeline_id": pipeline_id, "run_id": ""})),
        }
    }

    /// pipeline-executor.resume_pipeline：恢复该管道执行。
    ///
    /// 恢复语义按 run 状态三分（状态簿记与执行拉起统一在此收口）：
    /// - 最新 run **Running**：执行在飞，幂等空转不重复派发（RunChain 已有本轮）。
    /// - 最新 run **Suspended + metadata.pending_interaction_request_id**：审批
    ///   挂起在飞（引擎 run 未结束、human_interaction 工具进程内阻塞）——翻
    ///   Running 簿记，真唤醒走 interaction.respond（回复载荷不经本能力）。
    /// - **其余**（停泊挂起：child_task_guard/DSL Wait/G8 排空；或无在飞 run：
    ///   历史已终态）：拉起一轮续跑——经恢复派发闭包走与聊天/催促同一条
    ///   派发链，state 由 stage_recover_history 从快照/DB 全量恢复，调用方
    ///   无需透传 state。旧 run 不翻状态（挂起即其真实收束史，新 run 另起
    ///   一行承载执行，避免翻 Running 后无人收尾的幽灵 running）。
    async fn handle_pipeline_executor_resume_pipeline(
        &self,
        params: Value,
    ) -> Result<Value, McpError> {
        let pipeline_id = params
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| McpError::Protocol {
                message: "resume_pipeline 缺少 pipeline_id 参数".to_string(),
            })?;
        // 可选状态复位指令（B13①：任务域 resume 链清 task.status 终态键）——
        // 不透明透传给续跑派发，随派发 overlay 在恢复合并之后应用。
        let state_overlay = params
            .get("state_overlay")
            .filter(|v| v.is_object())
            .cloned();
        let store = self.store.as_ref().ok_or_else(|| McpError::Protocol {
            message: "resume_pipeline disabled: kernel store not injected".to_string(),
        })?;
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let runs = store
            .list_runs_by_pipeline(pipeline_id, &tenant_id)
            .await
            .map_err(|e| McpError::Protocol {
                message: format!("resume_pipeline 查询失败: {e}"),
            })?;
        let target = runs.into_iter().find(|r| {
            r.status == agentos_core::types::RunStatus::Running
                || r.status == agentos_core::types::RunStatus::Suspended
        });
        if let Some(run) = &target {
            if run.status == agentos_core::types::RunStatus::Running {
                // 执行在飞：幂等空转，不重复派发。
                return Ok(json!({
                    "status": "resumed", "pipeline_id": pipeline_id,
                    "run_id": run.run_id, "dispatched": false,
                }));
            }
            let approval_pending = run
                .metadata
                .as_ref()
                .and_then(|m| m.get("pending_interaction_request_id"))
                .is_some();
            if approval_pending {
                store
                    .upsert_state_fields(
                        pipeline_id,
                        &run.tenant_id,
                        &json!({ "run_status": "running" })
                            .as_object()
                            .unwrap()
                            .clone(),
                    )
                    .await
                    .map_err(|e| McpError::Protocol {
                        message: format!("resume_pipeline 失败: {e}"),
                    })?;
                return Ok(json!({
                    "status": "resumed", "pipeline_id": pipeline_id,
                    "run_id": run.run_id, "dispatched": false,
                }));
            }
        }
        // 停泊挂起 / 无在飞 run：拉起续跑轮。管道须挂真实会话（thread 是派发
        // 坐标；孤儿/伪造 id 拒绝——静默跑完会把回复发到无人订阅的频道）。
        let thread_id = match store.get_thread_id_by_pipeline(pipeline_id).await {
            Ok(Some(tid)) => tid,
            Ok(None) => {
                return Err(McpError::Protocol {
                    message: format!(
                        "resume_pipeline 的 pipeline_id（{pipeline_id}）不属于任何会话\
                         （pipeline_sessions 未命中）：孤儿或伪造 id，拒绝派发"
                    ),
                });
            }
            Err(e) => {
                return Err(McpError::Protocol {
                    message: format!("resume_pipeline 解析管道会话坐标失败: {e}"),
                });
            }
        };
        let Some(resumer) = self.pipeline_resumer.as_ref() else {
            // 恢复派发未装配：降级为既有簿记语义（suspended → running），留痕。
            if let Some(run) = &target {
                tracing::warn!(
                    pipeline = %pipeline_id,
                    run_id = %run.run_id,
                    "resume_pipeline 恢复派发未装配，降级纯簿记（翻 Running 不拉起执行）"
                );
                store
                    .upsert_state_fields(
                        pipeline_id,
                        &run.tenant_id,
                        &json!({ "run_status": "running" })
                            .as_object()
                            .unwrap()
                            .clone(),
                    )
                    .await
                    .map_err(|e| McpError::Protocol {
                        message: format!("resume_pipeline 失败: {e}"),
                    })?;
            }
            return Ok(json!({
                "status": "resumed", "pipeline_id": pipeline_id,
                "run_id": target.as_ref().map(|r| r.run_id.clone()).unwrap_or_default(),
                "dispatched": false,
            }));
        };
        // 派发身份（刀3，ADR docs/decisions/2026-11-02-resume-dispatch-identity-
        // from-execution-context.md）：反调面身份取执行上下文注入（PluginScopedRouter
        // 桥消费 `_user_id` 重建的 scope），params.user_id 形参退役不可指派
        // （ADR 2026-10-17 信任模型在本派发面的执行收口）。空身份 fail-fast：
        // 曾以 unwrap_or("") 空串透传到 WS 分发层才被 user→租户解析拒绝，文案
        // 误导为「token 持有者已删除」，resume 路径全堵死（装机 2026-11-02 实证：
        // TaskTool continue 三连拒 user_id= 空）。审批翻面/幂等空转分支不派发，
        // 不需要身份，不受此拦。
        let user_id = agentos_tenant::current()
            .and_then(|c| c.user_id)
            .filter(|u| !u.is_empty());
        let Some(user_id) = user_id else {
            return Err(McpError::Protocol {
                message: format!(
                    "resume_pipeline 拒绝派发：调用链未携带用户身份（执行上下文无注入 user；\
                     反调面身份由内核 scope 注入，params.user_id 已退役）pipeline={pipeline_id}"
                ),
            });
        };
        resumer(pipeline_id.to_string(), thread_id, user_id, state_overlay)
            .await
            .map_err(|e| McpError::Protocol {
                message: format!("resume_pipeline 续跑派发失败: {e}"),
            })?;
        Ok(json!({
            "status": "resumed",
            "pipeline_id": pipeline_id,
            "run_id": target.as_ref().map(|r| r.run_id.clone()).unwrap_or_default(),
            "dispatched": true,
        }))
    }

    /// pipeline-executor.get_run_status：复盘侧轮询子管道真实完成状态
    /// （F-REVIEW-2），直接查 runs 表并返回完整 RunRecord。
    async fn handle_pipeline_executor_get_run_status(
        &self,
        params: Value,
    ) -> Result<Value, McpError> {
        let run_id = params
            .get("run_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| McpError::Protocol {
                message: "get_run_status 缺少 run_id 参数".to_string(),
            })?;
        let store = self.store.as_ref().ok_or_else(|| McpError::Protocol {
            message: "get_run_status disabled: kernel store not injected".to_string(),
        })?;
        let run = store
            .get_run(run_id)
            .await
            .map_err(|e| McpError::Protocol {
                message: format!("get_run_status 失败: {e}"),
            })?;
        serde_json::to_value(&run).map_err(|e| McpError::Protocol {
            message: format!("get_run_status 编码失败: {e}"),
        })
    }

    /// pipeline-executor.wait_run_stop：事件驱动停止等待（长轮询）。
    ///
    /// 挂起等待该 run 的停止信号（dispatch_stop / suspend* 落标即经
    /// [`crate::run_signal::RunSignalHub`] 唤醒），窗口到点无信号返回
    /// running（run 正常在跑，调用方重挂下一窗）。取代 llm 插件 0.5s
    /// get_run_status 轮询（用户裁定 2026-10-21：停止感知必须事件驱动）。
    ///
    /// 顺序契约：**先占等待位、再查权威状态**——signal 落在两步之间由
    /// Notify 的许可存储兜底（notify_one 无等待者时存一枚许可，notified()
    /// 创建即消费），不丢醒。
    async fn handle_pipeline_executor_wait_run_stop(
        &self,
        params: Value,
    ) -> Result<Value, McpError> {
        let run_id = params
            .get("run_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| McpError::Protocol {
                message: "wait_run_stop 缺少 run_id 参数".to_string(),
            })?
            .to_string();
        // 窗口夹紧：下限防忙转空转，上限防插件侧长占 capability 通道。
        let timeout_secs = params
            .get("timeout_secs")
            .and_then(|v| v.as_f64())
            .unwrap_or(20.0)
            .clamp(1.0, 120.0);
        let store = self.store.as_ref().ok_or_else(|| McpError::Protocol {
            message: "wait_run_stop disabled: kernel store not injected".to_string(),
        })?;
        let hub = crate::run_signal::RunSignalHub::global();
        let wait_fut = hub.wait(&run_id, std::time::Duration::from_secs_f64(timeout_secs));
        let run = store
            .get_run(&run_id)
            .await
            .map_err(|e| McpError::Protocol {
                message: format!("wait_run_stop 查询失败: {e}"),
            })?;
        if run.status == agentos_core::types::RunStatus::Suspended {
            return Ok(json!({ "status": "suspended", "run_id": run_id }));
        }
        let signaled = wait_fut.await;
        Ok(json!({
            "status": if signaled { "suspended" } else { "running" },
            "run_id": run_id,
        }))
    }

    /// pipeline-executor.is_pipeline_viewed：管道是否为任一用户当前选中的
    /// 活跃会话（查看态查询）。notify_judge 的「用户正在看 → 跳过通知判定」
    /// 依据（用户裁定 2026-10-21）；真值源 = RunChainRegistry 活跃管道映射。
    async fn handle_pipeline_executor_is_pipeline_viewed(
        &self,
        params: Value,
    ) -> Result<Value, McpError> {
        let pipeline_id = params
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| McpError::Protocol {
                message: "is_pipeline_viewed 缺少 pipeline_id 参数".to_string(),
            })?;
        let viewed = crate::run_chain::RunChainRegistry::global().is_pipeline_viewed(pipeline_id);
        Ok(json!({ "pipeline_id": pipeline_id, "viewed": viewed }))
    }

    /// pipeline-executor.delete_pipeline：任务删除语义（0.2 任务 = 管道），删除
    /// 该管道全部执行数据并清内存 registry 条目。幂等：无记录返回 ok。
    async fn handle_pipeline_executor_delete_pipeline(
        &self,
        params: Value,
    ) -> Result<Value, McpError> {
        let pipeline_id = params
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| McpError::Protocol {
                message: "delete_pipeline 缺少 pipeline_id 参数".to_string(),
            })?;
        let store = self.store.as_ref().ok_or_else(|| McpError::Protocol {
            message: "delete_pipeline disabled: kernel store not injected".to_string(),
        })?;
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        // 返回值 = 实际删除清单（含血缘后代子任务管道），逐个逐出内存注册表
        let deleted_ids =
            store
                .delete_pipeline(pipeline_id)
                .await
                .map_err(|e| McpError::Protocol {
                    message: format!("delete_pipeline 失败: {e}"),
                })?;
        let registry = agentos_session::pipeline_state_registry::global_registry();
        for pid in &deleted_ids {
            registry.remove(&tenant_id, pid);
        }
        Ok(json!({"status": "deleted", "pipeline_id": pipeline_id}))
    }
}
