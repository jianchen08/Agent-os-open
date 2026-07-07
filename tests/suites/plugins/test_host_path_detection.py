"""宿主路径检测测试 — bash_execute 命令含 Windows 盘符路径时转 host + 审批。

测试覆盖：
- IsolationGuard._has_host_path 正则匹配（command / working_dir，正斜杠/反斜杠，边界）
- security_rules.yaml 的 host_path_access 规则正确加载且命中 needs_approval
- IsolationGuard 集成：含宿主路径时 provider 从 docker 改为 host
- IsolationGuard 集成：不含宿主路径时维持 docker 不变

背景：bash_execute 配置为容器隔离，但容器内只有挂载的 /workspace，
宿主路径（如 D:/myproject/）在容器内不存在，会返回 "No such file or directory"。
修复方案：检测到宿主路径时路由到 host 执行，由 security_check 弹审批把关。
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys

from tests.suites.plugins.conftest import load_module_from_file

_SRC_DIR = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "src"
))


def _load(module_name, rel_path):
    """加载指定模块。"""
    return load_module_from_file(
        module_name,
        os.path.join(_SRC_DIR, *rel_path),
    )


def _make_ctx(state=None, services=None):
    """创建 Mock PluginContext。

    默认标 L2（子任务）：本文件测的是"含宿主路径 → host + host_path_detected
    reason"和"无盘符 → docker"这类子任务场景；主 agent（L1）的 bash_execute
    一律走 host（reason=l1_main_agent_host），会盖掉 host_path_detected，
    故必须用 L2 才能测到 host_path 分支本身。
    """
    _state = {"agent_level": "L2"}
    if state:
        _state.update(state)
    return PluginContext(
        state=_state,
        config={},
        _services=services or {},
    )


def _make_isolation_guard(config=None):
    """创建 IsolationGuard 实例（patch 掉 decider 避免真实策略加载）。"""
    mod = _load("isolation_guard_host_path", ["plugins", "input", "isolation_guard", "plugin.py"])
    with patch("isolation.decider.IsolationDecider"):
        return mod.IsolationGuard(config=config)


def _mock_container_policy(plugin):
    """模拟 isolation=container 的策略（bash_execute 默认配置）。"""
    from isolation.types import IsolationLevel

    mock_policy = MagicMock()
    mock_policy.isolation = IsolationLevel.CONTAINER
    plugin._decider.resolve = MagicMock(return_value=mock_policy)
    return plugin


# ============================================================================
# 1. _has_host_path 正则匹配
# ============================================================================


class TestHasHostPath:
    """_has_host_path 的正则匹配边界测试。"""

    def _guard(self):
        return _make_isolation_guard(config={"docker_available": True})

    def test_command_with_forward_slash_drive(self):
        """command 含 D:/ 正斜杠盘符路径 → 命中。"""
        guard = self._guard()
        assert guard._has_host_path({"command": "ls D:/myproject/"}) is True

    def test_command_with_backslash_drive(self):
        """command 含 C:\\ 反斜杠盘符路径 → 命中。"""
        guard = self._guard()
        assert guard._has_host_path({"command": "dir C:\\Users\\test"}) is True

    def test_working_dir_with_drive(self):
        """working_dir 含盘符路径 → 命中。"""
        guard = self._guard()
        assert guard._has_host_path({"working_dir": "E:/data/sub"}) is True

    def test_command_unix_absolute_path_not_matched(self):
        """Unix 绝对路径（/etc、/workspace）不匹配 Windows 盘符模式。"""
        guard = self._guard()
        # 这些是容器内合法路径，不应误判为宿主路径
        assert guard._has_host_path({"command": "ls /etc/passwd"}) is False
        assert guard._has_host_path({"command": "cat /workspace/file.py"}) is False

    def test_command_no_path(self):
        """纯命令、无路径 → 不命中。"""
        guard = self._guard()
        assert guard._has_host_path({"command": "ls -la"}) is False
        assert guard._has_host_path({"command": "echo hello"}) is False

    def test_url_not_matched(self):
        """URL 中的冒号斜杠（https://、http://）不应误判为盘符。"""
        guard = self._guard()
        # 正则有前置边界（行首/空白/引号），URL 的 :// 前面是字母不匹配
        assert guard._has_host_path({"command": "echo https://example.com"}) is False

    def test_relative_path_not_matched(self):
        """相对路径（./src、../parent）不匹配盘符模式。"""
        guard = self._guard()
        assert guard._has_host_path({"command": "ls ./src"}) is False

    def test_empty_args(self):
        """空参数 / 缺字段 → 不命中。"""
        guard = self._guard()
        assert guard._has_host_path({}) is False
        assert guard._has_host_path({"command": ""}) is False

    def test_drive_at_start_of_command(self):
        """盘符在命令开头（如 cd D:/path）→ 命中。"""
        guard = self._guard()
        assert guard._has_host_path({"command": "D:/tools/bin/app.exe"}) is True

    def test_drive_after_quote(self):
        """盘符在引号后（如 open "D:/file"）→ 命中。"""
        guard = self._guard()
        assert guard._has_host_path({"command": 'cat "D:/data/x.txt"'}) is True

    def test_drive_after_equals(self):
        """盘符在等号后（如 VAR=D:/path）→ 命中。"""
        guard = self._guard()
        assert guard._has_host_path({"command": "export FOO=D:/data"}) is True

    def test_non_string_command_skipped(self):
        """command 非 string（如 None/int）应被跳过，不报错。"""
        guard = self._guard()
        assert guard._has_host_path({"command": None}) is False
        assert guard._has_host_path({"command": 123}) is False


