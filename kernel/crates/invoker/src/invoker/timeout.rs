//! 调用超时解析与有界等待纯函数族：工具声明超时解析序、非流式总时长包装、
//! 原生反调桥等待界与慢调用告警的两段有界等待、管道步壳可抬高 deadline。
//!
//! 自 invoker.rs 按关注点纯搬运拆出（2026-10-12 文件长度机械闸首个应用案例，
//! ADR docs/decisions/2026-10-12-file-length-mechanical-gate.md）。
//! L1 让位机制 + M3 单一解析点（ADR
//! docs/decisions/2026-11-05-timeout-taxonomy-and-merge，设计
//! docs/working/超时体系分类设计_20261105.md §5.1/§5.7）。

use std::time::Duration;

use super::*;

/// 工具调用的有效总时长超时（毫秒）；`None` = 显式豁免（P12，ADR 2026-09-07）。
///
/// 优先级：工具声明 `capabilities.tools[].timeout_ms` > 插件级声明需求（M2
/// 单声明派生：`capabilities.call_timeout_seconds` ∪ `mcp.request_timeout_secs`
/// 取最大，秒→毫秒，[`PluginManifest::declared_call_need_secs`]）> 内核默认
/// [`agentos_core::traits::default_capability_timeout_ms`]。插件级声明兜底
/// 保证长等待插件（human 审批 86400s 等，BUG-60 审批族统一值）既有声明继续
/// 生效，不被 300s 工具执行默认掐断；工具声明 `Some(0)` 为 0 哨兵豁免
/// （对齐 `lifecycle.idle_timeout_secs`）。
pub(super) fn tool_timeout_ms(manifest: &PluginManifest, tool_name: &str) -> Option<u64> {
    let declared = manifest
        .capabilities
        .tools
        .iter()
        .find(|t| t.name == tool_name)
        .and_then(|t| t.timeout_ms);
    match declared {
        Some(0) => None,
        Some(ms) => Some(ms),
        None => Some(
            manifest
                .declared_call_need_secs()
                .map(|secs| secs.saturating_mul(1000))
                .unwrap_or_else(agentos_core::traits::default_capability_timeout_ms),
        ),
    }
}

/// 非流式 capability 调用的总时长超时包装（P12，ADR 2026-09-07）。
///
/// `timeout_ms` 为 `None` 直接透传（豁免）；否则到点放弃内层 future——
/// 其持有的 in-flight guard 随 drop 回落——并返回结构化 `CAPABILITY_TIMEOUT`
/// （message 含插件 id、工具名与时长毫秒数）。
pub(super) async fn with_capability_timeout<T>(
    timeout_ms: Option<u64>,
    plugin_id: &str,
    tool_name: &str,
    fut: impl std::future::Future<Output = Result<T, PluginError>>,
) -> Result<T, PluginError> {
    let Some(ms) = timeout_ms else {
        return fut.await;
    };
    match tokio::time::timeout(Duration::from_millis(ms), fut).await {
        Ok(result) => result,
        Err(_) => Err(PluginError {
            message: format!(
                "capability call timed out after {ms}ms: plugin '{plugin_id}' tool '{tool_name}'"
            ),
            code: Some("CAPABILITY_TIMEOUT".to_string()),
            source: Some("plugin-invoker".to_string()),
        }),
    }
}

