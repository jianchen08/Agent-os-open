//! 连接注册表——user_id/thread_id → 连接集合，多端并存 + 配额超限踢旧
//!（ADR 2026-10-01-multi-frontend-connection，取代 §7.2 B10 单连接互斥）。
//!
//! 参考 0.1 `ws_handler.py:38,143`（`_global_connections` + `register_global`）。
//! 每个 user 可同时持有多个 WS 连接（多浏览器标签/多机器并存），事件按连接
//! 扇出；每 user 连接数上限 [`DEFAULT_MAX_CONNS_PER_USER`]（环境变量
//! `AGENTOS_MAX_CONNS_PER_USER` 可配），超限 LRU 踢最旧——既有两段式踢旧
//! 协议（kicked 帧 + Close 4000）转作超限踢旧通道，配额内不踢。

use std::collections::HashMap;
use std::sync::Arc;

use parking_lot::RwLock;

use crate::EventSink;

/// 单 user 的连接集合：conn_id → sink（多端并存的原子单位）。
type UserConnections = HashMap<u64, Arc<dyn EventSink>>;

/// 每 user 连接数缺省上限（AGENTOS_MAX_CONNS_PER_USER 未配置时生效）。
pub const DEFAULT_MAX_CONNS_PER_USER: usize = 4;

/// 解析每 user 连接数上限：环境变量 `AGENTOS_MAX_CONNS_PER_USER`，非法/非正值
/// 一律回退缺省 4（0 会使注册表无法容纳任何连接，视同非法）。
fn resolve_max_conns_per_user(raw: Option<&str>) -> usize {
    raw.and_then(|v| v.trim().parse::<usize>().ok())
        .filter(|&n| n > 0)
        .unwrap_or(DEFAULT_MAX_CONNS_PER_USER)
}

/// 连接注册表：user_id → 连接集合（conn_id → sink）+ thread_id → user_id 逻辑映射。
///
/// 三张 thread 映射条目随该 user **最后一条连接**的生命周期：user 名下最后一条
/// 连接注销（显式 unregister 或 broadcast 清理死连接）时同步清除其全部条目——
/// 映射只服务活跃连接的读面（send_to_thread 反查 / REST 会话列表回退），不留
/// 历史；仍有存活连接时注销其中一条不得清除（其余连接仍服务同一读面）。
pub struct ConnectionRegistry {
    /// user_id → (conn_id → sink)（多端并存的真相之源）。
    connections: RwLock<HashMap<String, UserConnections>>,
    /// 每 user 连接数上限（超限 LRU 踢最旧）。
    max_conns_per_user: usize,
    /// thread_id → user_id（仅持有 thread_id 的流式发送路径反查用）。
    thread_user_map: RwLock<HashMap<String, String>>,
    /// thread_id → pipeline_id（创建会话时回填，REST 会话列表/详情据此返回给前端）。
    thread_pipeline_map: RwLock<HashMap<String, String>>,
    /// thread_id → agent_id（创建会话或绑定 Agent 时写入）。
    thread_agent_map: RwLock<HashMap<String, String>>,
}

impl ConnectionRegistry {
    /// 创建注册表：上限取环境变量 `AGENTOS_MAX_CONNS_PER_USER`（缺省 4）。
    pub fn new() -> Self {
        Self::with_max_conns(resolve_max_conns_per_user(
            std::env::var("AGENTOS_MAX_CONNS_PER_USER").ok().as_deref(),
        ))
    }

