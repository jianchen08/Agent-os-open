# =============================================================================
# F-min 本机插件执行器（v3 装载器；部署支撑面组件，不入 kernel/ 与 plugins/ 主车道）
#
# 决策：docs/decisions/2026-10-12-fmin-remote-execution-via-mcp.md（F-min 快路径）
# 详规：docs/working/分布式执行设计_事件汇聚E与插件宿主F_20261012.md §2.0/§53
#
# 职责：把本机算力以标准 MCP server 形态暴露给云内核 mcp:external 插件。
# 文件/shell 面 = 真实 AgentOS 插件成员装载（config.members，裸名聚合——同名
# 同工具，工具实现与契约零手写，复用合宿宿主 _MemberLoader 隔离装载）；手机
# 面 = phone_* 十四件本机特有能力。安全边界 = allowed_roots 白名单 + Bearer
# token；路径零改写（保真不变量：模型上下文路径字符串与实际执行路径逐字节
# 同一），每次调用落一行审计日志（model_path/executed_path 分列）作为验收
# 取证面。
#
# 运行（本机侧）：
#   export AGENTOS_FMIN_LOCAL_TOKEN=<token>        # 不设则拒启（fail-closed）
#   python fmin_local_server.py --config config.json
# =============================================================================

from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import os
import re
import secrets
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uiautomator2 as u2
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

TOKEN_ENV_VAR = "AGENTOS_FMIN_LOCAL_TOKEN"


class PathOutsideAllowedRoots(Exception):
    """路径解析后不落在任何 allowed_root 内（越界，调用方可见的明确错误）。"""


class PhoneNotConnected(RuntimeError):
    """目标设备不可达/不在线（文案固定可判别，调用方可据此启动模拟器并 adb connect 自救）。"""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass
class FminConfig:
    host: str = "127.0.0.1"
    port: int = 9201
    allowed_roots: list[Path] = field(default_factory=list)
    audit_log: Path = Path("logs/fmin_local_mcp_audit.jsonl")
    # 手机工具组（phone_*）的目标设备串号；未配置时用缺省值。手机工具组经
    # uiautomator2 操作设备（adb 二进制由 u2/adbutils 自带，无需本机配置），
    # 未连接时返回明确错误、文件五件不受影响。
    adb_serial: str = "127.0.0.1:5555"
    # 成员插件清单（v1 装载器，设计文档 §2.0/§56）：真实 AgentOS 插件在本进程
    # 装载、裸名聚合暴露（同名同工具，10-13 用户裁定）。空 = 不装载（纯手机面）。
    members: list[str] = field(default_factory=list)
    # 插件根（plugins/shared 目录）；缺省 = 仓库内相对定位。
    plugins_root: Path | None = None


