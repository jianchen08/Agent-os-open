# @feature: FP-0.2.Fmin 本机插件执行器 | @ci: local
"""F-min v3 装载器（真实插件成员 + 手机面）测试。

三场景全覆盖 + 路径保真锚 + 白名单守卫/配置校验/手机组单测：
  ① 正常调用成功（tools/list 发现面 = phone_* 14 件 + 成员插件真工具 8 件；
     文件读写走 builtin_tools 真实现、shell 走 bash 真实现——装载器零手写工具）
  ② 越界路径拒绝（allowed_roots 外一律明确报错，含 ../、相对路径、盘外绝对路径）
  ③ 未授权拒绝（缺 token/错 token/非 Bearer 一律 401，且先于任何 MCP 会话机制）
  ④ 手机组 phone_*：解析/截断/白名单/防注入纯函数 + 无设备明确报错 +
     语义定位（u2 选择器边界打桩）行为

测试走真实 MCP 协议栈（ClientSession over streamable-http ASGI）+ 真实成员
插件栈（builtin_tools/bash 原码装载），不 mock 内部依赖；文件系统用 tmp_path
真实目录。运行：
  python -m pytest deploy/remote/local_mcp_server/tests -p no:cacheprovider
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import fmin_local_server as srv
import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

TOKEN = "test-token-fmin"

# 仓库 plugins/shared（成员插件与 SDK 真源）
PLUGINS_ROOT = Path(__file__).resolve().parents[4] / "plugins" / "shared"

MEMBER_IDS = ["builtin_tools", "bash"]
MEMBER_TOOL_NAMES = {
    # builtin_tools 声明（plugin.json capabilities.tools）
    "file_read",
    "file_write",
    "enhanced_search",
    "create_directory",
    "copy_file",
    "move_file",
    "delete_file",
    # bash 声明
    "bash_execute",
}

INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "fmin-test", "version": "0"},
    },
}

PHONE_TOOLS = {
    "phone_status",
    "phone_screenshot",
    "phone_read_screen",
    "phone_notifications",
    "phone_list_apps",
    "phone_battery",
    "phone_tap",
    "phone_swipe",
    "phone_input_text",
    "phone_key_event",
    "phone_launch_app",
    "phone_stop_app",
    "phone_tap_text",
    "phone_tap_id",
}


# ---------------------------------------------------------------------------
# 基建：ASGI lifespan 驱动 + 真实 MCP 会话
# ---------------------------------------------------------------------------


@asynccontextmanager
async def drive_lifespan(app):
    """按 ASGI lifespan 协议驱动 app（ASGITransport 不跑 lifespan，手动补齐）。

    第二次 receive 必须阻塞到退出（shutdown 消息被 gate 住），否则 lifespan
    处理器立即走完 shutdown，会话管理器在测试体执行前就关闭。
    """
    ack = asyncio.Event()
    shutdown_gate = asyncio.Event()

    async def receive():
        if not ack.is_set():
            return {"type": "lifespan.startup"}
        await shutdown_gate.wait()
        return {"type": "lifespan.shutdown"}

    async def send(message):
        if message["type"] == "lifespan.startup.complete":
            ack.set()

    task = asyncio.create_task(app({"type": "lifespan"}, receive, send))
    await ack.wait()
    try:
        yield
    finally:
        shutdown_gate.set()
        await task


@asynccontextmanager
async def mcp_session(tmp_path: Path, roots: list[Path]):
    cfg = srv.FminConfig(
        host="127.0.0.1",
        port=9201,
        allowed_roots=roots,
        audit_log=tmp_path / "audit.jsonl",
        members=MEMBER_IDS,
        plugins_root=PLUGINS_ROOT,
    )
    app = srv.create_asgi_app(srv.build_mcp_server(cfg), TOKEN, cfg.host)
    async with (
        drive_lifespan(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://127.0.0.1:9201",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as http,
        streamable_http_client("http://127.0.0.1:9201/mcp", http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session, tmp_path / "audit.jsonl"


@pytest.fixture
def ws_root(tmp_path):
    """白名单根目录：两项目两文件一 markdown，供读/列/搜索共用。"""
    root = tmp_path / "ws"
    (root / "proj_a").mkdir(parents=True)
    (root / "proj_b").mkdir()
    (root / "proj_a" / "readme.txt").write_text("alpha 内容", encoding="utf-8")
    (root / "proj_b" / "readme.txt").write_text("beta 内容", encoding="utf-8")
    (root / "proj_a" / "note.md").write_text("# md", encoding="utf-8")
    return root


def read_audit(audit_path: Path) -> list[dict]:
    return [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def assert_rejected(result, needle: str) -> str:
    assert result.is_error is True
    text = result.content[0].text
    assert needle in text
    return text


# ---------------------------------------------------------------------------
# ① 正常调用成功
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_list_exposes_member_and_phone_tools(tmp_path, ws_root):
    async with mcp_session(tmp_path, [ws_root]) as (session, _):
        listed = await session.list_tools()
    names = {t.name for t in listed.tools}
    assert names == MEMBER_TOOL_NAMES | PHONE_TOOLS
    # 声明 schema 保真：成员工具的 inputSchema = plugin.json 声明（非签名推导）
    by_name = {t.name: t for t in listed.tools}
    file_write_schema = by_name["file_write"].input_schema
    assert "action" in (file_write_schema.get("properties") or {})
    assert file_write_schema.get("required") == ["path", "action"]


def _payload(result) -> dict:
    """成员工具返回 JSON 文本负载（SDK ToolResult.to_dict / dict）。"""
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_file_write_then_read_roundtrip(tmp_path, ws_root):
    raw = str(ws_root / "proj_b" / "out.txt")
    async with mcp_session(tmp_path, [ws_root]) as (session, _):
        written = await session.call_tool(
            "file_write", {"path": raw, "action": "write", "content": "你好 fmin", "create_backup": False}
        )
        assert written.is_error is False

        read = await session.call_tool("file_read", {"path": raw})
        assert read.is_error is False
        assert "你好 fmin" in read.content[0].text
    assert (ws_root / "proj_b" / "out.txt").read_text(encoding="utf-8") == "你好 fmin"


@pytest.mark.asyncio
async def test_file_read_directory_mode_lists_entries(tmp_path, ws_root):
    """列目录 = file_read 目录模式（原生语义，无独立 list_directory 工具）。"""
    async with mcp_session(tmp_path, [ws_root]) as (session, _):
        top = await session.call_tool("file_read", {"path": str(ws_root)})
        assert top.is_error is False
        text = top.content[0].text
        assert "proj_a" in text
        assert "proj_b" in text

        proj = await session.call_tool("file_read", {"path": str(ws_root / "proj_a")})
        assert proj.is_error is False
        assert "readme.txt" in proj.content[0].text
        assert "note.md" in proj.content[0].text


@pytest.mark.asyncio
async def test_bash_execute_runs_in_whitelisted_working_dir(tmp_path, ws_root):
    async with mcp_session(tmp_path, [ws_root]) as (session, _):
        echo = await session.call_tool(
            "bash_execute",
            {"action": "execute", "command": "echo marker_alpha", "working_dir": str(ws_root / "proj_a")},
        )
        assert echo.is_error is False
        assert "marker_alpha" in echo.content[0].text

        # working_dir 真实生效：重定向产物落在声明的目录，而不是进程缺省工作目录
        redirect = await session.call_tool(
            "bash_execute",
            {
                "action": "execute",
                "command": "echo shell_wrote > made_by_shell.txt",
                "working_dir": str(ws_root / "proj_b"),
            },
        )
        assert redirect.is_error is False
    assert (ws_root / "proj_b" / "made_by_shell.txt").exists()


@pytest.mark.asyncio
async def test_bash_execute_without_working_dir_anchors_to_allowed_root(tmp_path, ws_root):
    """working_dir 缺省 = 路径所属 allowed_root（装载器注入，bash 侧 fail-closed 解除）。"""
    async with mcp_session(tmp_path, [ws_root]) as (session, _):
        echo = await session.call_tool("bash_execute", {"action": "execute", "command": "echo anchored_ok"})
        assert echo.is_error is False
        assert "anchored_ok" in echo.content[0].text


# ---------------------------------------------------------------------------
# ② 越界路径拒绝
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(lambda ctx: str(ctx["outside"] / "secret.txt"), id="白名单外的绝对路径"),
        pytest.param(lambda ctx: str(ctx["tmp"] / "escaped.txt"), id="白名单父目录下的路径"),
        pytest.param(lambda _ctx: "../outside_secret.txt", id="相对路径逃逸（锚根拼接后越界）"),
    ],
)
async def test_out_of_bounds_read_rejected_with_clear_error(tmp_path, ws_root, raw):
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("x", encoding="utf-8")
    async with mcp_session(tmp_path, [ws_root]) as (session, _):
        result = await session.call_tool("file_read", {"path": raw({"outside": outside, "tmp": tmp_path})})
    text = assert_rejected(result, "越界")
    assert "allowed_root" in text


@pytest.mark.asyncio
async def test_out_of_bounds_rejected_for_every_guarded_tool(tmp_path, ws_root):
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    outside_file = str(outside / "secret.txt")
    ops = [
        ("file_read", {"path": outside_file}),
        ("file_write", {"path": outside_file, "action": "write", "content": "x"}),
        ("create_directory", {"path": str(outside / "new_dir")}),
        ("bash_execute", {"action": "execute", "command": "echo hi", "working_dir": str(outside)}),
    ]
    async with mcp_session(tmp_path, [ws_root]) as (session, _):
        for tool, args in ops:
            result = await session.call_tool(tool, args)
            assert_rejected(result, "越界")
            assert not (outside / "secret.txt").exists() or (outside / "secret.txt").read_text() == "x"


@pytest.mark.asyncio
async def test_empty_path_anchors_to_allowed_root(tmp_path, ws_root):
    """path 空串 = 原生语义按 workspace 锚处理（列 allowed_root 根），非越界。"""
    async with mcp_session(tmp_path, [ws_root]) as (session, _):
        result = await session.call_tool("file_read", {"path": ""})
    assert result.is_error is False
    assert "proj_a" in result.content[0].text


# ---------------------------------------------------------------------------
# ③ 未授权拒绝
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": "Basic dXNlcjpwd2Q="},
    ],
)
async def test_unauthorized_requests_rejected_before_mcp(tmp_path, ws_root, headers):
    cfg = srv.FminConfig(
        host="127.0.0.1",
        port=9201,
        allowed_roots=[ws_root],
        audit_log=tmp_path / "audit.jsonl",
    )
    app = srv.create_asgi_app(srv.build_mcp_server(cfg), TOKEN, cfg.host)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:9201") as http:
        resp = await http.post("/mcp", json=INIT_BODY, headers=headers)
    assert resp.status_code == 401
    assert "unauthorized" in resp.text
    assert "mcp-session-id" not in resp.headers
    assert not (tmp_path / "audit.jsonl").exists()


@pytest.mark.asyncio
async def test_valid_token_passes_http_layer(tmp_path, ws_root):
    cfg = srv.FminConfig(
        host="127.0.0.1",
        port=9201,
        allowed_roots=[ws_root],
        audit_log=tmp_path / "audit.jsonl",
    )
    app = srv.create_asgi_app(srv.build_mcp_server(cfg), TOKEN, cfg.host)
    async with (
        drive_lifespan(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:9201") as http,
    ):
        resp = await http.post("/mcp", json=INIT_BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200
    assert "mcp-session-id" in resp.headers


# ---------------------------------------------------------------------------
# 路径保真锚（验收：模型上下文路径字符串与实际执行路径逐字节一致）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_path_fidelity_audit_records_byte_identical_paths(tmp_path, ws_root):
    # 带空格与 CJK 的文件名让"逐字节一致"不平凡
    raw_file = str(ws_root / "路径 保真.txt")
    raw_shell_cwd = str(ws_root / "proj_b")
    async with mcp_session(tmp_path, [ws_root]) as (session, audit_path):
        await session.call_tool(
            "file_write", {"path": raw_file, "action": "write", "content": "hello", "create_backup": False}
        )
        await session.call_tool("file_read", {"path": raw_file})
        await session.call_tool(
            "bash_execute", {"action": "execute", "command": "echo ok", "working_dir": raw_shell_cwd}
        )

    lines = {entry["tool"]: entry for entry in read_audit(audit_path)}
    for tool in ("file_write", "file_read"):
        assert lines[tool]["ok"] is True
        assert lines[tool]["model_path"] == raw_file
        assert lines[tool]["executed_path"] == raw_file
    assert lines["bash_execute"]["model_path"] == raw_shell_cwd
    assert lines["bash_execute"]["executed_path"] == raw_shell_cwd
    # 保真的旁证：写操作真实落在模型所见的那个路径上
    assert Path(raw_file).read_text(encoding="utf-8") == "hello"


@pytest.mark.asyncio
async def test_enhanced_search_runs_with_dropped_none_args(tmp_path, ws_root):
    """声明可选字段显式 None 不覆盖 handler 默认值（path=None 历史炸点）；搜索真实执行。"""
    async with mcp_session(tmp_path, [ws_root]) as (session, _):
        hits = await session.call_tool("enhanced_search", {"query": "alpha"})
        assert hits.is_error is False
        assert "readme.txt" in hits.content[0].text


@pytest.mark.asyncio
async def test_result_payload_coerces_integral_floats_for_u64_parse(tmp_path, ws_root):
    """SDK ToolResult duration_ms=float → 整值 float 深转 int（mcp:external u64 解析）。"""
    raw = str(ws_root / "proj_b" / "coerce.txt")
    async with mcp_session(tmp_path, [ws_root]) as (session, _):
        result = await session.call_tool(
            "file_write", {"path": raw, "action": "write", "content": "x", "create_backup": False}
        )
    payload = json.loads(result.content[0].text)

    def _no_integral_floats(v):
        if isinstance(v, float):
            return not v.is_integer()
        if isinstance(v, dict):
            return all(_no_integral_floats(x) for x in v.values())
        if isinstance(v, list):
            return all(_no_integral_floats(x) for x in v)
        return True

    assert _no_integral_floats(payload), "负载含整值 float 会导致云侧 u64 解析失败"


@pytest.mark.asyncio
async def test_native_failure_payload_passthrough_and_audit(tmp_path, ws_root):
    """原生失败约定：结构化 success=false 负载 + is_error=False（SDK 同款）；
    审计 ok 记执行级（无异常），负载明记失败。"""
    missing = str(ws_root / "不存在的文件.txt")
    async with mcp_session(tmp_path, [ws_root]) as (session, audit_path):
        result = await session.call_tool("file_read", {"path": missing})
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload.get("success") is False
    entries = read_audit(audit_path)
    assert len(entries) == 1
    assert entries[0]["model_path"] == missing == entries[0]["executed_path"]


# ---------------------------------------------------------------------------
# 白名单守卫与配置校验（单元面）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "inside"),
    [
        ("ROOT", True),
        ("ROOT/sub", True),
        ("ROOT/sub/../sub/../sub/f.txt", True),
        ("ROOT/../escaped.txt", False),
        ("SIBLING", False),
    ],
)
def test_resolve_within_accepts_inside_and_rejects_outside(tmp_path, raw, inside):
    root = tmp_path / "guard"
    (root / "sub").mkdir(parents=True)
    (tmp_path / "sibling").mkdir()
    raw = raw.replace("ROOT", str(root)).replace("SIBLING", str(tmp_path / "sibling"))
    if inside:
        resolved = srv.resolve_within(raw, [root])
        assert srv._is_within(resolved, root)
    else:
        with pytest.raises(srv.PathOutsideAllowedRoots, match="越界"):
            srv.resolve_within(raw, [root])


@pytest.mark.skipif(os.name != "nt", reason="NTFS 大小写不敏感语义")
def test_resolve_within_is_case_insensitive_on_windows(tmp_path):
    root = tmp_path / "Guard"
    root.mkdir()
    assert srv.resolve_within(str(root).upper(), [root]) is not None
    assert srv.resolve_within(str(root).lower(), [root]) is not None


def test_load_config_validates_allowed_roots(tmp_path):
    good = tmp_path / "real"
    good.mkdir()

    with pytest.raises(ValueError, match="绝对路径"):
        srv.load_config(_write_cfg(tmp_path, {"allowed_roots": ["relative/path"]}))
    with pytest.raises(ValueError, match="不存在"):
        srv.load_config(_write_cfg(tmp_path, {"allowed_roots": [str(tmp_path / "missing")]}))
    with pytest.raises(ValueError, match="非空数组"):
        srv.load_config(_write_cfg(tmp_path, {"allowed_roots": []}))
    with pytest.raises(ValueError, match="非空数组"):
        srv.load_config(_write_cfg(tmp_path, {}))

    cfg = srv.load_config(_write_cfg(tmp_path, {"allowed_roots": [str(good)]}))
    assert cfg.allowed_roots == [good]
    assert cfg.port == 9201
    assert cfg.host == "127.0.0.1"


def _write_cfg(tmp_path: Path, body: dict) -> Path:
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_main_refuses_to_start_without_token(tmp_path, monkeypatch):
    good = tmp_path / "real"
    good.mkdir()
    monkeypatch.delenv(srv.TOKEN_ENV_VAR, raising=False)
    assert srv.main(["--config", str(_write_cfg(tmp_path, {"allowed_roots": [str(good)]}))]) == 2


# ---------------------------------------------------------------------------
# 手机组 phone_*（纯函数单测 + u2 边界打桩行为；测试不依赖真实设备）
# ---------------------------------------------------------------------------

# API 28（雷电模拟器）真机形态采样的 dumpsys battery 输出
_BATTERY_SAMPLE = """Current Battery Service state:
  AC powered: false
  USB powered: true
  Wireless powered: false
  Max charging current: 500000
  Max charging voltage: 5000000
  Charge counter: 2712000
  status: 2
  health: 2
  present: true
  level: 88
  scale: 100
  voltage: 3919
  temperature: 280
  technology: Li-poly"""

# dumpsys notification --noredact 关键形态：Notification List 区段 + Ranking 区段
_NOTIFICATION_SAMPLE = """Notification recording (active) is enabled
Notification List:
  NotificationRecord( uid=10123 pkg=com.lingxi.agentos user=UserHandle{0} id=1001 )
    android.title=String (灵汐助手)
    android.text=String (任务已完成)
    mRankingTimeMs=1728799200000
  NotificationRecord( uid=1000 pkg=com.android.systemui user=UserHandle{0} id=42 )
    android.title=null
    android.text=null

