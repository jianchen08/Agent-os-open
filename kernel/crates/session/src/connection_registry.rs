//! 连接注册表——user_id/thread_id → 连接集合，多端并存 + 配额超限踢旧
//!（ADR 2026-10-01-multi-frontend-connection，取代 §7.2 B10 单连接互斥）。
//!
//! 参考 0.1 `ws_handler.py:38,143`（`_global_connections` + `register_global`）。
//! 每个 user 可同时持有多个 WS 连接（多浏览器标签/多机器并存），事件按连接
//! 扇出；每 user 连接数上限 [`DEFAULT_MAX_CONNS_PER_USER`]（环境变量
//! `AGENTOS_MAX_CONNS_PER_USER` 可配），超限 LRU 踢最旧——既有两段式踢旧
//! 协议（kicked 帧 + Close 4000）转作超限踢旧通道，配额内不踢。
//!
//! thread 三映射（user/pipeline/agent）带 TTL（整改 R-2）：REST 建会话
//! 无条件三写而清理只挂 WS 最后一条连接注销——从未建 WS 的条目原先只增
//! 不减。TTL 兜底回收这类无 WS 生命周期锚点的条目：写入/成功投递续期，
//! 读路径惰性过期 + 清理节拍 sweep（见 [`ConnectionRegistry::sweep_expired_threads`]）。

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use parking_lot::RwLock;

use crate::EventSink;

/// 单 user 的连接集合：conn_id → sink（多端并存的原子单位）。
type UserConnections = HashMap<u64, Arc<dyn EventSink>>;

/// 每 user 连接数缺省上限（AGENTOS_MAX_CONNS_PER_USER 未配置时生效）。
pub const DEFAULT_MAX_CONNS_PER_USER: usize = 4;

/// thread 映射 TTL 缺省（秒）= 24h（AGENTOS_THREAD_MAP_TTL_SECS 未配置时生效）。
pub const DEFAULT_THREAD_MAP_TTL_SECS: u64 = 86_400;

/// 时钟源（TTL 判定用；生产恒 `Instant::now`，测试注入 fake clock 断言过期）。
type Clock = Arc<dyn Fn() -> Instant + Send + Sync>;

/// 解析每 user 连接数上限：环境变量 `AGENTOS_MAX_CONNS_PER_USER`，非法/非正值
/// 一律回退缺省 4（0 会使注册表无法容纳任何连接，视同非法）。
fn resolve_max_conns_per_user(raw: Option<&str>) -> usize {
    raw.and_then(|v| v.trim().parse::<usize>().ok())
        .filter(|&n| n > 0)
        .unwrap_or(DEFAULT_MAX_CONNS_PER_USER)
}

/// 解析 thread 映射 TTL 秒：环境变量 `AGENTOS_THREAD_MAP_TTL_SECS`，非法/非正
/// 值一律回退缺省 24h（0 会使条目即写即废，视同非法）。
fn resolve_thread_map_ttl_secs(raw: Option<&str>) -> u64 {
    raw.and_then(|v| v.trim().parse::<u64>().ok())
        .filter(|&s| s > 0)
        .unwrap_or(DEFAULT_THREAD_MAP_TTL_SECS)
}

/// 单 thread 的三映射条目：user/pipeline/agent 同生命周期同 TTL，统一续期。
struct ThreadEntry {
    /// thread_id → user_id（仅持有 thread_id 的流式发送路径反查用）。
    user: Option<String>,
    /// thread_id → pipeline_id（创建会话时回填，REST 会话列表/详情据此返回给前端）。
    pipeline: Option<String>,
    /// thread_id → agent_id（创建会话或绑定 Agent 时写入）。
    agent: Option<String>,
    /// 最近一次写入/成功投递时刻（TTL 续期锚点）。
    last_touch: Instant,
}

