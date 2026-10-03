//! M1 e2e 场景复现：CONDITION 触发器「值不等」条件经 store 提交锚点的边沿点火。
//!
//! 复刻生产链路：注册 T2（其持久化即 state 写源）→ 注册 T1（条件 `K2 != '<v1>'`，
//! 种子=假）→ 改写 K2（值变）→ 断言 T1 恰好点火一次。

use agentos_engine::condition::parse_condition;
use agentos_engine::store::SqliteStore;
use agentos_engine::trigger::{global_registry, TriggerRegistration};
use serde_json::json;

#[test]
fn anchor_fires_on_registry_value_change() {
    let store = SqliteStore::open_memory().unwrap();
    let pid = "thread-e2e";
    let tenant = "default";
    let k2 = "task.trigger.registry.trigger_condition_873d9629bd4d";

    // T2 注册持久化：K2 = v1（写经锚点覆盖的 upsert_state_fields）。
    let v1 = json!({
        "action": "notify", "condition_expression": "1 == 2", "fire_count": 0,
        "message": "[e2e] T2", "status": "active",
        "trigger_id": "trigger_condition_873d9629bd4d", "trigger_type": "condition",
    });
    let mut fields = serde_json::Map::new();
    fields.insert(k2.to_string(), v1.clone());
    store.upsert_state_fields(pid, tenant, &fields).unwrap();

    // T1 注册（内核同款入口：注册方装载种子行 → 种子求值应=假）。
    // 生产 e2e 实际条件形态：对象字段下钻（读面把 JSON 文本解码回对象）。
    let condition_src = format!("{k2}.fire_count != 0");
    let seed_row = store
        .load_pipeline_state(pid, tenant)
        .map(|map| serde_json::Value::Object(map.into_iter().collect()))
        .unwrap();
    let seed_fired = global_registry().register(
        TriggerRegistration {
            trigger_id: "t1".into(),
            tenant_id: tenant.into(),
            pipeline_id: Some(pid.into()),
            condition: parse_condition(&condition_src).unwrap(),
            watched_keys: vec![],
            owner_plugin_id: "trigger_setup_tool".into(),
        },
        &seed_row,
    );
    assert!(!seed_fired, "种子求值必须为假（K2 当前值 == 字面量）");

    // K2 值变化（模拟 manual fire 后的 persist）。
    let v2 = json!({
        "action": "notify", "condition_expression": "1 == 2", "fire_count": 1,
        "message": "[e2e] T2", "status": "active",
        "trigger_id": "trigger_condition_873d9629bd4d", "trigger_type": "condition",
    });
    let mut fields2 = serde_json::Map::new();
    fields2.insert(k2.to_string(), v2);
    store.upsert_state_fields(pid, tenant, &fields2).unwrap();

    let fires = global_registry().reconcile();
    assert_eq!(fires.len(), 1, "T1 应恰好点火一次，实际: {fires:?}");
    assert_eq!(fires[0].trigger_id, "t1");
    assert_eq!(fires[0].view, "committed");
}