/// 原生反调桥的等待界（毫秒）：被调工具声明的超时解析序 > 内核默认。
///
/// 仅 `tool-executor.invoke` 且参数携带显式 `plugin_id` 时可同步解析被调方
/// manifest（经 [`PluginLoader::get_manifest`]，读缓存路径不碰 async 面），按
/// [`tool_timeout_ms`] 既有解析序取值（工具声明 `timeout_ms` > 插件级声明
/// 需求 > 内核默认）；其余 capability（event-bus / service-registry 等）与
/// 注册表反查路径桥面同步不可解析 → 内核默认界。解析链得到的豁免哨兵
/// （工具声明 `Some(0)` → `None`）在桥面回落默认界：桥面界是挂死护栏而非
/// 业务超时，业务豁免不重开无界驻留（ADR 2026-09-28）。
pub(super) fn bridge_timeout_ms(
    loader: &dyn PluginLoader,
    capability: &str,
    method: &str,
    params: &Value,
) -> u64 {
    let declared = if capability == "tool-executor" && method == "invoke" {
        params
            .get("tool_name")
            .and_then(|v| v.as_str())
            .and_then(|tool_name| {
                params
                    .get("plugin_id")
                    .and_then(|v| v.as_str())
                    .filter(|s| !s.is_empty())
                    .and_then(|plugin_id| loader.get_manifest(plugin_id))
                    .and_then(|manifest| tool_timeout_ms(&manifest, tool_name))
            })
    } else {
        None
    };
    declared.unwrap_or_else(agentos_core::traits::default_capability_timeout_ms)
}

/// 桥面有界等待：先等至慢告警阈，未完成发一次 `on_slow` 后继续等至总界；
/// 总界到点放弃内层 future 返回 `None`（调用方落结构化错误）。内层 future 经
/// `Pin<&mut F>` 重借用跨段存活——第一段超时只丢弃重借用，不丢弃等待对象。
pub(super) async fn bounded_with_slow_warn<F: std::future::Future>(
    fut: F,
    total: Duration,
    slow_after: Duration,
    on_slow: impl FnOnce(Duration),
) -> Option<F::Output> {
    let mut fut = std::pin::pin!(fut);
    let first = slow_after.min(total);
    match tokio::time::timeout(first, &mut fut).await {
        Ok(out) => Some(out),
        Err(_) => {
            on_slow(first);
            if total <= first {
                return None;
            }
            tokio::time::timeout(total - first, &mut fut).await.ok()
        }
    }
}

/// state 显式收紧档解析（M3 单一解析点的共享读面，ADR
/// 2026-11-05-timeout-taxonomy-and-merge）：`plugin_call_timeout_seconds`
/// 有限数且 ≥0 才算在场（负值/-1、非有限数按未配置——单次层不提供无限制
/// 出口，无界单次调用即根因本身）。引擎 run 级封顶层（A6）消费同一读面
/// ——state 键全仓只此一处解析。
pub fn state_plugin_call_timeout(state: &Value) -> Option<Duration> {
    state
        .get("plugin_call_timeout_seconds")
        .and_then(|v| v.as_f64())
        .filter(|v| v.is_finite() && *v >= 0.0)
        .map(Duration::from_secs_f64)
}

/// 管道插件单次调用有效界（M3 单一解析点，ADR
/// 2026-11-05-timeout-taxonomy-and-merge M3）：
///
/// - [`ResolvedCallLimit::Fixed`]：state 显式收紧（`plugin_call_timeout_seconds`
///   在场，[`state_plugin_call_timeout`] 单点解析）∩ 基础档取 min——**压过
///   一切**（含 L1 让位），收紧必须显式、静默截断禁止。
/// - [`ResolvedCallLimit::Raisable`]：基础档 = manifest 自报
///   （`capabilities.call_timeout_seconds`，0 值非法按未声明）∪ 缺省档
///   [`agentos_core::traits::DEFAULT_PLUGIN_CALL_TIMEOUT_SECS`]（600s）——
///   **L1 让位**（§5.1）：在飞内层调用声明需求更大时等待 deadline 抬高至
///   now+声明值（max 合成单调不减，恒有界不变量保留——有效界 = 全链最大
///   声明值）。缺省壳只管沉默者：human/approval 等已声明长等待的内层不再被
///   600s 缺省掐断（装机 2026-11-05 叠卡事故的运行期修复本体）。
///
/// 本函数恒产出正时长基础档（「所有路径恒有界」的基础保证）；run 级封顶
/// （预算/配置外层壳，A6）在引擎侧嵌套合成，各管一事：基础界归插件调用
/// 模块，run 级知识（预算锚）归引擎。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ResolvedCallLimit {
    /// 显式收紧：固定界，不让位。
    Fixed(Duration),
    /// 缺省档/声明档：可让位抬高的初始界。
    Raisable { initial: Duration },
}

