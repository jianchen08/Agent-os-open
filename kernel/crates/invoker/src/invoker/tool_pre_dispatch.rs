// @feature: FP-0.2.一 native 工具异步预派发(同步 ABI 跨进程等待卸载) | @ci: rust-test
//! native 插件的异步工具预派发（同步 ABI 的跨进程等待卸载）。
//!
//! 背景：`host_type: in_process` 的 cdylib 走同步 `execute`；插件若在 execute
//! 内反向调用内核 capability（如 `tool-executor.invoke`），只能在阻塞线程里
//! `block_on` 等跨进程返回——等返回期间线程被占，工具一多就撞穿阻塞池。
//!
//! 本模块把这段等待移回 async：manifest `native.pre_dispatch` 声明后，内核在
//! 进入 cdylib 之前先 `await router.handle(...)` 执行工具，把结果作为
//! `_pre_decided_results` 注入插件 config；插件只做整形/校验/事件，不再同步
//! 等待。等待期间只占一个 future，不占线程、不占阻塞池。
//!
//! 契约：
//! - 只对声明 `native.pre_dispatch` 的插件生效；
//! - 参数构造与 tool_core 的 `build_invoke_params` 逐键对齐（tool_name/args/
//!   `_call_context`/workspace），保证被调工具收到的上下文不变；
//! - 工具调用参数 JSON 非法时不预派发（插件按既有解析失败路径直出）；
//! - 已有 guard 预填（state `pre_decided_results` 命中）时不执行——拦截优先；
//! - 超时/路由错误转失败信封预填，插件照常出结果，run 不被挂死。

use std::time::{Duration, Instant};

use agentos_core::traits::PluginManifest;
use agentos_core::types::{PluginContext, TenantContext};
use serde_json::{json, Value};
use tracing::{info, warn};

use super::timeout::tool_timeout_ms;
use super::PluginInvokerImpl;

/// 预派发结果在插件 config 中的注入键（下划线内部键，不进 state、不进 trace）。
pub(super) const PRE_DECIDED_CONFIG_KEY: &str = "_pre_decided_results";

/// state 中 guard 预填结果键（与 tool_core 读取面同名）。
const STATE_PRE_DECIDED_KEY: &str = "pre_decided_results";

/// `current_call` 解析结果（形状与 tool_core `ToolCall::parse` 对齐）。
#[derive(Debug, Clone, PartialEq)]
pub(super) struct PreDispatchCall {
    pub name: String,
    pub args: Value,
    pub call_id: Option<String>,
}

impl PreDispatchCall {
    /// 解析 `current_call`；非法/缺名以下与 tool_core 同口径，args JSON 非法
    /// 返回 None（预派发放弃，插件按解析失败路径直出）。
    pub(super) fn parse(raw: &Value) -> Option<Self> {
        if !raw.is_object() || raw.as_object().is_some_and(|o| o.is_empty()) {
            return None;
        }
        let name = raw
            .get("name")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or("unknown")
            .to_string();
        let mut args = raw
            .get("args")
            .cloned()
            .or_else(|| raw.get("arguments").cloned())
            .unwrap_or_else(|| json!({}));
        if let Some(text) = args.as_str() {
            args = serde_json::from_str(text).ok()?;
        }
        if !args.is_object() {
            args = json!({});
        }
        let call_id = raw.get("id").and_then(Value::as_str).map(String::from);
        Some(Self {
            name,
            args,
            call_id,
        })
    }

    /// 构造 `tool-executor.invoke` 参数（与 tool_core build_invoke_params 逐键对齐）。
    pub(super) fn invoke_params(&self, state: &Value) -> Value {
        let thread_id = state
            .get("session_id")
            .and_then(Value::as_str)
            .or_else(|| state.get("thread_id").and_then(Value::as_str))
            .unwrap_or("");
        let pipeline_id = state
            .get("pipeline_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let message_id = state
            .get("message_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let mut params = json!({
            "tool_name": self.name,
            "args": self.args.clone(),
            "_call_context": {
                "call_id": self.call_id.clone().unwrap_or_default(),
                "pipeline_id": pipeline_id,
                "message_id": message_id,
                "thread_id": thread_id,
            },
        });
        if let Some(workspace) = state
            .get("workspace")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
        {
            params["workspace"] = Value::String(workspace.to_string());
        }
        params
    }

