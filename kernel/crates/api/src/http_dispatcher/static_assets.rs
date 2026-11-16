//! /ext 静态资源直读面（http_dispatcher 子模块）：插件目录 web/ 资产的
//! 路径解析、体积上限读取与扩展名 mime 映射；`mime_for_extension` 经父模块
//! 再导出供 routes 内核静态路由共用。
use std::collections::HashMap;

/// 静态资源单文件大小上限（字节）：超限直接 413 拒绝，不整读入内存。
/// 插件前端资源（JS/CSS/图片）正常远小于此；上限只挡异常巨物（误放文件/
/// 恶意构造），同时约束单请求读盘的内存峰值。
pub(super) const STATIC_ASSET_MAX_BYTES: u64 = 8 * 1024 * 1024;

/// 尝试把 `/ext/{plugin_id}/assets/{*rest}` 解析为插件 `web/` 子目录下的文件并直读返回。
///
/// 返回：
/// - `Some(response)` —— 路径形态匹配静态资源（无论命中/未命中/被拒），由调用方直接返回；
/// - `None` —— 不是静态资源路径形态，或该插件未声明目录，调用方应继续走 dispatcher。
///
/// 路径安全：
/// - 拒绝 `..` 段；
/// - canonicalize 后必须仍在插件 `web/` 子树内（防 symlink 逃逸）。
///
/// 文件读取契约：整读移出 async 执行器（`spawn_blocking`），且先 stat 校验
/// [`STATIC_ASSET_MAX_BYTES`] 上限——超限 413 拒绝，不整读超限文件。
pub(super) async fn try_serve_static_asset(
    path: &str,
    plugin_dirs: &HashMap<String, std::path::PathBuf>,
) -> Option<axum::response::Response> {
    use axum::body::Body;
    use axum::http::{HeaderName, HeaderValue, StatusCode};
    use axum::response::IntoResponse;

    // 解析 /ext/{plugin_id}/assets/{rest}。
    let stripped = path.strip_prefix("/ext/")?;
    let mut iter = stripped.splitn(3, '/');
    let plugin_id = iter.next()?;
    let mid = iter.next()?;
    if mid != "assets" {
        return None; // 非 assets 子路径：交回 dispatcher
    }
    let rest = iter.next().unwrap_or("");
    // 空路径（/ext/{plugin}/assets 或 /ext/{plugin}/assets/）：交回 dispatcher，
    // 让插件自己决定要不要给一个 http_endpoint 处理目录索引。
    if rest.is_empty() {
        return None;
    }

    let plugin_dir = plugin_dirs.get(plugin_id)?;
    // 路径安全：先拒 .. 段（canonicalize 之前），防止 ./.. 逃逸。
    for seg in rest.split('/') {
        if seg == ".." {
            return Some((StatusCode::NOT_FOUND, "not found").into_response());
        }
    }
    let web_root = plugin_dir.join("web");
    // web/ 不存在 → 此插件不托管静态资源，交回 dispatcher。
    if !web_root.exists() {
        return None;
    }

    let file_path = web_root.join(rest);

    // canonicalize 防 symlink 逃逸：file 必须在 web_root 子树内。
    let canonical_web = match std::fs::canonicalize(&web_root) {
        Ok(p) => p,
        Err(_) => return None,
    };
    let canonical_file = match std::fs::canonicalize(&file_path) {
        Ok(p) => p,
        Err(_) => {
            // 文件不存在 → 此路径形态确实归静态资源所有，直接 404（不交回 dispatcher，
            // 否则 dispatcher 也会 404，但语义上 /assets/** 是静态资源命名空间）。
            return Some((StatusCode::NOT_FOUND, "not found").into_response());
        }
    };
    if !canonical_file.starts_with(&canonical_web) || !canonical_file.is_file() {
        return Some((StatusCode::NOT_FOUND, "not found").into_response());
    }

    // 整读在阻塞线程执行（async 执行器上不做同步磁盘 IO），先 stat 校验
    // 上限——超限 413 拒绝，不整读超限文件。
    let read_path = canonical_file.clone();
    let read = tokio::task::spawn_blocking(move || read_static_asset_capped(&read_path)).await;
    let bytes = match read {
        Ok(Ok(bytes)) => bytes,
        Ok(Err(StaticAssetReadError::TooLarge)) => {
            return Some(
                (
                    StatusCode::PAYLOAD_TOO_LARGE,
                    "static asset exceeds size limit",
                )
                    .into_response(),
            );
        }
        Ok(Err(StaticAssetReadError::Io)) => {
            return Some((StatusCode::NOT_FOUND, "not found").into_response());
        }
        Err(_) => {
            return Some(
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "static asset read task failed",
                )
                    .into_response(),
            );
        }
    };

    let mime = mime_for_extension(
        canonical_file
            .extension()
            .and_then(|s| s.to_str())
            .unwrap_or(""),
    );

    let mut builder = axum::response::Response::builder().status(StatusCode::OK);
    if let (Ok(name), Ok(val)) = (
        HeaderName::try_from("content-type"),
        HeaderValue::try_from(mime),
    ) {
        builder = builder.header(name, val);
    }
    // 静态资源通常可缓存；这里给一个保守的 no-cache（开发期 SPA 热更新友好）。
    if let (Ok(name), Ok(val)) = (
        HeaderName::try_from("cache-control"),
        HeaderValue::try_from("no-cache"),
    ) {
        builder = builder.header(name, val);
    }
    Some(builder.body(Body::from(bytes)).unwrap_or_else(|_| {
        (StatusCode::INTERNAL_SERVER_ERROR, "response build failed").into_response()
    }))
}

