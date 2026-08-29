//! 插件中间态内存寄存器（Transient State Register，ADR 2026-08-27）。
//!
//! 中间态不落库、引擎内存持有、停止时合并落库、用完即清（ADR 决策核心）。
//! 两个区域：
//!
//! - **A 键值区**：`per-(tenant,pipeline) → per-key` 中间态快照，值 =
//!   `{ value: Value, updated_at: Instant }`。键命名 `<type>:<message_id>`
//!   （如 `chunk:mc_xxx`）——与 message_slots 主键 `(pipeline_id, seq)`
//!   键空间物理隔离。LRU 上限逐出最老（防长流式撑爆内存）。
//! - **B 运行上下文登记区**：`message_id → { step_id }`，由引擎派发步骤执行时
//!   写入、step 收尾清除——流事件发射点在 api 层拦截点，载荷只有
//!   pipeline_id/message_id 而无 step 标识，step 级钩子最小作用域装载
//!   （管道步骤服务化提案 §3.6 条款④）需这张映射做归属判定。
//!
//! 生命周期（三清 + 一限，两区共用触发点）：
//!
//! | 清理路径 | 触发点 |
//! |---|---|
//! | 显式 clear | `transient.clear` 能力（stream_end 处理器清 chunk 键） |
//! | 落库自动清 | `merge_and_project` 落消息后按同 message_id 清 |
//! | 管道结束清 | 管道/会话删除时清整管道 |
//! | LRU 上限 | 超限逐出最老（仅 A 区） |
//!
//! [来源: docs/decisions/2026-08-27-transient-state-register.md]
//! [来源: docs/working/插件中间态统一管理方案_20260827.md §2.2]

use std::collections::HashMap;
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use parking_lot::{Mutex, RwLock};
use serde_json::{json, Value};

/// A 区 LRU 上限（键数/进程）。超限逐出 `updated_at` 最老键。
pub const MAX_KEYS_PER_PROCESS: usize = 1000;

/// chunk 累积节流（方案 §2.4）：每 N 个 chunk 或距上次落盘 ≥ 本间隔才真正写
/// 寄存器一次（写入频率 ~2-5 次/秒，防长流式高频 upsert）。
pub const CHUNK_FLUSH_EVERY: u64 = 10;

/// chunk 累积节流时间窗（毫秒）。
pub const CHUNK_FLUSH_INTERVAL_MS: u64 = 500;

/// 全局单例：进程级唯一 TransientStateRegistry。
///
/// 不放进 AppState 是为避免 AppState 体积膨胀触发主线程栈溢出
///（Windows 主线程栈仅 1MB，AppState 含大量字段，再加一个 registry 字段
/// 在 debug 构建下会突破栈极限）。改用全局单例，AppState 零体积增量。
///（同款理由见 session crate pipeline_state_registry.rs。）
static GLOBAL_REGISTRY: OnceLock<TransientStateRegistry> = OnceLock::new();

/// 获取全局 TransientStateRegistry 单例（首次调用时惰性初始化）。
pub fn global_registry() -> &'static TransientStateRegistry {
    GLOBAL_REGISTRY.get_or_init(TransientStateRegistry::new)
}

/// A 区单键条目。
#[derive(Clone)]
pub struct TransientEntry {
    pub value: Value,
    pub updated_at: Instant,
}

/// A 区单管道键集合：key → 条目（按 key 插入序 + updated_at 维护 LRU）。
type PipelineKeyMap = HashMap<String, TransientEntry>;

/// B 区登记条目：message_id → step 归属。
#[derive(Clone, Debug)]
pub struct MessageBinding {
    pub step_id: String,
}

/// chunk 累积节流档（方案 §2.4）：每 (tenant,pipeline,message) 一档，跨 chunk
/// 持有增量拼接缓冲，达 [`CHUNK_FLUSH_EVERY`] 个或距上次落盘
/// ≥ [`CHUNK_FLUSH_INTERVAL_MS`] 时把**累计快照**写进 A 区一次。
struct ChunkAccumulator {
    count: u64,
    last_flush: Instant,
    /// text/reasoning 增量拼接缓冲（复用 llm_service `_PartialAccumulator`
    /// 快照语义的轻量版：只拼 text/thinking，不拼 tool_call 参数增量）。
    text: String,
    thinking: String,
}