/// 连接注册表：user_id → 连接集合（conn_id → sink）+ thread 三映射条目。
///
/// thread 条目两个收回路径互补：
/// - **WS 生命周期锚定**：user 名下最后一条连接注销（显式 unregister 或
///   broadcast 清理死连接）时同步清除其全部条目——映射只服务活跃连接的读面
///   （send_to_thread 反查 / REST 会话列表回退），不留历史；仍有存活连接时
///   注销其中一条不得清除（其余连接仍服务同一读面）。
/// - **TTL 兜底**（R-2）：REST 建会话三写但从未建 WS 的条目无注销锚点，
///   距最近一次写入/成功投递超过 TTL（缺省 24h）后由读路径惰性过期与清理
///   节拍 sweep 收回；WS 每帧重注册（幂等）即恢复。
pub struct ConnectionRegistry {
    /// user_id → (conn_id → sink)（多端并存的真相之源）。
    connections: RwLock<HashMap<String, UserConnections>>,
    /// 每 user 连接数上限（超限 LRU 踢最旧）。
    max_conns_per_user: usize,
    /// thread_id → 三映射条目（last_touch + TTL）。
    thread_map: RwLock<HashMap<String, ThreadEntry>>,
    /// thread 条目 TTL。
    thread_ttl: Duration,
    /// 时钟源（测试 fake clock 注入）。
    clock: Clock,
}

impl ConnectionRegistry {
    /// 创建注册表：上限取环境变量 `AGENTOS_MAX_CONNS_PER_USER`（缺省 4）。
    pub fn new() -> Self {
        Self::with_max_conns(resolve_max_conns_per_user(
            std::env::var("AGENTOS_MAX_CONNS_PER_USER").ok().as_deref(),
        ))
    }

    /// 以指定每 user 连接数上限创建（测试/嵌入式注入配额用；下限钳 1）。
    /// TTL 取环境变量 `AGENTOS_THREAD_MAP_TTL_SECS`（缺省 24h），时钟为真实钟。
    pub fn with_max_conns(max_conns_per_user: usize) -> Self {
        Self::with_config(
            max_conns_per_user,
            Duration::from_secs(resolve_thread_map_ttl_secs(
                std::env::var("AGENTOS_THREAD_MAP_TTL_SECS").ok().as_deref(),
            )),
            Arc::new(Instant::now),
        )
    }

    /// 以显式配置创建（TTL/时钟注入用，测试面；下限钳 1）。
    fn with_config(max_conns_per_user: usize, thread_ttl: Duration, clock: Clock) -> Self {
        Self {
            connections: RwLock::new(HashMap::new()),
            max_conns_per_user: max_conns_per_user.max(1),
            thread_map: RwLock::new(HashMap::new()),
            thread_ttl,
            clock,
        }
    }

    /// 注册连接（多端并存：配额内新连接不踢旧）。
    ///
    /// 超 [`Self::max_conns_per_user`] 时按 LRU 踢最旧（conn_id 全局单调递增，
    /// 最小 id = 最早建立的连接），被踢 sink 随返回值交调用方关闭（两段式
    /// kicked 帧 + Close 4000）——注册表自身不 shutdown，超限踢旧计数与关闭
    /// 语义归 [`crate::SessionCoordinator::register`]。
    pub fn register(&self, user_id: &str, sink: Arc<dyn EventSink>) -> Vec<Arc<dyn EventSink>> {
        let mut conns = self.connections.write();
        let entry = conns.entry(user_id.to_string()).or_default();
        entry.insert(sink.id(), sink);
        let mut evicted = Vec::new();
        while entry.len() > self.max_conns_per_user {
            let Some(oldest_id) = entry.keys().copied().min() else {
                break;
            };
            if let Some(old) = entry.remove(&oldest_id) {
                evicted.push(old);
            }
        }
        evicted
    }

    /// 快照 user 的全部活跃连接（事件扇出/诊断读面）。
    pub fn sinks_of_user(&self, user_id: &str) -> Vec<Arc<dyn EventSink>> {
        self.connections
            .read()
            .get(user_id)
            .map(|m| m.values().cloned().collect())
            .unwrap_or_default()
    }