def load_config(path: Path) -> FminConfig:
    """读取并校验配置：allowed_roots 必须是非空的绝对路径且真实存在。

    校验失败抛 ValueError（启动即失败，不带病运行）。
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    roots_raw = raw.get("allowed_roots")
    if not isinstance(roots_raw, list) or not roots_raw:
        raise ValueError(f"配置 {path}: allowed_roots 必须是非空数组（本机允许暴露给云内核的根目录清单）")
    roots: list[Path] = []
    for item in roots_raw:
        p = Path(str(item))
        if not p.is_absolute():
            raise ValueError(f"配置 {path}: allowed_roots 条目必须是绝对路径：{item}")
        if not p.is_dir():
            raise ValueError(f"配置 {path}: allowed_roots 条目不存在或不是目录：{item}")
        roots.append(p)
    audit = Path(str(raw.get("audit_log", "logs/fmin_local_mcp_audit.jsonl")))
    members_raw = raw.get("members", [])
    if not isinstance(members_raw, list) or not all(isinstance(m, str) and m for m in members_raw):
        raise ValueError(f"配置 {path}: members 必须是非空字符串数组（成员插件 id/目录名）")
    plugins_root_raw = raw.get("plugins_root")
    return FminConfig(
        host=str(raw.get("host", "127.0.0.1")),
        port=int(raw.get("port", 9201)),
        allowed_roots=roots,
        audit_log=audit,
        adb_serial=str(raw.get("adb_serial", "127.0.0.1:5555")),
        members=[str(m) for m in members_raw],
        plugins_root=Path(str(plugins_root_raw)) if plugins_root_raw else None,
    )


# ---------------------------------------------------------------------------
# 白名单守卫（路径零改写：只校验，不改写；实际操作用调用方原样字符串）
# ---------------------------------------------------------------------------


def _is_within(resolved: Path, root: Path) -> bool:
    """resolved 是否等于 root 或落在 root 之下。

    normcase：Windows 上小写化并统一分隔符（盘内大小写不敏感语义与 NTFS 一致），
    POSIX 上为恒等。边界用 os.sep 后缀防止 /a/b 误配 /a/bc。
    """
    res = os.path.normcase(str(resolved))
    rt = os.path.normcase(str(root))
    if res == rt:
        return True
    prefix = rt if rt.endswith(os.sep) else rt + os.sep
    return res.startswith(prefix)


def resolve_within(raw: str, allowed_roots: list[Path]) -> Path:
    """校验 raw 路径（含 ..、junction/symlink 解析后）落在某 allowed_root 内。

    返回 resolve 后的真实位置（仅供守卫判定与错误提示）；工具操作一律使用调用方
    原样字符串，不改写——路径保真不变量由"全链路零改写点"构造保证。
    """
    if not raw or not raw.strip():
        raise ValueError("path/cwd 参数为空")
    resolved = Path(raw).resolve()
    for root in allowed_roots:
        if _is_within(resolved, root):
            return resolved
    roots_text = "\n  ".join(str(r) for r in allowed_roots)
    raise PathOutsideAllowedRoots(
        f"路径越界，已拒绝：{raw}\n解析后位置：{resolved}\n不在任何 allowed_root 内（允许的根目录：\n  {roots_text}\n）"
    )


# ---------------------------------------------------------------------------
# 审计日志（保真取证面：model_path = 模型上下文里的路径字符串，executed_path =
# 实际交给 OS 的路径字符串；两列由调用点显式记录，不做推导）
# ---------------------------------------------------------------------------


def audit_call(
    audit_log: Path,
    tool: str,
    model_path: str,
    executed_path: str,
    detail: dict[str, Any],
) -> None:
    line = json.dumps(
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tool": tool,
            "model_path": model_path,
            "executed_path": executed_path,
            **detail,
        },
        ensure_ascii=False,
    )
    try:
        audit_log.parent.mkdir(parents=True, exist_ok=True)
        with open(audit_log, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        # 审计面是验收取证通道，写不进去宁可使本次调用失败（fail-closed），不静默。
        raise RuntimeError(f"审计日志写入失败（{audit_log}）：{e}") from e


def run_audited(tool: str, model_path: str, op: Callable[[], tuple[dict[str, Any], Any]], audit_log: Path) -> Any:
    """执行 op 并落一行审计；op 抛错也落审计（ok=false）后转 ToolError 传播。

    mcp SDK 2.3 起 tool 体内非 ToolError 异常对客户端脱敏（只剩
    "Error executing tool <name>"），越界/设备未连接等预期失败的自救
    指引文案必须在边界转 ToolError 才可达模型。"""

    try:
        detail, result = op()
    except Exception as e:
        audit_call(audit_log, tool, model_path, model_path, {"ok": False, "error": str(e)})
        raise ToolError(str(e)) from e
    audit_call(audit_log, tool, model_path, model_path, {"ok": True, **detail})
    return result


# ---------------------------------------------------------------------------
# Bearer token 中间件（纯 ASGI，包住整个 streamable-http app：
# 本服务只有 MCP 一个面，任何缺/错 token 的请求一律 401）
# ---------------------------------------------------------------------------


class BearerTokenMiddleware:
    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = b""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                supplied = value
                break
        if not secrets.compare_digest(supplied, self._expected):
            body = json.dumps({"error": "unauthorized", "detail": "缺少或错误的 Bearer token"}).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"www-authenticate", b'Bearer realm="fmin-local"'),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# 手机采集/控制基建（phone_* 十四件共用；2026-10-13 用户裁定：采集六件只读 +
# 控制六件 + 屏幕定位两件同刀加全，暴露≠使用，调用侧治理由云端权限模式管）
#
# 路线：uiautomator2（openatx 成熟包）为基座——连接/设备 RPC/CJK 输入通道/语义
# 选择器全用 u2 自带能力，不手写 adb 轮子；dumpsys 文本解析（通知/电池）与参数
# 校验保留为纯函数（可直接单测）。无设备连接抛固定文案 PhoneNotConnected，不
# crash server 其他工具。注入面：包名走点分段正则白名单；自由文本走 u2 RPC
# 通道（无宿主/设备 shell 拼接面）。
# ---------------------------------------------------------------------------

PHONE_SHELL_TIMEOUT_SECS = 15  # 设备 shell 超时（u2 缺省 60s，统一收紧）
SCREEN_XML_MAX_BYTES = 20 * 1024  # 复杂屏 UI XML 可到 100KB+，超限截断带标记
PHONE_SHOTS_DIRNAME = "phone_shots"  # 落 allowed_roots 首根下，云端可用 read_file 回取
PHONE_NOTIFICATION_MAX_ITEMS = 30

# dumpsys battery 数值枚举 → 文案（Android 官方 BATTERY_STATUS/BATTERY_HEALTH）
_BATTERY_STATUS = {1: "unknown", 2: "charging", 3: "discharging", 4: "not_charging", 5: "full"}
_BATTERY_HEALTH = {1: "unknown", 2: "good", 3: "overheat", 4: "dead", 5: "over_voltage", 6: "failure", 7: "cold"}


def parse_battery(raw: str) -> dict[str, Any]:
    """dumpsys battery 输出 → 关键字段结构化；键缺失容错为 None/空串，不炸。"""
    kv: dict[str, str] = {}
    for line in raw.splitlines():
        m = re.match(r"[ \t]*([A-Za-z][A-Za-z0-9 _]*?)[ \t]*:[ \t]*(.+?)[ \t]*$", line)
        if m:
            kv[m.group(1).strip().lower()] = m.group(2)

    def _int(key: str) -> int | None:
        value = kv.get(key)
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None

    temperature = _int("temperature")
    voltage = _int("voltage")
    status = _int("status")
    health = _int("health")
    sources = [
        name
        for key, name in (("ac powered", "ac"), ("usb powered", "usb"), ("wireless powered", "wireless"))
        if kv.get(key) == "true"
    ]
    return {
        "level": _int("level"),
        "scale": _int("scale"),
        "temperature_c": temperature / 10 if temperature is not None else None,
        "plugged": bool(sources),
        "plugged_source": "+".join(sources),
        "status": _BATTERY_STATUS.get(status or -1, "unknown"),
        "health": _BATTERY_HEALTH.get(health or -1, "unknown"),
        "voltage_v": voltage / 1000 if voltage is not None else None,
        "technology": kv.get("technology", ""),
    }


def _extras_string(block: str, key: str) -> str:
    """记录块内 extras 键的字符串值。dumpsys 形态 `key=String (...)` / `key=null`；
    键缺失或形态不认识一律空串（解析容错契约）。"""
    m = re.search(rf"^[ \t]*{re.escape(key)}=(.*)$", block, re.MULTILINE)
    if not m:
        return ""
    raw = m.group(1).strip()
    if raw == "null":
        return ""
    if raw.startswith("String (") and raw.endswith(")"):
        return raw[len("String (") : -1]
    return raw


def _parse_notification_block(block: str) -> dict[str, str]:
    header = block.split("\n", 1)[0]
    pkg_m = re.search(r"pkg=([^\s:,]+)", header)
    post_m = re.search(r"mRankingTimeMs=(\d+)", block)
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(post_m.group(1)) / 1000)) if post_m else ""
    return {
        "pkg": pkg_m.group(1) if pkg_m else "",
        "title": _extras_string(block, "android.title"),
        "text": _extras_string(block, "android.text"),
        "when": when,
    }


def parse_notifications(raw: str) -> list[dict[str, str]]:
    """dumpsys notification --noredact 输出 → active 通知结构化列表。

    优先只扫 Notification List 区段（到 Ranking/Zen/Historical 等后续区段为止），
    区段名不认识则全文扫描兜底；字段缺失留空不炸；数量截 PHONE_NOTIFICATION_MAX_ITEMS。
    """
    scan = raw
    list_m = re.search(r"^[ \t]*Notification List.*$", raw, re.MULTILINE)
    if list_m:
        scan = raw[list_m.end() :]
        scan = re.split(
            r"^[ \t]*(?:Ranking Config|Zen|Historical|notification archive|Remotes|Snapshot)",
            scan,
            maxsplit=1,
            flags=re.MULTILINE,
        )[0]
    items: list[dict[str, str]] = []
    for block in re.split(r"(?=^[ \t]*NotificationRecord\()", scan, flags=re.MULTILINE):
        if not block.lstrip().startswith("NotificationRecord("):
            continue
        item = _parse_notification_block(block)
        if item["pkg"]:
            items.append(item)
        if len(items) >= PHONE_NOTIFICATION_MAX_ITEMS:
            break
    return items


def truncate_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    """超 max_bytes 的 UTF-8 文本按字节截断并追加标记；多字节字符不撕碎（errors=ignore 落边）。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    cut = encoded[:max_bytes].decode("utf-8", errors="ignore")
    marker = f"\n<!-- fmin-truncated: showing first {max_bytes} of {len(encoded)} bytes -->"
    return cut + marker, True


