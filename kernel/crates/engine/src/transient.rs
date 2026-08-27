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
use std::time::Instant;

use parking_lot::RwLock;
use serde_json::Value;

/// A 区 LRU 上限（键数/进程）。超限逐出 `updated_at` 最老键。
pub const MAX_KEYS_PER_PROCESS: usize = 1000;

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

/// 键值区（A 区）整体：per-(tenant,pipeline) → per-key。
type KeyedStore = HashMap<(String, String), PipelineKeyMap>;
/// 上下文登记区（B 区）整体：per-(tenant,pipeline) → (message_id → step_id)。
type BindingStore = HashMap<(String, String), HashMap<String, MessageBinding>>;

/// 两区内存寄存器。
///
/// 进程级单例（`global_registry()`），两区共用 `(tenant_id, pipeline_id)` 键
/// 与 `clear_pipeline` 生命周期；A 区另有 LRU 上限逐出。
#[derive(Clone)]
pub struct TransientStateRegistry {
    keys: std::sync::Arc<RwLock<KeyedStore>>,
    bindings: std::sync::Arc<RwLock<BindingStore>>,
}

impl TransientStateRegistry {
    /// 创建空寄存器（单测/独立装配用；生产走 [`global_registry`] 单例）。
    pub fn new() -> Self {
        Self {
            keys: std::sync::Arc::new(RwLock::new(HashMap::new())),
            bindings: std::sync::Arc::new(RwLock::new(HashMap::new())),
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
        pipe.insert(key.to_string(), TransientEntry { value, updated_at: now });
        if pipe.len() > MAX_KEYS_PER_PROCESS {
            // 逐出 updated_at 最老的键（同刻并列时取最先遍历到的，保证确定性）
            if let Some(oldest_key) = pipe.iter().min_by_key(|(_, e)| e.updated_at).map(|(k, _)| k.clone())
            {
                pipe.remove(&oldest_key);
            }
        }    }

    /// 读一个中间态键。
    pub fn get(&self, tenant_id: &str, pipeline_id: &str, key: &str) -> Option<Value> {
        self.keys
            .read()
            .get(&(tenant_id.to_string(), pipeline_id.to_string()))
            .and_then(|pipe| pipe.get(key))
            .map(|e| e.value.clone())
    }

    /// 枚举一个管道的全部存活键（key/value/updated_at），前端刷新恢复用。
    pub fn list(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
    ) -> Vec<(String, Value, Instant)> {
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
            .insert(message_id.to_string(), MessageBinding { step_id: step_id.to_string() });
    }

    /// 清除一条 message 绑定（step 收尾时调用）。
    pub fn clear_message_binding(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        message_id: &str,
    ) {
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

    // ── 两区共用生命周期 ──────────────────────────────────────

    /// 清一个管道的两区全部条目（管道/会话删除、run 收尾兜底时调用）。
    pub fn clear_pipeline(&self, tenant_id: &str, pipeline_id: &str) {
        let pipe_key = (tenant_id.to_string(), pipeline_id.to_string());
        self.keys.write().remove(&pipe_key);
        self.bindings.write().remove(&pipe_key);
    }

    /// 清全部两区条目（跨租户；全量执行数据清理时使用）。
    pub fn clear_all(&self) {
        self.keys.write().clear();
        self.bindings.write().clear();
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
}