    /// 注销连接（该 user 仍有其他连接时仅移除本条；最后一条注销时连同其名下
    /// 全部 thread 映射条目一并清除）。仅当传入的 sink id 是已注册连接时才
    /// 删除——防止旧连接的 finally 块误删其他连接（参考 0.1
    /// `unregister_global` 的 `current is not websocket` 判定的多连接推广）。
    pub fn unregister(&self, user_id: &str, sink_id: u64) {
        self.remove_connection(user_id, sink_id);
    }

    /// 三映射写入公共件：建/续条目并刷新 last_touch（写即续期，R-2）。
    fn upsert_thread_field(&self, thread_id: &str, set: impl FnOnce(&mut ThreadEntry)) {
        let now = (self.clock)();
        let mut map = self.thread_map.write();
        let entry = map
            .entry(thread_id.to_string())
            .or_insert_with(|| ThreadEntry {
                user: None,
                pipeline: None,
                agent: None,
                last_touch: now,
            });
        set(entry);
        entry.last_touch = now;
    }

    /// 条目是否已过 TTL（clock 单调；checked 防御倒退，倒退视作未过期）。
    fn entry_expired(&self, entry: &ThreadEntry, now: Instant) -> bool {
        now.checked_duration_since(entry.last_touch)
            .is_some_and(|elapsed| elapsed > self.thread_ttl)
    }

    /// 惰性过期读：命中且未过期返回字段；已过期则写锁复核后**整条收回**
    /// （double-check 防并发续期/重注册被误删）。
    fn get_thread_field<T>(
        &self,
        thread_id: &str,
        field: impl Fn(&ThreadEntry) -> Option<T>,
    ) -> Option<T> {
        let now = (self.clock)();
        {
            let map = self.thread_map.read();
            match map.get(thread_id) {
                Some(entry) if !self.entry_expired(entry, now) => return field(entry),
                Some(_) => {}
                None => return None,
            }
        }
        let mut map = self.thread_map.write();
        if let Some(entry) = map.get(thread_id) {
            if !self.entry_expired(entry, now) {
                return field(entry);
            }
        }
        map.remove(thread_id);
        None
    }

    /// 建立 thread_id → user_id 逻辑映射（流式发送路径反查用）。
    /// 幂等：WS 每帧重注册同键即续期（TTL 过期后重注册即恢复）。
    pub fn register_thread(&self, thread_id: &str, user_id: &str) {
        if !thread_id.is_empty() && !user_id.is_empty() {
            self.upsert_thread_field(thread_id, |entry| {
                entry.user = Some(user_id.to_string());
            });
        }
    }

    /// 反查 thread_id 对应的 user_id（已过期条目惰性收回并返回 None）。
    pub fn get_user_for_thread(&self, thread_id: &str) -> Option<String> {
        self.get_thread_field(thread_id, |entry| entry.user.clone())
    }

    /// 建立 thread_id → pipeline_id 映射（创建会话时回填）。
    /// 前端发消息需要 pipeline_id 作 WS 路由键，REST 会话列表/详情据此返回。
    pub fn register_thread_pipeline(&self, thread_id: &str, pipeline_id: &str) {
        if !thread_id.is_empty() && !pipeline_id.is_empty() {
            self.upsert_thread_field(thread_id, |entry| {
                entry.pipeline = Some(pipeline_id.to_string());
            });
        }
    }

    /// 建立 thread_id → agent_id 映射（创建会话/绑定 Agent 时写入）。
    pub fn register_thread_agent(&self, thread_id: &str, agent_id: &str) {
        if !thread_id.is_empty() && !agent_id.is_empty() {
            self.upsert_thread_field(thread_id, |entry| {
                entry.agent = Some(agent_id.to_string());
            });
        }
    }