def _connect_phone(cfg: FminConfig) -> u2.Device:
    """连上目标设备的 u2 句柄（现用现连：u2 连接自带 atx-agent 健康检查，成本低）。

    设备离线/adb server 未启动/atx-agent 不可达统一翻译为固定文案的
    PhoneNotConnected（调用方可据此启动模拟器并 adb connect 自救）。
    """
    try:
        return u2.connect(cfg.adb_serial)
    except Exception as e:
        raise PhoneNotConnected(
            f"phone not connected: run adb connect {cfg.adb_serial}（{type(e).__name__}: {e}）"
        ) from e


def _phone_shell(d: u2.Device, cmd: str, timeout: float = PHONE_SHELL_TIMEOUT_SECS) -> str:
    """设备 shell（u2 通道）：非零退出码带输出报错（禁吞）；超时由 u2 侧强制。"""
    resp = d.shell(cmd, timeout=timeout)
    if resp.exit_code != 0:
        raise RuntimeError(f"设备命令失败（exit={resp.exit_code}）：{cmd} → {resp.output.strip()[:300]}")
    return resp.output


# ---- 控制类参数校验（正则白名单；拒绝优先于静默降级）----

# Android 包名：点分段、段首字母、段内字母数字下划线（同时杜绝 -p 选项注入与 shell 元字符）
_PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")

# 命名按键白名单（白名单外拒绝）：BACK/HOME/ENTER/RECENT 走 u2 press 命名按键，
# WAKEUP/SLEEP 走 u2 screen_on/screen_off（亮/熄屏语义，比裸 keycode 更可靠）。
_PHONE_KEY_EVENTS = ("BACK", "HOME", "ENTER", "RECENT", "WAKEUP", "SLEEP")


def validate_package(package: str) -> str:
    """包名格式校验（防注入/防选项拼接）；不合规抛带原因的错误。"""
    if not isinstance(package, str) or not _PACKAGE_RE.match(package.strip()) or package.strip() != package:
        raise ValueError(f"非法包名（需点分段的 Android 包名，如 com.android.settings）：{package!r}")
    return package


