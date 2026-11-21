// @feature: FP-0.2.一 invoker 四路调用单次尝试(invoke_paths,B1 无恢复层) | @ci: rust-test
//! 四条调用路径的单次尝试（B1 不含恢复逻辑，供透明恢复包装器重试）：
//! sidecar pipeline/tool、native pipeline/tool。
//!
//! 自 invoker.rs 按关注点纯搬运拆出（2026-10-12 文件长度机械闸首个应用案例，
//! ADR docs/decisions/2026-10-12-file-length-mechanical-gate.md）。

use super::host_key::namespaced_tool_name;
use super::mcp_result::{
    extract_mcp_content, normalize_mcp_tool_result, normalize_native_tool_output,
    sanitize_external_mcp_arguments,
};
use super::native_services::{NativeHostServices, NATIVE_BRIDGE_SLOW_CALL_WARN_AFTER};

use super::*;

impl PluginInvokerImpl {
    /// sidecar pipeline 调用**单次尝试**（B1：不含恢复逻辑，供
    /// [`Self::with_transparent_recovery`] 重试）。
    ///
    /// 死亡判定（`PLUGIN_CRASHED`，可透明恢复）出现在两处：
    /// ① 调用前存活检查（stdio 进程已死——写失败标记/child 缺失/退出快照三
    ///    证据，见 [`McpClient::is_dead`]；HTTP transport 无进程不判死）；
    /// ② call_tool 失败后复查存活——在途调用中死亡（依赖被 idle GC 杀掉的
    ///    §9.4 场景；进程仍存活则归 MCP_CALL_FAILED，协议/工具错误重试无益，
    ///    运输写失败则由 is_recoverable_sidecar_death 特征兜底恢复）。
    pub(super) async fn attempt_sidecar_pipeline(
        &self,
        plugin_id: &str,
        manifest: &PluginManifest,
        ctx: &PluginContext<'_>,
    ) -> Result<PluginResult, PluginError> {
        // ADR 附录 D③（P6 命名治理）：从 manifest.invoke_entry 取 MCP 入口名
        // （如 "agent_config_load.execute"）。不再回退 capabilities.tools 或字面量
        // "execute"——缺 invoke_entry 是 manifest 错误，必须显式暴露。
        // 启动期 discover 聚合校验是主门；此处为运行期防线（深度防御）。
        let tool_name = manifest
            .invoke_entry
            .as_deref()
            .ok_or_else(|| PluginError {
                message: format!(
                    "pipeline plugin '{plugin_id}' missing manifest.invoke_entry (ADR 附录 D②)"
                ),
                code: Some("MISSING_INVOKE_ENTRY".to_string()),
                source: Some("plugin-invoker".to_string()),
            })?;
        // 合宿宿主以工具名命名空间区分成员（§4.2 第 3 条）：light 成员的每个
        // 工具在宿主侧注册为 `{plugin_id}.{tool_name}`，调用侧拼前缀分发；
        // 独占宿主无前缀（现状不变）。
        let mcp_tool_name = namespaced_tool_name(manifest, tool_name);

        // Sidecar 模式：通过 MCP 客户端调用
        let client_arc = self.get_or_create_mcp_client(manifest).await?;
        let client = client_arc.read().await;

        // ① 调用前存活检查（B1：死亡 → PLUGIN_CRASHED，交由恢复包装器 respawn+重试）
        if Self::is_dead_sidecar(&client).await {
            return Err(PluginError {
                message: format!("plugin process crashed: {plugin_id}"),
                code: Some("PLUGIN_CRASHED".to_string()),
                source: Some("plugin-invoker".to_string()),
            });
        }

        // _log_ctx：per-request 日志上下文，从 ctx.state 抽取（真实数据所在，
        // 见 server.rs 把 pipeline_id/session_id 写进 state）。SDK 在调 handler
        // 前 LogContext.bind，使 sidecar 日志能带 pipeline_id 关联内核日志。
        let log_ctx = serde_json::json!({
            "pipeline_id": ctx.state.get("pipeline_id").cloned().unwrap_or(Value::Null),
            "request_id": ctx.state.get("request_id").cloned().unwrap_or(Value::Null),
            "session_id": ctx.state.get("session_id").cloned().unwrap_or(Value::Null),
            "agent_name": ctx.state.get("agent.id").cloned().unwrap_or(Value::Null),
        });
        // _identity：执行上下文身份原头（刀1，ADR docs/working/
        // 2026-10-15-reverse-call-tenant-passthrough-adr-draft.md；刀2 用户维
        // ADR docs/decisions/2026-10-17-reverse-call-user-identity-injection.md）。
        // 内核下发、SDK 回显——插件反向调用内核时以 `_tenant_id`/`_session_id`/
        // `_user_id` 回带，桥入口（PluginScopedRouter）据此重建身份 scope，读侧
        // 不再恒落 default、派发面不再依赖插件手写 user。插件不感知该键（SDK
        // 层消费），普通工具参数不受影响。user 缺席序列化为 null（SDK 按
        // 非空字符串消费，畸形/缺席等同无用户维）。
        let identity = reverse_call_identity_payload(&ctx.tenant);
        // 每调用 config 注入（对齐 native 管道 invoke_native_pipeline）：引擎
        // 步骤 config 只携带 inputs/_step_method，config_files 注入按调用方
        // manifest 现算（shared::injected_config）——合宿宿主不共享注入，
        // 各成员只收自己的 config_files 命名空间，再与步骤 config 合并。
        let injected = crate::shared::injected_config(self.loader.as_ref(), manifest).await?;
        let call_config = crate::shared::merge_injected_with_step_config(injected, &ctx.config);
        // arguments 直接拼 JSON 串（call_tool_raw 原文透传）：state 每步整包
        // 过境，走中间 Value 树 = 每步多一次全量深拷（json! 宏隐式复制）。
        // state/config/_log_ctx 各自一遍 to_writer 进同一缓冲，帧语义不变。
        // state.reads 切片投喂（组装单点 shared.rs）：声明非空 → 白名单控制键
        // ∪ 声明键切片；未声明 = 全量借用（零拷贝，迁移期默认字节不变）。
        let state_payload =
            crate::shared::state_feed_payload_with_base(manifest, ctx.state, ctx.base_state);
        // M3b：三段序列化直写 client 复用缓冲（闭包借用 state_payload/config/
        // log_ctx——均活到本调用结束），arguments String 独立分配清零。
        let result = match client
            .call_tool_raw_fragments(&mcp_tool_name, |buf| {
                buf.extend_from_slice(b"{\"state\":");
                serde_json::to_writer(&mut *buf, &state_payload)?;
                buf.extend_from_slice(b",\"config\":");
                serde_json::to_writer(&mut *buf, &call_config)?;
                buf.extend_from_slice(b",\"_log_ctx\":");
                serde_json::to_writer(&mut *buf, &log_ctx)?;
                buf.extend_from_slice(b",\"_identity\":");
                serde_json::to_writer(&mut *buf, &identity)?;
                buf.extend_from_slice(b"}");
                Ok(())
            })
            .await
        {
            Ok(v) => v,
            Err(e) => {
                // ② 失败后复查存活：死亡（含在途死亡）→ PLUGIN_CRASHED（可透明恢复）；
                //    存活 → MCP_CALL_FAILED 语义（协议/工具错误）。
                //    （call_tool 已把底层错误统一包成 ToolCallFailed，无法按错误
                //    类型区分崩溃，故用存活复查作权威判定。）
                let died_mid_call = Self::is_dead_sidecar(&client).await;
                // ③ 假死复查（进程治理三信号模型）：进程存活但事件循环心跳停跳
                //    超过阈值 → 事件循环冻结（「活着却不处理任何请求」）。判定
                //    与取证只需 &client（读锁内完成），命中与否都不在此持锁做
                //    回收——force_unload 内部对同一 client Arc 取写锁（unload_host），
                //    读锁存活时执行 = 同任务自等写锁永久悬挂（请求卡死、该宿主
                //    全部写治理排队、InflightGuard 永不回落致 GC 跳过）。假死时
                //    调用仍按 MCP_CALL_FAILED 返回（透明恢复包装器按既有语义
                //    处理重试）。
                let frozen = !died_mid_call
                    && self
                        .evaluate_frozen_sidecar(&client, plugin_id, &e.to_string())
                        .await;
                // 评估取证完成，先释放读锁再触发 Force unload（unload 从
                // mcp_clients 注册表自取 Arc，锁契约见 evaluate_frozen_sidecar）。
                drop(client);
                if frozen {
                    if let Err(unload_err) = self.force_unload_impl(plugin_id).await {
                        tracing::error!(
                            target: "sidecar",
                            "[{}] 假死自愈 Force unload 失败: {}（残留进程待 idle GC 兜底）",
                            plugin_id, unload_err.message
                        );
                    }
                }
                return Err(if died_mid_call {
                    PluginError {
                        message: format!("plugin process died mid-call: {plugin_id}: {e}"),
                        code: Some("PLUGIN_CRASHED".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    }
                } else {
                    PluginError {
                        message: format!("MCP call failed: {e}"),
                        code: Some("MCP_CALL_FAILED".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    }
                });
            }
        };

        // 解析 MCP 响应——Python SDK 返回格式为：
        // { "content": [{ "type": "text", "text": "<json_string>" }], "isError": false }
        // 提取 content[0].text 并反序列化为 PluginResult
        let inner = extract_mcp_content(&result);
        // isError 信封（{"success": false, "error": <text>}，extract 在 isError=true
        // 提取点的产物）→ PluginResult 形再反序列化：PluginResult.error 是
        // PluginError 结构体（message/code/source），信封的字符串 error 直塞必败
        // （invalid type: string, expected struct PluginError——2026-11-18 管道
        // 7fb38fe8 实证：LLM 上游故障经 SDK isError 出口，步骤失败丢结构化错误
        // 卡成 PARSE_ERROR）。success 判别键无碰撞面（PluginResult 无 success
        // 字段）；error 文本优先剥 SDK JSON 信封外壳取内层 error，非 JSON 回退原文。
        let plugin_value = if inner.get("success").and_then(|v| v.as_bool()) == Some(false) {
            let raw_err = inner
                .get("error")
                .and_then(|e| e.as_str())
                .unwrap_or("MCP pipeline step returned isError=true");
            let msg = serde_json::from_str::<serde_json::Value>(raw_err)
                .ok()
                .and_then(|v| {
                    v.get("error")
                        .and_then(|e| e.as_str())
                        .map(std::string::ToString::to_string)
                })
                .unwrap_or_else(|| raw_err.to_string());
            serde_json::json!({
                "state_updates": {},
                "skip_remaining": false,
                "error": {"message": msg}
            })
        } else {
            inner
        };
        let plugin_result: PluginResult =
            serde_json::from_value(plugin_value).map_err(|e| PluginError {
                message: format!("failed to parse MCP response as PluginResult: {e}"),
                code: Some("PARSE_ERROR".to_string()),
                source: Some("plugin-invoker".to_string()),
            })?;

        Ok(plugin_result)
    }

    /// sidecar tool 调用**单次尝试**（B1：与 [`Self::attempt_sidecar_pipeline`] 同构）。
    ///
    /// 派发前置面挂死护栏（run 生命周期防线②，ADR
    /// 2026-11-17-run-lifecycle-defense-lines）：宿主获取（含 spawn 锁排队/冷
    /// 启动握手）与客户端读锁等待是「进入 sidecar 传输前」的内核侧等待段，
    /// 各罩 300s 固定护栏（分类学 ipc 档，不可豁免——业务豁免只豁免执行时长，
    /// 不重开派发挂死驻留）。到点返回结构化 `PRE_DISPATCH_TIMEOUT`，按派发
    /// 失败处理（结构化错误回 agent，可重试）。台账第 183/186/202 轮实证：
    /// 该段单点挂死 2.5h、零运输层日志、无超时机制。
    pub(super) async fn attempt_sidecar_tool(
        &self,
        plugin_id: &str,
        manifest: &PluginManifest,
        tool_name: &str,
        inputs: &Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        let client_arc = with_pre_dispatch_timeout(
            plugin_id,
            tool_name,
            self.get_or_create_mcp_client(manifest),
        )
        .await?;
        let client = with_pre_dispatch_timeout_opt(client_arc.read())
            .await
            .ok_or_else(|| {
                pre_dispatch_timeout_error(PRE_DISPATCH_TIMEOUT_MS, plugin_id, tool_name)
            })?;

        // ① 调用前存活检查（B1：死亡 → PLUGIN_CRASHED，可透明恢复）
        if Self::is_dead_sidecar(&client).await {
            return Err(PluginError {
                message: format!("plugin process crashed: {plugin_id}"),
                code: Some("PLUGIN_CRASHED".to_string()),
                source: Some("plugin-invoker".to_string()),
            });
        }

        // 合宿宿主工具名命名空间前缀（§4.2 第 3 条，与 pipeline 路径同构）。
        let mcp_tool_name = namespaced_tool_name(manifest, tool_name);
        // 标准 MCP 出参归一（external MCP 一行接入配套）：外部标准 MCP server
        // （FastMCP 等）对未声明参数严格校验，内核/管道注入的会话上下文键
        // （_call_context/session_id/workspace 等）必须调用前剥离；内部 sidecar
        // 的 SDK 按签名过滤不受影响。
        let mcp_arguments = if manifest.entry == "mcp:external" {
            // 外部 MCP 不注入配置：未声明参数本会被剥离，且注入配置含
            // 凭证不得出境。
            sanitize_external_mcp_arguments(manifest, tool_name, inputs)
        } else {
            // 每调用 config 注入（对齐 attempt_sidecar_pipeline/native 工具
            // 路径）：本路径承载全部跨插件工具/服务调用（如 llm_core →
            // llm.complete_stream），不注入则目标插件配置冻结在握手快照，
            // 设置页改配置须重启才生效（2026-09-28 事故根因）。
            let injected = crate::shared::injected_config(self.loader.as_ref(), manifest).await?;
            let args = crate::shared::tool_arguments_with_config(inputs, injected);
            // 反调身份注入（隔离执法点，ADR docs/working/2026-10-15-reverse-call-
            // tenant-passthrough-adr-draft.md 刀1 的工具路径补齐）：租户 scope
            // 在场（PluginScopedRouter 消费 `_tenant_id` 建立）才注入——SDK 在
            // 目标插件调用边界弹出 `_identity` 绑定 contextvar，插件侧按
            // 「显式形参 > current_reverse_call_tenant_id() > default」取租户
            // （tasks 先例；1018 审计 G2：无此注入则跨插件服务调用在目标侧
            // 恒无身份，授权名单等租户数据面错落 default）。无 scope（裸工具
            // 调用）不注入 = 现状语义不变。
            let with_identity =
                tool_arguments_with_identity(args, agentos_tenant::current().as_ref());
            with_identity
        };
        let result = match client.call_tool(&mcp_tool_name, &mcp_arguments).await {
            Ok(v) => v,
            Err(e) => {
                // ② 失败后复查存活（在途死亡 → PLUGIN_CRASHED；否则原
                //    MCP_TOOL_CALL_FAILED 语义）
                let died_mid_call = Self::is_dead_sidecar(&client).await;
                return Err(if died_mid_call {
                    PluginError {
                        message: format!("plugin process died mid-call: {plugin_id}: {e}"),
                        code: Some("PLUGIN_CRASHED".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    }
                } else {
                    PluginError {
                        message: format!("MCP tool call failed: {e}"),
                        code: Some("MCP_TOOL_CALL_FAILED".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    }
                });
            }
        };

        // 解析 MCP 响应：extract_mcp_content 已提取 content[0].text 并反序列化为
        // 工具 handler 的原始返回（业务 dict，如 {"result": ...}）。
        //
        // 工具返回的是**纯业务数据**，不带 ToolExecutionResult 的 success/data 信封
        // （那是内核内部结构，插件不该感知）。所以这里不能直接 from_value 成
        // ToolExecutionResult——否则业务 dict 缺 success 字段会报
        // "missing field `success`"。这里按三层优先级智能构造：
        //   ① 若 isError=true（已被 extract 显式转 {"success": false, "error": ...}）
        //      → failure；
        //   ② 若返回值恰好已是 ToolExecutionResult 信封（带 success 字段）→ 直接用；
        //   ③ 否则视为纯业务数据 → success(data=inner)，与 pipeline 路径
        //      （ToolExecutionResult::success(to_value(plugin_result))）对齐。
        let inner = extract_mcp_content(&result);
        let tool_result = normalize_mcp_tool_result(inner, tool_name)?;

        Ok(tool_result)
    }

    /// 原生插件 pipeline 调用：config 注入 + 热重载 + 崩溃回调，与 sidecar 对齐。
    ///
    /// 直接 trait 对象版：构造 HostServices（包 router）+ PluginCtx，调 loader.execute
    /// 直接 trait 派发。不经 JSON 序列化中间层，无 host_call 函数指针。
    pub(super) async fn invoke_native_pipeline(
        &self,
        plugin_id: &str,
        manifest: &PluginManifest,
        ctx: &PluginContext<'_>,
    ) -> Result<PluginResult, PluginError> {
        let loader = self.native_loader_or_err(plugin_id)?;

        // 热重载指纹检测（与 sidecar 同逻辑）：代码/配置变更告警。
        if self.is_plugin_stale(plugin_id, manifest).await {
            warn!(
                "Native plugin '{}' source changed but cdylib cannot hot-unload (dlclose limit); \
                 restart kernel to pick up changes",
                plugin_id
            );
        }

        self.load_native(loader, plugin_id, manifest)?;

        // config 注入（shared::injected_config，按 manifest.config_files 命名空间）。
        let mut config = crate::shared::injected_config(self.loader.as_ref(), manifest).await?;

        // 异步预派发（manifest.native.pre_dispatch 声明）：跨进程工具调用在
        // async 面等待完成，结果作为预填结果注入 config；cdylib 的同步 execute
        // 只做整形，不再在阻塞线程里 block_on 等工具返回。
        if let Some(entry) = self.pre_dispatch_native(plugin_id, manifest, ctx).await {
            super::tool_pre_dispatch::inject_pre_decided(&mut config, entry);
        }

        // state.reads 切片投喂（与 sidecar 路径同一组装语义，shared.rs 单点）：
        // 声明非空 → 白名单控制键 ∪ 声明键切片；未声明 = 全量（迁移期默认）。
        let state_json =
            crate::shared::pipeline_state_json_with_base(manifest, ctx.state, ctx.base_state);

        // PluginCtx 在 blocking worker 内构造：config 序列化与字符串释放都留在
        // 同一常驻 worker；config buffer 从线程本地 scratch 取还，跨调用复用容量。
        let tenant_id = ctx.tenant.tenant_id.clone();
        let session_id = ctx.tenant.session_id.clone();
        let task_id = ctx.task_id.clone();
        let pipeline_id = ctx.pipeline_id.to_string();

        // 构造 HostServices（包 router）。router 缺失则 host=None，插件降级。
        // G6：携带 plugin_id 信任锚点（单点授权在 CapabilityRouter 校验）。
        // G7：execute 跑在 spawn_blocking 线程（cdylib 同步 C-ABI 可能长计算，
        // 不阻塞 tokio worker）；HostServices 标记 on_blocking_thread，
        // 内部 bridge 用直接 block_on（block_in_place 在 blocking 线程会 panic）。
        let host_svc: Option<NativeHostServices> =
            self.router
                .read()
                .as_ref()
                .map(|router| NativeHostServices {
                    router: Arc::clone(router),
                    plugin_id: plugin_id.to_string(),
                    // 反向调用身份透传（刀1 + A1 统一收口）：身份原头 = 引擎
                    // 执行上下文，经 inherit 全量继承三元组（user 随行——两参
                    // 构造丢 user 会使反调守卫 fail-fast）。
                    identity: Some(agentos_core::types::TenantContext::inherit(&ctx.tenant)),
                    on_blocking_thread: true,
                    response_buf: std::cell::UnsafeCell::new(String::with_capacity(4096)),
                    err_buf: std::cell::UnsafeCell::new(String::with_capacity(256)),
                    loader: Arc::clone(&self.loader),
                    bridge_timeout_override_ms: None,
                    slow_call_warn_after: NATIVE_BRIDGE_SLOW_CALL_WARN_AFTER,
                });

        let loader = Arc::clone(loader);
        let pid = plugin_id.to_string();
        let result = tokio::task::spawn_blocking(move || {
            let mut config_bytes = agentos_memory::scratch::take_bytes();
            if serde_json::to_writer(&mut config_bytes, &config).is_err() {
                config_bytes.clear();
                config_bytes.extend_from_slice(b"{}");
            }
            let plugin_ctx = agentos_native_sdk::PluginCtx {
                state_json,
                config_json: String::from_utf8(config_bytes).unwrap_or_else(|_| "{}".to_string()),
                tenant_id,
                session_id,
                task_id,
                pipeline_id,
                tool_call_json: None,
                hook_json: None,
            };
            let host_ref: Option<&dyn agentos_native_sdk::HostServices> = host_svc
                .as_ref()
                .map(|h| h as &dyn agentos_native_sdk::HostServices);
            let result = loader.execute(&pid, &plugin_ctx, host_ref);
            let agentos_native_sdk::PluginCtx { config_json, .. } = plugin_ctx;
            agentos_memory::scratch::put_bytes(config_json.into_bytes());
            result
        })
        .await
        .map_err(|join_err| PluginError {
            message: format!("native plugin '{plugin_id}' execute task panicked: {join_err}"),
            code: Some("NATIVE_EXECUTE_PANICKED".to_string()),
            source: Some("plugin-invoker".to_string()),
        })?;

        // execute 返回 state_updates JSON 字符串 → 解析为 PluginResult。
        let plugin_result = result.and_then(|state_updates_json| {
            let state_updates: std::collections::HashMap<String, serde_json::Value> =
                serde_json::from_str(&state_updates_json).map_err(|e| PluginError {
                    message: format!("native plugin state_updates parse failed: {e}"),
                    code: Some("NATIVE_OUTPUT_PARSE".to_string()),
                    source: Some("plugin-invoker".to_string()),
                })?;
            Ok(PluginResult {
                state_updates,
                skip_remaining: false,
                error: None,
            })
        });

        // 包装成 Result<Result<...>, _> 复用 notify_if_crash。
        let as_result = plugin_result;
        self.notify_if_crash(plugin_id, &as_result);
        as_result
    }

    /// 原生插件 tool 调用（B2：native 工具插件支持，M2 计划任务 B）。
    ///
    /// 复用生命周期钩子 `hook` 字段的既有模式：PluginCtx 加 **约定字段**
    /// `tool_call_json = {"name": tool_name}`（概念上即 PluginInput 的
    /// `{state: inputs, config: 注入配置, tool_call: {name: tool_name}}`），
    /// 经 native_loader 的 execute 入口调用——与 pipeline execute 同一 C-ABI 入口，
    /// 约定字段区分语义。
    ///
    /// 插件侧约定：execute 见 tool_call 字段走工具逻辑，返回 ToolExecutionResult
    /// 形状 JSON；**旧插件不认识该字段 → 忽略，按 pipeline 逻辑返回 state_updates**
    /// ——调用侧（[`normalize_native_tool_output`]）检测返回形状归一：能解析成
    /// ToolExecutionResult 信封就用，否则按现状包 success 信封（零破坏）。
    ///
    /// crash 语义对齐 [`Self::notify_if_crash`]（execute panic/fatal → 崩溃回调）。
    pub(super) async fn invoke_native_tool(
        &self,
        plugin_id: &str,
        manifest: &PluginManifest,
        tool_name: &str,
        inputs: &Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        let loader = self.native_loader_or_err(plugin_id)?;

        // 热重载指纹检测（与 invoke_native_pipeline 同逻辑）：代码/配置变更告警。
        if self.is_plugin_stale(plugin_id, manifest).await {
            warn!(
                "Native plugin '{}' source changed but cdylib cannot hot-unload (dlclose limit); \
                 restart kernel to pick up changes",
                plugin_id
            );
        }

        self.load_native(loader, plugin_id, manifest)?;

        // config 注入（shared::injected_config，按 manifest.config_files 命名空间）。
        let config = crate::shared::injected_config(self.loader.as_ref(), manifest).await?;

        // PluginCtx 在 blocking worker 内构造：config/工具标记序列化与释放同 worker；
        // 两个 JSON buffer 从线程本地 scratch 取还，跨调用复用容量。
        // state=inputs（工具入参），tool_call 约定字段表达工具语义；工具调用无
        // PluginContext（invoke_tool 不带租户/管道），租户等字段留空——与 sidecar
        // tools/call 直传 inputs（不带租户）对齐。
        let state_json = serde_json::to_string(inputs).unwrap_or_else(|_| "{}".into());
        let tool_name = tool_name.to_string();

        // 构造 HostServices（包 router）。G6/G7 语义与 invoke_native_pipeline 同构：
        // 携带 plugin_id 信任锚点；execute 跑 spawn_blocking（C-ABI 同步可能长计算）。
        let host_svc: Option<NativeHostServices> =
            self.router
                .read()
                .as_ref()
                .map(|router| NativeHostServices {
                    router: Arc::clone(router),
                    plugin_id: plugin_id.to_string(),
                    // 工具路径无执行上下文（invoke_tool 只有 inputs）：无租户，
                    // 反调走现状回退语义（与 sidecar 桥对齐；升级路径见 ADR）。
                    identity: None,
                    on_blocking_thread: true,
                    response_buf: std::cell::UnsafeCell::new(String::with_capacity(4096)),
                    err_buf: std::cell::UnsafeCell::new(String::with_capacity(256)),
                    loader: Arc::clone(&self.loader),
                    bridge_timeout_override_ms: None,
                    slow_call_warn_after: NATIVE_BRIDGE_SLOW_CALL_WARN_AFTER,
                });

        let loader = Arc::clone(loader);
        let pid = plugin_id.to_string();
        let raw = tokio::task::spawn_blocking(move || {
            let mut config_bytes = agentos_memory::scratch::take_bytes();
            if serde_json::to_writer(&mut config_bytes, &config).is_err() {
                config_bytes.clear();
                config_bytes.extend_from_slice(b"{}");
            }
            let mut tool_bytes = agentos_memory::scratch::take_bytes();
            if serde_json::to_writer(&mut tool_bytes, &serde_json::json!({ "name": tool_name }))
                .is_err()
            {
                tool_bytes.clear();
                tool_bytes.extend_from_slice(b"{}");
            }
            let plugin_ctx = agentos_native_sdk::PluginCtx {
                state_json,
                config_json: String::from_utf8(config_bytes).unwrap_or_else(|_| "{}".to_string()),
                tenant_id: String::new(),
                session_id: String::new(),
                task_id: String::new(),
                pipeline_id: String::new(),
                tool_call_json: Some(
                    String::from_utf8(tool_bytes).unwrap_or_else(|_| "{}".to_string()),
                ),
                hook_json: None,
            };
            let host_ref: Option<&dyn agentos_native_sdk::HostServices> = host_svc
                .as_ref()
                .map(|h| h as &dyn agentos_native_sdk::HostServices);
            let result = loader.execute(&pid, &plugin_ctx, host_ref);
            let agentos_native_sdk::PluginCtx {
                config_json,
                tool_call_json,
                ..
            } = plugin_ctx;
            agentos_memory::scratch::put_bytes(config_json.into_bytes());
            if let Some(tool_json) = tool_call_json {
                agentos_memory::scratch::put_bytes(tool_json.into_bytes());
            }
            result
        })
        .await
        .map_err(|join_err| PluginError {
            message: format!("native plugin '{plugin_id}' execute task panicked: {join_err}"),
            code: Some("NATIVE_EXECUTE_PANICKED".to_string()),
            source: Some("plugin-invoker".to_string()),
        })?;

        // execute 返回 JSON 字符串 → 解析 + 归一为 ToolExecutionResult（对齐
        // invoke_tool 的 sidecar 决策树：纯业务数据包 success 信封）。
        let result = raw.and_then(|out_json| {
            let inner: Value = serde_json::from_str(&out_json).map_err(|e| PluginError {
                message: format!("native plugin '{plugin_id}' tool output parse failed: {e}"),
                code: Some("NATIVE_OUTPUT_PARSE".to_string()),
                source: Some("plugin-invoker".to_string()),
            })?;
            Ok(normalize_native_tool_output(&inner))
        });

        // crash 语义对齐（notify_if_crash：PANICKED/FATAL → 崩溃回调）。
        self.notify_if_crash(plugin_id, &result);
        result
    }
}

/// invocation `_identity` 正向注入载荷（刀1/刀2 契约单一真值点）。
///
/// 键面：`tenant_id`/`session_id` 恒在；`user_id` 取 `TenantContext.user_id`
/// （None → null——SDK 端按非空字符串消费，null/缺席/畸形同归无用户维）。
pub(super) fn reverse_call_identity_payload(tenant: &agentos_core::types::TenantContext) -> Value {
    serde_json::json!({
        "tenant_id": tenant.tenant_id,
        "session_id": tenant.session_id,
        "user_id": tenant.user_id,
    })
}

/// 工具参数身份注入（[`Self::attempt_sidecar_tool`] 单点消费；纯函数便于测试）。
///
/// scope 在场 → 参数顶部注入 `_identity`（SDK 调用边界弹出，不泄漏给 handler
/// 形参）；scope 缺席（裸工具调用）或参数非 object → 原样返回（现状语义）。
pub(super) fn tool_arguments_with_identity(
    mut args: Value,
    tenant: Option<&agentos_core::types::TenantContext>,
) -> Value {
    if let Some(tenant) = tenant {
        if let Some(obj) = args.as_object_mut() {
            obj.insert(
                "_identity".to_string(),
                reverse_call_identity_payload(tenant),
            );
        }
    }
    args
}

#[cfg(test)]
mod identity_payload_tests {
    use agentos_core::types::TenantContext;

    use super::*;

    #[test]
    fn identity_payload_carries_user_when_present() {
        let mut ctx = TenantContext::new("maintenance", "thread-1");
        ctx.user_id = Some("u-abc".to_string());
        let v = reverse_call_identity_payload(&ctx);
        assert_eq!(v["tenant_id"], "maintenance");
        assert_eq!(v["session_id"], "thread-1");
        assert_eq!(v["user_id"], "u-abc");
    }

    #[test]
    fn identity_payload_user_absent_serializes_null() {
        let v = reverse_call_identity_payload(&TenantContext::new("default", "thread-2"));
        assert!(
            v["user_id"].is_null(),
            "None user 必须为 null（SDK 过滤），实际 {v}"
        );
    }

    #[test]
    fn tool_arguments_inject_identity_when_scope_present() {
        let mut ctx = TenantContext::new("u-maintenance", "thread-9");
        ctx.user_id = Some("u-maintenance".to_string());
        let args = serde_json::json!({ "state": {}, "config": {} });
        let out = tool_arguments_with_identity(args, Some(&ctx));
        assert_eq!(out["_identity"]["tenant_id"], "u-maintenance");
        assert_eq!(out["_identity"]["session_id"], "thread-9");
        // 既有键不受影响
        assert!(out.get("state").is_some());
    }

    #[test]
    fn tool_arguments_unchanged_without_scope() {
        let args = serde_json::json!({ "state": {} });
        let out = tool_arguments_with_identity(args, None);
        assert!(
            out.get("_identity").is_none(),
            "无 scope 不得注入（现状语义），实际 {out}"
        );
    }

    #[test]
    fn tool_arguments_non_object_untouched() {
        let mut ctx = TenantContext::new("t", "s");
        ctx.user_id = None;
        let args = serde_json::json!("scalar");
        let out = tool_arguments_with_identity(args, Some(&ctx));
        assert_eq!(out, serde_json::json!("scalar"));
    }
}
