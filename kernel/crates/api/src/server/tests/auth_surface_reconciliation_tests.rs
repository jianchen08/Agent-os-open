// @feature: FP-0.2.内核API面 | @vision: V3 可嵌入 | @ci: rust-test
use super::*;

/// 提取注册调用里的路径字面量：定位 `.route(`，跳过空白后取下一个
/// `"..."` 字符串；首参非字符串字面量（如 registry.route(&ns)）自然落空。
/// 只扫 `mod tests` 之前——测试内构造的 Router 不是注册面。
fn scan_registered_route_paths(src: &'static str) -> Vec<&'static str> {
    let body = src.split("mod tests").next().unwrap_or(src);
    const MARKER: &str = ".route(";
    let mut out = Vec::new();
    let mut rest = body;
    while let Some(pos) = rest.find(MARKER) {
        rest = &rest[pos + MARKER.len()..];
        if let Some(after) = rest.trim_start().strip_prefix('"') {
            if let Some(end) = after.find('"') {
                out.push(&after[..end]);
                rest = &after[end..];
            }
        }
    }
    out
}

/// 全量注册面 × 鉴权清单对账：每条路由必须落入恰一侧（豁免/任意用户/
/// 受保护前缀/域外），清单条目必须都在注册面——新路由漏登任一边即红。
#[test]
fn registered_routes_all_classified_and_lists_all_registered() {
    let mut paths: Vec<&str> = scan_registered_route_paths(include_str!("../../server.rs"));
    paths.extend(scan_registered_route_paths(include_str!(
        "../../http_dispatcher.rs"
    )));
    paths.sort_unstable();
    paths.dedup();
    // 性质断言：扫描必须枚举到足量注册面（对账测试自身防拟合——
    // 扫描器退化时全量通过是假绿）
    assert!(
        paths.len() >= 40,
        "注册面扫描仅得 {} 条，扫描器大概率失效（现役注册面 ≥40）",
        paths.len()
    );
    for &p in &paths {
        assert!(
            classify_auth_surface(p).is_some(),
            "路由 {p} 未落入鉴权面任何一侧——新路由须落在受保护前缀内，\
                 或显式登记进对应清单并注明缘由（ADR 2026-10-10 枚举制）"
        );
    }
    // 双向对账：清单条目必须都在注册面（路由下线后的过期条目即红）
    for &e in AUTH_ANONYMOUS_ALLOW
        .iter()
        .chain(AUTH_ANY_USER_ALLOW.iter())
        .chain(AUTH_OUTSIDE_MIDDLEWARE.iter())
    {
        assert!(
            paths.contains(&e),
            "清单条目 {e} 不在注册面（过期条目须随路由移除）"
        );
    }
    // 每个受保护前缀至少覆盖一条注册路由（前缀清单无悬空成员）
    for &pfx in AUTH_PROTECTED_PREFIXES {
        assert!(
            paths.iter().any(|p| p.starts_with(pfx)),
            "受保护前缀 {pfx} 未覆盖任何注册路由（悬空前缀）"
        );
    }
}

/// 分类矩阵 + 假想路由防拟合：保护语义来自前缀与清单，不是逐路由拟合。
#[test]
fn classification_matrix_and_default_deny_for_hypothetical_routes() {
    use AuthSurfaceClass as C;
    // 清单成员各自归位（可枚举输入全展开）
    for &p in AUTH_ANONYMOUS_ALLOW {
        assert_eq!(classify_auth_surface(p), Some(C::AnonymousAllow), "{p}");
    }
    for &p in AUTH_ANY_USER_ALLOW {
        assert_eq!(classify_auth_surface(p), Some(C::AnyUser), "{p}");
    }
    for &p in AUTH_OUTSIDE_MIDDLEWARE {
        assert_eq!(classify_auth_surface(p), Some(C::OutsideMiddleware), "{p}");
    }
    // 假想新路由（未注册、不在任何清单）：受保护前缀内缺省 Protected
    // ——default-deny 语义断言（翻转的本义：漏登不再等于匿名放行）
    for p in [
        "/api/v1/brand-new-surface",
        "/api/v1/x/y/z",
        "/uploads/anything.mp4",
        "/ws/new-endpoint",
        "/metrics",
    ] {
        assert_eq!(classify_auth_surface(p), Some(C::Protected), "{p}");
    }
    // 假想新路由落在前缀外且未登记 → None：对账测试据此判红
    for p in ["/v2/brand-new", "/internal/dump", "/"] {
        assert_eq!(classify_auth_surface(p), None, "{p}");
    }
    // 豁免是精确路径：子路径/尾斜杠/前缀拼缀不被豁免吞掉
    for p in [
        "/api/v1/auth/login/",
        "/api/v1/auth/loginx",
        "/ws/chat/extra",
    ] {
        assert_eq!(classify_auth_surface(p), Some(C::Protected), "{p}");
    }
    // 精确成员不受尾缀影响（/metrics 无子面）
    assert_eq!(classify_auth_surface("/metricsx"), None);
}