    /// 反查 thread_id 对应的 pipeline_id（已过期条目惰性收回并返回 None）。
    pub fn get_pipeline_for_thread(&self, thread_id: &str) -> Option<String> {
        self.get_thread_field(thread_id, |entry| entry.pipeline.clone())
    }

    /// 反查 thread_id 对应的 agent_id（已过期条目惰性收回并返回 None）。
    pub fn get_agent_for_thread(&self, thread_id: &str) -> Option<String> {
        self.get_thread_field(thread_id, |entry| entry.agent.clone())
    }

    /// 移除连接的注销语义（unregister 与 broadcast 死连接清理共用）：
    /// 仅当传入 sink id 在该 user 连接集合内时移除；返回是否移除了该 user
    /// 的**最后一条**连接（thread 映射条目只在此时同步清除）。
    fn remove_connection(&self, user_id: &str, sink_id: u64) -> bool {
        let removed_last = {
            let mut conns = self.connections.write();
            let mut removed_last = false;
            if let Some(entry) = conns.get_mut(user_id) {
                let removed = entry.remove(&sink_id).is_some();
                if entry.is_empty() {
                    if removed {
                        removed_last = true;
                    }
                    conns.remove(user_id);
                }
            }
            removed_last
        };
        if removed_last {
            self.purge_thread_maps(user_id);
        }
        removed_last
    }

    /// 清除 user 名下全部 thread 条目。thread 归属 user 唯一（entry.user 反查），
    /// 条目只服务活跃连接的读面，随该 user 最后一条连接移除（TTL 之外的
    /// WS 生命周期锚定收回路径）。
    fn purge_thread_maps(&self, user_id: &str) {
        self.thread_map
            .write()
            .retain(|_, entry| entry.user.as_deref() != Some(user_id));
    }

    /// 向指定 user 的全部连接扇出一条文本消息（唯一出口 push_to_user 底层）。
    ///
    /// 返回是否至少一条连接投递成功（false = user 不在线 / 全部发送失败）。
    /// 发送失败的连接不在本路径注销——清理归 broadcast 背压兜底与连接自身
    /// 关闭路径（CLOSE_BACKPRESSURE → 排空任务关 socket → finally 注销），
    /// 与单连接时代的语义一致。
    pub async fn send_to_user(&self, user_id: &str, text: &str) -> bool {
        // 快照连接集合，避免持有读锁跨 await
        let sinks = self.sinks_of_user(user_id);
        if sinks.is_empty() {
            return false;
        }
        let mut delivered = 0usize;
        for sink in &sinks {
            if sink.send_text(text).await {
                delivered += 1;
            }
        }
        delivered > 0
    }

    /// 向指定 thread 关联 user 的全部连接扇出消息（反查 thread→user→连接）。
    ///
    /// 投递成功即活跃：续期该 thread 条目（长流式期间无用户输入帧重注册，
    /// TTL 也不得误收在投的 thread）。
    pub async fn send_to_thread(&self, thread_id: &str, text: &str) -> bool {
        let user_id = match self.get_user_for_thread(thread_id) {
            Some(u) => u,
            None => return false,
        };
        let delivered = self.send_to_user(&user_id, text).await;
        if delivered {
            let now = (self.clock)();
            if let Some(entry) = self.thread_map.write().get_mut(thread_id) {
                entry.last_touch = now;
            }
        }
        delivered
    }

    /// 收回全部已过期 thread 条目，返回收回数（R-2 清理节拍入口）。
    ///
    /// 读路径惰性过期只能收回被触达的条目；从未再被读/写的孤儿条目
    /// （REST 建会话后无 WS）由此兜底。挂 agentos-kernel 指标 flush 节拍
    /// 周期调用（每分钟一扫）。
    pub fn sweep_expired_threads(&self) -> usize {
        let now = (self.clock)();
        let mut map = self.thread_map.write();
        let before = map.len();
        map.retain(|_, entry| !self.entry_expired(entry, now));
        before - map.len()
    }

