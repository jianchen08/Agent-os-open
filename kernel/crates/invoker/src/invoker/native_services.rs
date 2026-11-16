//! 内核侧 HostServices 实现：包 CapabilityRouter 供原生插件反调 capability
//! （C-ABI 同步桥，跨分配器缓冲契约）。
//!
//! 自 invoker.rs 按关注点纯搬运拆出（2026-10-12 文件长度机械闸首个应用案例，
//! ADR docs/decisions/2026-10-12-file-length-mechanical-gate.md）。

use std::time::Duration;

use super::timeout::{bounded_with_slow_warn, bridge_timeout_ms};

use super::*;

/// 内核侧 HostServices 实现：包 `CapabilityRouter`，供原生插件调 capability。
///
/// 与 sidecar（JSON-RPC 反调）走同一 router，两轨对齐。
/// trait 方法是 sync（插件经 C-ABI 同步调），内部用 block_in_place + block_on
/// 跑 async router.handle。
///
/// G6：`plugin_id` 是信任锚点（invoker 从 manifest 注入，插件无法伪造），
/// 每次调用自动写入 params._plugin_id——与 sidecar 的 PluginScopedRouter 同构，
/// 使 CapabilityRouter 单点授权校验对两轨同判。
///
/// 反向调用租户透传（刀1）：`identity` 由调用路径注入（invoke_native_pipeline
/// 自 PluginContext 携带——内核侧 origin，天然可信），在场时每次反调以
/// `agentos_tenant::scope` 包裹 router.handle，内核读侧按调用管道租户解析；
/// None（native 工具路径无执行上下文）→ 现状回退语义（与 sidecar 桥的
/// 无租户语义对齐，见 PluginScopedRouter 文档）。
pub(super) struct NativeHostServices {
    pub(super) router: Arc<dyn CapabilityRouter>,
    /// 调用方插件 id（信任锚点，G6 授权粒度统一）。
    pub(super) plugin_id: String,
    /// 执行上下文身份原头（内核下发，经 [`TenantContext::inherit`] 全量继承
    /// 三元组 tenant/session/user；None = 无租户路径，如 invoke_tool 只有
    /// inputs）。A1 统一收口：原 `(tenant_id, session_id)` 两元组丢 user 维，
    /// 反调守卫（resume_pipeline / session.create）fail-fast——升级为整只
    /// [`TenantContext`]，反调 scope 与父执行上下文同身份。
    pub(super) identity: Option<agentos_core::types::TenantContext>,
    /// G7：execute 是否跑在 spawn_blocking 线程上。blocking 线程不是
    /// runtime worker，`block_in_place` 在其上会 panic——改用直接
    /// `Handle::block_on`（blocking 线程不参与调度，阻塞安全）。
    pub(super) on_blocking_thread: bool,
    /// capability 调用结果缓冲（**exe 堆**，本结构持有）。跨分配器契约：
    /// `call_capability` 把结果写进这里再借 `&str` 给插件——exe 分配、exe 释放，
    /// 零跨界所有权转移。线程安全：execute 在 blocking 线程串行（同 host 一次
    /// 一个调用），`&self` 独占借用期间写读不重叠，UnsafeCell 足够。
    pub(super) response_buf: std::cell::UnsafeCell<String>,
    /// capability 调用**错误**缓冲（**exe 堆**，本结构持有）。Err 分支同契约
    /// （2026-09-02 收口）：错误消息写这里再借 `&str`——旧 `Err(String)` 把 exe
    /// 堆 String 交 dll 侧 drop = 反方向跨堆 free UB，真机 SIGSEGV 实测。
    pub(super) err_buf: std::cell::UnsafeCell<String>,
    /// 插件加载器（同步 `get_manifest` 读被调方声明，供等待界解析）。
    pub(super) loader: Arc<dyn PluginLoader>,
    /// 反调等待界覆盖（毫秒）。`None` = 按 [`bridge_timeout_ms`] 解析（生产
    /// 构造恒 None）；`Some(v)` = 测试注入固定界（短超时路径测试用，禁真等）。
    pub(super) bridge_timeout_override_ms: Option<u64>,
    /// 慢调用告警阈。等待超过该值先发一次 warn（含已等待秒数）再继续等至
    /// 等待界——生产恒 [`NATIVE_BRIDGE_SLOW_CALL_WARN_AFTER`]，测试注入短阈。
    pub(super) slow_call_warn_after: Duration,
}

