"""通用 MCP-over-HTTP Bridge 客户端（非浏览器专用）。

讲标准 MCP JSON-RPC（initialize / tools/list / tools/call），双传输：

- DirectTransport：宿主 sidecar 直发 Bridge（非隔离会话路径）。
- ContainerTransport：隔离任务路径——把内嵌的 stdlib 客户端脚本经
  ``docker exec <cid> python -`` 在任务容器内执行（真·沙箱内 MCP Client），
  地址候选静默自试（AGENTOS_BRIDGE_URL env → host.docker.internal →
  WSL 网关 IP），agent 不会看到端口实验过程。

会话 key（X-Bridge-Session）用 session_id：同会话复用同一浏览器实例，
跨会话隔离。caller（X-Bridge-Caller）标识执行域：host | sandbox。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from typing import Any

# 宿主/容器内均要求 docker 在 PATH（部署契约）；解析为完整路径满足进程启动审计
_DOCKER = shutil.which("docker") or "docker"

# Bridge 地址候选（容器内）：env 注入优先，其后 Docker Desktop 特殊域名。
BRIDGE_HOST_CANDIDATES = ("env:AGENTOS_BRIDGE_URL", "http://host.docker.internal:{port}")

# 容器内客户端脚本（stdlib-only）：单次调用——读 stdin JSON spec，POST
# /mcp/{upstream}，输出一行结果 JSON。经 docker exec python - 执行。
_INNER_SCRIPT = r"""
import json, sys, urllib.request

spec = json.loads(sys.stdin.read())
base_candidates = spec["base_candidates"]
token = spec["token"]
upstream = spec["upstream"]
session = spec["session"]
caller = spec["caller"]
payload = spec["payload"]
timeout = spec["timeout"]

last_err = None
for base in base_candidates:
    url = base.rstrip("/") + "/mcp/" + upstream
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("X-Bridge-Session", session)
    req.add_header("X-Bridge-Caller", caller)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            print(json.dumps({"ok": True, "status": resp.status, "body": body}))
            sys.exit(0)
    except Exception as e:  # 下一候选（连接失败）或直接报告（HTTP 4xx/5xx 也走这里）
        detail = getattr(e, "read", None)
        status = getattr(e, "code", None)
        if status is not None:
            # HTTP 错误响应是确定性结果，不再尝试下一候选。
            try:
                err_body = json.loads(detail().decode("utf-8")) if callable(detail) else {}
            except Exception:
                err_body = {}
            print(json.dumps({"ok": False, "status": status, "body": err_body or {"error": str(e)}}))
            sys.exit(0)
        last_err = str(e)