/// 键值区（A 区）整体：per-(tenant,pipeline) → per-key。
type KeyedStore = HashMap<(String, String), PipelineKeyMap>;
/// 上下文登记区（B 区）整体：per-(tenant,pipeline) → (message_id → step_id)。
type BindingStore = HashMap<(String, String), HashMap<String, MessageBinding>>;

type ChunkAccumTable = HashMap<(String, String, String), ChunkAccumulator>;

/// 两区内存寄存器。
///
/// 进程级单例（`global_registry()`），两区共用 `(tenant_id, pipeline_id)` 键
/// 与 `clear_pipeline` 生命周期；A 区另有 LRU 上限逐出。
/// chunk 累积节流计数（每 (tenant,pipeline,message) 一档）与两区同锁无关，
/// 独立短锁互不影响读写热路径。
#[derive(Clone)]
pub struct TransientStateRegistry {
    keys: std::sync::Arc<RwLock<KeyedStore>>,
    bindings: std::sync::Arc<RwLock<BindingStore>>,
    /// chunk 累积节流档：(tenant,pipeline,message_id) → 累积缓冲（短锁独立）。
    chunk_accum: std::sync::Arc<Mutex<ChunkAccumTable>>,
}

impl TransientStateRegistry {
    /// 创建空寄存器（单测/独立装配用；生产走 [`global_registry`] 单例）。
    pub fn new() -> Self {
        Self {
            keys: std::sync::Arc::new(RwLock::new(HashMap::new())),
            bindings: std::sync::Arc::new(RwLock::new(HashMap::new())),
            chunk_accum: std::sync::Arc::new(Mutex::new(HashMap::new())),
        }
    }

    // ── A 键值区 ──────────────────────────────────────────────

    /// 写/覆盖一个中间态键。超 LRU 上限时逐出该管道最老键（按 updated_at）。
    pub fn set(&self, tenant_id: &str, pipeline_id: &str, key: &str, value: Value) {
        let mut keys = self.keys.write();
        let pipe = keys
            .entry((tenant_id.to_string(), pipeline_id.to_string()))
            .or_default();
        let now = Instant::now();
        pipe.insert(
            key.to_string(),
            TransientEntry {
                value,
                updated_at: now,
            },
        );
        if pipe.len() > MAX_KEYS_PER_PROCESS {
            // 逐出 updated_at 最老的键（同刻并列时取最先遍历到的，保证确定性）
            if let Some(oldest_key) = pipe
                .iter()
                .min_by_key(|(_, e)| e.updated_at)
                .map(|(k, _)| k.clone())
            {
                pipe.remove(&oldest_key);
            }
        }
    }

    /// 读一个中间态键。
    pub fn get(&self, tenant_id: &str, pipeline_id: &str, key: &str) -> Option<Value> {
        self.keys
            .read()
            .get(&(tenant_id.to_string(), pipeline_id.to_string()))
            .and_then(|pipe| pipe.get(key))
            .map(|e| e.value.clone())
    }

    /// 枚举一个管道的全部存活键（key/value/updated_at），前端刷新恢复用。
    pub fn list(&self, tenant_id: &str, pipeline_id: &str) -> Vec<(String, Value, Instant)> {
        self.keys
            .read()
            .get(&(tenant_id.to_string(), pipeline_id.to_string()))
            .map(|pipe| {
                pipe.iter()
                    .map(|(k, e)| (k.clone(), e.value.clone(), e.updated_at))
                    .collect()
            })
            .unwrap_or_default()
    }

    /// 显式清一个键。
    pub fn clear(&self, tenant_id: &str, pipeline_id: &str, key: &str) {
        let mut keys = self.keys.write();
        let pipe_key = (tenant_id.to_string(), pipeline_id.to_string());
        let mut empty = false;
        if let Some(pipe) = keys.get_mut(&pipe_key) {
            pipe.remove(key);
            empty = pipe.is_empty();
        }
        if empty {
            keys.remove(&pipe_key);
        }
    }

    // ── B 运行上下文登记区 ────────────────────────────────────

    /// 登记 message_id → step 归属（引擎派发步骤执行时写入）。
    pub fn register_message_binding(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        message_id: &str,
        step_id: &str,
    ) {
        self.bindings
            .write()
            .entry((tenant_id.to_string(), pipeline_id.to_string()))
            .or_default()
            .insert(
                message_id.to_string(),
                MessageBinding {
                    step_id: step_id.to_string(),
                },
            );
    }

