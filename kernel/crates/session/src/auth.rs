//! WS 握手鉴权——token 校验 + 拒绝码（ADR §7.2，参考 0.1 app_factory.py:232）。
//!
//! token 校验复用 api crate 的 `verify_access_token`；本模块通过 verifier 闭包
//! 注入，保持 session crate 不直接依赖 api。ws_handler 在握手失败时 accept+close。

/// 拒绝码：token 缺失。
pub const REJECT_CODE_NO_TOKEN: u16 = 4001;
/// 拒绝码：token 无效或已过期。
pub const REJECT_CODE_INVALID_TOKEN: u16 = 4001;
/// 踢旧关闭码：本账号在其他位置登录（B10，前端收到不自动重连）。
pub const CLOSE_CODE_KICKED: u16 = 4004;

/// 握手鉴权结果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HandshakeAuth {
    /// 鉴权通过，附带已校验的用户身份。
    Ok { user_id: String, username: String },
    /// 鉴权拒绝，附带关闭码与原因。
    Rejected { code: u16, reason: String },
}

/// 校验 token 并返回 (user_id, username)，失败返回 None。
///
/// 注入式设计：ws_handler 传入 api crate 的 `verify_access_token` 作为 verifier，
/// session crate 本身不依赖 api。返回 `Ok((user_id, username))` 当 token 合法。
pub fn authenticate_handshake<F>(token: &str, verifier: &F) -> HandshakeAuth
where
    F: Fn(&str) -> Option<(String, String)>,
{
    if token.is_empty() {
        return HandshakeAuth::Rejected {
            code: REJECT_CODE_NO_TOKEN,
            reason: "全局连接需要 token 认证".to_string(),
        };
    }
    match verifier(token) {
        Some((user_id, username)) => HandshakeAuth::Ok { user_id, username },
        None => HandshakeAuth::Rejected {
            code: REJECT_CODE_INVALID_TOKEN,
            reason: "Token 无效或已过期".to_string(),
        },
    }
}