/// 单一解析点（M3）：manifest 自报 ∩ state 显式收紧 → 有效界。
pub fn resolve_call_limit(declared: Option<u64>, state: &Value) -> ResolvedCallLimit {
    let base = declared
        .filter(|s| *s > 0)
        .map(Duration::from_secs)
        .unwrap_or(Duration::from_secs(
            agentos_core::traits::DEFAULT_PLUGIN_CALL_TIMEOUT_SECS,
        ));
    match state_plugin_call_timeout(state) {
        Some(configured) => ResolvedCallLimit::Fixed(configured.min(base)),
        None => ResolvedCallLimit::Raisable { initial: base },
    }
}

/// L1 让位源登记面：`pipeline_id → {声明需求(秒): 在飞计数}`。
///
/// 登记点 = [`PluginInvoker::invoke_tool`]（内核侧唯一能解析被调工具声明界
/// 的单点）；消费点 = 管道步壳的让位查询（同 pipeline_id 的在飞最大声明
/// 需求）。计数多集语义：并行 for-each 迭代各自登记/递减，max 只看键集。
#[derive(Default)]
pub(crate) struct RaisedCallNeeds {
    in_flight: RwLock<HashMap<String, HashMap<u64, usize>>>,
}

impl RaisedCallNeeds {
    /// 登记一单在飞内层声明需求（RAII：guard drop 递减；调用 future 被
    /// 超时放弃时 guard 随局部变量 drop 同样递减——登记面不漏）。
    pub(crate) fn register(&self, pipeline_id: &str, need_secs: u64) -> RaisedNeedGuard<'_> {
        *self
            .in_flight
            .write()
            .entry(pipeline_id.to_string())
            .or_default()
            .entry(need_secs)
            .or_insert(0) += 1;
        RaisedNeedGuard {
            registry: self,
            pipeline_id: pipeline_id.to_string(),
            need_secs,
        }
    }

    /// 该管道当前在飞的最大声明需求（秒）。
    pub(crate) fn max_in_flight(&self, pipeline_id: &str) -> Option<u64> {
        self.in_flight
            .read()
            .get(pipeline_id)
            .and_then(|m| m.keys().copied().max())
    }
}

/// RAII 登记 guard：drop 递减在飞计数，键集空则摘除管道条目。
pub(crate) struct RaisedNeedGuard<'a> {
    registry: &'a RaisedCallNeeds,
    pipeline_id: String,
    need_secs: u64,
}

impl Drop for RaisedNeedGuard<'_> {
    fn drop(&mut self) {
        let mut map = self.registry.in_flight.write();
        if let Some(needs) = map.get_mut(&self.pipeline_id) {
            if let Some(count) = needs.get_mut(&self.need_secs) {
                *count -= 1;
                if *count == 0 {
                    needs.remove(&self.need_secs);
                }
            }
            if needs.is_empty() {
                map.remove(&self.pipeline_id);
            }
        }
    }
}

/// 从工具调用参数解析让位登记键：`_call_context.pipeline_id` 在场（非空）且
/// 声明毫秒界折算秒 ≥1 才登记（亚秒界对缺省壳无让位意义）。
pub(crate) fn raised_need_from_inputs(
    inputs: &Value,
    bound_ms: Option<u64>,
) -> Option<(String, u64)> {
    let pipeline_id = inputs
        .get("_call_context")
        .and_then(|c| c.get("pipeline_id"))
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())?
        .to_string();
    let secs = bound_ms? / 1000;
    if secs == 0 {
        return None;
    }
    Some((pipeline_id, secs))
}