    /// 清除一条 message 绑定（step 收尾时调用）。
    pub fn clear_message_binding(&self, tenant_id: &str, pipeline_id: &str, message_id: &str) {
        let mut bindings = self.bindings.write();
        let pipe_key = (tenant_id.to_string(), pipeline_id.to_string());
        let mut empty = false;
        if let Some(pipe) = bindings.get_mut(&pipe_key) {
            pipe.remove(message_id);
            empty = pipe.is_empty();
        }
        if empty {
            bindings.remove(&pipe_key);
        }
    }

    /// 按 message_id 反查其归属 step（None = 未登记）。
    pub fn resolve_step_of(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        message_id: &str,
    ) -> Option<String> {
        self.bindings
            .read()
            .get(&(tenant_id.to_string(), pipeline_id.to_string()))
            .and_then(|pipe| pipe.get(message_id))
            .map(|b| b.step_id.clone())
    }

    // ── chunk 累积（方案 §2.4：拦截点顺带累积，一次 IPC 两个动作）────────

    /// 累积一个流式 chunk 增量。返回 true = 本次真正写入了 A 区（节流判定通过）。
    ///
    /// 每 N 个 chunk 或距上次落盘 ≥ 500ms 才写一次（节流语义在寄存器模块内部，
    /// 调用方零感知）。`content` 累加到 text 快照，`thinking` 累加到
    /// reasoning 快照（thinking_chunk 事件带 thinking 字段时走这里）。
    /// 快照结构（方案 §2.4）：`{ text_len, blocks_摘要 }`——text_len = 累计
    /// 文本长度（轻量恢复占位）；reasoning_len 同理；不携带全文（节流粒度
    /// 快照，逐字重建需另立事件订阅能力）。
    pub fn accumulate_chunk(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        message_id: &str,
        content: &str,
        thinking: &str,
    ) -> bool {
        self.accumulate_chunk_at(
            tenant_id,
            pipeline_id,
            message_id,
            content,
            thinking,
            Instant::now(),
        )
    }

    /// `accumulate_chunk` 的时钟注入变体（fake clock：测试用可调 now 断言
    /// 时序不变量，禁止零延迟 sleep；生产只走 [`Self::accumulate_chunk`]）。
    fn accumulate_chunk_at(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        message_id: &str,
        content: &str,
        thinking: &str,
        now: Instant,
    ) -> bool {
        let mut acc = self.chunk_accum.lock();
        let key = (
            tenant_id.to_string(),
            pipeline_id.to_string(),
            message_id.to_string(),
        );
        let entry = acc.entry(key).or_insert_with(|| ChunkAccumulator {
            count: 0,
            last_flush: now,
            text: String::new(),
            thinking: String::new(),
        });
        entry.count += 1;
        if !content.is_empty() {
            entry.text.push_str(content);
        }
        if !thinking.is_empty() {
            entry.thinking.push_str(thinking);
        }
        let due = entry.count >= CHUNK_FLUSH_EVERY
            || now.duration_since(entry.last_flush)
                >= Duration::from_millis(CHUNK_FLUSH_INTERVAL_MS);
        if !due {
            return false;
        }
        // 落 A 区：chunk:<message_id> 键（键空间与 message_slots 主键隔离）。
        // 快照在 chunk 锁内构造、计数与时间戳就地复位，锁外写 A 区——
        // 与 clear_pipeline（keys → chunk_accum 顺序）锁序相反，绝不跨锁嵌套。
        let snapshot = json!({
            "text_len": entry.text.len(),
            "reasoning_len": entry.thinking.len(),
            "blocks": [{
                "type": "text",
                "content": entry.text,
            }],
        });
        entry.count = 0;
        entry.last_flush = now;
        drop(acc);
        self.set(
            tenant_id,
            pipeline_id,
            &format!("chunk:{message_id}"),
            snapshot,
        );
        true
    }

    /// 丢弃一条 chunk 累积（stream_end/stream_error 到达：最终形态已落
    /// message_slots，chunk 中间态使命完成）。顺带清 A 区 chunk 键。
    pub fn clear_chunk(&self, tenant_id: &str, pipeline_id: &str, message_id: &str) {
        let key = (
            tenant_id.to_string(),
            pipeline_id.to_string(),
            message_id.to_string(),
        );
        self.chunk_accum.lock().remove(&key);
        self.clear(tenant_id, pipeline_id, &format!("chunk:{message_id}"));
    }

