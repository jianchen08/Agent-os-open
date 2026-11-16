//! 轮次观察点（引擎 → 消费方回调）：agent 循环体每次迭代 = 一个 LLM 轮次。
//!
//! 引擎在迭代开始前为本轮分配消息 id 并注入运行状态（`state["message_id"]`，
//! llm_core/tool_core 均读它携带路由键），随后回调 [`RoundEvents::on_round_start`]；
//! 迭代结束后回调 [`RoundEvents::on_round_end`]，携带本轮新增 assistant 消息的
//! 完整持久形态（None = 本轮无产出）。消费方（api 层）把两次回调翻译为流式
//! 契约事件（stream_start/new_message/stream_end），使「一轮 = 一条消息」的
//! 实时面与 message_slots 逐轮持久化面同构（DSH 形态：每轮独立 message_id、
//! 渲染是事件的纯投影，流式与重放天然一致）。
//!
//! 仅在**循环体**（loop 模式）迭代时回调：非循环体（init/exit 前处理收尾体）
//! 不是 agent 回合，不产生轮次消息。回调按顺序 await——发射顺序与轮次严格一致，
//! 不依赖并发调度。

use std::future::Future;
use std::pin::Pin;

use serde_json::Value;

/// 轮次开始信息（引擎已把本轮 message_id 注入 `state["message_id"]`）。
#[derive(Debug, Clone)]
pub struct RoundStart {
    /// 轮次序号（per-run 从 1 递增）。
    pub round_index: i64,
    /// 本轮消息 id（`a_<uuid>` 内核命名空间——消费方按 stream_start 占位）。
    pub message_id: String,
    /// 入口保留的槽位序号（ADR 2026-11-06-round-entry-slot-placeholder 决策1：
    /// 内存消息 max+1，与出口 append 的 resolve 同基准——占位行与终态消息同槽，
    /// seq 分配权单点）。占位行建行失败（降级旧世界）时仍是本轮消息的预期落槽。
    pub sequence: i64,
    pub pipeline_id: String,
    pub thread_id: String,
}

/// 轮次结束信息。
#[derive(Debug, Clone)]
pub struct RoundEnd {
    pub round_index: i64,
    pub message_id: String,
    pub pipeline_id: String,
    pub thread_id: String,
    /// 本轮新增的 assistant 消息（state["messages"] 中的完整持久形态）。
    /// None = 本轮无 assistant 产出（消费方只发 stream_end，不发 new_message）。
    pub assistant: Option<Value>,
    /// 本轮（首轮）对应的 run user 消息：消费方据此构建 new_message 的
    /// user_message 认领回传（仅 round_index == 1 携带，其余为 None）。
    pub user_message: Option<Value>,
}

/// 注入条目权威 record（认领回传载荷；排序键不变量：终态转换必携权威序号）。
/// message_id = 内容指纹（`compute_message_id`，与 REST 读路径 record_id 同源）、
/// sequence = 引擎槽位（前端排序键）、content = 条目原文（本地乐观气泡缺失时
/// insert 补插用）。
#[derive(Debug, Clone)]
pub struct ConsumedPendingRecord {
    pub client_message_id: String,
    pub message_id: String,
    pub sequence: i64,
    pub content: String,
}

/// 待输入队列轮边界消费信息（设计真值 docs/working/待输入队列轮边界注入_方案_20261025.md）。
///
/// 引擎在 llm_call 迭代开轮前原子 pop 待输入队列并注入 user 消息后回调；
/// 消费方（api 层）据此发 `pending_inputs_changed("consumed")`，前端把队列条
/// 移入主消息流并按 records 走认领（与正常发送同一条 claim 链）。
#[derive(Debug, Clone)]
pub struct PendingConsumed {
    pub pipeline_id: String,
    /// 本次注入条目的 client_message_id（FIFO 序；REST /chat 同步桥按它寻址）。
    pub client_message_ids: Vec<String>,
    /// 逐条权威 record（与 client_message_ids 同序同长，仅含非空 cmid 条目）。
    pub records: Vec<ConsumedPendingRecord>,
}

/// 轮次事件回调（见模块头注释）。
pub trait RoundEvents: Send + Sync {
    fn on_round_start(&self, ev: RoundStart) -> Pin<Box<dyn Future<Output = ()> + Send + '_>>;
    fn on_round_end(&self, ev: RoundEnd) -> Pin<Box<dyn Future<Output = ()> + Send + '_>>;
    /// 待输入队列轮边界消费回调（默认空实现：不关心队列面的消费方可不实现）。
    fn on_pending_inputs_consumed(
        &self,
        _ev: PendingConsumed,
    ) -> Pin<Box<dyn Future<Output = ()> + Send + '_>> {
        Box::pin(async {})
    }
}