/// 管道插件单次调用的有效界包装（L1 让位 + M3 解析点消费，ADR
/// 2026-11-05-timeout-taxonomy-and-merge）：
///
/// - [`ResolvedCallLimit::Fixed`]：固定界到点放弃内层 future（tokio drop 取消，
///   其持有的 in-flight guard 随 drop 回落）。
/// - [`ResolvedCallLimit::Raisable`]：可抬高 deadline——deadline 自 now+initial
///   起步；`raise_lookup`（在飞内层最大声明需求）报告更大需求时 deadline 抬高
///   至 now+声明值（max 合成单调不减）。deadline 到点先重查让位源（内层声明
///   可能恰在到点前沿登记），无可让位才放弃内层返回 STEP_CALL_TIMEOUT 结构化
///   错误——与引擎 run 级封顶层同码（错误面单点可辨），经调用方中央错误广播
///   点进观察面。
///
/// 时钟纪律：deadline 算术全走 `tokio::time::Instant`（测试 paused 钟可驱动）。
pub(super) async fn with_raisable_call_timeout<T>(
    limit: ResolvedCallLimit,
    raise_lookup: Option<impl Fn() -> Option<Duration>>,
    plugin_id: &str,
    fut: impl std::future::Future<Output = Result<T, PluginError>>,
) -> Result<T, PluginError> {
    match limit {
        ResolvedCallLimit::Fixed(call_limit) => {
            with_fixed_call_timeout(call_limit, plugin_id, fut).await
        }
        ResolvedCallLimit::Raisable { initial } => {
            let mut fut = std::pin::pin!(fut);
            let start = tokio::time::Instant::now();
            let mut deadline = start + initial;
            loop {
                // L1 让位：在飞内层声明需求更大 → 抬高 deadline（单调不减）。
                if let Some(lookup) = &raise_lookup {
                    if let Some(need) = lookup() {
                        let raised = tokio::time::Instant::now() + need;
                        if raised > deadline {
                            deadline = raised;
                        }
                    }
                }
                let now = tokio::time::Instant::now();
                if now >= deadline {
                    return Err(step_call_timeout_error(
                        plugin_id,
                        initial,
                        deadline - start,
                    ));
                }
                match tokio::time::timeout(deadline - now, &mut fut).await {
                    Ok(result) => return result,
                    // 到点：重查让位源（抬高则继续等，否则下一轮 now >= deadline 报错）。
                    Err(_elapsed) => continue,
                }
            }
        }
    }
}

/// 固定界包装（显式收紧臂，不让位）：到点放弃挂死 future 并返回
/// STEP_CALL_TIMEOUT 结构化错误；限内完成原样透传。
async fn with_fixed_call_timeout<T>(
    call_limit: Duration,
    plugin_id: &str,
    fut: impl std::future::Future<Output = Result<T, PluginError>>,
) -> Result<T, PluginError> {
    match tokio::time::timeout(call_limit, fut).await {
        Ok(result) => result,
        Err(_) => Err(step_call_timeout_error(plugin_id, call_limit, call_limit)),
    }
}

/// 单次调用超时结构化错误（错误码 STEP_CALL_TIMEOUT，source=plugin_id）。
fn step_call_timeout_error(plugin_id: &str, initial: Duration, effective: Duration) -> PluginError {
    PluginError {
        message: format!(
            "单次插件调用超时（上界 {effective:?}，初始档 {initial:?}）：invoke 等待被放弃\
             （tokio drop 取消，sidecar 子进程交既有 idle-unload/respawn 兜底）"
        ),
        code: Some(agentos_core::traits::STEP_CALL_TIMEOUT_CODE.to_string()),
        source: Some(plugin_id.to_string()),
    }
}

#[cfg(test)]
mod pipeline_call_limit_tests {
    use super::*;
    use serde_json::json;