def validate_coord(name: str, value: Any) -> int:
    """屏幕坐标校验：非负整数（JSON 浮点整数值收整，其余拒绝）。"""
    if isinstance(value, bool):
        raise ValueError(f"坐标 {name} 必须是非负整数，得到 {value!r}")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or value < 0 or value > 100000:
        raise ValueError(f"坐标 {name} 必须是非负整数（≤100000），得到 {value!r}")
    return value


def validate_key_event(key: str) -> str:
    """命名按键 → 白名单内的规范名（大写）；白名单外抛错（附允许清单）。"""
    canonical = str(key).strip().upper()
    if canonical not in _PHONE_KEY_EVENTS:
        raise ValueError(f"不支持的按键 {key!r}（允许：{', '.join(_PHONE_KEY_EVENTS)}）")
    return canonical


# ---- 屏幕定位（tap_text/tap_id 共用）：语义选择器未命中时的自纠错文本清单 ----


def _visible_texts(root: Any, limit: int = 20) -> list[str]:
    """UI 树中可见文本清单（截前 limit 条、去重保序，供找不到元素时 agent 自纠错）。"""
    texts: list[str] = []
    for node in root.iter("node"):
        text = (node.get("text") or "").strip()
        if text and text not in texts:
            texts.append(text)
            if len(texts) >= limit:
                break
    return texts


def _visible_texts_from_xml(xml: str, limit: int = 20) -> list[str]:
    """UI 树 XML → 可见文本清单；解析失败翻译为明确错误（禁吞）。"""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise RuntimeError(f"UI 树 XML 解析失败：{e}") from None
    return _visible_texts(root, limit)


# ---------------------------------------------------------------------------
# 工具面（v3 装载器：真实 AgentOS 插件成员装载 + 手机十四件 phone_*。文件/
# shell 面不再手写——工具实现与契约全部来自成员插件声明，裸名聚合 = 同名同
# 工具（2026-10-13 用户裁定：同名=同一个工具，异名即异工具）；phone_* 为
# 本机特有能力保留）
# ---------------------------------------------------------------------------

# 成员工具参数中的路径类键：白名单守卫逐键校验。零改写——校验不通过即拒，
# 通过则参数原样下传 handler（路径保真不变量由"全链路零改写点"构造保证）。
_GUARD_ARG_KEYS = ("path", "cwd", "file_path", "working_dir")
# 内核 MCP 调用注入的保留键（SDK 同款剥离：身份/日志上下文/调用配置），不进
# guard 与 handler 参数。
_RESERVED_ARG_KEYS = ("_identity", "_log_ctx", "config")


@dataclass
class MemberTool:
    """成员工具（裸名聚合面）：handler 与契约全部来自成员插件声明。"""

    name: str
    handler: Callable[..., Any]
    schema: dict[str, Any]
    description: str
    plugin_id: str


def _default_plugins_root() -> Path:
    return Path(__file__).resolve().parents[3] / "plugins" / "shared"


def load_member_tools(plugins_root: Path, member_ids: list[str]) -> dict[str, MemberTool]:
    """装载成员插件并聚合其工具（裸名；同名冲突启动即败，fail-closed）。

    成员装载复用合宿宿主的 ``_MemberLoader``（sys.modules 平铺裸名隔离）；SDK
    经 ``plugins_root/../sdk/src`` 入 sys.path。成员 server.py 未暴露合法
    ``plugin`` 实例即抛错（合宿同款 fail-fast）。
    """
    sdk_src = plugins_root.parent / "sdk" / "src"
    if not (sdk_src / "agentos_plugin_sdk").is_dir():
        raise RuntimeError(f"SDK 源不可达：{sdk_src}（plugins_root 是否指向 plugins/shared？）")
    if str(sdk_src) not in sys.path:
        sys.path.insert(0, str(sdk_src))
    host_py = plugins_root / "_host" / "host.py"
    spec = importlib.util.spec_from_file_location("_fmin_cohost_host", host_py)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"合宿宿主装载失败：{host_py}")
    host_mod = importlib.util.module_from_spec(spec)
    # 注册后执行：dataclass 解析字符串注解要经 sys.modules 查宿主模块命名空间，
    # 不注册则 MemberLoadFailure 等冻结数据类在 exec 期即 AttributeError。
    sys.modules["_fmin_cohost_host"] = host_mod
    spec.loader.exec_module(host_mod)
    loader = host_mod._MemberLoader()
    tools: dict[str, MemberTool] = {}
    for member_id in member_ids:
        pdir = plugins_root / member_id
        if not pdir.is_dir():
            raise RuntimeError(f"成员插件目录不存在：{pdir}")
        plugin_obj = loader.load(member_id, pdir)
        for name, td in plugin_obj._tools.items():
            if name in tools:
                raise RuntimeError(f"成员工具裸名冲突：{name}（{tools[name].plugin_id} vs {member_id}）")
            tools[name] = MemberTool(
                name=name,
                handler=td.handler,
                schema=td.schema,
                description=td.description,
                plugin_id=member_id,
            )
    return tools


def _handler_accepted_params(handler: Callable[..., Any]) -> set[str] | None:
    """handler 签名接受的参数名集合；带 **kwargs 兜底形态（如 bash_execute）
    返回 None = 全参数透传。"""
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return None
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return None
    return {
        p.name
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }


def _coerce_integral_floats(value: Any) -> Any:
    """深转换：整值 float → int（0.0→0）。SDK ToolResult.to_dict 的
    duration_ms 等字段是 float，mcp:external 通道按 ToolExecutionResult
    u64 解析，float 直接解析失败。"""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _coerce_integral_floats(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_coerce_integral_floats(v) for v in value]
    return value