/// 单文件读取结果：超限 / IO 失败分立（分别映射 413 / 404；IO 细节按既有
/// 契约不外泄——读失败统一 404）。
pub(super) enum StaticAssetReadError {
    TooLarge,
    Io,
}

/// 阻塞线程内的静态资源读取：先 stat 校验大小上限，再整读。
pub(super) fn read_static_asset_capped(
    path: &std::path::Path,
) -> Result<Vec<u8>, StaticAssetReadError> {
    let len = match std::fs::metadata(path) {
        Ok(m) => m.len(),
        Err(_) => return Err(StaticAssetReadError::Io),
    };
    if len > STATIC_ASSET_MAX_BYTES {
        return Err(StaticAssetReadError::TooLarge);
    }
    std::fs::read(path).map_err(|_| StaticAssetReadError::Io)
}

/// 扩展名 → Content-Type 映射表（覆盖常见 web 资源类型）。
///
/// 未命中扩展名统一回退到 `application/octet-stream`（浏览器嗅探可识别大部分文本）。
/// 不引入 mime crate —— 任务范围控制依赖膨胀，常见 web 类型手写映射足够。
/// 单一来源（2026-08-24 合并）：`/uploads`（routes.rs）与 `/ext/{plugin}/assets`
/// 两处静态资源出口共用本映射，扩展名集合取两处并集。
pub(crate) fn mime_for_extension(ext: &str) -> &'static str {
    match ext.to_ascii_lowercase().as_str() {
        "html" | "htm" => "text/html; charset=utf-8",
        "js" | "mjs" => "application/javascript; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "json" => "application/json; charset=utf-8",
        "map" => "application/json",
        "txt" | "md" => "text/plain; charset=utf-8",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "avif" => "image/avif",
        "ico" => "image/x-icon",
        "woff" => "font/woff",
        "woff2" => "font/woff2",
        "ttf" => "font/ttf",
        "otf" => "font/otf",
        "eot" => "application/vnd.ms-fontobject",
        "wasm" => "application/wasm",
        "pdf" => "application/pdf",
        "xml" => "application/xml; charset=utf-8",
        "mp4" => "video/mp4",
        "webm" => "video/webm",
        "mp3" => "audio/mpeg",
        "wav" => "audio/wav",
        "m4a" => "audio/mp4",
        "ogg" => "audio/ogg",
        _ => "application/octet-stream",
    }
}

/// 把 manifest 路径约定（{param}/{param:path}）转成 axum 0.8 路径参数语法（:param/*param）。
///
/// - `{name}` → `:name`（单段捕获）
/// - `{name:path}` → `*name`（剩余多段通配，axum 0.8 用 `*` 前缀）
/// - 其他段原样保留。
#[cfg(test)]
pub(super) fn manifest_path_to_axum(manifest_path: &str) -> String {
    manifest_path
        .split('/')
        .map(|seg| {
            if seg.starts_with('{') && seg.ends_with('}') {
                let inner = &seg[1..seg.len() - 1]; // 去掉 { }
                if let Some(name) = inner.strip_suffix(":path") {
                    format!("{{*{name}}}") // axum 0.8 多段通配语法 {*name}
                } else {
                    format!("{{{inner}}}") // axum 0.8 单段捕获语法 {name}
                }
            } else {
                seg.to_string()
            }
        })
        .collect::<Vec<_>>()
        .join("/")
}

/// HeaderMap → HashMap<String,String>（多值取首个，key 转小写）。
pub(super) fn header_map_to_hashmap(headers: &axum::http::HeaderMap) -> HashMap<String, String> {
    let mut map = HashMap::new();
    for (k, v) in headers.iter() {
        let key = k.as_str().to_lowercase();
        if let Ok(val) = v.to_str() {
            map.entry(key).or_insert_with(|| val.to_string());
        }
    }
    map
}
