// @feature: FP-0.2.八 多租户核心系统 | @vision: V4 多用户 | @ci: rust-test

//! auth 测试——握手鉴权拒绝码 / 通过分支（ADR §7.2，参考 0.1 app_factory.py:232）。

use agentos_session::auth::{
    authenticate_handshake, HandshakeAuth, REJECT_CODE_INVALID_TOKEN, REJECT_CODE_NO_TOKEN,
};

#[tokio::test]
async fn handshake_rejects_missing_token() {
    // 无 token → 4001 NO_TOKEN
    let result = authenticate_handshake("", &|_| Some(("u1".into(), "alice".into())));
    match result {
        HandshakeAuth::Rejected { code, reason } => {
            assert_eq!(code, REJECT_CODE_NO_TOKEN);
            assert!(!reason.is_empty(), "拒绝原因应非空");
        }
        HandshakeAuth::Ok { .. } => panic!("空 token 必须拒绝"),
    }
}

#[tokio::test]
async fn handshake_rejects_when_verifier_returns_none() {
    // token 存在但 verifier 判定无效 → 4001 INVALID_TOKEN
    let result = authenticate_handshake("bad-token", &|_| None);
    match result {
        HandshakeAuth::Rejected { code, reason } => {
            assert_eq!(code, REJECT_CODE_INVALID_TOKEN);
            assert!(!reason.is_empty(), "拒绝原因应非空");
        }
        HandshakeAuth::Ok { .. } => panic!("无效 token 必须拒绝"),
    }
}

#[tokio::test]
async fn handshake_passes_with_valid_token() {
    let result = authenticate_handshake("good-token", &|t| {
        assert_eq!(t, "good-token");
        Some(("user-42".into(), "bob".into()))
    });
    assert_eq!(
        result,
        HandshakeAuth::Ok {
            user_id: "user-42".into(),
            username: "bob".into(),
        }
    );
}

#[tokio::test]
async fn handshake_reject_reason_is_nonempty_for_logging() {
    // 拒绝时 reason 应非空（供日志/前端提示）
    let result = authenticate_handshake("", &|_| Some(("u".into(), "n".into())));
    match result {
        HandshakeAuth::Rejected { reason, .. } => {
            assert!(!reason.is_empty(), "拒绝原因应非空");
        }
        HandshakeAuth::Ok { .. } => panic!("应拒绝"),
    }
}
