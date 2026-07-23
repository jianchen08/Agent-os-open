//! # Lingxi Tenant — 多租户上下文
//!
//! 通过 `tokio::task_local!` 穿透整个异步调用栈，插件代码无需感知租户参数。
//! 跨 `tokio::spawn` 会丢失（除非显式 scope），天然防止跨管道/跨租户泄露。
//!
//! [来源: docs/0.2_rust_plugin_solution.md §3.4]

use std::future::Future;

use agentos_core::types::TenantContext;

tokio::task_local! {
    /// 当前 task 的租户上下文。
    ///
    /// 跨 `tokio::spawn` 会丢失——这是 feature（防跨租户泄露）。
    /// 子任务若需要继承租户，必须在 spawn 后显式调用 [`scope`] 重新建立。
    pub static TENANT_CTX: TenantContext;
}

/// 读取当前 task 的租户上下文。
///
/// 未在 [`scope`] 内调用时返回 `None`。
pub fn current() -> Option<TenantContext> {
    TENANT_CTX.try_with(|c| c.clone()).ok()
}

/// 读取当前 task 的租户上下文，未设置时回退到默认租户。
///
/// `default_tenant_id` 仅在没有活跃 task_local 时作为 fallback；
/// 已设置时直接返回当前上下文（即使 tenant_id 不同）。
pub fn current_or_default(default_tenant_id: &str) -> TenantContext {
    current().unwrap_or_else(|| TenantContext::new(default_tenant_id, ""))
}

/// 在 `ctx` 的作用域内执行异步 future，使其内部可通过 [`current`] 读到租户。
///
/// 跨 `tokio::spawn` 时上下文不会自动传播——子任务需再次 `scope`。
pub async fn scope<F, R>(ctx: TenantContext, f: F) -> R
where
    F: Future<Output = R>,
{
    TENANT_CTX.scope(ctx, f).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn current_returns_none_outside_scope() {
        assert!(current().is_none());
    }

    #[tokio::test]
    async fn scope_propagates_context() {
        let ctx = TenantContext::new("tenant_a", "session_1");
        let tenant_id = scope(ctx.clone(), async { current().unwrap().tenant_id }).await;
        assert_eq!(tenant_id, "tenant_a");
    }

    #[tokio::test]
    async fn current_or_default_falls_back_when_unset() {
        let ctx = current_or_default("fallback_tenant");
        assert_eq!(ctx.tenant_id, "fallback_tenant");
    }

    #[tokio::test]
    async fn current_or_default_returns_active_context() {
        let ctx = TenantContext::new("active_tenant", "session_2");
        let tenant_id = scope(ctx, async {
            current_or_default("fallback_tenant").tenant_id
        })
        .await;
        assert_eq!(tenant_id, "active_tenant");
    }

    #[tokio::test]
    async fn context_does_not_leak_after_scope() {
        let ctx = TenantContext::new("ephemeral_tenant", "s");
        scope(ctx, async {}).await;
        assert!(current().is_none());
    }

    /// 验证 task_local 不跨 spawn 传播（隔离 feature）。
    #[tokio::test]
    async fn context_does_not_propagate_across_spawn() {
        let ctx = TenantContext::new("parent_tenant", "s");
        scope(ctx, async {
            let handle = tokio::spawn(async { current().is_some() });
            assert!(!handle.await.unwrap());
        })
        .await;
    }
}