    /// `tool-executor.invoke` 返回信封 → 预填 entry（七键与 guard 预填同形）。
    pub(super) fn result_entry(&self, response: &Value, duration_ms: f64) -> Value {
        let success = response
            .get("success")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let mut entry = json!({
            "tool_name": self.name,
            "success": success,
            "error": if success {
                Value::Null
            } else {
                Value::String(
                    response
                        .get("error")
                        .and_then(Value::as_str)
                        .filter(|s| !s.is_empty())
                        .unwrap_or("tool execution failed")
                        .to_string(),
                )
            },
            "data": response.get("data").cloned().unwrap_or(Value::Null),
            "metadata": response
                .get("metadata")
                .cloned()
                .filter(|m| !m.is_null())
                .unwrap_or(Value::Null),
            "duration_ms": duration_ms,
        });
        if let Some(id) = &self.call_id {
            entry["call_id"] = Value::String(id.clone());
        }
        entry
    }

    /// 失败预填（路由错误/超时/无 router 的 fail-closed 出口）。
    fn failed_entry(&self, error: &str) -> Value {
        self.result_entry(&json!({"success": false, "error": error}), 0.0)
    }
}

/// state 中是否已有命中本调用的 guard 预填（命中即拦截优先，不执行工具）。
///
/// 匹配口径与 tool_core `match_pre_decided` 一致（R15 逐 callId 精确配对）：
/// 有 id 只按 call_id 精确匹配（禁消费无 id entry——并发同工具调用结果串线
/// 通道）；无 id 调用按工具名匹配无 id entry（兼容面不变）。
pub(super) fn state_pre_decided(state: &Value, call: &PreDispatchCall) -> bool {
    let entries = state.get(STATE_PRE_DECIDED_KEY).and_then(Value::as_array);
    match_entries(entries, call.call_id.as_deref(), &call.name).is_some()
}

fn match_entries<'a>(
    entries: Option<&'a Vec<Value>>,
    call_id: Option<&str>,
    tool_name: &str,
) -> Option<&'a Value> {
    let entries = entries?;
    if let Some(id) = call_id.filter(|s| !s.is_empty()) {
        // R15：id 键调用只认同 id entry，不回落工具名（与 tool_core 同口径）。
        return entries
            .iter()
            .find(|entry| entry.get("call_id").and_then(Value::as_str) == Some(id));
    }
    entries.iter().find(|entry| {
        let idless = entry
            .get("call_id")
            .and_then(Value::as_str)
            .map(str::is_empty)
            .unwrap_or(true);
        idless && entry.get("tool_name").and_then(Value::as_str) == Some(tool_name)
    })
}

/// 把预填 entry 注入插件 config（对象 config 追加到 `_pre_decided_results` 数组）。
pub(super) fn inject_pre_decided(config: &mut Value, entry: Value) {
    let Some(obj) = config.as_object_mut() else {
        return;
    };
    let slot = obj
        .entry(PRE_DECIDED_CONFIG_KEY)
        .or_insert_with(|| json!([]));
    if let Some(arr) = slot.as_array_mut() {
        arr.push(entry);
    }
}

impl PluginInvokerImpl {
    /// 预派发等待界（毫秒）派生：tool-executor.invoke 目标经能力注册表反查
    /// （tool_name → plugin_id）后按 [`tool_timeout_ms`] 解析序取被调方声明界；
    /// 其余 capability / 反查不可达落内核 capability 缺省界（M4 派生语义，
    /// ADR 2026-11-05-timeout-taxonomy-and-merge）。
    pub(super) fn pre_dispatch_wait_ms(
        &self,
        capability: &str,
        method: &str,
        tool_name: &str,
        workspace: Option<&str>,
    ) -> u64 {
        let resolved = if capability == "tool-executor" && method == "invoke" {
            self.tool_registry
                .read()
                .as_ref()
                .and_then(|registry| registry.resolve_tool_instance(tool_name, workspace))
                .and_then(|descriptor| self.loader.get_manifest(&descriptor.plugin_id))
                .and_then(|manifest| tool_timeout_ms(&manifest, tool_name))
        } else {
            None
        };
        resolved.unwrap_or_else(agentos_core::traits::default_capability_timeout_ms)
    }