    /// 广播到全部活跃连接（ADR §3.5 第5条 broadcast scope）。
    ///
    /// 返回成功投递的连接数。失败的连接被注销（背压兜底）。
    pub async fn broadcast(&self, text: &str) -> usize {
        // 快照当前连接，避免持有读锁跨 await
        let sinks: Vec<(String, Arc<dyn EventSink>)> = self
            .connections
            .read()
            .iter()
            .flat_map(|(u, m)| m.values().map(move |s| (u.clone(), s.clone())))
            .collect();
        let mut delivered = 0usize;
        // 记录失败的 (user_id, sink_id)，用于精确注销（不误删其他连接）
        let mut dead: Vec<(String, u64)> = Vec::new();
        for (user_id, sink) in &sinks {
            if sink.send_text(text).await {
                delivered += 1;
            } else {
                dead.push((user_id.clone(), sink.id()));
            }
        }
        // 清理发送失败的连接：注销语义（id 比对 + 最后一条连接时清 thread 映射），
        // 与 unregister 共用同一路径
        for (user_id, failed_id) in &dead {
            self.remove_connection(user_id, *failed_id);
        }
        delivered
    }

    /// 当前活跃连接总数（监控 M2：gauge，监控设计 §三 通道1；多端并存下
    /// ≥ user 数）。
    pub fn active_count(&self) -> usize {
        self.connections.read().values().map(HashMap::len).sum()
    }

    /// 内存驻留快照（堆栈级诊断面 GET /api/v1/system/memory-breakdown 消费）。
    ///
    /// 连接数/三映射字段计数为**实测驻留计数**（不做 TTL 过滤——过期未扫的
    /// 条目也真实占内存，扫描滞后应在此可见）；`outbound_queued_frames`
    /// 为各连接出站队列在飞帧数实测和（sink 未披露队列深度时该连接按 0 计）。
    pub fn memory_stats(&self) -> ConnectionMemoryStats {
        let connections = self.connections.read();
        let (thread_user, thread_pipeline, thread_agent) = {
            let map = self.thread_map.read();
            (
                map.values().filter(|e| e.user.is_some()).count(),
                map.values().filter(|e| e.pipeline.is_some()).count(),
                map.values().filter(|e| e.agent.is_some()).count(),
            )
        };
        let mut stats = ConnectionMemoryStats {
            connections: connections.values().map(HashMap::len).sum(),
            thread_user,
            thread_pipeline,
            thread_agent,
            outbound_queued_frames: 0,
        };
        for sink in connections.values().flat_map(|m| m.values()) {
            stats.outbound_queued_frames += sink.pending_frames().unwrap_or(0);
        }
        stats
    }

    /// 枚举当前 thread_id → user_id 映射（供 REST 会话列表端点使用）。
    ///
    /// 只反映存活条目（该 user 最后一条连接注销时同步清除；TTL 过期条目
    /// 不出现——过期收回归 get 读路径与 sweep 节拍），不保留历史会话。
    pub fn list_threads(&self) -> Vec<(String, String)> {
        let now = (self.clock)();
        self.thread_map
            .read()
            .iter()
            .filter(|(_, entry)| !self.entry_expired(entry, now))
            .filter_map(|(tid, entry)| entry.user.clone().map(|user| (tid.clone(), user)))
            .collect()
    }
}

/// [`ConnectionRegistry::memory_stats`] 快照。
#[derive(Debug, Default, Clone, serde::Serialize)]
pub struct ConnectionMemoryStats {
    /// 活跃连接总数（Σ user 连接集合大小，≥ user 数）。
    pub connections: usize,
    /// thread → user 映射条目数。
    pub thread_user: usize,
    /// thread → pipeline 映射条目数。
    pub thread_pipeline: usize,
    /// thread → agent 映射条目数。
    pub thread_agent: usize,
    /// 全部连接出站队列在飞帧数实测和。
    pub outbound_queued_frames: usize,
}