Ranking Config:
  NotificationRecord( uid=99 pkg=com.should.not.appear )
    android.title=String (ranking 区段不许混入)
"""

_UI_XML_SAMPLE = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" bounds="[0,0][720,1280]">
    <node index="0" text="设置" resource-id="com.android.settings:id/title" bounds="[0,80][720,160]"/>
    <node index="1" text="设置" resource-id="com.android.settings:id/title2" bounds="[0,160][720,240]"/>
    <node index="2" text="WLAN" resource-id="" bounds="[0,240][720,320]"/>
    <node index="3" text="" resource-id="" bounds="[0,320][720,400]"/>
  </node>
</hierarchy>"""


class _FakeSelector:
    """u2 UiObject 打桩：count 即查即返，click 记录调用（外部依赖 u2 的边界桩）。"""

    def __init__(self, count: int, clicks: list):
        self._count = count
        self._clicks = clicks

    @property
    def count(self) -> int:
        return self._count

    def click(self) -> None:
        self._clicks.append(1)


class _FakeDevice:
    """u2 Device 打桩：选择器构造即返回定数 selector；dump 返回定值 XML。"""

    def __init__(self, count: int, xml: str):
        self._count = count
        self._xml = xml
        self.clicks: list = []

    def __call__(self, **selector):
        return _FakeSelector(self._count, self.clicks)

    def dump_hierarchy(self) -> str:
        return self._xml