    /// 以指定每 user 连接数上限创建（测试/嵌入式注入用；下限钳 1）。
    pub fn with_max_conns(max_conns_per_user: usize) -> Self {
        Self {
            connections: RwLock::new(HashMap::new()),
            max_conns_per_user: max_conns_per_user.max(1),
            thread_user_map: RwLock::new(HashMap::new()),
            thread_pipeline_map: RwLock::new(HashMap::new()),
            thread_agent_map: RwLock::new(HashMap::new()),
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

    /// 建立 thread_id → user_id 逻辑映射（流式发送路径反查用）。
    pub fn register_thread(&self, thread_id: &str, user_id: &str) {
        if !thread_id.is_empty() && !user_id.is_empty() {
            self.thread_user_map
                .write()
                .insert(thread_id.to_string(), user_id.to_string());
        }
    }

    /// 反查 thread_id 对应的 user_id。
    pub fn get_user_for_thread(&self, thread_id: &str) -> Option<String> {
        self.thread_user_map.read().get(thread_id).cloned()
    }

    /// 建立 thread_id → pipeline_id 映射（创建会话时回填）。
    /// 前端发消息需要 pipeline_id 作 WS 路由键，REST 会话列表/详情据此返回。
    pub fn register_thread_pipeline(&self, thread_id: &str, pipeline_id: &str) {
        if !thread_id.is_empty() && !pipeline_id.is_empty() {
            self.thread_pipeline_map
                .write()
                .insert(thread_id.to_string(), pipeline_id.to_string());
        }
    }

    /// 建立 thread_id → agent_id 映射（创建会话/绑定 Agent 时写入）。
    pub fn register_thread_agent(&self, thread_id: &str, agent_id: &str) {
        if !thread_id.is_empty() && !agent_id.is_empty() {
            self.thread_agent_map
                .write()
                .insert(thread_id.to_string(), agent_id.to_string());
        }
    }

    /// 反查 thread_id 对应的 pipeline_id。
    pub fn get_pipeline_for_thread(&self, thread_id: &str) -> Option<String> {
        self.thread_pipeline_map.read().get(thread_id).cloned()
    }

    /// 反查 thread_id 对应的 agent_id。
    pub fn get_agent_for_thread(&self, thread_id: &str) -> Option<String> {
        self.thread_agent_map.read().get(thread_id).cloned()
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

    /// 清除 user 名下全部 thread 映射条目。thread 归属 user 唯一
    /// （thread_user_map 值反查），条目只服务活跃连接的读面，随该 user 最后
    /// 一条连接移除。
    fn purge_thread_maps(&self, user_id: &str) {
        let mut thread_user = self.thread_user_map.write();
        let owned: Vec<String> = thread_user
            .iter()
            .filter(|(_, uid)| uid.as_str() == user_id)
            .map(|(tid, _)| tid.clone())
            .collect();
        for tid in owned {
            thread_user.remove(&tid);
            self.thread_pipeline_map.write().remove(&tid);
            self.thread_agent_map.write().remove(&tid);
        }
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
    pub async fn send_to_thread(&self, thread_id: &str, text: &str) -> bool {
        let user_id = match self.get_user_for_thread(thread_id) {
            Some(u) => u,
            None => return false,
        };
        self.send_to_user(&user_id, text).await
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
    /// 连接数/三张 thread 映射条目数为实测计数；`outbound_queued_frames`
    /// 为各连接出站队列在飞帧数实测和（sink 未披露队列深度时该连接按 0 计）。
    pub fn memory_stats(&self) -> ConnectionMemoryStats {
        let connections = self.connections.read();
        let mut stats = ConnectionMemoryStats {
            connections: connections.values().map(HashMap::len).sum(),
            thread_user: self.thread_user_map.read().len(),
            thread_pipeline: self.thread_pipeline_map.read().len(),
            thread_agent: self.thread_agent_map.read().len(),
            outbound_queued_frames: 0,
        };
        for sink in connections.values().flat_map(|m| m.values()) {
            stats.outbound_queued_frames += sink.pending_frames().unwrap_or(0);
        }
        stats
    }

    /// 枚举当前 thread_id → user_id 映射（供 REST 会话列表端点使用）。
    ///
    /// 只反映当前活跃连接名下的线程：该 user 最后一条连接注销（含 broadcast
    /// 清理死连接）时条目同步清除，不保留历史会话。
    pub fn list_threads(&self) -> Vec<(String, String)> {
        self.thread_user_map
            .read()
            .iter()
            .map(|(tid, uid)| (tid.clone(), uid.clone()))
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
}