def _normalize_tool_result(raw: Any) -> str:
    """SDK ToolResult（to_dict）/dict/协程/其他 → 文本负载。"""
    if inspect.iscoroutine(raw):
        raise RuntimeError("成员 handler 返回协程：调用侧必须 await（装载器内部错误）")
    if hasattr(raw, "to_dict"):
        raw = raw.to_dict()
    if isinstance(raw, dict):
        return json.dumps(_coerce_integral_floats(raw), ensure_ascii=False, default=str)
    return str(raw)


def _make_member_handler(
    tool: MemberTool, accepted: set[str] | None, roots: list[Path], audit_log: Path
) -> Callable[..., Any]:
    async def member_call(**kwargs: Any) -> str:
        for reserved in _RESERVED_ARG_KEYS:
            kwargs.pop(reserved, None)
        model_marker = next(
            (kwargs[k] for k in _GUARD_ARG_KEYS if isinstance(kwargs.get(k), str) and kwargs[k].strip()),
            "-",
        )
        matched_root = roots[0]
        call_kwargs = dict(kwargs)
        for key in _GUARD_ARG_KEYS:
            value = call_kwargs.get(key)
            if isinstance(value, str) and value.strip():
                # 相对路径按锚根拼后校验（原生语义：相对路径以注入根锚定）；
                # 只校验不回写——参数仍以调用方原样字符串下传（零改写）。
                # 越界拒绝文案转 ToolError（SDK 2.3 非预期异常对客户端脱敏）。
                candidate = value if Path(value).is_absolute() else str(matched_root / value)
                try:
                    resolved = resolve_within(candidate, roots)
                except Exception as e:
                    raise ToolError(str(e)) from e
                for root in roots:
                    if _is_within(resolved, root):
                        matched_root = root
                        break
        # fs 族工作空间锚注入：file_read/file_write 未注入 workspace 直接
        # 报错（fail-closed）——注入 = 该路径所属 allowed_root，两层守卫
        # （本进程白名单 + 插件工作空间检查）语义一致。仅 handler 声明了
        # 的参数才注入/透传（签名过滤，对齐 SDK _filter_handler_kwargs）。
        # 锚注入用真值判断而非 setdefault：pydantic 校验模型会把声明过的可选
        # 字段以 None 显式传入（setdefault 对 None 不生效）。
        if accepted is not None:
            if "workspace" in accepted and not call_kwargs.get("workspace"):
                call_kwargs["workspace"] = str(matched_root)
            if "project_root" in accepted and not call_kwargs.get("project_root"):
                call_kwargs["project_root"] = str(matched_root)
            # None 值参数丢弃（校验模型对声明可选字段显式传 None，会覆盖
            # handler 签名默认值——enhanced_search path=None 直接炸 TypeError）
            call_kwargs = {k: v for k, v in call_kwargs.items() if k in accepted and v is not None}
        # **kwargs 兜底形态（如 bash_execute）：工作目录锚缺省 = 路径所属
        # allowed_root（bash 侧 workspace/working_dir 未注入即 fail-closed
        # 拒执行；调用方显式 working_dir 优先，守卫已校验）。
        elif not call_kwargs.get("working_dir"):
            call_kwargs["working_dir"] = str(matched_root)
        # 成员 handler 可 async：执行 + 审计对（失败落审计后转 ToolError，
        # 与 run_audited 同契约——SDK 2.3 非预期异常对客户端脱敏）。
        try:
            raw = tool.handler(**call_kwargs)
            if inspect.iscoroutine(raw):
                raw = await raw
        except Exception as e:
            audit_call(audit_log, tool.name, model_marker, model_marker, {"ok": False, "error": str(e)})
            raise ToolError(str(e)) from e
        result_text = _normalize_tool_result(raw)
        audit_call(audit_log, tool.name, model_marker, model_marker, {"ok": True, "plugin": tool.plugin_id})
        return result_text

    # __signature__ 按声明 schema 构造：add_tool 的入参校验模型从签名推导，
    # **kwargs 形态会生成"名为 kwargs 的单字段"模型与平铺调用永不匹配——
    # 平铺字段模型必须与声明 schema 的 properties/required 对齐（校验语义 =
    # 成员插件声明契约）。派生 schema 随后仍被覆写为声明 schema（枚举/参数
    # 描述保真）。
    props = (tool.schema or {}).get("properties") or {}
    required = set((tool.schema or {}).get("required") or [])
    ann_map = {"string": str, "integer": int, "number": float, "boolean": bool}

    def _param(name: str, prop: dict[str, Any]) -> inspect.Parameter:
        prop_type = (prop or {}).get("type")
        return inspect.Parameter(
            name,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=ann_map.get(prop_type, Any) if isinstance(prop_type, str) else Any,
            default=inspect.Parameter.empty if name in required else (prop or {}).get("default"),
        )

    # 必选参数在前（Python 签名语法：带默认值参数不可先于无默认值参数；
    # 声明序不必保持——校验模型按名匹配，与参数顺序无关）。
    sig_params = [_param(n, props[n]) for n in props if n in required] + [
        _param(n, props[n]) for n in props if n not in required
    ]
    member_call.__signature__ = inspect.Signature(sig_params)
    return member_call


