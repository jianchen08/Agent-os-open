//! 管道 state 内存常驻注册表（对齐 0.1 `EngineRegistry._engines`）。
//!
//! ## 为什么需要它
//!
//! 0.2 的 [`PipelineExecutor`](../../engine/pipeline_loop.rs) 是**无状态一次性执行器**——
//! 每次 `run()` 跑完返回 final_state 即丢弃，不像 0.1 的 PipelineEngine 持有跨轮实例属性。
//! 若每轮都从零构造 state，多轮对话历史就丢失，LLM 看不到上下文。
//!
//! 真正跨轮延续的是 **state**（那块 JSON），不是引擎实例。本注册表按 pipeline_id
//! 常驻 state：热路径（正常多轮）走内存复用，冷启动（进程重启/新会话）才走 DB 重建。
//!
//! ## 与 ConnectionRegistry 的关系
//!
//! 职责正交，刻意分离：
//! - [`ConnectionRegistry`]：传输层真相（user_id→sink，单连接踢旧）
//! - `PipelineStateRegistry`：会话语义层（pipeline_id→state，跨轮延续）
//!
//! 一条 WS 连接可承载多个 pipeline；一个 pipeline 可在断线重连时换 sink。
//! 把两者塞一张表会破坏单连接语义，故独立。
//!
//! ## 多租户
//!
//! 主键是 `(tenant_id, pipeline_id)`。tenant_id 由调用方传入（从
//! `agentos_tenant::current()` 取），本 crate 不直接依赖 tenant，保持 session
//! 层零 tenant 依赖（与 coordinator 一致）。
//!
//! ## sequence 内存常驻
//!
//! 对齐 0.1 `PipelineEntry.msg_sequence`（`src/pipeline/pipeline_entry.py:42-60`）。
//! 用 `AtomicU64` 替代 0.1 的 `threading.Lock`，更轻。`init_sequence` 在冷启动时
//! 从 DB 续接（对齐 0.1 `_resume_entry_sequence`），保证不回退。

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, OnceLock};
use std::time::Instant;

use parking_lot::RwLock;
use serde_json::Value;

/// 全局单例：进程级唯一 PipelineStateRegistry。
///
/// 不放进 AppState 是为避免 AppState 体积膨胀触发主线程栈溢出
///（Windows 主线程栈仅 1MB，AppState 含大量字段，再加一个 registry 字段
/// 在 debug 构建下会突破栈极限）。改用全局单例，AppState 零体积增量。
static GLOBAL_REGISTRY: OnceLock<PipelineStateRegistry> = OnceLock::new();

/// 获取全局 PipelineStateRegistry 单例（首次调用时惰性初始化）。
pub fn global_registry() -> &'static PipelineStateRegistry {
    GLOBAL_REGISTRY.get_or_init(PipelineStateRegistry::new)
}

/// 按 (tenant_id, pipeline_id) 索引的常驻管道 state 条目表（每条管道一把独立读锁）。
type PipelineStateMap = HashMap<(String, String), Arc<RwLock<PipelineStateEntry>>>;

/// 按 `(tenant_id, pipeline_id)` 常驻的管道 state 注册表。
#[derive(Clone)]
pub struct PipelineStateRegistry {
    entries: Arc<RwLock<PipelineStateMap>>,
}

/// 单个管道的常驻 state 条目。
pub struct PipelineStateEntry {
    /// 上一轮结束后的 final_state（含 messages 历史、raw_result、router.* 等）。
    /// 用 RwLock 保护：process_via_engine 读 + 跑完后回写。
    pub state: Value,
    pub thread_id: String,
    pub agent_id: String,
    /// 内存常驻 sequence（对齐 0.1 msg_sequence）。
    pub msg_sequence: AtomicU64,
    pub updated_at: Instant,
}

/// [`PipelineStateRegistry::list`] 返回的条目定位信息（轻量，不含 state 本体）。
pub struct PipelineStateListing {
    pub tenant_id: String,
    pub pipeline_id: String,
    pub thread_id: String,
    pub agent_id: String,
    pub msg_sequence: u64,
    pub updated_at: Instant,
}