    /// 按 manifest 声明执行异步预派发；返回预填 entry（无需预派发/无声明 = None）。
    pub(super) async fn pre_dispatch_native(
        &self,
        plugin_id: &str,
        manifest: &PluginManifest,
        ctx: &PluginContext<'_>,
    ) -> Option<Value> {
        let spec = manifest.native.as_ref()?.pre_dispatch.as_ref()?;
        let raw = ctx.state.get("current_call")?;
        let call = PreDispatchCall::parse(raw)?;
        if state_pre_decided(ctx.state, &call) {
            return None;
        }
        let Some(router) = self.router.read().as_ref().cloned() else {
            warn!(
                target: "tool-pre-dispatch",
                plugin = %plugin_id,
                tool = %call.name,
                "native pre_dispatch 无 capability router，fail-closed 预填失败结果"
            );
            return Some(call.failed_entry("native pre_dispatch: capability router unavailable"));
        };

        let params = call.invoke_params(ctx.state);
        let started = Instant::now();
        // 身份继承门面（A1 统一收口）：执行/反调路径禁用两参 `new`（丢
        // user → resume_pipeline / session.create 守卫 fail-fast），从执行
        // 上下文全量继承三元组（tenant/session/user）。
        let identity = TenantContext::inherit(&ctx.tenant);
        let capability = spec.capability.clone();
        let method = spec.method.clone();
        // M4（ADR 2026-11-05-timeout-taxonomy-and-merge）：预派发等待界 =
        // 派发目标工具的声明界（[`tool_timeout_ms`] 解析序：工具 `timeout_ms`
        // > 插件声明需求 > 300s 内核 capability 缺省）。BUG-60 的「预派发界
        // 对齐审批族统一 24h」人肉常量退役——派生天然正确（human 声明
        // 86400 → 界 86400）；且与内层 invoke_tool 的 A4 界同源同值，外层
        // 永不先于内层到点。目标不可反查（注册表未装配/miss、非
        // tool-executor capability）落内核 capability 缺省界（与
        // [`bridge_timeout_ms`] 的桥面回退语义同构）。
        let wait_ms = self.pre_dispatch_wait_ms(
            &capability,
            &method,
            &call.name,
            params.get("workspace").and_then(Value::as_str),
        );
        let routed = agentos_tenant::scope(identity, async move {
            router.handle(&capability, &method, params).await
        });
        // tool_reported = 工具自身返回失败信封（工具内错误，如位置闸拒绝）——
        // 与内核派发面故障（路由错误/超时）分流，日志措辞随之分开（措辞即契约）。
        let (response, tool_reported) =
            match tokio::time::timeout(Duration::from_millis(wait_ms), routed).await {
                Ok(Ok(value)) => {
                    let tool_reported =
                        value.get("success").and_then(Value::as_bool) == Some(false);
                    (value, tool_reported)
                }
                Ok(Err(e)) => (json!({"success": false, "error": e.to_string()}), false),
                Err(_) => (
                    json!({
                        "success": false,
                        "error": format!("tool pre-dispatch timed out after {wait_ms}ms"),
                    }),
                    false,
                ),
            };
        let duration_ms = started.elapsed().as_secs_f64() * 1000.0;
        let success = response
            .get("success")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        if success {
            info!(
                target: "tool-pre-dispatch",
                plugin = %plugin_id,
                tool = %call.name,
                duration_ms,
                "native tool pre-dispatch done (async)"
            );
        } else {
            let error_text = response
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or("unknown");
            if tool_reported {
                warn!(
                    target: "tool-pre-dispatch",
                    plugin = %plugin_id,
                    tool = %call.name,
                    duration_ms,
                    error = %error_text,
                    "tool pre-dispatch: tool reported failure (tool-internal, not a kernel dispatch failure)"
                );
            } else {
                warn!(
                    target: "tool-pre-dispatch",
                    plugin = %plugin_id,
                    tool = %call.name,
                    duration_ms,
                    error = %error_text,
                    "tool pre-dispatch failed (kernel dispatch path: route error or timeout)"
                );
            }
        }
        Some(call.result_entry(&response, duration_ms))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn call(raw: Value) -> PreDispatchCall {
        PreDispatchCall::parse(&raw).expect("call parses")
    }

    #[test]
    fn parse_args_string_and_non_object_normalize() {
        let string_args = call(json!({"name": "f", "id": "c1", "args": "{\"a\": 1}"}));
        assert_eq!(string_args.args, json!({"a": 1}));
        assert_eq!(string_args.call_id.as_deref(), Some("c1"));

        let scalar_args = call(json!({"name": "f", "args": 7}));
        assert_eq!(scalar_args.args, json!({}));

        let arguments_key = call(json!({"name": "f", "arguments": {"b": 2}}));
        assert_eq!(arguments_key.args, json!({"b": 2}));
    }

    #[test]
    fn parse_invalid_or_empty_returns_none() {
        assert!(PreDispatchCall::parse(&json!("not-an-object")).is_none());
        assert!(PreDispatchCall::parse(&json!({})).is_none());
        assert!(PreDispatchCall::parse(&json!({"name": "f", "args": "{broken"})).is_none());
    }

    #[test]
    fn invoke_params_match_tool_core_shape() {
        let tc = call(json!({"name": "bash_execute", "id": "call_1", "args": {"command": "ls"}}));
        let state = json!({
            "session_id": "sess-1",
            "pipeline_id": "pipe-1",
            "message_id": "msg-1",
            "workspace": "D:\\ws",
        });
        let params = tc.invoke_params(&state);
        assert_eq!(params["tool_name"], "bash_execute");
        assert_eq!(params["args"], json!({"command": "ls"}));
        assert_eq!(params["workspace"], "D:\\ws");
        assert_eq!(
            params["_call_context"],
            json!({
                "call_id": "call_1",
                "pipeline_id": "pipe-1",
                "message_id": "msg-1",
                "thread_id": "sess-1",
            })
        );

        let fallback = call(json!({"name": "f"}));
        let params = fallback.invoke_params(&json!({"thread_id": "t-1"}));
        assert_eq!(params["_call_context"]["thread_id"], "t-1");
        assert!(params.get("workspace").is_none());
    }

    #[test]
    fn result_entry_maps_envelope_fields() {
        let tc = call(json!({"name": "f", "id": "c1"}));
        let ok = tc.result_entry(&json!({"success": true, "data": {"v": 1}}), 12.5);
        assert_eq!(ok["success"], true);
        assert_eq!(ok["data"], json!({"v": 1}));
        assert_eq!(ok["error"], Value::Null);
        assert_eq!(ok["metadata"], Value::Null);
        assert_eq!(ok["call_id"], "c1");
        assert_eq!(ok["duration_ms"], 12.5);

        let failed = tc.result_entry(&json!({"success": false, "error": "boom"}), 3.0);
        assert_eq!(failed["success"], false);
        assert_eq!(failed["error"], "boom");

        let missing_error = tc.result_entry(&json!({"success": false}), 1.0);
        assert_eq!(missing_error["error"], "tool execution failed");

        let with_meta = tc.result_entry(
            &json!({"success": true, "data": null, "metadata": {"action": "x"}}),
            1.0,
        );
        assert_eq!(with_meta["metadata"], json!({"action": "x"}));
    }

    #[test]
    fn state_pre_decided_matches_id_then_idless_name() {
        let by_id = call(json!({"name": "f", "id": "c1"}));
        assert!(state_pre_decided(
            &json!({"pre_decided_results": [{"call_id": "c1", "success": false}]}),
            &by_id
        ));
        assert!(!state_pre_decided(
            &json!({"pre_decided_results": [{"call_id": "other", "success": false}]}),
            &by_id
        ));
        // R15 逐 callId 精确配对：id 键调用禁消费无 id entry（并发同工具
        // 调用串线通道——不拦截即回落预派发执行，语义正确）。
        assert!(!state_pre_decided(
            &json!({"pre_decided_results": [{"tool_name": "f", "success": false}]}),
            &by_id
        ));

        let idless_call = call(json!({"name": "file_write"}));
        assert!(state_pre_decided(
            &json!({"pre_decided_results": [{"tool_name": "file_write", "success": false}]}),
            &idless_call
        ));
        assert!(!state_pre_decided(
            &json!({"pre_decided_results": [{"tool_name": "other", "success": false}]}),
            &idless_call
        ));
        // 对照臂：无 id 调用不消费 id 键 entry。
        assert!(!state_pre_decided(
            &json!({"pre_decided_results": [{"call_id": "c1", "tool_name": "file_write", "success": false}]}),
            &idless_call
        ));
    }

    #[test]
    fn inject_pre_decided_appends_to_config_array() {
        let mut config = json!({"tool_output": {"max_chars": 10}});
        inject_pre_decided(&mut config, json!({"tool_name": "f", "success": true}));
        inject_pre_decided(&mut config, json!({"tool_name": "g", "success": false}));
        assert_eq!(
            config[PRE_DECIDED_CONFIG_KEY],
            json!([
                {"tool_name": "f", "success": true},
                {"tool_name": "g", "success": false}
            ])
        );
        assert_eq!(config["tool_output"]["max_chars"], 10);
    }
}
