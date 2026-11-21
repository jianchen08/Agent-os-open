//! 表面鉴权分类与 CORS 中间件（路由装配的对账判定面）。

use super::*;
/// 鉴权层域外（鉴权另有所属，对账清单）：
/// - /health：存活探测（release.yml 冒烟与装机探测），无数据面；
/// - /ext/{*rest}：插件声明制鉴权（manifest auth 字段缺省 401 fail-closed，
///   ADR 2026-09-11），由 http_dispatcher 自持。
pub(super) const AUTH_OUTSIDE_MIDDLEWARE: &[&str] = &["/health", "/ext/{*rest}"];

/// 鉴权面分类（对账枚举的判定结果；`None` = 路径未落入任何一侧）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum AuthSurfaceClass {
    /// 匿名豁免（handler 自鉴权或登录前入口）
    AnonymousAllow,
    /// 任意已认证用户，无角色门
    AnyUser,
    /// 受保护：GET → admin/viewer，其余 method → admin
    Protected,
    /// 鉴权层域外（/health 探测、/ext 声明制）
    OutsideMiddleware,
}

/// 路径 → 鉴权面分类。豁免/任意用户清单精确匹配优先于受保护前缀；未落入
/// 任何一侧返回 None——注册面出现此类路径时对账测试判红（新路由必须在
/// 受保护前缀内，或显式登记进某一清单并注明缘由）。
pub(super) fn classify_auth_surface(path: &str) -> Option<AuthSurfaceClass> {
    if AUTH_ANONYMOUS_ALLOW.contains(&path) {
        return Some(AuthSurfaceClass::AnonymousAllow);
    }
    if AUTH_ANY_USER_ALLOW.contains(&path) {
        return Some(AuthSurfaceClass::AnyUser);
    }
    if AUTH_PROTECTED_PREFIXES.iter().any(|p| path.starts_with(p))
        || AUTH_PROTECTED_EXACT.contains(&path)
    {
        return Some(AuthSurfaceClass::Protected);
    }
    if AUTH_OUTSIDE_MIDDLEWARE.contains(&path) {
        return Some(AuthSurfaceClass::OutsideMiddleware);
    }
    None
}

/// 内核静态面鉴权中间件（default-deny 枚举制，ADR 2026-10-10）。
///
/// `needs_auth = 受保护路径 && !匿名豁免`：命中受保护前缀且不在豁免清单的
/// 路径按 method 分权（GET → admin/viewer，POST/PUT/PATCH/DELETE → admin，
/// require_surface_role）；受保护前缀内新注册的路由缺省即受保护（枚举制
/// 翻转的本意），前缀外新路由须登记进域外清单，否则对账测试红。豁免清单
/// 与域外清单逐项缘由见 [AUTH_ANONYMOUS_ALLOW] / [AUTH_OUTSIDE_MIDDLEWARE]。
pub(crate) async fn write_surface_auth(
    State(state): State<AppState>,
    req: Request,
    next: Next,
) -> Result<Response, ApiError> {
    let path = req.uri().path();
    let method = req.method().clone();

    // WS 票据签发：任意已认证用户（user/admin 均可），不做角色区分——
    // 票据只是本人 WS 握手凭证（单次消费 + 60s TTL），非管理面写操作。
    if AUTH_ANY_USER_ALLOW.contains(&path) {
        agentos_http::auth::resolve_request_user(state.store.as_ref(), req.headers()).await?;
        return Ok(next.run(req).await);
    }

    let needs_auth = matches!(
        classify_auth_surface(path),
        Some(AuthSurfaceClass::Protected)
    );

    if !needs_auth {
        return Ok(next.run(req).await);
    }

    let result = match method {
        Method::GET => require_surface_role(&state, req.headers(), false).await,
        _ => require_surface_role(&state, req.headers(), true).await,
    };
    match result {
        Ok(_) => Ok(next.run(req).await),
        Err(e) => Err(e),
    }
}

/// 受保护面角色校验（原 db_routes::require_read_role /
/// require_admin_role 的 api 侧等价实现——db_routes 已拆至 db-admin crate，
/// 此处用 api 自身的 resolve_request_user（agentos-http 单一实现），
/// 语义与错误消息保持与拆分前一致）。
pub(crate) async fn require_surface_role(
    state: &AppState,
    headers: &HeaderMap,
    write: bool,
) -> Result<(), ApiError> {
    let (_, _, role, _) =
        agentos_http::auth::resolve_request_user(state.store.as_ref(), headers).await?;
    let ok = if write {
        role == "admin"
    } else {
        role == "admin" || role == "viewer"
    };
    if ok {
        Ok(())
    } else if write {
        Err(ApiError::Forbidden {
            message: "写操作需要 admin 角色".to_string(),
        })
    } else {
        Err(ApiError::Forbidden {
            message: "需要 admin 或 viewer 角色".to_string(),
        })
    }
}