def register_member_tools(server: MCPServer, cfg: FminConfig) -> int:
    """装载成员插件并把其工具注册进 server（裸名 + 声明 schema 保真）。

    返回注册的工具数。members 为空 = 不装载（纯手机面），返回 0。
    """
    if not cfg.members:
        return 0
    plugins_root = cfg.plugins_root or _default_plugins_root()
    tools = load_member_tools(plugins_root, cfg.members)
    for tool in tools.values():
        accepted = _handler_accepted_params(tool.handler)
        handler = _make_member_handler(tool, accepted, cfg.allowed_roots, cfg.audit_log)
        server.add_tool(handler, name=tool.name, description=tool.description)
        # 声明 schema 保真：add_tool 从包装签名推导 schema，此处覆写为成员
        # 插件声明（枚举/默认值/参数描述齐全）。依赖 mcp 2.x ToolManager
        # 私有面——SDK 版本漂移在此炸开（fail-closed 启动失败），不静默降级
        # 为签名近似 schema。
        tool_registry = server._tool_manager._tools
        if not isinstance(tool_registry, dict):
            raise RuntimeError(f"mcp SDK ToolManager 内部面漂移（_tools 非字典）：{tool.name} 无法做声明 schema 保真")
        entry = tool_registry.get(tool.name)
        if entry is None:
            raise RuntimeError(f"成员工具注册后不可见（mcp SDK 内部面漂移？）：{tool.name}")
        entry.parameters = tool.schema
    return len(tools)