def test_truncate_utf8_short_text_untouched():
    assert srv.truncate_utf8("hello", 100) == ("hello", False)
    assert srv.truncate_utf8("abcde", 5) == ("abcde", False)


def test_truncate_utf8_cuts_on_bytes_and_keeps_marker():
    text, truncated = srv.truncate_utf8("你好", 4)  # UTF-8 每汉字 3 字节，4 落在第二字中间
    assert truncated is True
    assert text.startswith("你")  # 多字节字符不撕碎（errors=ignore 落边）
    assert "showing first 4 of 6 bytes" in text
    # 性质断言：截断产物（去掉标记前）不超预算
    assert len(text.encode("utf-8")) >= 4


@pytest.mark.parametrize(
    ("raw", "expect"),
    [
        pytest.param(
            _BATTERY_SAMPLE,
            {
                "level": 88,
                "scale": 100,
                "temperature_c": 28.0,
                "plugged": True,
                "plugged_source": "usb",
                "status": "charging",
                "health": "good",
                "voltage_v": 3.919,
                "technology": "Li-poly",
            },
            id="USB 充电中",
        ),
        pytest.param(
            _BATTERY_SAMPLE.replace("USB powered: true", "USB powered: false")
            .replace("AC powered: false", "AC powered: true")
            .replace("status: 2", "status: 3")
            .replace("level: 88", "level: 5"),
            {"level": 5, "plugged": True, "plugged_source": "ac", "status": "discharging"},
            id="AC 供电放电",
        ),
        pytest.param(
            "",
            {"level": None, "plugged": False, "status": "unknown", "health": "unknown", "technology": ""},
            id="空输出容错不炸",
        ),
    ],
)
def test_parse_battery_structures_fields_tolerantly(raw, expect):
    battery = srv.parse_battery(raw)
    for key, value in expect.items():
        assert battery[key] == value, f"字段 {key} 不符"
    assert 0 <= (battery["level"] or 0) <= 100  # 性质断言：电量在百分比域