    /// 解析矩阵（M3 单一解析点）：manifest 自报 > 缺省档；state 显式收紧
    /// （Fixed，min 语义）；0/-1/缺席各按哨兵语义处理；全缺席落缺省档 600s。
    #[test]
    fn resolve_matrix() {
        let empty = serde_json::json!({});
        // 全缺席 → 缺省档 600s，可让位（缺省壳只管沉默者）。
        assert_eq!(
            resolve_call_limit(None, &empty),
            ResolvedCallLimit::Raisable {
                initial: Duration::from_secs(600)
            }
        );
        // manifest 自报抬高基础档（大模型调用类插件声明 1800）。
        assert_eq!(
            resolve_call_limit(Some(1800), &empty),
            ResolvedCallLimit::Raisable {
                initial: Duration::from_secs(1800)
            }
        );
        // manifest 自报 0 = 非法哨兵，按未声明落缺省档。
        assert_eq!(
            resolve_call_limit(Some(0), &empty),
            ResolvedCallLimit::Raisable {
                initial: Duration::from_secs(600)
            }
        );
        // state 显式收紧 = Fixed（压过一切，不让位）：30s < manifest 1800 → 30s。
        let tight = serde_json::json!({ "plugin_call_timeout_seconds": 30 });
        assert_eq!(
            resolve_call_limit(Some(1800), &tight),
            ResolvedCallLimit::Fixed(Duration::from_secs(30))
        );
        // state 收紧 ≥ 声明档：min 语义取声明档（Fixed 1800）。
        assert_eq!(
            resolve_call_limit(Some(30), &tight),
            ResolvedCallLimit::Fixed(Duration::from_secs(30))
        );
        // state 负值（-1 无限制哨兵）按未配置 → 声明档可让位。
        let neg = serde_json::json!({ "plugin_call_timeout_seconds": -1 });
        assert_eq!(
            resolve_call_limit(Some(1800), &neg),
            ResolvedCallLimit::Raisable {
                initial: Duration::from_secs(1800)
            }
        );
        // state 负值且无声明 → 缺省档。
        assert_eq!(
            resolve_call_limit(None, &neg),
            ResolvedCallLimit::Raisable {
                initial: Duration::from_secs(600)
            }
        );
    }

    /// state 解析单面（M3 共享读面）：负值/非有限数/缺席按未配置；0 在场。
    #[test]
    fn state_parse_sentinels() {
        assert_eq!(state_plugin_call_timeout(&json!({})), None);
        assert_eq!(
            state_plugin_call_timeout(&json!({"plugin_call_timeout_seconds": 90})),
            Some(Duration::from_secs(90))
        );
        assert_eq!(
            state_plugin_call_timeout(&json!({"plugin_call_timeout_seconds": 0})),
            Some(Duration::ZERO)
        );
        assert_eq!(
            state_plugin_call_timeout(&json!({"plugin_call_timeout_seconds": -1})),
            None
        );
        assert_eq!(
            state_plugin_call_timeout(&json!({"plugin_call_timeout_seconds": "x"})),
            None
        );
    }

    /// 让位登记面：登记/递减多集语义——同键多单各自递减，计数归零摘键，
    /// 管道条目清空摘管道。
    #[test]
    fn raised_needs_multiset_register_and_drop() {
        let needs = RaisedCallNeeds::default();
        assert_eq!(needs.max_in_flight("p1"), None);
        let g1 = needs.register("p1", 86_400);
        assert_eq!(needs.max_in_flight("p1"), Some(86_400));
        let g2 = needs.register("p1", 86_400);
        let g3 = needs.register("p1", 1_800);
        assert_eq!(needs.max_in_flight("p1"), Some(86_400));
        drop(g1);
        assert_eq!(needs.max_in_flight("p1"), Some(86_400), "仍有在飞同值单");
        drop(g2);
        assert_eq!(needs.max_in_flight("p1"), Some(1_800), "同值计数归零摘键");
        drop(g3);
        assert_eq!(needs.max_in_flight("p1"), None, "管道条目清空摘除");
    }

