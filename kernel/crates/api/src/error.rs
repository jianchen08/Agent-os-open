//! API 错误类型（统一实现已下沉至 agentos-http crate，此处再导出保持既有引用不变）

pub use agentos_http::error::ApiError;