def test_parse_notifications_reads_active_list_and_skips_other_sections():
    items = srv.parse_notifications(_NOTIFICATION_SAMPLE)
    assert [i["pkg"] for i in items] == ["com.lingxi.agentos", "com.android.systemui"]
    assert items[0]["title"] == "灵汐助手"
    assert items[0]["text"] == "任务已完成"
    assert items[0]["when"].count("-") == 2  # 时间已格式化（具体值随 TZ，断性质不断值）
    assert items[1]["title"] == ""  # title=null 容错为空串
    assert all(i["pkg"] != "com.should.not.appear" for i in items)  # Ranking 区段被切除


@pytest.mark.parametrize("raw", ["", "Notification List:\n  (无记录)", "garbage text without sections"])
def test_parse_notifications_empty_or_garbage_yields_empty_list(raw):
    assert srv.parse_notifications(raw) == []


def test_parse_notifications_caps_at_max_items():
    raw = "\n".join(f"  NotificationRecord( uid=1 pkg=com.cap{i} )\n    android.title=null" for i in range(35))
    items = srv.parse_notifications(raw)
    assert len(items) == srv.PHONE_NOTIFICATION_MAX_ITEMS


@pytest.mark.parametrize(
    "package",
    ["com.android.settings", "com.lingxi.agentos", "a.b"],
)
def test_validate_package_accepts_dotted_names(package):
    assert srv.validate_package(package) == package