    /// 登记键解析：_call_context.pipeline_id 在场 + 界折算秒 ≥1；亚秒界/缺
    /// pipeline_id/豁免（None 界）不登记。
    #[test]
    fn raised_need_from_inputs_gates() {
        let ctx = json!({"_call_context": {"pipeline_id": "pipe-1"}});
        assert_eq!(
            raised_need_from_inputs(&ctx, Some(86_400_000)),
            Some(("pipe-1".to_string(), 86_400))
        );
        // 亚秒界（<1s）不登记。
        assert_eq!(raised_need_from_inputs(&ctx, Some(500)), None);
        // 豁免（None 界）不登记。
        assert_eq!(raised_need_from_inputs(&ctx, None), None);
        // 缺 pipeline_id 不登记。
        assert_eq!(raised_need_from_inputs(&json!({}), Some(86_400_000)), None);
        // 空 pipeline_id 不登记。
        let empty_pid = json!({"_call_context": {"pipeline_id": ""}});
        assert_eq!(raised_need_from_inputs(&empty_pid, Some(86_400_000)), None);
    }

    /// 基础界包装（Fixed 臂）：到点放弃挂死 future 并返回 STEP_CALL_TIMEOUT
    /// 结构化错误（与引擎 run 级封顶层同码）；限内完成原样透传。真实钟小
    /// 时长（毫秒档上界），不起 paused 钟——挂死 future 无定时器唤醒点，
    /// advance 驱不动。
    #[tokio::test]
    async fn wrapper_cuts_hang_at_limit_and_passes_through_within() {
        use std::sync::atomic::{AtomicBool, Ordering};
        use std::sync::Arc;

        // 限内完成：原样透传，不产生错误。
        let ok: Result<u8, PluginError> = Ok(7);
        let out = with_raisable_call_timeout(
            ResolvedCallLimit::Fixed(Duration::from_secs(1)),
            None::<fn() -> Option<Duration>>,
            "p_a",
            async {
                tokio::time::sleep(Duration::from_millis(10)).await;
                ok
            },
        )
        .await
        .expect("限内完成不得被切");
        assert_eq!(out, 7);

        // 到点切挂：错误码 STEP_CALL_TIMEOUT、source=plugin_id；内层 entered
        // 置位后永久挂起（模拟 sidecar 卡死）。
        let entered = Arc::new(AtomicBool::new(false));
        let entered2 = entered.clone();
        let err = with_raisable_call_timeout(
            ResolvedCallLimit::Fixed(Duration::from_millis(50)),
            None::<fn() -> Option<Duration>>,
            "p_hang",
            async move {
                entered2.store(true, Ordering::SeqCst);
                std::future::pending::<Result<u8, PluginError>>().await
            },
        )
        .await
        .expect_err("到点必须切挂死调用");
        assert!(entered.load(Ordering::SeqCst));
        assert_eq!(
            err.code.as_deref(),
            Some(agentos_core::traits::STEP_CALL_TIMEOUT_CODE)
        );
        assert_eq!(err.source.as_deref(), Some("p_hang"));
    }

    /// 三向用例·② 声明者让位（L1 本体，§5.1）：Raisable 档下在飞内层声明
    /// 需求（human 86400s 形态）大于初始缺省档，壳在缺省档到点后**让位继续
    /// 等**，内层完成时原样透传——装机 2026-11-05 叠卡事故的回归面：无让位
    /// 的旧实现（固定 600s 壳）在本用例即红。
    ///
    /// 同为 L2 动态面启动期回归断言（§5.2）：新包裹层忘接让位路径（把
    /// Raisable 实现回固定 timeout）本用例永久红。
    #[tokio::test(start_paused = true)]
    async fn raisable_shell_yields_to_inflight_declared_need() {
        let raised: Arc<parking_lot::Mutex<Option<Duration>>> =
            Arc::new(parking_lot::Mutex::new(None));
        // 内层：1s 时置位声明需求（模拟内层工具调用的声明登记晚于壳进入），
        // 2s 时完成——若无让位，600s 档虽不至于提前切，但本用例用短档放大：
        // 初始档 100ms，声明让位 10s，内层 300ms 完成。
        let initial = Duration::from_millis(100);
        let need = Duration::from_secs(10);
        let raised_for_lookup = raised.clone();
        let lookup = move || *raised_for_lookup.lock();
        let inner_started = tokio::time::Instant::now();
        let fut = async {
            tokio::time::sleep(Duration::from_millis(50)).await;
            *raised.lock() = Some(need);
            tokio::time::sleep(Duration::from_millis(250)).await;
            Ok::<u8, PluginError>(9)
        };
        let out = with_raisable_call_timeout(
            ResolvedCallLimit::Raisable { initial },
            Some(lookup),
            "p_human",
            fut,
        )
        .await
        .expect("声明让位后内层必须活到完成");
        assert_eq!(out, 9);
        assert!(
            tokio::time::Instant::now() - inner_started >= Duration::from_millis(300),
            "内层完整跑完（100ms 初始档 < 300ms 内层时长——让位必须生效）"
        );
    }