impl Default for ConnectionRegistry {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 不发送任何数据的最小 sink（仅占位验证注册/逐出簿记）。
    struct IdleSink;

    #[async_trait::async_trait]
    impl EventSink for IdleSink {
        async fn send_text(&self, _text: &str) -> bool {
            false
        }
        fn id(&self) -> u64 {
            1
        }
    }

    /// resolve_max_conns_per_user：合法值直取、非法/缺失/非正回退缺省。
    #[test]
    fn resolve_max_conns_falls_back_to_default_on_invalid() {
        assert_eq!(resolve_max_conns_per_user(None), DEFAULT_MAX_CONNS_PER_USER);
        assert_eq!(
            resolve_max_conns_per_user(Some("")),
            DEFAULT_MAX_CONNS_PER_USER
        );
        assert_eq!(
            resolve_max_conns_per_user(Some("abc")),
            DEFAULT_MAX_CONNS_PER_USER
        );
        assert_eq!(
            resolve_max_conns_per_user(Some("0")),
            DEFAULT_MAX_CONNS_PER_USER
        );
        assert_eq!(
            resolve_max_conns_per_user(Some("-2")),
            DEFAULT_MAX_CONNS_PER_USER
        );
        assert_eq!(resolve_max_conns_per_user(Some("2")), 2);
        assert_eq!(resolve_max_conns_per_user(Some(" 8 ")), 8);
    }

    /// with_max_conns 下限钳 1：0 上限会使注册表容不下任何连接。
    #[test]
    fn with_max_conns_clamps_to_at_least_one() {
        let registry = ConnectionRegistry::with_max_conns(0);
        let sink: Arc<dyn EventSink> = Arc::new(IdleSink);
        let evicted = registry.register("user-A", sink);
        assert!(evicted.is_empty(), "钳 1 后首个连接不逐出");
        assert_eq!(registry.active_count(), 1);
    }

    /// resolve_thread_map_ttl_secs：合法直取、非法/缺失/非正回缺省。
    #[test]
    fn resolve_thread_ttl_falls_back_to_default_on_invalid() {
        assert_eq!(
            resolve_thread_map_ttl_secs(None),
            DEFAULT_THREAD_MAP_TTL_SECS
        );
        assert_eq!(
            resolve_thread_map_ttl_secs(Some("")),
            DEFAULT_THREAD_MAP_TTL_SECS
        );
        assert_eq!(
            resolve_thread_map_ttl_secs(Some("abc")),
            DEFAULT_THREAD_MAP_TTL_SECS
        );
        assert_eq!(
            resolve_thread_map_ttl_secs(Some("0")),
            DEFAULT_THREAD_MAP_TTL_SECS
        );
        assert_eq!(
            resolve_thread_map_ttl_secs(Some("-2")),
            DEFAULT_THREAD_MAP_TTL_SECS
        );
        assert_eq!(resolve_thread_map_ttl_secs(Some("3600")), 3600);
        assert_eq!(resolve_thread_map_ttl_secs(Some(" 120 ")), 120);
    }

    /// fake clock：受控单调推进（读闭包与推进端共享同一 Instant 单元，
    /// TTL 时序断言不依赖真实等待）。
    fn fake_clock() -> (Arc<std::sync::Mutex<Instant>>, Clock) {
        let cell: Arc<std::sync::Mutex<Instant>> = Arc::new(std::sync::Mutex::new(Instant::now()));
        let reader: Clock = {
            let c = Arc::clone(&cell);
            Arc::new(move || *c.lock().unwrap())
        };
        (cell, reader)
    }

    /// 带 fake clock 的注册表（TTL 秒显式注入）。
    fn ttl_registry(ttl_secs: u64, clock: &Clock) -> ConnectionRegistry {
        ConnectionRegistry::with_config(4, Duration::from_secs(ttl_secs), Arc::clone(clock))
    }