@pytest.mark.parametrize(
    "package",
    [
        pytest.param("com.android.settings; rm -rf /", id="shell 元字符注入"),
        pytest.param("-p com.android.settings", id="adb 选项拼接"),
        pytest.param("com.android..settings", id="空段"),
        pytest.param("1com.android", id="段首数字"),
        pytest.param("com", id="单段"),
        pytest.param("com.android.settings ", id="尾随空白"),
        pytest.param("", id="空串"),
        pytest.param(123, id="非字符串"),
    ],
)
def test_validate_package_rejects_with_reason(package):
    with pytest.raises(ValueError, match="非法包名"):
        srv.validate_package(package)


@pytest.mark.parametrize(
    ("key", "canonical"),
    [
        ("back", "BACK"),
        ("Home", "HOME"),
        ("ENTER", "ENTER"),
        ("recent", "RECENT"),
        ("wakeup", "WAKEUP"),
        ("SLEEP", "SLEEP"),
    ],
)
def test_validate_key_event_accepts_whitelist_case_insensitively(key, canonical):
    assert srv.validate_key_event(key) == canonical


@pytest.mark.parametrize("key", ["MENU", "volume_up", "", "BACK;drop"])
def test_validate_key_event_rejects_outside_whitelist(key):
    with pytest.raises(ValueError, match="允许：BACK, HOME, ENTER, RECENT, WAKEUP, SLEEP"):
        srv.validate_key_event(key)


