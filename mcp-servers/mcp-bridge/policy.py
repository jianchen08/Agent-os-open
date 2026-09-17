"""MCP Bridge 网关——治理层：认证、工具白名单、域名策略、审计。"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse


class PolicyError(Exception):
    """治理层拒绝。http_status：401（认证）/403（策略）/404（未知上游）/413（超限）。"""

    def __init__(self, message: str, http_status: int = 403):
        super().__init__(message)
        self.http_status = http_status


@dataclass
class UpstreamPolicy:
    """单个上游的治理声明（来自 config/bridge/bridge.yaml 的 upstreams.<name>）。"""

    # 工具白名单；None = 不设限（透传全部）。空列表 = 全拒绝。
    allowed_tools: list[str] | None = None
    # 域名策略：仅对声明了 url 参数名的工具调用生效。
    domain_policy: dict = field(default_factory=dict)
    # 工具参数（arguments）序列化后的最大字节数。
    max_args_bytes: int = 256 * 1024
    # 上游启动命令（stdio）。
    command: str = ""
    args: list[str] = field(default_factory=list)
    cwd: str | None = None


@dataclass
class BridgePolicy:
    """网关整体治理配置。"""

    token_env: str = "AGENTOS_BRIDGE_TOKEN"
    _token_cache: str | None = field(default=None, repr=False)
    _token_read_at: float = field(default=0.0, repr=False)
    upstreams: dict = field(default_factory=dict)
    # 审计文件目录；空字符串 = 关闭审计。
    audit_dir: str = "logs/mcp-bridge"
    _audit_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def from_config(cls, config: dict) -> "BridgePolicy":
        auth = config.get("auth") or {}
        return cls(
            token_env=str(auth.get("token_env", "AGENTOS_BRIDGE_TOKEN")),
            audit_dir=str(config.get("audit_dir", "logs/mcp-bridge")),
            upstreams=config.get("upstreams") or {},
        )

    # ── 认证 ─────────────────────────────────────────────────

    def bearer_token(self) -> str | None:
        """读 token（env）。每 10s 重读一次（容忍改环境后不重启进程）。"""
        now = time.monotonic()
        if self._token_cache is None or now - self._token_read_at > 10.0:
            self._token_cache = os.environ.get(self.token_env) or None
            self._token_read_at = now
        return self._token_cache

    def check_auth(self, auth_header: str | None) -> None:
        """校验 Authorization: Bearer <token>。token 未配置时拒绝一切调用。"""
        expected = self.bearer_token()
        provided = (auth_header or "").strip()
        if not provided.startswith("Bearer ") or not expected or provided[len("Bearer ") :].strip() != expected:
            raise PolicyError("认证失败（缺 Bearer 头 / token 未配置或不匹配）", http_status=401)

    # ── 白名单 / 参数上限 / 域名 ───────────────────────────────

    def upstream_policy(self, upstream: str) -> UpstreamPolicy:
        spec = self.upstreams.get(upstream)
        if spec is None:
            raise PolicyError(f"未知上游: {upstream}", http_status=404)
        return UpstreamPolicy(
            allowed_tools=spec.get("tools"),
            domain_policy=spec.get("domain_policy") or {},
            max_args_bytes=int(spec.get("max_args_bytes", 256 * 1024)),
            command=str(spec.get("command", "")),
            args=list(spec.get("args") or []),
            cwd=spec.get("cwd"),
        )

    def check_tool(self, upstream: str, params: dict) -> None:
        """tools/call 的工具白名单 + 参数大小 + 域名策略检查。"""
        up = self.upstream_policy(upstream)
        name = str((params or {}).get("name", ""))
        if not name:
            raise PolicyError("tools/call 缺少 name")
        if up.allowed_tools is not None and name not in up.allowed_tools:
            raise PolicyError(f"工具不在白名单: {name}", http_status=403)
        args = (params or {}).get("arguments") or {}
        try:
            size = len(json.dumps(args, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError) as e:
            raise PolicyError(f"参数不可序列化: {e}") from e
        if size > up.max_args_bytes:
            raise PolicyError(f"参数超限: {size} > {up.max_args_bytes} bytes", http_status=413)
        if up.domain_policy:
            check_domain_policy(up.domain_policy, name, args)

    # ── 审计 ──────────────────────────────────────────────────

    def audit(
        self,
        upstream: str,
        caller: str,
        session_key: str,
        method: str,
        tool: str,
        decision: str,
        reason: str = "",
        elapsed_ms: float = 0.0,
    ) -> None:
        """一行 JSONL 追加到 audit_dir/audit-YYYYMMDD.jsonl。失败静默（审计不阻塞业务）。"""
        if not self.audit_dir:
            return
        record = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "upstream": upstream,
            "caller": caller,
            "session": session_key,
            "method": method,
            "tool": tool,
            "decision": decision,
            "reason": reason,
            "elapsed_ms": round(elapsed_ms, 1),
        }
        line = json.dumps(record, ensure_ascii=False)
        try:
            os.makedirs(self.audit_dir, exist_ok=True)
            day = datetime.now(timezone.utc).strftime("%Y%m%d")
            with self._audit_lock:
                with open(os.path.join(self.audit_dir, f"audit-{day}.jsonl"), "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError:
            pass


# ── 域名策略 ─────────────────────────────────────────────────

def check_domain_policy(policy: dict, tool_name: str, args: dict) -> None:
    """对含 url 参数的调用执行域名黑白名单与私网拦截。

    policy 结构（bridge.yaml 的 domain_policy）：
      url_arg_names: ["url"]          # 参与检查的参数名
      blocked_domains: []             # 域名黑名单（后缀匹配），优先于白名单
      allowed_domains: null           # 非 null 时为白名单（后缀匹配）
      allow_private_addresses: false  # 私网/环回/元数据地址放行开关
    """
    url_args = policy.get("url_arg_names") or ["url"]
    for arg_name in url_args:
        raw = args.get(arg_name)
        if not isinstance(raw, str) or not raw:
            continue
        parsed = urlparse(raw)
        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https"):
            raise PolicyError(f"{tool_name}: 仅允许 http/https URL", http_status=403)
        host = (parsed.hostname or "").lower()
        if not host:
            raise PolicyError(f"{tool_name}: URL 缺少主机名", http_status=403)
        if not policy.get("allow_private_addresses", False) and is_private_host(host):
            raise PolicyError(f"{tool_name}: 拒绝访问内网/环回/元数据地址: {host}", http_status=403)
        for d in (str(x).lower() for x in (policy.get("blocked_domains") or [])):
            if host == d or host.endswith("." + d):
                raise PolicyError(f"{tool_name}: 域名被禁止: {host}", http_status=403)
        allowed = policy.get("allowed_domains")
        if allowed is not None:
            allowed_l = [str(x).lower() for x in allowed]
            if not any(host == d or host.endswith("." + d) for d in allowed_l):
                raise PolicyError(f"{tool_name}: 域名不在白名单: {host}", http_status=403)


_HOST_RE = re.compile(r"^[0-9.]+$")
_PRIVATE_HOST_SUFFIXES = (".local", ".internal", ".lan", ".home.arpa")
_PRIVATE_IP_PREFIXES = ("10.", "127.", "169.254.", "192.168.")


def is_private_host(host: str) -> bool:
    """环回/私网/链路本地/元数据/CGNAT/本地域名判定（字面判定，不做 DNS 解析）。

    IPv4 字面量走前缀/八位组判定；IPv6 字面量（urlparse 已去方括号）经
    ipaddress 判定：环回 ::1 / 链路本地 fe80::/10 / ULA fc00::/7 / 未指定 ::，
    IPv4 映射形式 ::ffff:x.x.x.x 递归按 IPv4 判；非法 IPv6 字面量按私网
    fail-closed（拒绝而非放行）。
    """
    h = host.lower()
    if h in ("localhost", "metadata", "metadata.google.internal"):
        return True
    if h.endswith(_PRIVATE_HOST_SUFFIXES):
        return True
    if ":" in h:
        try:
            addr = ipaddress.IPv6Address(h)
        except ValueError:
            return True
        mapped = addr.ipv4_mapped
        if mapped is not None:
            return is_private_host(str(mapped))
        return (
            addr.is_loopback
            or addr.is_link_local
            or addr.is_private
            or addr.is_unspecified
        )
    if _HOST_RE.match(h):
        if h.startswith(_PRIVATE_IP_PREFIXES):
            return True
        parts = h.split(".")
        if len(parts) == 4:
            o1, o2 = int(parts[0]), int(parts[1])
            if o1 == 172 and 16 <= o2 <= 31:  # 172.16.0.0/12
                return True
            if o1 == 100 and 64 <= o2 <= 127:  # 100.64.0.0/10 CGNAT
                return True
    return False