/// CORS 中间件：拦截 OPTIONS 预检（返回 204 + CORS 头），并给所有响应注入 CORS 头。
/// 反射请求 Origin（开发友好，支持任意 localhost/自定义前端源）。凭据策略：
/// 内核认证走 Authorization Bearer 头（非 cookie），本地开发源不回
/// Allow-Credentials；显式配置的生产白名单源保留凭据放行。
pub(crate) async fn cors_middleware(req: Request, next: Next) -> Response {
    let origin = req
        .headers()
        .get(header::ORIGIN)
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string());

    // 预检请求：直接回 204 + CORS 头，不进入业务路由（业务路由未注册 OPTIONS 会 405）
    if req.method() == Method::OPTIONS {
        let mut resp = Response::builder()
            .status(StatusCode::NO_CONTENT)
            .body(Body::empty())
            .expect("empty cors preflight body");
        apply_cors_headers(resp.headers_mut(), origin.as_deref());
        return resp;
    }

    let mut resp = next.run(req).await;
    apply_cors_headers(resp.headers_mut(), origin.as_deref());
    resp
}

/// 判断 origin 是否为本地开发源（localhost / 127.0.0.1 / [::1]，任意端口）。
///
/// base 后必须紧跟 `:`（端口分隔），防止 `localhost.evil.com` 这类前缀绕过。
pub(super) fn is_local_origin(origin: &str) -> bool {
    const LOCAL_BASES: [&str; 6] = [
        "http://localhost",
        "https://localhost",
        "http://127.0.0.1",
        "https://127.0.0.1",
        "http://[::1]",
        "https://[::1]",
    ];
    for base in LOCAL_BASES {
        if origin == base {
            return true;
        }
        if let Some(rest) = origin.strip_prefix(base) {
            if rest.starts_with(':') {
                return true;
            }
        }
    }
    false
}

/// 精确匹配 origin 是否在生产白名单中（不做子域/前缀模糊匹配）。
pub(super) fn origin_matches_allowlist(origin: &str, allowlist: &[&str]) -> bool {
    allowlist.contains(&origin)
}

/// 判断 origin 是否被放行：本地源任意端口，或命中 `AGENTOS_CORS_ORIGINS` 白名单。
///
/// 生产白名单由环境变量 `AGENTOS_CORS_ORIGINS` 提供（逗号分隔的完整 origin，
/// 如 `https://app.example.com,https://www.example.com`）。未配置时仅放行本地源。
pub(super) fn is_origin_allowed(origin: &str) -> bool {
    if is_local_origin(origin) {
        return true;
    }
    if let Ok(raw) = std::env::var("AGENTOS_CORS_ORIGINS") {
        let entries: Vec<&str> = raw
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .collect();
        return origin_matches_allowlist(origin, &entries);
    }
    false
}

/// 注入 CORS 响应头。仅当 origin 被放行时才反射 Origin；Allow-Credentials 只给
/// 显式配置的生产白名单源（`AGENTOS_CORS_ORIGINS`）——本地开发源反射但不带凭据头。
/// 内核认证走 Authorization Bearer 头（前端 apiClient 注入，不依赖 cookie），
/// 本地源去掉凭据头不影响任何合法调用，同时消除「反射任意 localhost 源 +
/// Allow-Credentials」被恶意本地页面带凭据跨域调用的面。其余头固定。
pub(super) fn apply_cors_headers(headers: &mut HeaderMap, origin: Option<&str>) {
    if let Some(o) = origin {
        if is_origin_allowed(o) {
            if let Ok(v) = HeaderValue::from_str(o) {
                headers.insert(header::ACCESS_CONTROL_ALLOW_ORIGIN, v);
            }
            // 凭据放行仅限显式生产白名单（本地源反射不附带）。
            let explicit_allowlisted = std::env::var("AGENTOS_CORS_ORIGINS")
                .map(|raw| raw.split(',').map(str::trim).any(|e| e == o))
                .unwrap_or(false);
            if explicit_allowlisted {
                headers.insert(
                    header::ACCESS_CONTROL_ALLOW_CREDENTIALS,
                    HeaderValue::from_static("true"),
                );
            }
        }
    }
    headers.insert(
        header::ACCESS_CONTROL_ALLOW_METHODS,
        HeaderValue::from_static("GET, POST, PUT, PATCH, DELETE, OPTIONS"),
    );
    headers.insert(
        header::ACCESS_CONTROL_ALLOW_HEADERS,
        HeaderValue::from_static(
            "Content-Type, Authorization, X-Requested-With, X-Auth-Token, Accept, Origin",
        ),
    );
    headers.insert(
        header::ACCESS_CONTROL_MAX_AGE,
        HeaderValue::from_static("86400"),
    );
}