    /// 取该 message 的 chunk 累积快照（中断合并落库的数据源；None = 无累积）。
    pub fn take_chunk_snapshot(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        message_id: &str,
    ) -> Option<Value> {
        let key = (
            tenant_id.to_string(),
            pipeline_id.to_string(),
            message_id.to_string(),
        );
        self.chunk_accum.lock().get(&key).map(|e| {
            json!({
                "text_len": e.text.len(),
                "reasoning_len": e.thinking.len(),
                "blocks": [{
                    "type": "text",
                    "content": e.text,
                }],
            })
        })
    }

    // ── 两区共用生命周期 ──────────────────────────────────────

    /// 清一个管道的两区全部条目（管道/会话删除、run 收尾兜底时调用）。
    /// chunk 累积节流档同清（管道级中间态整体作废）。
    pub fn clear_pipeline(&self, tenant_id: &str, pipeline_id: &str) {
        let pipe_key = (tenant_id.to_string(), pipeline_id.to_string());
        self.keys.write().remove(&pipe_key);
        self.bindings.write().remove(&pipe_key);
        self.chunk_accum
            .lock()
            .retain(|(t, p, _), _| t != tenant_id || p != pipeline_id);
    }

    /// 清全部两区条目（跨租户；全量执行数据清理时使用）。
    pub fn clear_all(&self) {
        self.keys.write().clear();
        self.bindings.write().clear();
        self.chunk_accum.lock().clear();
    }
}

impl Default for TransientStateRegistry {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const TENANT: &str = "default";

    #[test]
    fn set_get_roundtrip() {
        let reg = TransientStateRegistry::new();
        reg.set(TENANT, "pipe_1", "chunk:mc_a", json!({"text_len": 3}));
        let v = reg.get(TENANT, "pipe_1", "chunk:mc_a").expect("写入可读");
        assert_eq!(v["text_len"], json!(3));
        // 覆盖语义：同 key 再写取最新值
        reg.set(TENANT, "pipe_1", "chunk:mc_a", json!({"text_len": 7}));
        assert_eq!(
            reg.get(TENANT, "pipe_1", "chunk:mc_a").unwrap()["text_len"],
            json!(7)
        );
        // 未写过的 key 返回 None
        assert!(reg.get(TENANT, "pipe_1", "chunk:mc_b").is_none());
    }

    #[test]
    fn list_enumerates_pipeline_keys_with_updated_at() {
        let reg = TransientStateRegistry::new();
        reg.set(TENANT, "pipe_1", "chunk:mc_a", json!({"text_len": 3}));
        reg.set(TENANT, "pipe_1", "progress:1", json!({"pct": 50}));
        let rows = reg.list(TENANT, "pipe_1");
        assert_eq!(rows.len(), 2);
        assert!(rows.iter().any(|(k, _, _)| k == "chunk:mc_a"));
        assert!(rows.iter().any(|(k, _, _)| k == "progress:1"));
        // 未写过的管道返回空
        assert!(reg.list(TENANT, "pipe_unknown").is_empty());
    }

    #[test]
    fn clear_removes_single_key() {
        let reg = TransientStateRegistry::new();
        reg.set(TENANT, "pipe_1", "chunk:mc_a", json!({"text_len": 3}));
        reg.set(TENANT, "pipe_1", "progress:1", json!({"pct": 50}));
        reg.clear(TENANT, "pipe_1", "chunk:mc_a");
        assert!(reg.get(TENANT, "pipe_1", "chunk:mc_a").is_none());
        assert!(reg.get(TENANT, "pipe_1", "progress:1").is_some());
        // 清掉管道最后一个键后管道整体消失
        reg.clear(TENANT, "pipe_1", "progress:1");
        assert!(reg.list(TENANT, "pipe_1").is_empty());
    }

    #[test]
    fn lru_evicts_oldest_key() {
        let reg = TransientStateRegistry::new();
        // 压过上限：先写 MAX_KEYS 个，再补一个触发逐出
        for i in 0..MAX_KEYS_PER_PROCESS {
            reg.set(TENANT, "pipe_1", &format!("k_{i}"), json!({"i": i}));
        }
        // 补第 MAX+1 个：最老的 k_0 应被逐出
        reg.set(TENANT, "pipe_1", "k_overflow", json!({"i": -1}));
        assert!(reg.get(TENANT, "pipe_1", "k_0").is_none(), "LRU 逐出最老键");
        assert!(reg.get(TENANT, "pipe_1", "k_overflow").is_some());
        assert_eq!(reg.list(TENANT, "pipe_1").len(), MAX_KEYS_PER_PROCESS);
        // 管道间独立：另一个管道不受影响
        reg.set(TENANT, "pipe_2", "k_0", json!({"i": 0}));
        assert!(reg.get(TENANT, "pipe_2", "k_0").is_some());
    }

