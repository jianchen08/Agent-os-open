"""内核 0.2 开发期 token 自解析公共模块 —— 四站点单点实现。

安全前提契约（消费方必读）：

- 本模块**自解析、不验签**：仅从 token payload 恢复 caller 可读身份
  （user_id/username/exp），供插件做归属闸/垂直隔离等业务判断；
- 鉴权权威在**内核 dispatcher**：HMAC 验签 + manifest ``http_endpoints[].auth``
  声明 fail-closed（未声明 auth 默认拒绝、admin 角色强制，见
  kernel/crates/api/src/http_dispatcher.rs enforce_ext_auth）。插件侧不得把
  本解码结果当作已认证凭据使用。

token 形态（kernel http/src/auth.rs decode_token 同构）：

- 旧式：``base64_nopad("access:{user_id}:{username}:{exp}")``；
- 新式（内核 30d1b0959 起）：``base64(payload).base64(hmac)`` 带点号两段式，
  payload 内 exp 后追加 HMAC 签名尾巴。

导入形态与 http_json 先例一致：插件侧把 plugins/shared 推上 sys.path 后裸名
导入（``from kernel_token import decode_kernel_token as _decode_kernel_token``）。

[来源: e5df14240 四站点回归修复；docs/working/修复方案_双审查报告_20260911.md 5.1]
"""

from __future__ import annotations

import base64


def decode_kernel_token(token: str) -> tuple[str, str, int] | None:
    """解码内核 0.2 开发期 token → ``(user_id, username, exp)``；无效返回 None。

    不做过期判定（exp 原样返回，调用方按自身语义处置：workspace/tasks 视为
    未认证空身份，agent_manager 归 401）；签名不验证（见模块 docstring 安全
    前提契约）。
    """
    try:
        # 新式 token = base64(payload).base64(signature)（带点分隔）：先切掉
        # 签名段再解 payload，否则点号后的签名字节会污染 UTF-8 解码。
        bare = token.strip().split(".", 1)[0]
        padded = bare + "=" * (-len(bare) % 4)
        payload = base64.b64decode(padded, validate=False).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    # split(":", 4)：第 5 段起是 HMAC 签名（内核 30d1b0959 起 token 带签名尾巴），
    # 身份三段（user_id/username/exp）不受签名增补影响；自解析不验签，鉴权权威
    # 仍在内核 dispatcher（x-agentos-user 注入头），此处仅恢复 caller 可读性。
    parts = payload.split(":", 4)
    if len(parts) < 4:
        return None
    try:
        exp = int(parts[3])
    except ValueError:
        return None
    return parts[1], parts[2], exp