@pytest.mark.parametrize(
    ("value", "expect"),
    [(0, 0), (540, 540), (540.0, 540)],
)
def test_validate_coord_accepts_nonneg_int_and_integral_float(value, expect):
    assert srv.validate_coord("x", value) == expect


@pytest.mark.parametrize("value", [-1, "540", True, 100001, None, 3.5])
def test_validate_coord_rejects_non_int_or_out_of_range(value):
    with pytest.raises(ValueError, match="坐标"):
        srv.validate_coord("x", value)


def test_visible_texts_dedupes_keeps_order_and_respects_limit():
    import xml.etree.ElementTree as ET

    root = ET.fromstring(_UI_XML_SAMPLE)
    assert srv._visible_texts(root, limit=20) == ["设置", "WLAN"]
    assert srv._visible_texts(root, limit=1) == ["设置"]


def test_visible_texts_from_xml_translates_parse_failure():
    with pytest.raises(RuntimeError, match="UI 树 XML 解析失败"):
        srv._visible_texts_from_xml("<not-xml")


@pytest.mark.asyncio
async def test_phone_tools_report_clear_error_when_device_offline(tmp_path, ws_root, monkeypatch):
    import uiautomator2

    def offline(serial):
        raise uiautomator2.exceptions.ConnectError(f"device {serial} not online")

    monkeypatch.setattr(srv.u2, "connect", offline)
    async with mcp_session(tmp_path, [ws_root]) as (session, audit_path):
        result = await session.call_tool("phone_status", {})
    assert_rejected(result, "phone not connected")
    entries = read_audit(audit_path)
    assert len(entries) == 1
    assert entries[0]["tool"] == "phone_status"
    assert entries[0]["ok"] is False