    #[test]
    fn tenant_isolation() {
        let reg = TransientStateRegistry::new();
        reg.set("tenant_a", "pipe_x", "chunk:mc_a", json!({"text_len": 1}));
        assert!(reg.get("tenant_a", "pipe_x", "chunk:mc_a").is_some());
        assert!(
            reg.get("tenant_b", "pipe_x", "chunk:mc_a").is_none(),
            "租户间同管道同键必须隔离"
        );
        reg.clear_pipeline("tenant_a", "pipe_x");
        assert!(reg.get("tenant_a", "pipe_x", "chunk:mc_a").is_none());
        // B 区同租户隔离
        reg.register_message_binding("tenant_a", "pipe_x", "m1", "step1");
        assert!(reg.resolve_step_of("tenant_b", "pipe_x", "m1").is_none());
    }

    #[test]
    fn binding_register_clear_resolve() {
        let reg = TransientStateRegistry::new();
        assert!(reg.resolve_step_of(TENANT, "pipe_1", "m1").is_none());
        reg.register_message_binding(TENANT, "pipe_1", "m1", "core");
        assert_eq!(
            reg.resolve_step_of(TENANT, "pipe_1", "m1").as_deref(),
            Some("core")
        );
        // 覆盖：同 message 再登记取新 step
        reg.register_message_binding(TENANT, "pipe_1", "m1", "post");
        assert_eq!(
            reg.resolve_step_of(TENANT, "pipe_1", "m1").as_deref(),
            Some("post")
        );
        reg.clear_message_binding(TENANT, "pipe_1", "m1");
        assert!(reg.resolve_step_of(TENANT, "pipe_1", "m1").is_none());
        // 未登记管道返回 None
        assert!(reg.resolve_step_of(TENANT, "pipe_other", "m1").is_none());
    }

    #[test]
    fn clear_pipeline_clears_both_zones() {
        let reg = TransientStateRegistry::new();
        reg.set(TENANT, "pipe_1", "chunk:mc_a", json!({"text_len": 3}));
        reg.register_message_binding(TENANT, "pipe_1", "m1", "core");
        reg.set(TENANT, "pipe_2", "chunk:mc_b", json!({"text_len": 5}));
        reg.clear_pipeline(TENANT, "pipe_1");
        // A 区：pipe_1 全清，pipe_2 不受影响
        assert!(reg.get(TENANT, "pipe_1", "chunk:mc_a").is_none());
        assert!(reg.get(TENANT, "pipe_2", "chunk:mc_b").is_some());
        // B 区：pipe_1 全清
        assert!(reg.resolve_step_of(TENANT, "pipe_1", "m1").is_none());
    }

    #[test]
    fn clear_all_wipes_across_tenants() {
        let reg = TransientStateRegistry::new();
        reg.set("tenant_a", "pipe_1", "chunk:mc_a", json!({"text_len": 1}));
        reg.register_message_binding("tenant_b", "pipe_2", "m1", "core");
        reg.clear_all();
        assert!(reg.get("tenant_a", "pipe_1", "chunk:mc_a").is_none());
        assert!(reg.resolve_step_of("tenant_b", "pipe_2", "m1").is_none());
    }

    // ── chunk 累积 + 节流（方案 §2.4）──────────────────────────

    #[test]
    fn chunk_accumulate_throttles_until_count_threshold() {
        let reg = TransientStateRegistry::new();
        // 前 CHUNK_FLUSH_EVERY-1 个 chunk：节流不落 A 区（无 chunk: 键）
        for i in 0..(CHUNK_FLUSH_EVERY - 1) {
            let wrote = reg.accumulate_chunk(TENANT, "pipe_1", "m1", &format!("c{i}"), "");
            assert!(!wrote, "节流窗内不得落 A 区");
        }
        assert!(reg.get(TENANT, "pipe_1", "chunk:m1").is_none());
        // 第 N 个 chunk：达计数阈值落 A 区一次
        let wrote = reg.accumulate_chunk(TENANT, "pipe_1", "m1", "c9", "");
        assert!(wrote, "达计数阈值必须落 A 区");
        let snap = reg.get(TENANT, "pipe_1", "chunk:m1").unwrap();
        assert_eq!(snap["text_len"], json!((CHUNK_FLUSH_EVERY as usize) * 2));
        // 下一轮重新计数：单 chunk 不落
        assert!(!reg.accumulate_chunk(TENANT, "pipe_1", "m1", "x", ""));
        assert!(reg.get(TENANT, "pipe_1", "chunk:m1").is_some());
    }