/// 原生反调慢调用告警阈：等待超过 5s 即 warn（capability/method/已等待秒数），
/// 不中断等待——保证同步桥上的长时间驻留在日志面可见。
pub(super) const NATIVE_BRIDGE_SLOW_CALL_WARN_AFTER: Duration = Duration::from_secs(5);

// SAFETY: response_buf/err_buf 的 UnsafeCell 仅在 `&self` 独占借用期间写入；借出的 &str
// 由调用方（插件，blocking 线程内同步消费）在下一次同 host 调用前读完。execute 的
// 调用形态=单 blocking 线程串行，无并发写面。
unsafe impl Send for NativeHostServices {}
unsafe impl Sync for NativeHostServices {}

impl agentos_native_sdk::HostServices for NativeHostServices {
    fn call_capability(
        &self,
        capability: &str,
        method: &str,
        params_json: &str,
    ) -> Result<&str, &str> {
        let router = Arc::clone(&self.router);
        let cap = capability.to_string();
        let mth = method.to_string();
        // G6：注入 _plugin_id 信任锚点（插件侧参数不可覆盖——已存在时以内核注入为准）。
        // 入参非合法 JSON = 插件侧调用缺陷，早失败回传根因，不得静默降级为 Null 续跑。
        let mut params: Value = match serde_json::from_str(params_json) {
            Ok(v) => v,
            Err(e) => {
                let buf = unsafe { &mut *self.err_buf.get() };
                buf.clear();
                buf.push_str(&format!(
                    "capability {capability}.{method} params JSON invalid: {e}"
                ));
                return Err(buf.as_str());
            }
        };
        if let Value::Object(ref mut map) = params {
            map.insert(
                "_plugin_id".to_string(),
                Value::String(self.plugin_id.clone()),
            );
        } else {
            let mut map = serde_json::Map::new();
            map.insert(
                "_plugin_id".to_string(),
                Value::String(self.plugin_id.clone()),
            );
            params = Value::Object(map);
        }
        // sync→async（G7 上下文感知）：
        // - async worker 线程：block_in_place 让出 worker 再 block_on，避免死锁；
        // - spawn_blocking 线程：直接 block_on（blocking 线程不参与调度，阻塞安全；
        //   block_in_place 在非 worker 线程会 panic）。
        // 等待有界（桥面护栏）：同步桥整体包界——被调 capability 挂死时到点放弃
        // 内层 future 返回结构化错误，execute 线程（exec_lock 全程持有）不再无限
        // 驻留；超慢告警阈未完成先发一次 warn 保证日志面可见。
        let timeout_ms = self.bridge_timeout_override_ms.unwrap_or_else(|| {
            bridge_timeout_ms(self.loader.as_ref(), capability, method, &params)
        });
        tracing::info!(
            target: "native-bridge",
            caller = %self.plugin_id,
            capability = %capability,
            method = %method,
            timeout_ms = timeout_ms,
            "native capability call enter (bounded sync bridge)"
        );
        let started = std::time::Instant::now();
        // 反向调用身份透传（刀1 + A1 统一收口）：身份在场 → router.handle 跑在
        // 继承 scope 内（全量三元组 tenant/session/user——user 缺席会使反调
        // 守卫 fail-fast；内核侧 origin 注入，不经 params——native 桥直连共享
        // 路由器，params 注入会泄漏为业务参数）。两臂 async block 类型不同
        // （scope 包装），统一装箱为 trait 对象。
        let fut: std::pin::Pin<Box<dyn std::future::Future<Output = _> + Send>> =
            match &self.identity {
                Some(inherited) => {
                    let tenant = inherited.clone();
                    let inner = async move { router.handle(&cap, &mth, params).await };
                    Box::pin(async move { agentos_tenant::scope(tenant, inner).await })
                }
                None => Box::pin(async move { router.handle(&cap, &mth, params).await }),
            };
        let slow_after = self.slow_call_warn_after;
        let caller = self.plugin_id.clone();
        let cap_name = capability.to_string();
        let mth_name = method.to_string();
        let wait = bounded_with_slow_warn(
            fut,
            Duration::from_millis(timeout_ms),
            slow_after,
            move |_stage| {
                tracing::warn!(
                    target: "native-bridge",
                    caller = %caller,
                    capability = %cap_name,
                    method = %mth_name,
                    waited_secs = started.elapsed().as_secs_f64(),
                    "native capability call still pending beyond slow-call threshold"
                );
            },
        );
        let result = if self.on_blocking_thread {
            tokio::runtime::Handle::current().block_on(wait)
        } else {
            tokio::task::block_in_place(|| tokio::runtime::Handle::current().block_on(wait))
        };
        let elapsed_ms = started.elapsed().as_millis();
        match result {
            Some(Ok(v)) => {
                tracing::debug!(
                    target: "native-bridge",
                    caller = %self.plugin_id,
                    capability = %capability,
                    method = %method,
                    elapsed_ms = elapsed_ms as u64,
                    "native capability call done"
                );
                // 跨分配器契约：结果写进自我持有的缓冲（exe 堆），返回 &str 借用——
                // dll 只读消费（立即转 Value），不存引用、不释放。旧 `String` 返回会
                // 把 exe 分配的串交 dll 侧 drop = 反方向跨堆 free UB。
                // SAFETY(跨分配器契约)：response_buf 由本结构（exe 侧）持有，
                // 此处写、借出 &str 给 dll 只读——分配/释放全在 exe 堆。
                // 并发安全性：execute 在单个 blocking 线程串行，同 host 的
                // call_capability 不重入（tool_core 同轮串行调用），&self 已独占。
                let buf = unsafe { &mut *self.response_buf.get() };
                buf.clear();
                buf.push_str(&serde_json::to_string(&v).unwrap_or_default());
                Ok(buf.as_str())
            }
            Some(Err(e)) => {
                tracing::debug!(
                    target: "native-bridge",
                    caller = %self.plugin_id,
                    capability = %capability,
                    method = %method,
                    elapsed_ms = elapsed_ms as u64,
                    "native capability call done (router error)"
                );
                // Err 分支同契约（2026-09-02 收口）：错误消息写 err_buf（exe 堆）
                // 借 &str 返回——dll 只读消费（立即拷贝/格式化），不持有、不释放。
                // 旧 `Err(format!("{e}"))` 把 exe 堆 String 交 dll 侧 drop = 反方向
                // 跨堆 free UB（真机 SIGSEGV 实测，与 execute 的 Err(String) 同根）。
                let buf = unsafe { &mut *self.err_buf.get() };
                buf.clear();
                buf.push_str(&format!("{e}"));
                Err(buf.as_str())
            }
            None => {
                // 等待界到点：内层 future 已被放弃（其持有的锁/计数随 drop 回落）。
                // 结构化错误走既有 err_buf 契约回传——插件以失败收尾，管道可继续，
                // 不静默驻留。错误含 capability/method/等待时长，直接定位挂点。
                tracing::warn!(
                    target: "native-bridge",
                    caller = %self.plugin_id,
                    capability = %capability,
                    method = %method,
                    waited_ms = elapsed_ms as u64,
                    "native capability call exceeded bridge wait bound"
                );
                let buf = unsafe { &mut *self.err_buf.get() };
                buf.clear();
                buf.push_str(&format!(
                    "capability {capability}.{method} bridge wait timed out after {elapsed_ms}ms"
                ));
                Err(buf.as_str())
            }
        }
    }
}
