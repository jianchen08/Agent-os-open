//! 连接注册表——user_id/thread_id → 连接，单连接踢旧（ADR §7.2 B10）。
//!
//! 参考 0.1 `ws_handler.py:38,143`（`_global_connections` + `register_global`）。
//! 每个 user 一条 WS，新连接踢旧连接（避免同用户多连接状态分裂）。

use std::collections::HashMap;
use std::sync::Arc;

use parking_lot::RwLock;

use crate::EventSink;

/// 连接注册表：user_id → 当前活跃连接 + thread_id → user_id 逻辑映射。
pub struct ConnectionRegistry {
    /// user_id → 当前活跃 sink（单连接真相之源）。
    connections: RwLock<HashMap<String, Arc<dyn EventSink>>>,
    /// thread_id → user_id（仅持有 thread_id 的流式发送路径反查用）。
    thread_user_map: RwLock<HashMap<String, String>>,
    /// thread_id → pipeline_id（创建会话时回填，REST 会话列表/详情据此返回给前端）。
    thread_pipeline_map: RwLock<HashMap<String, String>>,
    /// thread_id → agent_id（创建会话或绑定 Agent 时写入）。
    thread_agent_map: RwLock<HashMap<String, String>>,
}

impl ConnectionRegistry {
    /// 创建空注册表。
    pub fn new() -> Self {
        Self {
            connections: RwLock::new(HashMap::new()),
            thread_user_map: RwLock::new(HashMap::new()),
            thread_pipeline_map: RwLock::new(HashMap::new()),
            thread_agent_map: RwLock::new(HashMap::new()),
        }
    }

    /// 注册全局单连接。若该 user 已有旧连接，踢出旧连接并返回（B10）。
    ///
    /// 调用方拿到返回的旧 sink 后应向其发送 4004 关闭码（"本账号在其他位置登录"）。
    pub fn register(
        &self,
        user_id: &str,
        sink: Arc<dyn EventSink>,
    ) -> Option<Arc<dyn EventSink>> {
        let mut conns = self.connections.write();
        let old = conns.insert(user_id.to_string(), sink);
        old // Some(old) 表示踢出了旧连接
    }

    /// 查询 user 的当前活跃连接。
    pub fn get_by_user(&self, user_id: &str) -> Option<Arc<dyn EventSink>> {
        self.connections.read().get(user_id).cloned()
    }

    /// 注销连接。仅当传入的 sink id 是当前注册的 sink 时才删除——
    /// 防止旧连接的 finally 块误删已被新连接替换的注册项（参考 0.1
    /// `unregister_global` 的 `current is not websocket` 判定）。
    pub fn unregister(&self, user_id: &str, sink_id: u64) {
        let mut conns = self.connections.write();
        if let Some(current) = conns.get(user_id) {
            if current.id() == sink_id {
                conns.remove(user_id);
            }
        }
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

    /// 向指定 user 的连接推送一条文本消息（唯一出口 push_to_user 底层）。
    ///
    /// 返回是否投递成功（false = user 不在线 / 发送失败）。
    pub async fn send_to_user(&self, user_id: &str, text: &str) -> bool {
        let sink = match self.get_by_user(user_id) {
            Some(s) => s,
            None => return false,
        };
        sink.send_text(text).await
    }

    /// 向指定 thread 关联 user 的连接推送消息（反查 thread→user→连接）。
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
            .map(|(u, s)| (u.clone(), s.clone()))
            .collect();
        let mut delivered = 0usize;
        // 记录失败的 (user_id, sink_id)，用于精确注销（不误删新连上的）
        let mut dead: Vec<(String, u64)> = Vec::new();
        for (user_id, sink) in &sinks {
            if sink.send_text(text).await {
                delivered += 1;
            } else {
                dead.push((user_id.clone(), sink.id()));
            }
        }
        // 清理发送失败的连接：仅当注册表当前 sink id 与失败的一致时才删
        if !dead.is_empty() {
            let mut conns = self.connections.write();
            for (user_id, failed_id) in &dead {
                if conns.get(user_id).map(|s| s.id()) == Some(*failed_id) {
                    conns.remove(user_id);
                }
            }
        }
        delivered
    }

    /// 当前活跃连接数（监控 M2：gauge，监控设计 §三 通道1）。
    pub fn active_count(&self) -> usize {
        self.connections.read().len()
    }

    /// 枚举当前 thread_id → user_id 映射（供 REST 会话列表端点使用）。
    ///
    /// 0.2 暂无持久化会话历史，此列表仅反映内存中的活跃/曾注册线程。
    pub fn list_threads(&self) -> Vec<(String, String)> {
        self.thread_user_map
            .read()
            .iter()
            .map(|(tid, uid)| (tid.clone(), uid.clone()))
            .collect()
    }
}

impl Default for ConnectionRegistry {
    fn default() -> Self {
        Self::new()
    }
}