    #[test]
    fn chunk_accumulate_throttles_until_time_threshold() {
        let reg = TransientStateRegistry::new();
        let t0 = Instant::now();
        // 未达计数阈值且未超时间窗 → 不落
        assert!(!reg.accumulate_chunk_at(TENANT, "pipe_1", "m1", "a", "", t0));
        // 未达计数阈值但距上次落盘超过时间窗 → 落 A 区（fake clock 注入）
        let wrote = reg.accumulate_chunk_at(
            TENANT,
            "pipe_1",
            "m1",
            "b",
            "",
            t0 + Duration::from_millis(CHUNK_FLUSH_INTERVAL_MS + 1),
        );
        assert!(wrote, "超时间窗必须落 A 区");
        let snap = reg.get(TENANT, "pipe_1", "chunk:m1").unwrap();
        assert_eq!(snap["text_len"], json!(2), "text 增量拼接");
        // 落盘后计数复位：紧接着的同刻 chunk 不再落
        assert!(!reg.accumulate_chunk_at(TENANT, "pipe_1", "m1", "c", "", t0));
    }

    #[test]
    fn chunk_accumulate_thinking_separate() {
        let reg = TransientStateRegistry::new();
        // thinking 增量单独累加（thinking_chunk 事件）
        for i in 0..CHUNK_FLUSH_EVERY {
            reg.accumulate_chunk(TENANT, "pipe_1", "m1", "", &format!("t{i}"));
        }
        let snap = reg.get(TENANT, "pipe_1", "chunk:m1").unwrap();
        assert_eq!(snap["text_len"], json!(0), "无 text 增量");
        assert_eq!(
            snap["reasoning_len"],
            json!((CHUNK_FLUSH_EVERY as usize) * 2),
            "thinking 增量按 reasoning_len 累积"
        );
    }

    #[test]
    fn chunk_clear_removes_accumulator_and_key() {
        let reg = TransientStateRegistry::new();
        for _ in 0..CHUNK_FLUSH_EVERY {
            reg.accumulate_chunk(TENANT, "pipe_1", "m1", "x", "");
        }
        assert!(reg.get(TENANT, "pipe_1", "chunk:m1").is_some());
        reg.clear_chunk(TENANT, "pipe_1", "m1");
        assert!(reg.get(TENANT, "pipe_1", "chunk:m1").is_none());
        // 清除后重新累积从零开始（第 N 个才落）
        assert!(!reg.accumulate_chunk(TENANT, "pipe_1", "m1", "a", ""));
    }

    #[test]
    fn chunk_snapshot_take_for_stop_merge() {
        let reg = TransientStateRegistry::new();
        assert!(reg.take_chunk_snapshot(TENANT, "pipe_1", "m1").is_none());
        reg.accumulate_chunk(TENANT, "pipe_1", "m1", "半截", "想");
        let snap = reg.take_chunk_snapshot(TENANT, "pipe_1", "m1").unwrap();
        assert!(snap["blocks"][0]["content"]
            .as_str()
            .unwrap()
            .contains("半截"));
        assert_eq!(snap["reasoning_len"], json!(3));
    }

    #[test]
    fn chunk_accumulator_isolated_per_pipeline_and_tenant() {
        let reg = TransientStateRegistry::new();
        reg.accumulate_chunk(TENANT, "pipe_1", "m1", "aaa", "");
        reg.accumulate_chunk("tenant_b", "pipe_1", "m1", "bb", "");
        for _ in 0..CHUNK_FLUSH_EVERY {
            reg.accumulate_chunk(TENANT, "pipe_1", "m1", "c", "");
        }
        // 管道/租户间计数独立：tenant_b 的档仍在节流窗内
        assert!(!reg.accumulate_chunk("tenant_b", "pipe_1", "m1", "x", ""));
        // clear_pipeline 只清本管道（含节流档）
        reg.clear_pipeline(TENANT, "pipe_1");
        assert!(reg.get(TENANT, "pipe_1", "chunk:m1").is_none());
        assert!(
            !reg.accumulate_chunk(TENANT, "pipe_1", "m1", "a", ""),
            "节流档已随管道清除"
        );
    }
}