def register_tools(server: MCPServer, cfg: FminConfig) -> None:
    roots = cfg.allowed_roots
    audit_log = cfg.audit_log
    register_member_tools(server, cfg)

    # ---- 手机数据采集组（六件只读）----

    @server.tool()
    async def phone_status() -> str:
        """手机健康检查（只读）：连接状态、设备型号、安卓版本、电池、当前前台应用。手机任务的先行探针；设备未连接时返回明确错误（可先启动模拟器并 adb connect 自救）。"""

        def op() -> tuple[dict[str, Any], str]:
            d = _connect_phone(cfg)
            info = d.device_info
            battery = parse_battery(_phone_shell(d, "dumpsys battery"))
            current = d.app_current() or {}
            payload = {
                "connected": True,
                "serial": cfg.adb_serial,
                "model": info.get("model", ""),
                "brand": info.get("brand", ""),
                "android_version": info.get("version", ""),
                "battery": battery,
                "foreground": {
                    "package": current.get("package", ""),
                    "activity": current.get("activity", ""),
                    "source": "app_current",
                },
            }
            return (
                {"args": {}, "model": payload["model"], "foreground_app": payload["foreground"]["package"]},
                json.dumps(payload, ensure_ascii=False),
            )

        return run_audited("phone_status", cfg.adb_serial, op, audit_log)

    @server.tool()
    async def phone_screenshot() -> str:
        """截取手机当前屏幕（只读）：PNG 落盘到 allowed_roots 首根下 phone_shots/（时间戳命名），返回路径+字节数；随后可用 read_file 读取该 PNG。"""

        def op() -> tuple[dict[str, Any], str]:
            d = _connect_phone(cfg)
            out_dir = roots[0] / PHONE_SHOTS_DIRNAME
            out_dir.mkdir(parents=True, exist_ok=True)
            stem = time.strftime("%Y%m%d_%H%M%S")
            out = out_dir / f"phone_shot_{stem}.png"
            suffix = 0
            while out.exists():
                suffix += 1
                out = out_dir / f"phone_shot_{stem}_{suffix}.png"
            d.screenshot(filename=str(out))
            with out.open("rb") as f:
                head = f.read(8)
            if head != b"\x89PNG\r\n\x1a\n":
                raise RuntimeError(f"截图产物不是 PNG（前 8 字节：{head!r}）")
            size = out.stat().st_size
            return {"args": {}, "bytes": size, "path": str(out)}, f"OK 截图已保存：{out}（{size} 字节）"

        return run_audited("phone_screenshot", str(roots[0] / PHONE_SHOTS_DIRNAME), op, audit_log)

    @server.tool()
    async def phone_read_screen() -> str:
        """读取手机当前屏 UI 结构（只读）：uiautomator XML 文本，含每个控件的 text/resource-id/bounds/clickable。
        标准操作链路：先用本工具拿到 UI 树，再用 phone_tap_text / phone_tap_id 按元素语义点击（坐标 phone_tap 只是兜底）。超 20KB 截断带标记。"""

        def op() -> tuple[dict[str, Any], str]:
            d = _connect_phone(cfg)
            xml = d.dump_hierarchy()
            if not xml.strip():
                raise RuntimeError("UI 树读取为空（设备可能正在动画中，重试一次本工具）")
            text, truncated = truncate_utf8(xml, SCREEN_XML_MAX_BYTES)
            return {"args": {}, "bytes": len(xml.encode("utf-8")), "truncated": truncated}, text

        return run_audited("phone_read_screen", cfg.adb_serial, op, audit_log)

    @server.tool()
    async def phone_notifications() -> str:
        """读取手机当前活动通知（只读）：包名/标题/文本/时间的结构化 JSON 列表。"""

        def op() -> tuple[dict[str, Any], str]:
            d = _connect_phone(cfg)
            items = parse_notifications(_phone_shell(d, "dumpsys notification --noredact"))
            return {"args": {}, "count": len(items)}, json.dumps(items, ensure_ascii=False)

        return run_audited("phone_notifications", cfg.adb_serial, op, audit_log)

    @server.tool()
    async def phone_list_apps() -> str:
        """列出手机已安装应用包名（只读，u2 app_list，含系统应用）。"""

        def op() -> tuple[dict[str, Any], str]:
            d = _connect_phone(cfg)
            pkgs = sorted(d.app_list())
            return {"args": {}, "count": len(pkgs)}, json.dumps(pkgs, ensure_ascii=False)

        return run_audited("phone_list_apps", cfg.adb_serial, op, audit_log)

    @server.tool()
    async def phone_battery() -> str:
        """读取手机电池状态（只读）：电量/温度/充电来源/状态/健康度。"""

        def op() -> tuple[dict[str, Any], str]:
            d = _connect_phone(cfg)
            battery = parse_battery(_phone_shell(d, "dumpsys battery"))
            return {"args": {}, "level": battery.get("level")}, json.dumps(battery, ensure_ascii=False)

        return run_audited("phone_battery", cfg.adb_serial, op, audit_log)

    # ---- 手机控制组（六件；暴露≠使用，调用侧治理由云端权限模式管）----

    @server.tool()
    async def phone_tap(x: float, y: float) -> str:
        """按屏幕绝对像素坐标点击（控制类，兜底路径）。优先用 phone_read_screen 拿 UI 树 + phone_tap_text/phone_tap_id 语义定位；坐标可取 UI 节点 bounds 的中心。"""

        def op() -> tuple[dict[str, Any], str]:
            d = _connect_phone(cfg)
            cx, cy = validate_coord("x", x), validate_coord("y", y)
            d.click(cx, cy)
            return {"args": {"x": cx, "y": cy}}, f"OK 已点击 ({cx},{cy})"

        return run_audited("phone_tap", cfg.adb_serial, op, audit_log)

    @server.tool()
    async def phone_swipe(x1: float, y1: float, x2: float, y2: float, duration_ms: float = 300) -> str:
        """按坐标滑动（控制类）：从 (x1,y1) 滑到 (x2,y2)，duration_ms 滑动时长（默认 300ms）。上滑返回桌面可 (360,1000)→(360,200)。"""

        def op() -> tuple[dict[str, Any], str]:
            d = _connect_phone(cfg)
            coords = [validate_coord(n, v) for n, v in (("x1", x1), ("y1", y1), ("x2", x2), ("y2", y2))]
            if (
                isinstance(duration_ms, bool)
                or not isinstance(duration_ms, (int, float))
                or not 1 <= duration_ms <= 60000
            ):
                raise ValueError(f"duration_ms 必须在 1..60000 之间，得到 {duration_ms!r}")
            d.swipe(coords[0], coords[1], coords[2], coords[3], duration=duration_ms / 1000)
            return (
                {"args": {"from": coords[:2], "to": coords[2:], "duration_ms": int(duration_ms)}},
                f"OK 已滑动 ({coords[0]},{coords[1]})→({coords[2]},{coords[3]})（{int(duration_ms)}ms）",
            )

        return run_audited("phone_swipe", cfg.adb_serial, op, audit_log)

    @server.tool()
    async def phone_input_text(text: str) -> str:
        """往手机当前聚焦的输入框输入文本（控制类）。需先点出一个输入框（phone_tap_text 等）使其聚焦。
        中文等 CJK 走 u2 自带输入通道（无需额外输入法配置），ASCII 同路；参数经 u2 RPC 传输，无 shell 拼接面。"""

        def op() -> tuple[dict[str, Any], str]:
            d = _connect_phone(cfg)
            if not isinstance(text, str) or not text:
                raise ValueError("text 为空")
            d.send_keys(text)
            return {"args": {"chars": len(text)}}, f"OK 已输入 {len(text)} 字符（u2 输入通道）"

        return run_audited("phone_input_text", cfg.adb_serial, op, audit_log)

    @server.tool()
    async def phone_key_event(key: str) -> str:
        """发送命名按键（控制类）：BACK（返回）/HOME（桌面）/ENTER（回车）/RECENT（最近任务）/WAKEUP（亮屏）/SLEEP（熄屏）。白名单外拒绝。"""

        def op() -> tuple[dict[str, Any], str]:
            d = _connect_phone(cfg)
            canonical = validate_key_event(key)
            if canonical == "WAKEUP":
                d.screen_on()
            elif canonical == "SLEEP":
                d.screen_off()
            else:
                d.press(canonical.lower())
            return {"args": {"key": canonical}}, f"OK 已发送按键 {canonical}"

        return run_audited("phone_key_event", cfg.adb_serial, op, audit_log)

    @server.tool()
    async def phone_launch_app(package: str) -> str:
        """启动手机上指定应用（控制类）：u2 app_start（自动解析 LAUNCHER activity）。包名可用 phone_list_apps 查询；格式校验防注入。"""

        def op() -> tuple[dict[str, Any], str]:
            d = _connect_phone(cfg)
            pkg = validate_package(package)
            d.app_start(pkg)
            return {"args": {"package": pkg}}, f"OK 已启动 {pkg}"

        return run_audited("phone_launch_app", cfg.adb_serial, op, audit_log)

    @server.tool()
    async def phone_stop_app(package: str) -> str:
        """强制停止手机上指定应用（控制类）：u2 app_stop。包名格式校验防注入。"""

        def op() -> tuple[dict[str, Any], str]:
            d = _connect_phone(cfg)
            pkg = validate_package(package)
            d.app_stop(pkg)
            return {"args": {"package": pkg}}, f"OK 已停止 {pkg}"

        return run_audited("phone_stop_app", cfg.adb_serial, op, audit_log)

    # ---- 屏幕语义定位组（两件；读屏→定位→点击的闭环执行件，u2 选择器承载）----

    @server.tool()
    async def phone_tap_text(text: str, exact: bool = False) -> str:
        """按屏上可见文本点击对应控件（控制类，语义定位优先路径）。u2 选择器匹配 text 属性（exact=true 精确 / 默认 textContains 包含）；
        多命中取第一个（返回说明命中数）；找不到时报错并附当前屏可见文本前 20 条供自纠错。标准链路：phone_read_screen → phone_tap_text/phone_tap_id → phone_read_screen 验证；坐标 phone_tap 仅兜底。"""

        def op() -> tuple[dict[str, Any], str]:
            d = _connect_phone(cfg)
            if not isinstance(text, str) or not text.strip():
                raise ValueError("text 为空")
            sel = d(text=text) if exact else d(textContains=text)
            hits = sel.count
            if hits == 0:
                shown = " | ".join(_visible_texts_from_xml(d.dump_hierarchy()))
                raise ValueError(
                    f"屏上未找到 text 匹配 {text!r} 的节点（exact={exact}）。当前屏可见文本（前 20 条）：{shown}"
                )
            sel.click()
            note = f"（命中 {hits} 处，取第 1 处）" if hits > 1 else ""
            return {"args": {"text": text, "exact": exact, "hits": hits}}, f"OK 已点击 text={text!r}{note}"

        return run_audited("phone_tap_text", cfg.adb_serial, op, audit_log)

    @server.tool()
    async def phone_tap_id(resource_id: str, exact: bool = False) -> str:
        """按控件 resource-id 点击（控制类，语义定位优先路径）。匹配语义同 phone_tap_text（exact=true 精确/默认包含），定位/兜底/自纠错行为一致；
        resource-id 可先从 phone_read_screen 的 XML 里查（形如 com.android.settings:id/title）。"""

        def op() -> tuple[dict[str, Any], str]:
            d = _connect_phone(cfg)
            if not isinstance(resource_id, str) or not resource_id.strip():
                raise ValueError("resource_id 为空")
            sel = d(resourceId=resource_id) if exact else d(resourceIdMatches=f".*{re.escape(resource_id)}.*")
            hits = sel.count
            if hits == 0:
                shown = " | ".join(_visible_texts_from_xml(d.dump_hierarchy()))
                raise ValueError(
                    f"屏上未找到 resource-id 匹配 {resource_id!r} 的节点（exact={exact}）。当前屏可见文本（前 20 条）：{shown}"
                )
            sel.click()
            note = f"（命中 {hits} 处，取第 1 处）" if hits > 1 else ""
            return {
                "args": {"resource_id": resource_id, "exact": exact, "hits": hits}
            }, f"OK 已点击 resource-id={resource_id!r}{note}"

        return run_audited("phone_tap_id", cfg.adb_serial, op, audit_log)