    /// 三映射过期收回（R-2 主行为）：TTL 内存活；过 TTL 后读路径惰性收回——
    /// 三字段皆 None 且条目不驻留（memory_stats 归零，无泄漏）。
    #[test]
    fn thread_maps_expire_after_ttl_and_lazily_reclaimed() {
        let (clock, now) = fake_clock();
        let registry = ttl_registry(60, &now);
        registry.register_thread("T1", "u1");
        registry.register_thread_pipeline("T1", "p1");
        registry.register_thread_agent("T1", "a1");

        // TTL 内存活（边界内回归）
        *clock.lock().unwrap() += Duration::from_secs(59);
        assert_eq!(registry.get_user_for_thread("T1").as_deref(), Some("u1"));
        assert_eq!(
            registry.get_pipeline_for_thread("T1").as_deref(),
            Some("p1")
        );
        assert_eq!(registry.get_agent_for_thread("T1").as_deref(), Some("a1"));

        // 过 TTL → 惰性整条收回：首读触发移除，后续字段读与驻留计数皆空
        *clock.lock().unwrap() += Duration::from_secs(2);
        assert_eq!(registry.get_user_for_thread("T1"), None);
        assert_eq!(registry.get_pipeline_for_thread("T1"), None);
        assert_eq!(registry.get_agent_for_thread("T1"), None);
        assert!(
            registry.list_threads().is_empty(),
            "过期 thread 不得再进会话列表"
        );
        let stats = registry.memory_stats();
        assert_eq!(
            (stats.thread_user, stats.thread_pipeline, stats.thread_agent),
            (0, 0, 0),
            "过期条目必须整体收回（三映射无泄漏）"
        );
    }

    /// 过期后重注册即恢复（WS 每帧行为），重复注册幂等不累积。
    #[test]
    fn expired_thread_reregister_recovers_idempotently() {
        let (clock, now) = fake_clock();
        let registry = ttl_registry(10, &now);
        registry.register_thread("T2", "u2");
        registry.register_thread("T2", "u2"); // 重复注册（WS 每帧重注册形态）
        assert_eq!(registry.memory_stats().thread_user, 1);

        *clock.lock().unwrap() += Duration::from_secs(11);
        assert_eq!(
            registry.get_user_for_thread("T2"),
            None,
            "过 TTL 必须先失效"
        );
        // TTL 过期后的下一帧行为：重注册即恢复（幂等，无需 WS 重连之外的
        // 任何显式恢复动作）
        registry.register_thread("T2", "u2");
        assert_eq!(registry.get_user_for_thread("T2").as_deref(), Some("u2"));
        assert_eq!(
            registry.memory_stats().thread_user,
            1,
            "重注册不得产生重复条目"
        );
    }

    /// 任一写入刷新条目 TTL（性质断言：自最近一次写/续期的 elapsed ≤ TTL
    /// 恒活，> TTL 必死）。
    #[test]
    fn any_write_refreshes_entry_ttl() {
        let (clock, now) = fake_clock();
        let registry = ttl_registry(10, &now);
        registry.register_thread("T3", "u3");

        // t=9 写 agent 续期 → t=17（距写 8s ≤ 10s）仍活（无续期则 17>10 已死）
        *clock.lock().unwrap() += Duration::from_secs(9);
        registry.register_thread_agent("T3", "a3");
        *clock.lock().unwrap() += Duration::from_secs(8);
        assert_eq!(
            registry.get_user_for_thread("T3").as_deref(),
            Some("u3"),
            "写入必须续期整条目（三字段同 TTL）"
        );
        // 距写 19s > 10s → 死
        *clock.lock().unwrap() += Duration::from_secs(11);
        assert_eq!(registry.get_user_for_thread("T3"), None);
        assert_eq!(registry.get_agent_for_thread("T3"), None);
    }