impl PipelineStateRegistry {
    /// 创建空注册表。
    pub fn new() -> Self {
        Self {
            entries: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    /// 获取或初始化一个管道的 state 条目。
    ///
    /// - **命中**（热路径）：返回内存里已有的 entry，调用方复用其 state。
    /// - **未命中**（冷启动）：用 `cold_start_state` 注册新条目并返回。
    ///
    /// `tenant_id` 由调用方从 `agentos_tenant::current()` 取，本 crate 不依赖 tenant。
    pub fn get_or_init(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        thread_id: &str,
        agent_id: &str,
        cold_start_state: Value,
    ) -> Arc<RwLock<PipelineStateEntry>> {
        let key = (tenant_id.to_string(), pipeline_id.to_string());
        // 快速路径：读锁查命中
        if let Some(entry) = self.entries.read().get(&key) {
            return entry.clone();
        }
        // 慢路径：写锁注册（double-check 防并发重复注册）
        let mut entries = self.entries.write();
        if let Some(entry) = entries.get(&key) {
            return entry.clone();
        }
        let entry = Arc::new(RwLock::new(PipelineStateEntry {
            state: cold_start_state,
            thread_id: thread_id.to_string(),
            agent_id: agent_id.to_string(),
            msg_sequence: AtomicU64::new(0),
            updated_at: Instant::now(),
        }));
        entries.insert(key, entry.clone());
        entry
    }

    /// 查询一个管道是否已注册。
    pub fn contains(&self, tenant_id: &str, pipeline_id: &str) -> bool {
        let key = (tenant_id.to_string(), pipeline_id.to_string());
        self.entries.read().contains_key(&key)
    }

    /// 取一个管道的 entry（只读快照引用）。
    pub fn get(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
    ) -> Option<Arc<RwLock<PipelineStateEntry>>> {
        let key = (tenant_id.to_string(), pipeline_id.to_string());
        self.entries.read().get(&key).cloned()
    }

    /// 回写某管道跑完后的 final_state（热路径延续）。
    /// 对齐 0.1 `_current_state` 跨轮保留：下一轮 get_or_init 命中时即读到这份 state。
    pub fn update_state(&self, tenant_id: &str, pipeline_id: &str, final_state: Value) {
        let key = (tenant_id.to_string(), pipeline_id.to_string());
        if let Some(entry) = self.entries.read().get(&key).cloned() {
            let mut e = entry.write();
            e.state = final_state;
            e.updated_at = Instant::now();
        }
    }

    /// 内存常驻 sequence 递增并返回（对齐 0.1 PipelineEntry.next_sequence）。
    pub fn next_sequence(&self, tenant_id: &str, pipeline_id: &str) -> Option<u64> {
        let key = (tenant_id.to_string(), pipeline_id.to_string());
        self.entries
            .read()
            .get(&key)
            .map(|entry| entry.read().msg_sequence.fetch_add(1, Ordering::SeqCst) + 1)
    }

    /// 冷启动时从 DB 续接 sequence（对齐 0.1 _resume_entry_sequence）。
    /// 取 max(内存, db_max)，绝不回退。
    pub fn init_sequence(&self, tenant_id: &str, pipeline_id: &str, db_max_seq: u64) {
        let key = (tenant_id.to_string(), pipeline_id.to_string());
        if let Some(entry) = self.entries.read().get(&key).cloned() {
            let e = entry.read();
            let _ = e.msg_sequence.fetch_max(db_max_seq, Ordering::SeqCst);
        }
    }

    /// 注销一个管道（会话删除时清理）。
    pub fn remove(&self, tenant_id: &str, pipeline_id: &str) {
        let key = (tenant_id.to_string(), pipeline_id.to_string());
        self.entries.write().remove(&key);
    }

    /// 清空全部常驻条目（跨租户；全量执行数据清理时使用）。
    ///
    /// 与 [`PipelineStateRegistry::remove`] 的单条注销不同，本方法语义即
    /// "清掉全部执行数据"（DB 侧配套删除 runs/traces/… 九表），调用方为
    /// db-admin 的 clear_execution_data——热路径常驻 state 全部作废，
    /// 后续轮次走冷启动重建。
    pub fn clear(&self) {
        self.entries.write().clear();
    }

    /// 列出全部常驻条目的定位信息（不含 state 本体——messages 可能很大，
    /// 调用方按需经 [`PipelineStateRegistry::get`] 取锁内快照或读 DB checkpoint）。
    ///
    /// `GET /api/v1/pipelines/state` 的数据源：前端任务树直接消费内核 state
    /// （会话/阶段/迭代等运行时真值），不走插件任务表。
    pub fn list(&self) -> Vec<PipelineStateListing> {
        self.entries
            .read()
            .iter()
            .map(|((tenant_id, pipeline_id), entry)| {
                let e = entry.read();
                PipelineStateListing {
                    tenant_id: tenant_id.clone(),
                    pipeline_id: pipeline_id.clone(),
                    thread_id: e.thread_id.clone(),
                    agent_id: e.agent_id.clone(),
                    msg_sequence: e.msg_sequence.load(Ordering::SeqCst),
                    updated_at: e.updated_at,
                }
            })
            .collect()
    }

    /// 当前注册的管道数（监控用）。
    pub fn len(&self) -> usize {
        self.entries.read().len()
    }

    /// 是否为空。
    pub fn is_empty(&self) -> bool {
        self.entries.read().is_empty()
    }
}

impl Default for PipelineStateRegistry {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const TENANT: &str = "default";

    fn make_state(msgs: &[&str]) -> Value {
        let messages: Vec<Value> = msgs
            .iter()
            .map(|m| json!({"role": "user", "content": m}))
            .collect();
        json!({ "messages": messages, "raw_result": "" })
    }

    #[test]
    fn test_get_or_init_cold_start_registers() {
        let reg = PipelineStateRegistry::new();
        assert!(!reg.contains(TENANT, "pipe_1"));
        let entry = reg.get_or_init(TENANT, "pipe_1", "thread_1", "agentos", make_state(&["hi"]));
        assert!(reg.contains(TENANT, "pipe_1"));
        assert_eq!(entry.read().thread_id, "thread_1");
        assert_eq!(entry.read().agent_id, "agentos");
    }

    #[test]
    fn test_get_or_init_hot_path_reuses() {
        let reg = PipelineStateRegistry::new();
        // 冷启动注册
        let e1 = reg.get_or_init(TENANT, "pipe_2", "t", "a", make_state(&["msg1"]));
        // 更新 state（模拟跑完回写）
        reg.update_state(TENANT, "pipe_2", make_state(&["msg1", "msg2"]));
        // 再次 get_or_init 应命中，返回同一 entry，state 是更新后的
        let e2 = reg.get_or_init(TENANT, "pipe_2", "t", "a", make_state(&["should_not_use"]));
        assert!(Arc::ptr_eq(&e1, &e2), "热路径应返回同一 Arc");
        let state = e2.read();
        let msgs = state.state["messages"].as_array().unwrap();
        assert_eq!(msgs.len(), 2, "热路径应复用更新后的 state，而非 cold_start");
    }

    #[test]
    fn test_update_state_persists_for_next_turn() {
        let reg = PipelineStateRegistry::new();
        reg.get_or_init(TENANT, "pipe_3", "t", "a", make_state(&["u1"]));
        // 模拟跑完：final_state 含 assistant 回复
        let final_state = json!({
            "messages": [
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"}
            ],
            "raw_result": "a1"
        });
        reg.update_state(TENANT, "pipe_3", final_state);
        // 下一轮命中，state 应含上一轮的完整对话
        let entry = reg.get(TENANT, "pipe_3").unwrap();
        let guard = entry.read();
        let msgs = guard.state["messages"].as_array().unwrap();
        assert_eq!(msgs.len(), 2);
        assert_eq!(msgs[1]["content"], "a1");
    }

    #[test]
    fn test_sequence_monotonic() {
        let reg = PipelineStateRegistry::new();
        reg.get_or_init(TENANT, "pipe_4", "t", "a", make_state(&[]));
        assert_eq!(reg.next_sequence(TENANT, "pipe_4"), Some(1));
        assert_eq!(reg.next_sequence(TENANT, "pipe_4"), Some(2));
        assert_eq!(reg.next_sequence(TENANT, "pipe_4"), Some(3));
        // 未注册的管道返回 None
        assert_eq!(reg.next_sequence(TENANT, "pipe_unknown"), None);
    }

    #[test]
    fn test_init_sequence_from_db_no_regression() {
        let reg = PipelineStateRegistry::new();
        reg.get_or_init(TENANT, "pipe_5", "t", "a", make_state(&[]));
        // 内存已递增到 2
        reg.next_sequence(TENANT, "pipe_5");
        reg.next_sequence(TENANT, "pipe_5");
        // 冷启动续接：DB 已有 10，取 max(内存2, 10)=10，下一次 next=11
        reg.init_sequence(TENANT, "pipe_5", 10);
        assert_eq!(
            reg.next_sequence(TENANT, "pipe_5"),
            Some(11),
            "续接后不应回退"
        );
    }

    #[test]
    fn test_remove_clears_entry() {
        let reg = PipelineStateRegistry::new();
        reg.get_or_init(TENANT, "pipe_6", "t", "a", make_state(&[]));
        assert!(reg.contains(TENANT, "pipe_6"));
        reg.remove(TENANT, "pipe_6");
        assert!(!reg.contains(TENANT, "pipe_6"));
        // 移除后重新 get_or_init 是冷启动
        let entry = reg.get_or_init(TENANT, "pipe_6", "t", "a", make_state(&["fresh"]));
        assert_eq!(entry.read().state["messages"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn test_tenant_isolation() {
        let reg = PipelineStateRegistry::new();
        // 租户 A 注册 pipe_x
        reg.get_or_init("tenant_a", "pipe_x", "t", "a", make_state(&["a-msg"]));
        // 租户 B 的 pipe_x 应独立（不存在）
        assert!(!reg.contains("tenant_b", "pipe_x"));
        assert!(reg.contains("tenant_a", "pipe_x"));
    }

    #[test]
    fn test_clear_removes_all_entries_across_tenants() {
        let reg = PipelineStateRegistry::new();
        reg.get_or_init("tenant_a", "pipe_1", "t", "a", make_state(&[]));
        reg.get_or_init("tenant_b", "pipe_2", "t", "a", make_state(&[]));
        assert!(!reg.is_empty());
        reg.clear();
        assert!(reg.is_empty(), "clear 后注册表应为空");
        assert!(!reg.contains("tenant_a", "pipe_1"));
        assert!(!reg.contains("tenant_b", "pipe_2"));
    }
}