def build_mcp_server(cfg: FminConfig) -> MCPServer:
    server = MCPServer(
        name="agentos-fmin-local",
        version="0.4.0",
        instructions=(
            "本机算力网关（F-min 装载器）。文件与 shell 工具为真实 AgentOS 插件成员"
            "（裸名同名同工具），仅限 allowed_roots 白名单内的原生绝对路径，所见即所执行。"
            "手机工具组（phone_*，目标设备经 config.adb_serial 指定，需设备在线）："
            "数据采集六件只读（status/screenshot/read_screen/notifications/list_apps/battery），"
            "控制与屏幕定位八件（tap/swipe/input_text/key_event/launch_app/stop_app/tap_text/tap_id）。"
            "手机操作标准链路：phone_read_screen 拿 UI 树 → phone_tap_text/phone_tap_id 语义点击 → "
            "phone_read_screen 验证；坐标 phone_tap 只作兜底。"
        ),
    )
    register_tools(server, cfg)
    return server


def create_asgi_app(mcp_server: MCPServer, token: str, host: str) -> Any:
    """streamable-http app 包一层 Bearer 中间件。

    host 传给 SDK：bind 环回时 SDK 自动启用 DNS rebinding 防护（放行 127.0.0.1/
    localhost/[::1] 的 Host 头）——生产经 SSH 反向隧道只应 bind 127.0.0.1。
    """
    inner = mcp_server.streamable_http_app(streamable_http_path="/mcp", host=host)
    return BearerTokenMiddleware(inner, token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="F-min v0 本机 MCP server")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent / "config.json",
        help="配置文件路径（缺省取同目录 config.json）",
    )
    args = parser.parse_args(argv)

    token = os.environ.get(TOKEN_ENV_VAR)
    if not token:
        print(
            f"环境变量 {TOKEN_ENV_VAR} 未设置，拒绝启动（fail-closed）",
            file=sys.stderr,
        )
        return 2

    cfg = load_config(args.config)
    app = create_asgi_app(build_mcp_server(cfg), token, cfg.host)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