# ============================================================================
# 2. security_rules.yaml host_path_access 规则加载
# ============================================================================


class TestHostPathAccessRule:
    """security_rules.yaml 中 host_path_access 规则的加载与匹配。"""

    def _make_security_check(self, config=None):
        """创建 SecurityCheckPlugin 实例。"""
        mod = _load("security_check_host_path", ["plugins", "input", "security_check", "plugin.py"])
        return mod.SecurityCheckPlugin(config=config)

    def test_rule_loaded_from_yaml(self):
        """host_path_access 规则应从 YAML 加载。"""
        plugin = self._make_security_check()
        rule_names = [r.get("name") for r in plugin._rules]
        assert "host_path_access" in rule_names

    def test_rule_action_is_needs_approval(self):
        """host_path_access 的 action 必须是 needs_approval。"""
        plugin = self._make_security_check()
        rule = next(r for r in plugin._rules if r.get("name") == "host_path_access")
        assert rule["action"] == "needs_approval"
        assert rule["tools"] == ["bash_execute"]

    def test_rule_matches_drive_command(self):
        """_match_rules 对含盘符的 bash 命令返回 needs_approval。"""
        plugin = self._make_security_check()
        action, rule_name = plugin._match_rules(
            "bash_execute", {"command": "ls D:/myproject/"}
        )
        assert action == "needs_approval"
        assert rule_name == "host_path_access"

    def test_rule_matches_backslash_command(self):
        """反斜杠盘符路径也应命中（正斜杠/反斜杠都覆盖）。"""
        plugin = self._make_security_check()
        action, rule_name = plugin._match_rules(
            "bash_execute", {"command": "dir C:\\Windows"}
        )
        assert action == "needs_approval"
        assert rule_name == "host_path_access"

    def test_rule_does_not_match_unix_path(self):
        """Unix 路径（/workspace）不应命中 host_path_access。"""
        plugin = self._make_security_check()
        action, rule_name = plugin._match_rules(
            "bash_execute", {"command": "ls /workspace"}
        )
        # /workspace 会被后面的 safe_commands 白名单放行，不命中 host_path_access
        assert rule_name != "host_path_access"

    def test_rule_priority_before_safe_commands(self):
        """host_path_access 规则在 safe_commands 之前（列表顺序靠前）。

        这确保 ls D:/path 先命中 needs_approval，而非被 safe_commands 的
        ^ls\\s 白名单放行。
        """
        plugin = self._make_security_check()
        names = [r.get("name") for r in plugin._rules]
        host_idx = names.index("host_path_access")
        safe_idx = names.index("safe_commands")
        assert host_idx < safe_idx, (
            "host_path_access 必须在 safe_commands 之前，否则 "
            "ls D:/path 会被白名单放行而不弹审批"
        )


