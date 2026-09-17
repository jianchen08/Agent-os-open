# @feature: FP-0.2.二 security_check 缺口分支补测 | @ci: python-coverage
# @ci: python-coverage
"""security_check 既有分支缺口补测（coverage.xml 缺口行）。

按行为分组补齐此前未覆盖的分支：
1. 装配缝：_set_plugin_ref 注入 → human-interaction capability 按名自动解析
   （含解析异常降级软拦截 + approval_channel_missing 标记）。
2. 权限模式持久化：_load/_save_permission_modes 的成功/缺文件/损坏/写失败分支。
3. execute 早退：disabled / 非 tool_execute / 无工具调用；priority 配置。
4. 降级提示推送失败不阻断（frontend.emit 异常吞并 + warning 留痕）。
5. 内置底线：CMD 风格 nul 重定向检测（变体/良性/非字符串参数 + 端到端软拦截）。
6. 危险工具授权放行：allow 白名单优先、accept_edits 文件类放行（对照 default 弹审批）。
7. 审批链路异常面：create_choice 失败、wait_for_choice 非 dict / error dict /
   异常实例（denied/cancelled/timeout/未知）→ 各自软拦截语义。
8. 审批描述格式化：字符串参数 JSON 解析、四类参数渲染与超长截断。
9. 规则引擎：非法正则告警不崩溃、合法正则命中。
10. 辅助契约：code 参数指纹、_first_args 参数形状、危险声明空条目、
    敏感路径检查非字符串参数、空字节路径 fail-closed、registry 缺工具返回空。
11. 路径穿越残余分支：resolve 后仍含 ``..``（CWD 字面量带点）的解析命中、
    含 NUL 路径经 resolve 失败走 Invalid path、编码/双重编码穿越与良性百分号
    路径的对照矩阵。
12. ``_args_hit_dangerous_ops`` 三类声明形态（op:path 前缀 / op: 空模式操作值 /
    裸命令子串）的大小写与归一化矩阵。
13. ``sensitive_paths`` 共享模块：空路径与非字符串假值、resolve 失败回落原文
    后仍按黑名单 fail-closed 命中、Windows 黑名单前缀语义。

结构性不可达防御分支（逐条说明，勿硬凑）：
- ``plugin.py`` 第 1296 行（``return "Null byte injection detected: ..."``）：
  进入该行要求 NUL 路径先通过 1269-1274 的 ``Path(path).resolve()``，而
  CPython 的 resolve 对含 NUL 路径恒抛 ValueError（``stat: embedded null
  character in path``）→ 提前在 1274 返回 ``Invalid path``。NUL 检测是
  解析成功语义下的兜底护栏，当前解释器不可达（测试以 Invalid path 断言同一
  拒绝语义，见 ``test_null_byte_rejected_via_invalid_path``）。
- ``plugin.py`` 第 1434 行（``continue``）：前置条件为 ``if pattern:``（pattern
  非空），而 ``norm_pattern = pattern.replace("\\", "/").lower()`` 对任何非空
  pattern 都产出非空串（反斜杠被换成斜杠而非删除）→ ``not norm_pattern``
  恒假。空模式声明走的是 ``else``（操作值等值）分支，不经此处。
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_THIS_DIR = str(Path(__file__).resolve().parent)
_SHARED_DIR = str(Path(__file__).resolve().parents[3])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

# 本目录测试惯例：importlib 显式按路径加载本插件 plugin.py（裸名 plugin 会与
# 其它插件目录同名模块互相劫持），本文件独享一个模块实例，全局注入态互不串扰。
_spec = importlib.util.spec_from_file_location(
    "security_check_plugin_gap_branches", str(Path(_THIS_DIR) / "plugin.py")
)
assert _spec is not None
assert _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
SecurityCheckPlugin = _mod.SecurityCheckPlugin

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_module_seams():
    """测试后恢复本模块实例的注入态（plugin 引用/审批通道/前端通道/模式表）。"""
    yield
    _mod._PLUGIN_REF = None
    _mod.set_human_interaction_cap(None)
    _mod.set_frontend_emit(None)
    _mod._PERMISSION_MODES.clear()


# needs_approval 守门规则：命中 rm -rf 的命令必须弹审批（显式注入，不触发降级）。
_GUARD_RM: list[dict[str, Any]] = [
    {
        "name": "guard_rm",
        "tools": ["*"],
        "params": ["command", "cmd"],
        "action": "needs_approval",
        "patterns": [{"type": "keyword", "value": "rm -rf"}],
    }
]


class _StubPolicyLoader:
    """固定 policy.execution 的替身（真实加载器依赖 ConfigCenter，测试环境不就绪）。"""

    def __init__(self, execution: str) -> None:
        self._execution = execution

    def resolve(self, _tool_name: str) -> Any:
        return SimpleNamespace(execution=self._execution)


def _stub_policy(monkeypatch: pytest.MonkeyPatch, execution: str) -> None:
    """把本模块实例的隔离策略加载器替换为固定 execution 的替身。"""
    monkeypatch.setattr(_mod, "_policy_loader", _StubPolicyLoader(execution))


def _tool_ctx(
    tool_name: str,
    args: dict[str, Any],
    *,
    pipeline_id: str = "p-gap",
    session_id: str = "sess-gap",
) -> Any:
    """构造 tool_execute 的最小 PluginContext（host provider、非隔离）。"""
    from pipeline.plugin import PluginContext

    return PluginContext(
        state={
            "core_type": "tool_execute",
            "pipeline_id": pipeline_id,
            "session_id": session_id,
            "raw_tool_calls": [{"name": tool_name, "args": args}],
            "execution_contexts": [
                {"tool_name": tool_name, "provider": "host", "task_isolated": False}
            ],
            "messages": [],
        },
        _services={},
    )


class _ScriptedApprovalCap:
    """假 human-interaction capability（外部交互服务边界的脚本化替身）。

    create_choice 返回 create_result（None 时默认成功）；wait_for_choice 返回
    wait_result 或抛出 wait_raises。requests 记录审批请求载荷（审批面可见的
    描述文本），wait_calls 记录等待调用（断言"未进入等待"）。
    """

    def __init__(
        self,
        create_result: Any = None,
        wait_result: Any = None,
        wait_raises: Exception | None = None,
    ) -> None:
        self.create_result = create_result
        self.wait_result = wait_result
        self.wait_raises = wait_raises
        self.requests: list[dict[str, Any]] = []
        self.wait_calls: list[dict[str, Any]] = []

    async def call(self, name: str, params: dict[str, Any], **kwargs: Any) -> Any:  # noqa: ARG002
        if name == "create_choice":
            self.requests.append(dict(params))
            if self.create_result is not None:
                return self.create_result
            return {"request_id": f"req-{len(self.requests)}"}
        if name == "wait_for_choice":
            self.wait_calls.append(dict(params))
            if self.wait_raises is not None:
                raise self.wait_raises
            return self.wait_result
        raise AssertionError(f"unexpected cap.call: {name}")


def _approval_plugin(monkeypatch: pytest.MonkeyPatch) -> Any:
    """构造弹审批路径就绪的插件（bash_execute 判危险 + rm -rf 守门规则）。"""
    _stub_policy(monkeypatch, "command_in_container")
    return SecurityCheckPlugin(config={"enabled": True, "rules": _GUARD_RM})


# ═══════════════════════════════════════════════════════════════
# 1. 装配缝：plugin 引用注入 → capability 自动解析
# ═══════════════════════════════════════════════════════════════


class TestPluginRefResolution:
    """_set_plugin_ref 注入后，human-interaction capability 按名自动解析。"""

    @pytest.mark.parametrize("exc_type", [KeyError, AttributeError])
    def test_broken_plugin_ref_degrades_to_none_with_warn(
        self, caplog: pytest.LogCaptureFixture, exc_type: type[Exception]
    ) -> None:
        """plugin 引用解析异常（manifest 未声明/注入链断）→ None + warn 留痕。"""

        class _BrokenRef:
            def get_capability(self, _name: str) -> Any:
                raise exc_type("human-interaction")

        _mod._set_plugin_ref(_BrokenRef())
        with caplog.at_level(logging.WARNING, logger=_mod.__name__):
            assert _mod._get_human_interaction_cap() is None
        records = [r for r in caplog.records if "human-interaction" in r.getMessage()]
        assert records, "解析失败必须留 warn"
        assert "get_capability" in records[0].getMessage(), "解析来源须可观测"

    def test_healthy_plugin_ref_resolves_capability(self) -> None:
        """健康引用：get_capability("human-interaction") 的返回值即审批通道。"""
        cap = object()
        _mod._set_plugin_ref(SimpleNamespace(get_capability=lambda _name: cap))
        assert _mod._get_human_interaction_cap() is cap

    @pytest.mark.asyncio
    async def test_approval_via_plugin_ref_capability(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """生产行为：on_load 注入 plugin 引用后审批链自动可用并完成一次审批。"""
        cap = _ScriptedApprovalCap(wait_result={"selected_option": "approved_once"})
        _mod._set_plugin_ref(SimpleNamespace(get_capability=lambda _name: cap))
        plugin = _approval_plugin(monkeypatch)

        r = await plugin.execute(_tool_ctx("bash_execute", {"command": "rm -rf /gap-ref-probe"}))

        assert len(cap.requests) == 1, "审批请求应经 plugin 引用自动解析发出"
        assert r.state_updates["security.decision"]["reason"] == "approved"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exc_type", [KeyError, AttributeError])
    async def test_broken_plugin_ref_soft_blocks_with_missing_marker(
        self, monkeypatch: pytest.MonkeyPatch, exc_type: type[Exception]
    ) -> None:
        """审批底座解析失败 → 软拦截且显式落 approval_channel_missing 标记。"""

        class _BrokenRef:
            def get_capability(self, _name: str) -> Any:
                raise exc_type("human-interaction")

        _mod._set_plugin_ref(_BrokenRef())
        plugin = _approval_plugin(monkeypatch)

        r = await plugin.execute(_tool_ctx("bash_execute", {"command": "rm -rf /gap-broken-ref"}))

        decision = r.state_updates["security.decision"]
        assert decision["approval_channel_missing"] is True
        assert "soft_block" in decision["reason"]
        assert r.state_updates["raw_tool_calls"] == []


# ═══════════════════════════════════════════════════════════════
# 2. 权限模式持久化
# ═══════════════════════════════════════════════════════════════


class TestPermissionModePersistence:
    """权限模式表的启动加载与持久化（失败留痕不阻断）。"""

    def _modes_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: str
    ) -> Path:
        f = tmp_path / "permission_modes.json"
        f.write_text(payload, encoding="utf-8")
        monkeypatch.setattr(_mod, "_PERMISSION_MODES_FILE", str(f))
        return f

    def test_load_valid_file_filters_unknown_modes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """合法文件正常加载；非法模式值被过滤，只留已知档位。"""
        self._modes_file(
            monkeypatch, tmp_path, json.dumps({"p1": "auto", "p2": "alien_mode", "p3": "bypass"})
        )
        with caplog.at_level(logging.INFO, logger=_mod.__name__):
            _mod._load_permission_modes()
        assert _mod._PERMISSION_MODES == {"p1": "auto", "p3": "bypass"}
        assert any("权限模式表加载" in r.getMessage() for r in caplog.records)

    def test_load_missing_file_silent_keeps_table(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """首启无持久化文件（FileNotFoundError）：静默保留现状、零告警。"""
        monkeypatch.setattr(
            _mod, "_PERMISSION_MODES_FILE", str(tmp_path / "absent" / "pm.json")
        )
        _mod._PERMISSION_MODES["stale"] = "auto"
        with caplog.at_level(logging.WARNING, logger=_mod.__name__):
            _mod._load_permission_modes()
        assert _mod._PERMISSION_MODES == {"stale": "auto"}
        assert not [r for r in caplog.records if "权限模式表" in r.getMessage()]

    def test_load_corrupt_json_warns_keeps_table(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """损坏 JSON：warning 留痕、不崩溃、模式表不丢。"""
        self._modes_file(monkeypatch, tmp_path, "{corrupt-json")
        _mod._PERMISSION_MODES["keep"] = "default"
        with caplog.at_level(logging.WARNING, logger=_mod.__name__):
            _mod._load_permission_modes()
        assert any("权限模式表" in r.getMessage() for r in caplog.records)
        assert _mod._PERMISSION_MODES == {"keep": "default"}

    def test_load_non_dict_json_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """JSON 非 dict 形状：按空表处理，无告警。"""
        self._modes_file(monkeypatch, tmp_path, '["p1"]')
        with caplog.at_level(logging.WARNING, logger=_mod.__name__):
            _mod._load_permission_modes()
        assert _mod._PERMISSION_MODES == {}
        assert not [r for r in caplog.records if "权限模式表" in r.getMessage()]

    def test_save_writes_and_roundtrips(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """持久化写盘后重载一致（原子替换的落盘契约）。"""
        f = tmp_path / "pm.json"
        monkeypatch.setattr(_mod, "_PERMISSION_MODES_FILE", str(f))
        _mod._PERMISSION_MODES["p1"] = "accept_edits"
        _mod._save_permission_modes()
        assert json.loads(f.read_text(encoding="utf-8")) == {"p1": "accept_edits"}
        _mod._PERMISSION_MODES.clear()
        _mod._load_permission_modes()
        assert _mod._PERMISSION_MODES == {"p1": "accept_edits"}

    def test_save_failure_warns_not_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """持久化失败（目录位被文件占用）：warning 留痕，不向调用方抛异常。"""
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setattr(_mod, "_PERMISSION_MODES_FILE", str(blocker / "pm.json"))
        with caplog.at_level(logging.WARNING, logger=_mod.__name__):
            _mod._save_permission_modes()
        assert any("权限模式表" in r.getMessage() for r in caplog.records)


# ═══════════════════════════════════════════════════════════════
# 3. execute 早退与 priority
# ═══════════════════════════════════════════════════════════════


class TestExecuteShortCircuits:
    """非检查场景直接放行（不出任何拒绝副作用）。"""

    @pytest.mark.asyncio
    async def test_disabled_plugin_allows(self) -> None:
        plugin = SecurityCheckPlugin(config={"enabled": False, "rules": _GUARD_RM})
        r = await plugin.execute(_tool_ctx("bash_execute", {"command": "rm -rf /x"}))
        d = r.state_updates["security.decision"]
        assert d["allowed"] is True
        assert d["reason"] == "security check disabled"
        assert "tool_results" not in r.state_updates

    @pytest.mark.asyncio
    async def test_non_tool_core_skips_check(self) -> None:
        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": _GUARD_RM})
        ctx = _tool_ctx("bash_execute", {"command": "rm -rf /x"})
        ctx.state["core_type"] = "llm_call"
        r = await plugin.execute(ctx)
        d = r.state_updates["security.decision"]
        assert d["allowed"] is True
        assert d["reason"] == "not a tool execution"

    @pytest.mark.asyncio
    async def test_no_tool_calls_allows(self) -> None:
        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": _GUARD_RM})
        ctx = _tool_ctx("bash_execute", {"command": "rm -rf /x"})
        ctx.state["raw_tool_calls"] = []
        r = await plugin.execute(ctx)
        d = r.state_updates["security.decision"]
        assert d["allowed"] is True
        assert d["reason"] == "no tool calls to check"

    def test_priority_from_config(self) -> None:
        """priority 缺省 70，config 可覆盖。"""
        assert SecurityCheckPlugin(config={"rules": _GUARD_RM}).priority == 70
        assert SecurityCheckPlugin(config={"priority": 33}).priority == 33


# ═══════════════════════════════════════════════════════════════
# 4. 降级提示推送失败不阻断
# ═══════════════════════════════════════════════════════════════


class TestDegradedNoticeEmitFailure:
    @pytest.mark.asyncio
    async def test_emit_failure_does_not_block_check(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """frontend.emit 抛异常：通知放弃（warning 留痕），安全检查照常出决策。"""

        class _BoomEmit:
            async def __call__(
                self, _event: str, _payload: dict[str, Any], _thread_id: str
            ) -> None:
                raise RuntimeError("emit down")

        _mod.set_frontend_emit(_BoomEmit())
        plugin = SecurityCheckPlugin(
            config={"enabled": True, "security_rules": {"mode": "blacklist"}}
        )
        assert plugin._rules_degraded is True

        with caplog.at_level(logging.WARNING, logger=_mod.__name__):
            r = await plugin.execute(
                _tool_ctx("bash_execute", {"command": "rm -rf /gap-emit-probe"})
            )

        d = r.state_updates["security.decision"]
        assert d["allowed"] is True
        assert "soft_block" in d["reason"], "emit 失败不得改变安全决策（命中默认规则→审批链→cap 缺席→软拦截）"
        assert any("事件推送失败" in rec.getMessage() for rec in caplog.records)


# ═══════════════════════════════════════════════════════════════
# 5. 内置底线：CMD 风格 nul 重定向
# ═══════════════════════════════════════════════════════════════


class TestNulRedirectBuiltin:
    """nul 重定向检测：变体全命中、良性命令放行、非字符串参数跳过。"""

    @pytest.mark.parametrize(
        "command",
        [
            "dir >nul",
            "echo probe 2>nul",
            "type a.txt >>nul",
            "run.exe >&nul",
            "ping host > NUL",
            "build 2>&1 > nul ",
        ],
    )
    def test_nul_redirect_variants_detected(self, command: str) -> None:
        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": _GUARD_RM})
        reason = plugin._check_nul_redirect({"command": command})
        assert reason.startswith("CMD-style nul redirect detected"), (
            f"{command!r} 应命中 nul 重定向检测，实际 reason={reason!r}"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "echo hi > /dev/null",
            "sort in.txt > null_report.txt",
            "python app.py --nullable flag",
            "make build 2>/dev/null | tee out.log",
            "echo done",
        ],
    )
    def test_benign_commands_pass(self, command: str) -> None:
        """/dev/null、null 单词、无重定向命令不得误命中。"""
        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": _GUARD_RM})
        assert plugin._check_nul_redirect({"command": command}) == ""

    def test_non_string_command_ignored(self) -> None:
        """command/cmd 参数非字符串（形状异常）时跳过检测，不崩溃。"""
        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": _GUARD_RM})
        assert plugin._check_nul_redirect({"command": 123, "cmd": None}) == ""

    @pytest.mark.asyncio
    async def test_nul_redirect_soft_blocks_before_authorization(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """端到端：内置底线优先于危险判定/权限模式，命中即软拦截反馈 LLM。"""
        _stub_policy(monkeypatch, "command_in_container")
        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": _GUARD_RM})

        with caplog.at_level(logging.WARNING, logger=_mod.__name__):
            r = await plugin.execute(
                _tool_ctx("bash_execute", {"command": "echo probe 2>nul"})
            )

        d = r.state_updates["security.decision"]
        assert "soft_block" in d["reason"]
        assert "CMD 风格重定向被拦截" in r.state_updates["raw_result"]
        assert r.state_updates["raw_tool_calls"] == []
        assert any("nul redirect" in rec.getMessage() for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_dev_null_redirect_passes_full_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """对照：2>/dev/null 是合法习惯，检查链走完放行、零副作用。"""
        _stub_policy(monkeypatch, "command_in_container")
        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": _GUARD_RM})
        r = await plugin.execute(
            _tool_ctx("bash_execute", {"command": "wc -l a.txt 2>/dev/null"})
        )
        d = r.state_updates["security.decision"]
        assert d == {"allowed": True, "reason": "all checks passed"}
        assert "tool_results" not in r.state_updates


# ═══════════════════════════════════════════════════════════════
# 6. 危险工具授权放行：allow 白名单 / accept_edits
# ═══════════════════════════════════════════════════════════════


class TestDangerousToolAuthorizationPasses:
    @pytest.mark.asyncio
    async def test_allow_rule_passes_dangerous_tool_without_approval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """危险工具参数命中 allow 白名单 → 放行，审批通道零发起（allow 优先）。"""
        _stub_policy(monkeypatch, "command_in_container")
        cap = _ScriptedApprovalCap()
        _mod.set_human_interaction_cap(cap)
        plugin = SecurityCheckPlugin(
            config={
                "enabled": True,
                "rules": [
                    {
                        "name": "safe_echo",
                        "tools": ["*"],
                        "params": ["command"],
                        "action": "allow",
                        "patterns": [{"type": "keyword", "value": "echo gap-allow"}],
                    },
                    {
                        "name": "guard_curl",
                        "tools": ["*"],
                        "params": ["command"],
                        "action": "needs_approval",
                        "patterns": [{"type": "keyword", "value": "curl"}],
                    },
                ],
            }
        )
        # 同一命令同时命中 allow（safe_echo）与 needs_approval（guard_curl）：
        # allow 优先 → 直接放行，不发起审批
        r = await plugin.execute(
            _tool_ctx(
                "bash_execute",
                {"command": "echo gap-allow-probe && curl http://example.com"},
            )
        )
        d = r.state_updates["security.decision"]
        assert d == {"allowed": True, "reason": "all checks passed"}
        assert cap.requests == [], "allow 白名单命中不得发起审批"
        assert "tool_results" not in r.state_updates

    @pytest.mark.asyncio
    async def test_same_command_without_allow_hits_approval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """对照：移除 allow 规则后同一命令走审批链（needs_approval 命中）。"""
        _stub_policy(monkeypatch, "command_in_container")
        cap = _ScriptedApprovalCap(wait_result={"selected_option": "approved_once"})
        _mod.set_human_interaction_cap(cap)
        plugin = SecurityCheckPlugin(
            config={
                "enabled": True,
                "rules": [
                    {
                        "name": "guard_curl",
                        "tools": ["*"],
                        "params": ["command"],
                        "action": "needs_approval",
                        "patterns": [{"type": "keyword", "value": "curl"}],
                    }
                ],
            }
        )
        r = await plugin.execute(
            _tool_ctx(
                "bash_execute",
                {"command": "echo gap-allow-probe && curl http://example.com"},
            )
        )
        assert len(cap.requests) == 1, "无 allow 命中时必须弹审批"
        assert r.state_updates["security.decision"]["reason"] == "approved"

    def _file_write_plugin(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        """构造 file_write 危险判定就绪的插件（host_direct + 声明 write:/tmp/）。"""
        _stub_policy(monkeypatch, "host_direct")
        plugin = SecurityCheckPlugin(
            config={
                "enabled": True,
                "rules": [
                    {
                        "name": "tmp_write_guard",
                        "tools": ["file_write"],
                        "params": ["path"],
                        "action": "needs_approval",
                        "patterns": [{"type": "keyword", "value": "/tmp/"}],
                    }
                ],
            }
        )
        plugin._dangerous_ops_by_tool = {"file_write": ["write:/tmp/"]}
        return plugin

    @pytest.mark.asyncio
    async def test_accept_edits_file_tool_skips_approval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """accept_edits 模式：file_write（危险工具）文件类放行，规则命中也不弹审批。"""
        _mod._PERMISSION_MODES["sess-gap"] = "accept_edits"
        cap = _ScriptedApprovalCap()
        _mod.set_human_interaction_cap(cap)
        plugin = self._file_write_plugin(monkeypatch)

        r = await plugin.execute(
            _tool_ctx("file_write", {"path": "/tmp/gap-target/x.txt", "content": "hi"})
        )

        d = r.state_updates["security.decision"]
        assert d == {"allowed": True, "reason": "all checks passed"}
        assert cap.requests == [], "accept_edits 文件类放行不得发起审批"

    @pytest.mark.asyncio
    async def test_default_mode_same_file_tool_asks_approval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """对照：default 模式下同参数弹审批（accept_edits 与 default 有真实差异）。"""
        cap = _ScriptedApprovalCap(wait_result={"selected_option": "approved_once"})
        _mod.set_human_interaction_cap(cap)
        plugin = self._file_write_plugin(monkeypatch)

        r = await plugin.execute(
            _tool_ctx("file_write", {"path": "/tmp/gap-target/y.txt", "content": "hi"})
        )

        assert len(cap.requests) == 1
        assert r.state_updates["security.decision"]["reason"] == "approved"
        assert "path" in cap.requests[0]["description"], "审批描述应包含待审路径"


# ═══════════════════════════════════════════════════════════════
# 7. 审批链路异常面
# ═══════════════════════════════════════════════════════════════


class TestApprovalFailureSurface:
    """create_choice / wait_for_choice 各失败形态 → 对应软拦截语义，不中断管道。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("create_result", [{"error": "kernel busy"}, "not-a-dict"])
    async def test_create_choice_failure_soft_blocks_without_waiting(
        self, monkeypatch: pytest.MonkeyPatch, create_result: Any
    ) -> None:
        """审批请求创建失败（error dict / 非 dict）：软拦截且不得进入等待。"""
        plugin = _approval_plugin(monkeypatch)
        cap = _ScriptedApprovalCap(create_result=create_result)
        _mod.set_human_interaction_cap(cap)

        r = await plugin.execute(
            _tool_ctx("bash_execute", {"command": "rm -rf /gap-create-fail"})
        )

        d = r.state_updates["security.decision"]
        assert d["allowed"] is True
        assert "审批服务异常" in d["reason"]
        assert "soft_block" in d["reason"]
        assert cap.wait_calls == [], "请求未创建成功不得进入等待"

    @pytest.mark.asyncio
    async def test_wait_non_dict_response_soft_blocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """等待响应非 dict（协议破坏）：软拦截反馈 LLM。"""
        plugin = _approval_plugin(monkeypatch)
        cap = _ScriptedApprovalCap(wait_result="garbage-response")
        _mod.set_human_interaction_cap(cap)

        r = await plugin.execute(
            _tool_ctx("bash_execute", {"command": "rm -rf /gap-wait-shape"})
        )

        d = r.state_updates["security.decision"]
        assert "审批服务异常" in d["reason"]
        assert r.state_updates["raw_tool_calls"] == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("error_msg", "expected_fragment"),
        [
            ("request denied by user", "用户拒绝执行"),
            ("request cancelled by user", "审批被取消"),
            ("timeout reached", "审批超时未响应"),
            ("backend exploded", "审批服务异常"),
        ],
    )
    async def test_wait_error_dict_converts_to_matching_soft_block(
        self,
        monkeypatch: pytest.MonkeyPatch,
        error_msg: str,
        expected_fragment: str,
    ) -> None:
        """等待返回 error dict：按语义（denied/cancel/timeout/其它）转换处置。"""
        plugin = _approval_plugin(monkeypatch)
        cap = _ScriptedApprovalCap(wait_result={"error": error_msg})
        _mod.set_human_interaction_cap(cap)

        r = await plugin.execute(
            _tool_ctx("bash_execute", {"command": "rm -rf /gap-wait-error"})
        )

        d = r.state_updates["security.decision"]
        assert d["allowed"] is True
        assert expected_fragment in d["reason"], (
            f"error={error_msg!r} 应映射为 {expected_fragment}，实际 reason={d['reason']!r}"
        )
        assert "soft_block" in d["reason"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("wait_raises", "expected_fragment"),
        [
            (_mod.InteractionDeniedError("req-x", "user said no"), "用户拒绝执行"),
            (_mod.InteractionCancelledError("req-x", "user closed panel"), "审批被取消"),
            (_mod.InteractionTimeoutError("req-x", 86400.0), "审批超时未响应"),
        ],
    )
    async def test_interaction_exceptions_soft_block(
        self,
        monkeypatch: pytest.MonkeyPatch,
        wait_raises: Exception,
        expected_fragment: str,
    ) -> None:
        """交互异常实例直接上抛（不经 error dict 转换）：同样落对应软拦截。"""
        plugin = _approval_plugin(monkeypatch)
        cap = _ScriptedApprovalCap(wait_raises=wait_raises)
        _mod.set_human_interaction_cap(cap)

        r = await plugin.execute(
            _tool_ctx("bash_execute", {"command": "rm -rf /gap-exc-surface"})
        )

        d = r.state_updates["security.decision"]
        assert expected_fragment in d["reason"]
        assert "soft_block" in d["reason"]

    @pytest.mark.asyncio
    async def test_denied_with_empty_reason_falls_back_to_rule_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """拒绝异常无 reason 时回退规则名（拒绝原因始终可观测）。"""
        plugin = _approval_plugin(monkeypatch)
        cap = _ScriptedApprovalCap(wait_raises=_mod.InteractionDeniedError("req-x", ""))
        _mod.set_human_interaction_cap(cap)

        r = await plugin.execute(
            _tool_ctx("bash_execute", {"command": "rm -rf /gap-empty-reason"})
        )

        reason = r.state_updates["security.decision"]["reason"]
        # 回退值是发起审批时的 reason 文案（"参数命中安全规则: <规则名>"）
        assert "用户拒绝执行: 参数命中安全规则: guard_rm" in reason