    /// 三向用例·③ 显式收紧压过：Fixed 档不让位——在飞声明需求更大也照切
    /// （收紧必须显式、静默截断禁止的另一面：显式收紧即运营者明示压制）。
    #[tokio::test(start_paused = true)]
    async fn fixed_shell_ignores_inflight_declared_need() {
        let lookup = || Some(Duration::from_secs(86_400));
        let err = with_raisable_call_timeout(
            ResolvedCallLimit::Fixed(Duration::from_millis(100)),
            Some(lookup),
            "p_tight",
            async { std::future::pending::<Result<u8, PluginError>>().await },
        )
        .await
        .expect_err("显式收紧必须压过在飞声明需求");
        assert_eq!(
            err.code.as_deref(),
            Some(agentos_core::traits::STEP_CALL_TIMEOUT_CODE)
        );
    }

    /// 三向用例·① 沉默者缺省：Raisable 档在飞无声明需求时按初始档到点切挂
    /// （缺省壳照常管辖未声明内层——事故里 600s 管沉默 compute 的本意保留）。
    #[tokio::test(start_paused = true)]
    async fn raisable_shell_cuts_silent_inner_at_initial() {
        let lookup = || None;
        let err = with_raisable_call_timeout(
            ResolvedCallLimit::Raisable {
                initial: Duration::from_millis(100),
            },
            Some(lookup),
            "p_silent",
            async { std::future::pending::<Result<u8, PluginError>>().await },
        )
        .await
        .expect_err("沉默内层按初始档到点切");
        assert_eq!(
            err.code.as_deref(),
            Some(agentos_core::traits::STEP_CALL_TIMEOUT_CODE)
        );
        assert!(err.message.contains("100ms"), "错误面须可辨有效上界");
    }

    /// 让位单调不减：deadline 只升不降——声明需求从大变小（或消失）不得缩回
    /// 初始档（恒有界不变量的单调面）。
    #[tokio::test(start_paused = true)]
    async fn raisable_deadline_never_shrinks() {
        let raised: Arc<parking_lot::Mutex<Option<Duration>>> =
            Arc::new(parking_lot::Mutex::new(Some(Duration::from_secs(10))));
        let raised_for_lookup = raised.clone();
        let lookup = move || *raised_for_lookup.lock();
        let fut = async {
            // 50ms 后让位源清零（内层完成登记递减）；壳不得因此缩回 100ms
            // 初始档——内层在 300ms 完成须活到那一刻。
            tokio::time::sleep(Duration::from_millis(50)).await;
            *raised.lock() = None;
            tokio::time::sleep(Duration::from_millis(250)).await;
            Ok::<u8, PluginError>(1)
        };
        let out = with_raisable_call_timeout(
            ResolvedCallLimit::Raisable {
                initial: Duration::from_millis(100),
            },
            Some(lookup),
            "p_mono",
            fut,
        )
        .await
        .expect("deadline 已抬高的壳不得因让位源消失而缩档");
        assert_eq!(out, 1);
    }
}
