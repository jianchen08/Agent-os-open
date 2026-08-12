// @feature: FP-0.2.八 多租户核心系统 | @vision: V4 多用户 | @ci: rust-test

//! 管道注册表「用户旅程」串联验证（功能验证专用）
//!
//! 模拟真实用户多轮对话的完整闭环（对齐 server.rs process_via_engine 的
//! 热/冷路径装配语义）：
//!
//!   步骤1 冷启动注册（首轮消息 → get_or_init）
//!     → 步骤2 引擎跑完回写 final_state（update_state，模拟一轮对话结束）
//!     → 步骤3 下一轮热路径复用（重复 get_or_init 返回同一 Arc，读完整历史）
//!     → 步骤4 sequence 单调递增 + DB 续接不回退（next_sequence / init_sequence）
//!     → 步骤5 注销清理（remove → contains=false → 重建为冷启动）
//!     → 步骤6 租户隔离（tenant_a/tenant_b 的 pipe_x 互不可见）
//!
//! 每一步的输入是上一步的输出（同一 registry、同一 entry Arc、state 内容、
//! sequence 计数器延续），验证「管道 state 跨轮常驻」这一注册表核心职责。
//!
//! 运行：cargo test -p agentos-session --test registry_journey_verify

use agentos_session::PipelineStateRegistry;
use serde_json::{json, Value};
use std::sync::Arc;

const TENANT: &str = "default";

fn make_state(msgs: &[&str]) -> Value {
    let messages: Vec<Value> = msgs
        .iter()
        .map(|m| json!({"role": "user", "content": m}))
        .collect();
    json!({ "messages": messages, "raw_result": "" })
}

#[test]
fn test_user_journey_multi_turn_state_continuity() {
    let reg = PipelineStateRegistry::new();

    // ── 步骤1：冷启动注册（首轮消息，注册表无此管道）──
    assert!(!reg.contains(TENANT, "pipe_journey"));
    let entry = reg.get_or_init(TENANT, "pipe_journey", "thread_42", "agentos", make_state(&["你好"]));
    assert!(reg.contains(TENANT, "pipe_journey"));
    assert_eq!(entry.read().thread_id, "thread_42");
    assert_eq!(entry.read().agent_id, "agentos");
    assert_eq!(entry.read().state["messages"].as_array().unwrap().len(), 1);

    // ── 步骤2：引擎跑完回写 final_state（模拟一轮对话结束，assistant 已回复）──
    //    状态传递：步骤1 注册的 entry 是步骤2 的回写载体
    let final_state = json!({
        "messages": [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好，我是灵汐"}
        ],
        "raw_result": "你好，我是灵汐"
    });
    reg.update_state(TENANT, "pipe_journey", final_state);

    // ── 步骤3：下一轮热路径复用（状态传递：步骤2 的 final_state 是步骤3 的输入）──
    //    重复 get_or_init 应命中内存 entry（Arc 相同），state 是上一轮的完整对话
    let entry2 = reg.get_or_init(TENANT, "pipe_journey", "thread_42", "agentos", make_state(&["should_not_use"]));
    assert!(Arc::ptr_eq(&entry, &entry2), "热路径应返回同一 Arc");
    let guard = entry2.read();
    let msgs = guard.state["messages"].as_array().unwrap();
    assert_eq!(msgs.len(), 2, "热路径应读到上一轮的完整 messages");
    assert_eq!(msgs[1]["content"], "你好，我是灵汐");
    drop(guard);

    // ── 步骤4：sequence 单调递增 + DB 续接（状态传递：步骤3 的 entry 计数器延续）──
    assert_eq!(reg.next_sequence(TENANT, "pipe_journey"), Some(1));
    assert_eq!(reg.next_sequence(TENANT, "pipe_journey"), Some(2));
    assert_eq!(reg.next_sequence(TENANT, "pipe_journey"), Some(3));
    // DB 续接：模拟进程重启后从 DB 恢复 max=10，取 max(内存3, 10)=10，下次=11 不回退
    reg.init_sequence(TENANT, "pipe_journey", 10);
    assert_eq!(reg.next_sequence(TENANT, "pipe_journey"), Some(11), "续接后不应回退");
    // 未注册管道返回 None
    assert_eq!(reg.next_sequence(TENANT, "pipe_unknown"), None);

    // ── 步骤5：注销清理（状态传递：步骤4 的 entry 被移除，重建为冷启动）──
    reg.remove(TENANT, "pipe_journey");
    assert!(!reg.contains(TENANT, "pipe_journey"));
    let fresh = reg.get_or_init(TENANT, "pipe_journey", "thread_42", "agentos", make_state(&["fresh"]));
    assert_eq!(
        fresh.read().state["messages"].as_array().unwrap().len(),
        1,
        "remove 后重建应为冷启动（fresh state），而非复用旧历史"
    );

    // ── 步骤6：租户隔离（同一 pipeline_id 在不同租户下互不可见）──
    reg.get_or_init("tenant_a", "pipe_x", "t", "a", make_state(&["a-msg"]));
    assert!(reg.contains("tenant_a", "pipe_x"));
    assert!(!reg.contains("tenant_b", "pipe_x"), "租户 B 的 pipe_x 不应可见租户 A 的注册");
    reg.get_or_init("tenant_b", "pipe_x", "t2", "b", make_state(&["b-msg"]));
    // 两租户各自独立条目，互不影响
    let a = reg.get("tenant_a", "pipe_x").unwrap();
    let b = reg.get("tenant_b", "pipe_x").unwrap();
    assert!(!Arc::ptr_eq(&a, &b), "不同租户的 entry 必须是不同 Arc");
    assert_eq!(a.read().state["messages"].as_array().unwrap()[0]["content"], "a-msg");
    assert_eq!(b.read().state["messages"].as_array().unwrap()[0]["content"], "b-msg");
}