class TestApprovalChannelErrorRetryMarker:
    """BUG-41：审批通道故障（wait 异常）的软拦截必须带 retry_allowed 标记。

    该路径人审结果未知（用户可能已点批准但响应丢失）、调用从未执行——
    duplicate_check 据标记把该次未执行的签名从重复窗口摘除，人审后的重试
    不得被去重拦截。用户主动拒绝/超时/取消不是通道故障，不带标记。
    """

    @pytest.mark.asyncio
    async def test_wait_exception_marks_retry_allowed_with_arguments(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plugin = _approval_plugin(monkeypatch)
        cap = _ScriptedApprovalCap(wait_raises=RuntimeError("sidecar restarted mid-wait"))
        _mod.set_human_interaction_cap(cap)

        r = await plugin.execute(
            _tool_ctx("bash_execute", {"command": "rm -rf /gap-retry-marker"})
        )

        d = r.state_updates["security.decision"]
        assert "审批服务异常" in d["reason"]
        results = r.state_updates["tool_results"]
        assert results, "软拦截必须产出拒绝 tool_result"
        for entry in results:
            assert entry["success"] is False
            assert entry.get("retry_allowed") is True
            assert entry["arguments"] == {"command": "rm -rf /gap-retry-marker"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "wait_raises",
        [
            _mod.InteractionDeniedError("req-x", "user said no"),
            _mod.InteractionCancelledError("req-x", "user closed panel"),
            _mod.InteractionTimeoutError("req-x", 86400.0),
        ],
    )
    async def test_denied_cancelled_timeout_do_not_mark_retry_allowed(
        self, monkeypatch: pytest.MonkeyPatch, wait_raises: Exception
    ) -> None:
        """非通道故障的软拦截（拒/取消/超时）不带 retry_allowed——去重闸保留。"""
        plugin = _approval_plugin(monkeypatch)
        cap = _ScriptedApprovalCap(wait_raises=wait_raises)
        _mod.set_human_interaction_cap(cap)

        r = await plugin.execute(
            _tool_ctx("bash_execute", {"command": "rm -rf /gap-no-retry"})
        )

        for entry in r.state_updates["tool_results"]:
            assert "retry_allowed" not in entry
            assert "arguments" not in entry


# ═══════════════════════════════════════════════════════════════
# 8. 审批描述格式化
# ═══════════════════════════════════════════════════════════════


class TestApprovalDescriptionRendering:
    """审批描述是用户在审批面上看到的唯一信息，渲染契约在此锁定。"""

    @pytest.mark.asyncio
    async def test_description_renders_and_truncates_all_param_kinds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """命令/路径/内容/代码/URL 全渲染，超长项截断并标注。"""
        cap = _ScriptedApprovalCap(wait_result={"selected_option": "approved_once"})
        _mod.set_human_interaction_cap(cap)
        plugin = _approval_plugin(monkeypatch)

        await plugin.execute(
            _tool_ctx(
                "bash_execute",
                {
                    "command": "rm -rf /gap-target && echo " + "p" * 600,
                    "path": "docs/gap.md",
                    "content": "lorem\n" * 100,
                    "code": "x = 1\n" * 80,
                    "url": "https://example.com/gap-probe",
                },
            )
        )

        desc = cap.requests[0]["description"]
        assert "命令过长，已截断" in desc
        assert "内容过长，已截断" in desc
        assert "代码过长，已截断" in desc
        assert "URL: https://example.com/gap-probe" in desc
        assert "path: docs/gap.md" in desc
        assert "工具: bash_execute" in desc

    @pytest.mark.asyncio
    async def test_short_values_not_truncated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """对照：短参数完整渲染，无截断标注。"""
        cap = _ScriptedApprovalCap(wait_result={"selected_option": "approved_once"})
        _mod.set_human_interaction_cap(cap)
        plugin = _approval_plugin(monkeypatch)

        await plugin.execute(
            _tool_ctx("bash_execute", {"command": "rm -rf /gap-short-target"})
        )

        desc = cap.requests[0]["description"]
        assert "rm -rf /gap-short-target" in desc
        assert "命令过长" not in desc
        assert "内容过长" not in desc
        assert "代码过长" not in desc

    @pytest.mark.parametrize(
        ("raw_args", "expect_command_section"),
        [
            ('{"command": "ls gap-args-probe"}', True),
            ("not-json{", False),
            ('["not", "a", "dict"]', False),
        ],
    )
    def test_string_args_shapes_render(
        self, raw_args: str, expect_command_section: bool
    ) -> None:
        """args 为字符串时按 JSON 解析：合法 dict 展开渲染，非法/非 dict 视为空不崩溃。"""
        desc = SecurityCheckPlugin._format_args_for_approval(
            [{"name": "bash_execute", "args": raw_args}], "bash_execute"
        )
        assert "工具: bash_execute" in desc
        assert ("命令:" in desc) is expect_command_section


# ═══════════════════════════════════════════════════════════════
# 9. 规则引擎边缘
# ═══════════════════════════════════════════════════════════════


class TestRuleRegexEdge:
    def test_invalid_regex_warns_and_does_not_match(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """规则中的非法正则：告警留痕（含规则名），不崩溃、不误判命中。"""
        plugin = SecurityCheckPlugin(
            config={
                "enabled": True,
                "rules": [
                    {
                        "name": "bad_regex_rule",
                        "tools": ["*"],
                        "params": ["command"],
                        "action": "block",
                        "patterns": [{"type": "regex", "value": "["}],
                    }
                ],
            }
        )
        with caplog.at_level(logging.WARNING, logger=_mod.__name__):
            action, rule = plugin._match_rules(
                "bash_execute", {"command": "anything gap-regex-probe"}
            )
        assert (action, rule) == ("", "")
        records = [r for r in caplog.records if "Invalid regex" in r.getMessage()]
        assert records, "非法正则必须告警留痕"
        assert "bad_regex_rule" in records[0].getMessage(), "告警须含规则名"

    def test_valid_regex_matches(self) -> None:
        """对照：合法正则正常命中。"""
        plugin = SecurityCheckPlugin(
            config={
                "enabled": True,
                "rules": [
                    {
                        "name": "curl_regex_rule",
                        "tools": ["*"],
                        "params": ["command"],
                        "action": "block",
                        "patterns": [{"type": "regex", "value": r"curl\s+-s"}],
                    }
                ],
            }
        )
        action, rule = plugin._match_rules("bash_execute", {"command": "curl -s http://x"})
        assert (action, rule) == ("block", "curl_regex_rule")


# ═══════════════════════════════════════════════════════════════
# 10. 辅助契约：指纹 / 参数形状 / 内置检测边缘
# ═══════════════════════════════════════════════════════════════


class TestSignatureAndHelpers:
    def test_code_param_counts_into_signature(self) -> None:
        """code 参数计入指纹：空白归一化同指纹，代码内容不同则指纹不同。"""
        plugin = SecurityCheckPlugin()
        sig1 = plugin._make_signature("code_run", {"code": "print(1)\nprint(2)"})
        sig2 = plugin._make_signature("code_run", {"code": "print(1)   print(2)"})
        sig3 = plugin._make_signature("code_run", {"code": "print(999)"})
        assert sig1 is not None and sig1.startswith("code_run:")
        assert sig1 == sig2, "仅空白差异应归一为同指纹"
        assert sig1 != sig3, "代码不同必须产生不同指纹"

    @pytest.mark.parametrize(
        ("tool_calls", "tool", "expected"),
        [
            ([{"name": "t", "args": '{"command": "x"}'}], "t", {}),
            ([{"name": "other", "args": {"command": "x"}}], "t", {}),
            ([{"name": "t", "args": 42}], "t", {}),
            ([{"name": "t", "args": {"command": "x"}}], "t", {"command": "x"}),
        ],
    )
    def test_first_args_shapes(
        self,
        tool_calls: list[dict[str, Any]],
        tool: str,
        expected: dict[str, Any],
    ) -> None:
        """触发工具参数形状异常（字符串/缺失/非 dict）时返回空 dict，正常时取参。"""
        assert SecurityCheckPlugin._first_args(tool_calls, tool) == expected

    def test_empty_entry_in_dangerous_declaration_is_ignored(self) -> None:
        """危险声明中的空条目跳过（不崩溃不误判），非空条目照常子串命中。"""
        plugin = SecurityCheckPlugin()
        assert plugin._args_hit_dangerous_ops({"command": "ls -la"}, [""]) is False
        assert plugin._args_hit_dangerous_ops({"command": "rm -rf /x"}, ["rm -rf"]) is True

    def test_sensitive_path_check_skips_non_string_values(self) -> None:
        """路径参数非字符串（形状异常）时跳过黑名单检查，字符串参数照常判定。"""
        plugin = SecurityCheckPlugin()
        assert plugin._check_sensitive_paths({"path": 123}) == ""
        assert plugin._check_sensitive_paths({"file_path": ["a"]}) == ""
        assert plugin._check_sensitive_paths({"path": "workspace/normal.txt"}) == ""

    def test_null_byte_path_rejected_fail_closed(self) -> None:
        """空字节路径必须被拒（fail-closed）：无论解析层如何处置，检测不得放行。"""
        plugin = SecurityCheckPlugin()
        reason = plugin._check_path_traversal({"path": "gap/null\x00byte.txt"})
        assert reason != "", "空字节路径不得静默放行"
        assert "gap/null\x00byte.txt" in reason, "拦截原因应回显原始路径"

    def test_registry_missing_tool_returns_empty(self) -> None:
        """注入配置未命中时回退 registry；registry 查无此工具返回空声明。"""
        plugin = SecurityCheckPlugin(config={})
        registry = SimpleNamespace(get=lambda _name: None)
        ctx = SimpleNamespace(
            state={},
            get_service=lambda name: registry if name == "tool_registry" else None,
        )
        assert plugin._get_dangerous_operations(ctx, "ghost_tool") == []


if __name__ == "__main__":
    pytest.main([__file__, "-q"])


# ============================================================
# 路径穿越/敏感路径残余分支（coverage.xml 缺口靶行）
# ============================================================


class TestPathTraversalResolvedBranch:
    """``_check_path_traversal`` 的 resolve 后判定与编码绕过分支。"""

    def _plugin(self) -> Any:
        return SecurityCheckPlugin(config={"enabled": True})

    def test_resolved_path_containing_dotdot_is_rejected(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """CWD 自身含 ``..`` 字面量时，相对路径 resolve 后仍带 ``..`` →
        按解析后穿越拒绝（原始路径无 ``..`` 也拦得住，防 CWD 形态绕过）。"""
        import os

        weird_cwd = tmp_path / "a..b"
        weird_cwd.mkdir()
        monkeypatch.chdir(weird_cwd)
        try:
            reason = self._plugin()._check_path_traversal({"path": "sub/file.txt"})
        finally:
            os.chdir(str(tmp_path))

        assert "Path traversal in resolved path" in reason
        assert "sub/file.txt" in reason

    @pytest.mark.parametrize(
        "path",
        ["a\x00b.png", "C:/x\x00y.png", "dir/keep\x00.png"],
    )
    def test_null_byte_path_always_rejected(self, path: str) -> None:
        """含 NUL 的路径必须被拒绝（安全不变量），拒绝理由二选一：

        - ``Invalid path``：``Path.resolve()`` 对 NUL 抛 ValueError（当前
          解释器实测路径）；
        - ``Null byte injection detected``：解析成功语义下的兜底检查。

        断"必被拒绝 + 理由可定位"而非钉某一条消息——两条路径都是拒绝语义，
        钉死单一文案会让该不变量在实现微调时假红。
        """
        reason = self._plugin()._check_path_traversal({"path": path})

        assert reason, "含 NUL 的路径绝不允许放行"
        assert ("Invalid path" in reason) or ("Null byte injection" in reason), reason
        assert path.split("\x00")[0] in reason, "拒绝理由须含可定位的路径前缀"

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("%2e%2e%2fetc%2fpasswd", "Encoded path traversal"),
            ("%252e%252e%252fetc", "Encoded path traversal"),   # 双重编码
            ("a%2e%2eb", "Encoded path traversal"),              # 解码出 .. 即命中（编码等同原形）
            ("a%20b.png", ""),                                    # 良性编码（空格）放行
            ("100%25.png", ""),                                   # 良性百分号放行
        ],
    )
    def test_encoded_traversal_matrix(self, path: str, expected: str) -> None:
        """URL 编码/双重编码穿越命中，良性百分号路径放行（不误报）。"""
        reason = self._plugin()._check_path_traversal({"path": path})

        if expected:
            assert expected in reason
        else:
            assert reason == ""

    @pytest.mark.parametrize(
        ("path", "expected_hit"),
        [
            ("plain/file.txt", False),
            ("C:/Users/me/proj/a.py", False),
            ("../escape", True),
            (chr(46) * 2 + chr(92) + "escape", True),  # 反斜杠形态穿越
        ],
    )
    def test_benign_and_traversal_symmetry(self, path: str, expected_hit: bool) -> None:
        """良性路径零产出、穿越路径命中（同契约两面，防单向拟合）。"""
        reason = self._plugin()._check_path_traversal({"path": path})

        assert bool(reason) is expected_hit

    def test_empty_args_yields_empty_reason(self) -> None:
        """无路径参数 → 空串（不产生误导性拒绝）。"""
        assert self._plugin()._check_path_traversal({}) == ""


class TestDangerousOpsDeclarations:
    """``_args_hit_dangerous_ops`` 声明形态分派。"""

    def _plugin(self) -> Any:
        return SecurityCheckPlugin(config={"enabled": True})

    @pytest.mark.parametrize(
        ("tool_args", "ops", "expected"),
        [
            (None, ["rm -rf"], False),                                   # 无参数
            ({}, ["rm -rf"], False),
            ({"path": "/etc/passwd"}, ["read:/etc/"], True),             # 路径前缀命中
            ({"path": "/ETC/passwd"}, ["read:/etc/"], True),             # 大小写不敏感
            ({"path": "/etc2/passwd"}, ["read:/etc"], True),             # 前缀语义（非目录边界）
            ({"path": "/tmp/x"}, ["read:/etc/"], False),                 # 前缀不符
            ({"file_path": "C:/Windows/x"}, ["write:c:\\windows\\"], True),  # 盘符归一
            ({"path": "relative/x"}, ["read:/etc/"], False),             # 相对路径不匹配
        ],
    )
    def test_operation_path_prefix_declarations(
        self, tool_args: Any, ops: list[str], expected: bool,
    ) -> None:
        """``op:path`` 形态按路径参数前缀匹配（分隔符/大小写归一）。"""
        assert self._plugin()._args_hit_dangerous_ops(tool_args, ops) is expected

    @pytest.mark.parametrize(
        ("tool_args", "ops", "expected"),
        [
            ({"operation": "delete_lines"}, ["delete_lines:"], True),   # 空模式：操作值等值
            ({"op": "DELETE_LINES"}, ["delete_lines:"], True),          # 大小写不敏感
            ({"action": "delete_lines"}, ["delete_lines:"], True),
            ({"operation": "other"}, ["delete_lines:"], False),
            ({"path": "delete_lines"}, ["delete_lines:"], False),       # 不看路径参数
        ],
    )
    def test_empty_pattern_matches_operation_value(
        self, tool_args: Any, ops: list[str], expected: bool,
    ) -> None:
        """``op:``（空模式）形态按操作参数值等值匹配。"""
        assert self._plugin()._args_hit_dangerous_ops(tool_args, ops) is expected

    @pytest.mark.parametrize(
        ("tool_args", "ops", "expected"),
        [
            ({"command": "sudo rm -rf /"}, ["rm -rf"], True),
            ({"cmd": "RM -RF /"}, ["rm -rf"], True),                    # 大小写不敏感
            ({"command": "ls -la"}, ["rm -rf"], False),
            ({"path": "rm -rf"}, ["rm -rf"], False),                    # 裸模式只看 command/cmd
            ({"command": "echo hi"}, [""], False),                      # 空声明跳过
        ],
    )
    def test_bare_command_substring_match(
        self, tool_args: Any, ops: list[str], expected: bool,
    ) -> None:
        """裸命令形态按 command/cmd 子串匹配（空声明跳过，不误伤全部调用）。"""
        assert self._plugin()._args_hit_dangerous_ops(tool_args, ops) is expected

    @pytest.mark.parametrize("value", [1, 2.5, "x"])
    def test_scalar_args_coerced_to_str(self, value: Any) -> None:
        """标量参数（int/float/str）转字符串参与匹配；容器类型参数剔除。"""
        p = self._plugin()

        assert p._args_hit_dangerous_ops({"command": value}, [str(value)]) is True
        assert p._args_hit_dangerous_ops({"command": value, "x": {"a": 1}}, ["nope"]) is False


class TestSensitivePathSharedModule:
    """``sensitive_paths.is_sensitive_path``（插件平铺模块）契约。"""

    def test_empty_path_not_sensitive(self) -> None:
        """空路径 → (False, "")（无路径即无命中，调用点零分支）。"""
        import sensitive_paths

        assert sensitive_paths.is_sensitive_path("") == (False, "")

    @pytest.mark.parametrize("path", [None, 0, [], {}])
    def test_falsy_non_str_path_not_sensitive(self, path: Any) -> None:
        """假值非字符串 → 同空路径语义（不崩、不误报）。"""
        import sensitive_paths

        assert sensitive_paths.is_sensitive_path(path) == (False, "")

    def test_resolution_failure_falls_back_to_raw_path(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """resolve 抛 OSError/ValueError（NUL、超长路径等）→ 回落原始串参与匹配
        （fail-closed：宁可对原文比对，也不静默放行）。"""
        from pathlib import Path

        import sensitive_paths

        def _boom(self: Any) -> Any:
            raise ValueError("stat: embedded null character in path")

        monkeypatch.setattr(Path, "resolve", _boom)

        hit, prefix = sensitive_paths.is_sensitive_path("C:/Windows/System32")

        assert hit is True, "回落原文后仍须命中黑名单（Windows 敏感目录）"
        assert prefix

    @pytest.mark.parametrize(
        ("path", "expect_hit"),
        [
            ("C:/Users/me/proj/a.py", False),
            ("C:/Windows/System32/drivers", True),
            ("relative/path/file.txt", False),
        ],
    )
    def test_windows_blacklist_prefix_semantics(self, path: str, expect_hit: bool) -> None:
        """Windows 黑名单按「相等或前缀 + /」匹配：敏感目录子树命中，良性路径放行。"""
        import os

        import sensitive_paths

        if os.name != "nt":
            pytest.skip("Windows 黑名单仅 Windows 生效")

        hit, prefix = sensitive_paths.is_sensitive_path(path)

        assert hit is expect_hit
        if expect_hit:
            assert prefix and prefix.startswith("c:/")