# ============================================================================
# 3. IsolationGuard 集成：宿主路径 → 路由 host
# ============================================================================


class TestIsolationGuardHostPathRouting:
    """IsolationGuard 在 bash_execute 含宿主路径时路由到 host。"""

    @pytest.mark.asyncio
    async def test_host_path_routes_to_host_not_docker(self):
        """bash_execute + 含 D:/ 盘符 → provider=host，不进容器。"""
        guard = _make_isolation_guard(config={"docker_available": True})
        _mock_container_policy(guard)

        ctx = _make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [
                {"name": "bash_execute", "args": {"command": "ls D:/myproject/"}},
            ],
        })

        result = await guard.execute(ctx)
        contexts = result.state_updates.get("execution_contexts", [])

        assert len(contexts) == 1
        ctx_entry = contexts[0]
        assert ctx_entry["provider"] == "host"
        assert ctx_entry["reason"] == "host_path_detected"
        assert ctx_entry.get("blocked") is not True

    @pytest.mark.asyncio
    async def test_no_host_path_stays_docker(self):
        """bash_execute + 无盘符路径 → 仍进容器（provider=docker）。

        无 task 上下文时 isolation_level 缺失，归一化为默认隔离（isolated），
        走 metadata 决策分支（reason=task_metadata）而非工具 policy 分支。
        """
        guard = _make_isolation_guard(config={"docker_available": True})
        _mock_container_policy(guard)

        ctx = _make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [
                {"name": "bash_execute", "args": {"command": "ls -la /workspace"}},
            ],
        })

        result = await guard.execute(ctx)
        contexts = result.state_updates.get("execution_contexts", [])

        assert len(contexts) == 1
        assert contexts[0]["provider"] == "docker"

    @pytest.mark.asyncio
    async def test_working_dir_host_path_routes_to_host(self):
        """working_dir 含盘符路径也触发 host 路由。"""
        guard = _make_isolation_guard(config={"docker_available": True})
        _mock_container_policy(guard)

        ctx = _make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [
                {"name": "bash_execute", "args": {
                    "command": "ls",
                    "working_dir": "D:/project/sub",
                }},
            ],
        })

        result = await guard.execute(ctx)
        contexts = result.state_updates.get("execution_contexts", [])

        assert len(contexts) == 1
        assert contexts[0]["provider"] == "host"
        assert contexts[0]["reason"] == "host_path_detected"

    @pytest.mark.asyncio
    async def test_json_string_args_parsed(self):
        """args 为 JSON 字符串时也能解析并检测宿主路径。"""
        guard = _make_isolation_guard(config={"docker_available": True})
        _mock_container_policy(guard)

        ctx = _make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [
                {"name": "bash_execute", "arguments": '{"command": "ls D:/data/"}'},
            ],
        })

        result = await guard.execute(ctx)
        contexts = result.state_updates.get("execution_contexts", [])

        assert len(contexts) == 1
        assert contexts[0]["provider"] == "host"
        assert contexts[0]["reason"] == "host_path_detected"

    @pytest.mark.asyncio
    async def test_non_bash_tool_ignores_host_path(self):
        """非 bash_execute 工具即使参数含盘符也不触发 host 路由。

        宿主路径检测只针对 bash_execute（容器执行的命令工具）。
        """
        guard = _make_isolation_guard(config={"docker_available": True})
        # file_read 的 policy 默认是 host（不走容器），不受影响
        from isolation.types import IsolationLevel

        mock_policy = MagicMock()
        mock_policy.isolation = IsolationLevel.HOST
        guard._decider.resolve = MagicMock(return_value=mock_policy)

        ctx = _make_ctx({
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [
                {"name": "file_read", "args": {"path": "D:/some/file.txt"}},
            ],
        })

        result = await guard.execute(ctx)
        contexts = result.state_updates.get("execution_contexts", [])

        assert len(contexts) == 1
        # file_read 本来就是 host，reason 是 policy 而非 host_path_detected
        assert contexts[0]["provider"] == "host"
        assert contexts[0]["reason"] == "policy"