    /// 清理节拍 sweep 收回孤儿条目（REST 三写后从未有 WS/读路径触达——
    /// 惰性过期够不到的 R-2 泄漏面）；未过期条目不误收。
    #[test]
    fn sweep_reclaims_expired_orphans_without_reads() {
        let (clock, now) = fake_clock();
        let registry = ttl_registry(10, &now);
        registry.register_thread("T4", "u4");
        registry.register_thread_pipeline("T4", "p4");
        registry.register_thread_agent("T4", "a4");

        *clock.lock().unwrap() += Duration::from_secs(11);
        assert_eq!(registry.sweep_expired_threads(), 1, "孤儿条目由 sweep 收回");
        let stats = registry.memory_stats();
        assert_eq!(
            (stats.thread_user, stats.thread_pipeline, stats.thread_agent),
            (0, 0, 0)
        );

        // 未过期条目不误收（性质：sweep 只删已过期）
        registry.register_thread("T5", "u5");
        assert_eq!(registry.sweep_expired_threads(), 0);
        assert_eq!(registry.get_user_for_thread("T5").as_deref(), Some("u5"));
    }

    /// 投递成功的 sink（send_text 恒 true）。
    struct OkSink(u64);

    #[async_trait::async_trait]
    impl EventSink for OkSink {
        async fn send_text(&self, _text: &str) -> bool {
            true
        }
        fn id(&self) -> u64 {
            self.0
        }
    }

    /// send_to_thread 成功投递续期条目（长流式期间无用户输入帧重注册，
    /// TTL 不得误收在投 thread）；投递失败（无连接）不续期。
    #[tokio::test]
    async fn successful_delivery_refreshes_entry_ttl() {
        let (clock, now) = fake_clock();
        let registry = ttl_registry(10, &now);
        registry.register_thread("T6", "u6");
        registry.register("u6", Arc::new(OkSink(1)));

        // t=9 投递成功续期 → t=17（距续期 8s）仍活（无续期则 17>10 已死）
        *clock.lock().unwrap() += Duration::from_secs(9);
        assert!(
            registry.send_to_thread("T6", "ping").await,
            "有活跃连接必须投递成功"
        );
        *clock.lock().unwrap() += Duration::from_secs(8);
        assert_eq!(
            registry.get_user_for_thread("T6").as_deref(),
            Some("u6"),
            "成功投递必须续期条目"
        );

        // 无连接投递失败不续期：距上次续期 19s > 10s → 收回
        *clock.lock().unwrap() += Duration::from_secs(2);
        registry.unregister("u6", 1);
        assert!(
            !registry.send_to_thread("T6", "ping2").await,
            "无连接投递必须失败"
        );
        *clock.lock().unwrap() += Duration::from_secs(9);
        assert_eq!(registry.get_user_for_thread("T6"), None);
    }

    /// 最后一条连接注销仍同步清除条目（既有 WS 生命周期锚定语义回归，
    /// 统一条目重构不改变该行为）；仍有其他连接时不清除。
    #[test]
    fn last_connection_unregister_still_purges_thread_maps() {
        let (_clock, now) = fake_clock();
        let registry = ttl_registry(3600, &now);
        registry.register_thread("T7", "u7");
        registry.register_thread_pipeline("T7", "p7");
        registry.register_thread_agent("T7", "a7");

        let sink_a: Arc<dyn EventSink> = Arc::new(IdleSink);
        let sink_b: Arc<dyn EventSink> = Arc::new(OkSink(9));
        registry.register("u7", sink_a.clone());
        registry.register("u7", sink_b.clone());

        // 注销其一（非最后一条）：条目保留
        registry.unregister("u7", sink_a.id());
        assert_eq!(registry.get_user_for_thread("T7").as_deref(), Some("u7"));

        // 注销最后一条：条目整条清除
        registry.unregister("u7", sink_b.id());
        assert_eq!(registry.get_user_for_thread("T7"), None);
        assert_eq!(registry.get_pipeline_for_thread("T7"), None);
        assert_eq!(registry.get_agent_for_thread("T7"), None);
    }
}