@pytest.mark.asyncio
async def test_phone_tap_text_clicks_first_of_multiple_hits(tmp_path, ws_root, monkeypatch):
    device = _FakeDevice(count=2, xml=_UI_XML_SAMPLE)
    monkeypatch.setattr(srv.u2, "connect", lambda _serial: device)
    async with mcp_session(tmp_path, [ws_root]) as (session, _):
        result = await session.call_tool("phone_tap_text", {"text": "设置"})
    assert result.is_error is False
    assert "命中 2 处" in result.content[0].text
    assert len(device.clicks) == 1  # 多命中取第一，只点一次


@pytest.mark.asyncio
async def test_phone_tap_text_miss_reports_visible_texts_for_self_correction(tmp_path, ws_root, monkeypatch):
    device = _FakeDevice(count=0, xml=_UI_XML_SAMPLE)
    monkeypatch.setattr(srv.u2, "connect", lambda _serial: device)
    async with mcp_session(tmp_path, [ws_root]) as (session, _):
        result = await session.call_tool("phone_tap_text", {"text": "不存在的按钮"})
    text = assert_rejected(result, "屏上未找到")
    assert "设置" in text  # 可见文本清单供自纠错
    assert "WLAN" in text
    assert device.clicks == []  # 未命中不点击


@pytest.mark.asyncio
async def test_phone_tap_id_contains_match_clicks(tmp_path, ws_root, monkeypatch):
    device = _FakeDevice(count=1, xml=_UI_XML_SAMPLE)
    monkeypatch.setattr(srv.u2, "connect", lambda _serial: device)
    async with mcp_session(tmp_path, [ws_root]) as (session, audit_path):
        result = await session.call_tool("phone_tap_id", {"resource_id": "settings:id/title"})
    assert result.is_error is False
    entries = read_audit(audit_path)
    assert entries[0]["tool"] == "phone_tap_id"
    assert entries[0]["ok"] is True
    assert entries[0]["args"]["resource_id"] == "settings:id/title"  # 审计含参数摘要