print(json.dumps({"ok": False, "status": 0, "body": {"error": f"bridge unreachable: {last_err}"}}))
"""


class BridgeClientError(Exception):
    """Bridge 调用失败（不可达 / 治理拒绝 / 上游错误）。"""


class BridgeClient:
    """通用 MCP-over-HTTP Bridge 客户端。

    Args:
        config: config/tools/browser/browser.yaml 的 bridge 段（bridge_base/url 等）。
        session_id: 会话 key（param_inject 注入；缺省 default——同进程共享上游会话）。
        caller: host | sandbox（审计字段；容器路径强制 sandbox）。
        container_id: 隔离上下文的任务容器 id（调用方显式传入；sandbox 路径
            必填。sidecar 进程多会话共享，身份不得经进程级 os.environ 传递，
            否则并发交错时 A 会话可携 B 容器身份发出）。
    """

    def __init__(
        self,
        config: dict[str, Any],
        session_id: str = "",
        caller: str = "host",
        container_id: str = "",
        exec_backend: dict[str, Any] | None = None,
    ):
        self._base = str(config.get("bridge_base") or os.environ.get("AGENTOS_BRIDGE_URL") or "http://127.0.0.1:8765")
        self._upstream = str(config.get("upstream", "browser"))
        self._timeout = float(config.get("timeout_secs", 120))
        self._session = session_id or "default"
        self._caller = caller
        self._container_id = container_id
        # 执行环境后端信息（isolation_guard 与 _container_id 同级注入，仅原生
        # 环境非 None）：sandbox 路径据此选传输（wsl_native → wsl 传输；
        # docker 环境为 None → docker exec 通路）。
        self._exec_backend = exec_backend if isinstance(exec_backend, dict) else None
        # WSL NAT 宿主 IP 探测缓存（实例生命周期；mypy 显式定型）
        self._wsl_gateway_cache: str = ""
        self._token = self._resolve_token(config)

    @staticmethod
    def _resolve_token(config: dict[str, Any]) -> str:
        # config 优先（bridge.token）：装机侧 sidecar 无 env/.env 供给通道，
        # 用户空间配置（config_files 注入）是唯一合规凭据面（env 仍是 dev 首选）。
        cfg_token = str(config.get("token") or "").strip()
        if cfg_token:
            return cfg_token
        token = os.environ.get("AGENTOS_BRIDGE_TOKEN", "")
        if not token:
            # 宿主开发环境：从项目 .env 读（与内核 env_file 同键，避免要求
            # sidecar 进程预先继承）。
            env_path = os.environ.get("AGENTOS_PROJECT_ROOT") or _find_project_root()
            if env_path:
                dot_env = os.path.join(env_path, ".env")
                try:
                    with open(dot_env, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith("AGENTOS_BRIDGE_TOKEN="):
                                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                                break
                except OSError:
                    pass
        if not token:
            raise BridgeClientError("AGENTOS_BRIDGE_TOKEN 未配置（.env 或环境变量）。在项目根 .env 设置后重启 Bridge。")
        return token

    # ── MCP 协议 ─────────────────────────────────────────────

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._rpc("tools/list")
        return list(result.get("tools") or [])

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """tools/call，返回 MCP result（含 content 数组）。"""
        return self._rpc("tools/call", {"name": tool, "arguments": arguments})

    def initialize(self) -> dict[str, Any]:
        return self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "agentos-browser-tool", "version": "0.1.0"},
            },
        )

    def live_ticket(self) -> dict[str, Any]:
        """取直播换票（GET /browser/live-ticket）。

        返回 {"ok", "status", "body"}——body 含 ticket/mjpeg_url/page_url；
        浏览器运行时未启用/不可用时 ok=False（调用方降级，不抛异常）。
        """
        url = self._base.rstrip("/") + "/browser/live-ticket"
        # base 为运维侧配置的本地 Bridge 地址（env/config，http/https），非用户输入
        req = urllib.request.Request(url, method="GET")  # noqa: S310
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("X-Bridge-Session", self._session)
        req.add_header("X-Bridge-Caller", self._caller)
        return self._open_with_errors(req, timeout=min(self._timeout, 15.0))

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            payload["params"] = params
        result = self._send(payload)
        if not result.get("ok"):
            status = result.get("status")
            body = result.get("body") or {}
            msg = str(body.get("error") or body)
            if status == 401:
                raise BridgeClientError(f"Bridge 认证失败: {msg}")
            if status == 403:
                raise BridgeClientError(f"Bridge 治理拒绝: {msg}")
            if status == 502:
                raise BridgeClientError(f"上游不可用: {msg}")
            raise BridgeClientError(f"Bridge 调用失败(status={status}): {msg}")
        return result.get("body", {}).get("result") or {}

    # ── 双传输 ────────────────────────────────────────────────

    def _send(self, payload: dict[str, Any]) -> dict[str, Any]:
        """按 caller 选路径：sandbox 走容器/沙箱环境内执行，host 直发。"""
        if self._caller == "sandbox":
            backend = (self._exec_backend or {}).get("backend")
            if backend == "wsl_native":
                return self._send_via_wsl_native(payload)
            if backend == "linux_native":
                # linux_native 后端的沙箱内通路 v1 未覆盖（与 wsl_native 同族，
                # 实现时复用同款 argv 契约）；显式报错优于误导性的 docker 报错。
                raise BridgeClientError("linux_native 后端的浏览器沙箱内通路 v1 未覆盖（请反馈补全）")
            return self._send_via_container(payload)
        return self._send_direct(payload)

    def _send_direct(self, payload: dict[str, Any]) -> dict[str, Any]:
        """宿主直发（非隔离路径）。"""
        url = self._base.rstrip("/") + "/mcp/" + self._upstream
        # base 为运维侧配置的本地 Bridge 地址（env/config，http/https），非用户输入
        req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST")  # noqa: S310
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("X-Bridge-Session", self._session)
        req.add_header("X-Bridge-Caller", self._caller)
        return self._open_with_errors(req, timeout=self._timeout)

    def _open_with_errors(self, req: urllib.request.Request, timeout: float) -> dict[str, Any]:
        """urlopen + 统一错误映射（宿主直发路径共用：MCP RPC 与 REST 换票）。"""
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return {"ok": True, "status": resp.status, "body": json.loads(resp.read().decode("utf-8"))}
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                body = {"error": str(e)}
            return {"ok": False, "status": e.code, "body": body}
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            return {
                "ok": False,
                "status": 0,
                "body": {
                    "error": f"Bridge 不可达（{req.full_url}）: {e}。请先在宿主机启动 Bridge：scripts/start_bridge.bat"
                },
            }

    def _send_via_wsl_native(self, payload: dict[str, Any]) -> dict[str, Any]:
        """wsl_native 路径：环境内 python 直连宿主桥（无 Docker 层）。

        argv 契约与 WslNativeProvider/_wsl_transport 同语义（跨插件不可
        import，漂移由 tests/test_wsl_native_wiring.py 钉）：``--exec`` 逐字
        传递（禁 ``--``——登录 shell 重解析会吞 stdin/改写参数，2026-09-14
        实测）。内嵌脚本与 docker 路径共用（stdlib-only，spec 走 stdin）；
        bridge 地址候选按 WSL 拓扑换装：mirrored 网络 127.0.0.1 直达宿主，
        NAT 模式走默认网关（宿主 vEthernet IP，探一次缓存）。
        """
        backend = self._exec_backend or {}
        wsl_exe = str(backend.get("wsl_exe", "wsl"))
        distro = str(backend.get("distro", "Ubuntu"))
        user = str(backend.get("user", "") or "")
        workspace_wsl = str(backend.get("workspace_wsl", "") or "")
        argv = [wsl_exe, "-d", distro]
        if user:
            argv.extend(["-u", user])
        if workspace_wsl:
            argv.extend(["--cd", workspace_wsl])
        argv.extend(["--exec"])
        sandbox_cmd = backend.get("sandbox_cmd") or []
        if isinstance(sandbox_cmd, list):
            argv.extend(str(x) for x in sandbox_cmd)
        argv.extend(["python3", "-c", _INNER_SCRIPT])
        spec = {
            "base_candidates": self._wsl_base_candidates(),
            "token": self._token,
            "upstream": self._upstream,
            "session": self._session,
            "caller": "sandbox",
            "payload": payload,
            "timeout": self._timeout,
        }
        try:
            proc = subprocess.run(  # noqa: S603 — wsl_exe/distro/user 来自 isolation_guard 服务端注入（与 _container_id 同级信任链）
                argv,
                input=json.dumps(spec).encode("utf-8"),
                capture_output=True,
                timeout=self._timeout + 10,
            )
        except subprocess.TimeoutExpired as e:
            return {"ok": False, "status": 0, "body": {"error": f"wsl 环境内 MCP Client 超时: {e}"}}
        except OSError as e:
            return {"ok": False, "status": 0, "body": {"error": f"wsl 传输执行失败: {e}"}}
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace")[:300]
            return {"ok": False, "status": 0, "body": {"error": f"wsl 环境内 MCP Client 执行失败: {stderr}"}}
        lines = [x for x in proc.stdout.decode("utf-8", "replace").strip().splitlines() if x.strip()]
        if not lines:
            return {"ok": False, "status": 0, "body": {"error": "wsl 环境内客户端无输出"}}
        try:
            parsed: dict[str, Any] = json.loads(lines[-1])
            return parsed
        except json.JSONDecodeError:
            return {"ok": False, "status": 0, "body": {"error": f"wsl 环境内客户端输出不可解析: {lines[-1][:200]}"}}

    def _wsl_base_candidates(self) -> list[str]:
        """WSL 环境内可达宿主桥的地址候选（拓扑自适应）。

        - env 注入优先（部署显式指定）；
        - 127.0.0.1：mirrored 网络（Win11 22H2+）下直达宿主端口；NAT 模式
          指向发行版自身 loopback，连接秒拒即落下一候选（零成本尝试）；
        - 默认网关：NAT 模式宿主 vEthernet IP（探 `ip route show default`，
          实例内缓存；探测失败仅少一个候选）。
        """
        candidates: list[str] = []
        env_url = os.environ.get("AGENTOS_BRIDGE_URL", "")
        if env_url:
            candidates.append(env_url)
        port = self._port()
        candidates.append(f"http://127.0.0.1:{port}")
        gateway = self._wsl_gateway_ip()
        if gateway:
            candidates.append(f"http://{gateway}:{port}")
        return candidates

    def _wsl_gateway_ip(self) -> str:
        """WSL 视角的宿主 IP（NAT 模式默认网关）。探一次缓存（实例生命周期）。"""
        if getattr(self, "_wsl_gateway_cache", None):
            return str(self._wsl_gateway_cache)
        backend = self._exec_backend or {}
        argv = [
            str(backend.get("wsl_exe", "wsl")),
            "-d",
            str(backend.get("distro", "Ubuntu")),
        ]
        user = str(backend.get("user", "") or "")
        if user:
            argv.extend(["-u", user])
        argv.extend(["--exec", "ip", "route", "show", "default"])
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=15)  # noqa: S603 — wsl_exe/distro/user 来自服务端注入信任链
        except (OSError, subprocess.SubprocessError):
            return ""
        if proc.returncode != 0:
            return ""
        out = proc.stdout.decode("utf-8", "replace")
        gateway = ""
        for part in out.split():
            if gateway and part != "via":
                gateway = part
                break
            if part == "via":
                gateway = "?"
        if gateway and gateway != "?":
            self._wsl_gateway_cache = gateway
        return gateway

    def _send_via_container(self, payload: dict[str, Any]) -> dict[str, Any]:
        """容器路径：内嵌脚本经 docker exec 在任务容器内执行（沙箱内 MCP Client）。

        地址候选静默自试；agent 无感。容器身份取构造时显式传入的
        ``container_id``（实例私有，多会话并发不串号）。
        """
        container_id = self._container_id
        if not container_id:
            return {
                "ok": False,
                "status": 0,
                "body": {"error": "隔离上下文缺失 container_id，无法从沙箱发起调用"},
            }
        candidates = self._container_base_candidates()
        spec = {
            "base_candidates": candidates,
            "token": self._token,
            "upstream": self._upstream,
            "session": self._session,
            "caller": "sandbox",
            "payload": payload,
            "timeout": self._timeout,
        }
        # 脚本经 -c 承载，stdin 专供 spec JSON——`python -` 会把整个 stdin
        # 当程序源码消费，_INNER_SCRIPT 运行时将读不到 spec
        proc = subprocess.run(  # noqa: S603 — container_id 由内核注入的隔离上下文提供，非用户输入
            [_DOCKER, "exec", "-i", "-e", "PYTHONIOENCODING=utf-8", container_id, "python", "-c", _INNER_SCRIPT],
            input=json.dumps(spec).encode("utf-8"),
            capture_output=True,
            timeout=self._timeout + 10,
        )
        if proc.returncode != 0:
            return {
                "ok": False,
                "status": 0,
                "body": {"error": f"容器内 MCP Client 执行失败: {proc.stderr.decode('utf-8', 'replace')[:300]}"},
            }
        lines = [x for x in proc.stdout.decode("utf-8", "replace").strip().splitlines() if x.strip()]
        if not lines:
            return {"ok": False, "status": 0, "body": {"error": "容器内客户端无输出"}}
        try:
            result: dict[str, Any] = json.loads(lines[-1])
            return result
        except json.JSONDecodeError:
            return {"ok": False, "status": 0, "body": {"error": f"容器内客户端输出不可解析: {lines[-1][:200]}"}}

    def _container_base_candidates(self) -> list[str]:
        """Bridge 地址候选：env 注入 → host.docker.internal → 宿主网关 IP。"""
        port = self._port()
        candidates: list[str] = []
        env_url = os.environ.get("AGENTOS_BRIDGE_URL", "")
        if env_url:
            candidates.append(env_url)
        candidates.append(f"http://host.docker.internal:{port}")
        gateway_ip = _docker_gateway_ip()
        if gateway_ip:
            candidates.append(f"http://{gateway_ip}:{port}")
        return candidates

    def _port(self) -> int:
        from urllib.parse import urlparse

        return urlparse(self._base).port or 8765


def _docker_gateway_ip() -> str:
    """宿主侧查 docker 网桥网关 IP（WSL 原生 docker 无 host.docker.internal 时的候选）。"""
    try:
        proc = subprocess.run(  # noqa: S603 — 固定参数查 docker 网桥元信息，无外部输入
            [_DOCKER, "network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        ip = proc.stdout.strip()
        return ip if proc.returncode == 0 and ip else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _find_project_root() -> str:
    """从 cwd 向上找含 .env 的目录（宿主 sidecar 开发环境兜底）。"""
    cur = os.getcwd()
    for _ in range(6):
        if os.path.isfile(os.path.join(cur, ".env")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return ""
        cur = parent
    return ""
