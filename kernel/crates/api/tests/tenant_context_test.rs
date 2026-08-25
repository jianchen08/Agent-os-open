// @feature: FP-0.2.八 多租户核心系统 | @vision: V4 多用户 | @ci: rust-test
//! F-TENANT-B-KERNEL：`tenant-context.get` capability 测试。
//!
//! Python 侧 `plugins/shared/tenant_data.py` 经此能力取当前租户 ID 决定数据根
//! （方案 B 目录隔离）。契约：
//! - 无活跃 task_local scope → 返回 `tenant_id: "default"`（与 Python 侧回退一致，永不报错）；
//! - 在 `agentos_tenant::scope` 内 → 返回 scope 的 tenant_id/session_id。
//!
//! 本测试锁定「Python 侧多租户通路真实可用」这一目标：tenant-context 必须
//! 由 capability_router 显式路由（不得落在 catch-all"未实现"）。

use agentos_api::capability_router::KernelCapabilityRouter;
use agentos_api::metrics::MetricsAggregator;
use agentos_mcp::CapabilityRouter;
use serde_json::json;

fn router() -> KernelCapabilityRouter {
    KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
}

#[tokio::test]
async fn tenant_context_get_returns_default_without_scope() {
    let r = router();
    let v = r
        .handle("tenant-context", "get", json!({}))
        .await
        .expect("tenant-context.get 不应报错（无 scope 回退 default）");
    assert_eq!(
        v["tenant_id"], "default",
        "无活跃 task_local 应回退 default: {v}"
    );
    assert!(v["session_id"].is_string());
}

#[tokio::test]
async fn tenant_context_get_returns_active_tenant_inside_scope() {
    let r = router();
    let ctx = agentos_core::types::TenantContext::new("tenant-A", "sess-1");
    agentos_tenant::scope(ctx, async {
        let v = r
            .handle("tenant-context", "get", json!({}))
            .await
            .expect("tenant-context.get 在 scope 内应返回当前租户");
        assert_eq!(v["tenant_id"], "tenant-A", "应返回 scope 的 tenant_id: {v}");
        assert_eq!(v["session_id"], "sess-1", "应返回 scope 的 session_id: {v}");
    })
    .await;
}
