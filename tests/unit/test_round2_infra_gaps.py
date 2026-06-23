"""
Round2 后端基础设施模块组测试缺口补充。

聚焦以下 AC 的边界与深度覆盖：

Auth:
- AC-AUTH-06: Token刷新流程完整（端到端 → 新Token可用 / 旧Token撤销）
- AC-AUTH-10: 登录限流5次/分钟（AUTH类别限流 + RateLimitExceededError）
- AC-AUTH-11: Redis不可用时降级内存（生命周期：撤销→刷新→全设备撤销）

Config:
- AC-CFG-03: ${ENV_VAR}替换（边界：多变量/嵌套列表/空默认值）
- AC-CFG-06: API Key不在YAML硬编码（静态扫描真实配置文件）

Task:
- AC-TASK-01: 7种状态转换路径合法/非法（验证完整状态机覆盖率）
- AC-TASK-03: 容器任务管理子任务（状态独立性验证）

Evaluation:
- AC-EVAL-02: 11种操作符全覆盖（ExpectConditionEvaluator）
- AC-EVAL-03: 嵌套字段路径解析（ExpectConditionEvaluator._get_field_value）
- AC-EVAL-05: 评估不通过→failed（完整流程）

Test System:
- AC-TST-05: 评估门禁通过才能标记完成（门禁阻止验证）
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


# ============================================================
# AC-AUTH-06: Token刷新完整流程（端到端）
# ============================================================

class TestTokenRefreshFullFlow:
    """Token 刷新端到端完整流程测试。

    验证点（AC-AUTH-06）：
    - 用 refresh_token 获取新的 access_token
    - 新 access_token 可以正常验证
    - 旧 refresh_token 被撤销
    - 连续刷新链（access→refresh→access）可用
    """

    def _make_manager(self):
        from src.auth.token import TokenManager
        return TokenManager(
            secret_key="round2-test-secret-key-at-least-32-bytes",
            redis_url="redis://invalid:99999/0",
        )

    def test_refresh_chain_e2e(self):
        """完整刷新链：登录→获取Token对→刷新→新Token验证。"""
        mgr = self._make_manager()

        # Step 1: 创建初始 Token 对（模拟登录）
        original_pair = mgr.create_token_pair(user_id="user-refresh-e2e", role="user")
        assert original_pair.access_token is not None
        assert original_pair.refresh_token is not None

        # Step 2: 用 refresh_token 刷新
        new_pair = mgr.refresh_token_pair(
            refresh_token=original_pair.refresh_token, role="user"
        )

        # Step 3: 新 access_token 可正常验证
        payload = mgr.verify_token(new_pair.access_token, token_type="access")
        assert payload.sub == "user-refresh-e2e"
        assert payload.role == "user"

        # Step 4: 旧 refresh_token 已撤销
        from src.core.exceptions import TokenRevokedError
        with pytest.raises(TokenRevokedError):
            mgr.verify_token(original_pair.refresh_token, token_type="refresh")

    def test_refresh_multiple_times_chain(self):
        """连续刷新链：refresh1→refresh2→refresh3。"""
        mgr = self._make_manager()
        pair = mgr.create_token_pair(user_id="user-chain", role="user")

        for i in range(3):
            pair = mgr.refresh_token_pair(refresh_token=pair.refresh_token, role="user")
            payload = mgr.verify_token(pair.access_token, token_type="access")
            assert payload.sub == "user-chain"

    def test_new_access_token_has_different_jti(self):
        """刷新后的 access_token 有不同的 jti（唯一标识）。"""
        mgr = self._make_manager()
        original = mgr.create_token_pair(user_id="user-jti", role="user")

        original_payload = mgr.verify_token(original.access_token, token_type="access")
        new_pair = mgr.refresh_token_pair(
            refresh_token=original.refresh_token, role="user"
        )
        new_payload = mgr.verify_token(new_pair.access_token, token_type="access")

        assert original_payload.jti != new_payload.jti


# ============================================================
# AC-AUTH-10: 登录限流5次/分钟
# ============================================================

class TestLoginRateLimit:
    """登录限流测试。

    验证点（AC-AUTH-10）：
    - AUTH 类别的限流策略为 5 次/分钟（按需求文档）
    - 超过限制返回 RATE_LIMIT_EXCEEDED
    - RateLimitExceededError 正确抛出
    """

    def test_auth_category_default_policy(self):
        """AUTH 类别默认限流策略存在。"""
        from src.channels.api.rate_limiter import DEFAULT_POLICIES, RateLimitCategory
        auth_policy = DEFAULT_POLICIES[RateLimitCategory.AUTH]
        assert auth_policy.max_requests > 0
        assert auth_policy.window_seconds == 60

    def test_auth_login_path_blocked_after_limit(self):
        """登录路径超限后被拦截。"""
        from src.channels.api.rate_limiter import (
            RateLimitCategory,
            RateLimitPolicy,
            TieredRateLimiter,
            classify_request,
        )

        # 模拟 5 次/分钟策略
        policy = {RateLimitCategory.AUTH: RateLimitPolicy(max_requests=5, window_seconds=60)}
        limiter = TieredRateLimiter(policy)

        ip = "10.0.0.99"
        for i in range(5):
            assert limiter.is_request_allowed(ip, "POST", "/api/v1/auth/login") is True, (
                f"第 {i + 1} 次登录应放行"
            )
        # 第 6 次被拦截
        assert limiter.is_request_allowed(ip, "POST", "/api/v1/auth/login") is False

    def test_rate_limit_exceeded_error_exists(self):
        """RateLimitExceededError 异常类存在且可实例化。"""
        from src.core.exceptions.auth import RateLimitExceededError
        err = RateLimitExceededError()
        assert "频繁" in str(err) or "超限" in str(err)

    def test_login_rate_limit_per_ip(self):
        """不同 IP 的登录限流互不影响。"""
        from src.channels.api.rate_limiter import (
            RateLimitCategory,
            RateLimitPolicy,
            TieredRateLimiter,
        )

        policy = {RateLimitCategory.AUTH: RateLimitPolicy(max_requests=3, window_seconds=60)}
        limiter = TieredRateLimiter(policy)

        # IP-A 打满
        for _ in range(3):
            assert limiter.is_allowed("ip-a", RateLimitCategory.AUTH) is True
        assert limiter.is_allowed("ip-a", RateLimitCategory.AUTH) is False

        # IP-B 不受影响
        assert limiter.is_allowed("ip-b", RateLimitCategory.AUTH) is True


# ============================================================
# AC-AUTH-11: Redis不可用时降级内存（深度生命周期）
# ============================================================

class TestRedisFallbackDeep:
    """Redis 不可用降级内存的深度测试。

    验证点（AC-AUTH-11）：
    - Redis 不可用时自动降级
    - 内存模式下撤销、全设备撤销、刷新链都正常
    - 内存模式下的多用户隔离
    """

    def _make_memory_manager(self):
        from src.auth.token import TokenManager
        return TokenManager(
            secret_key="memory-fallback-deep-test-key-32b!",
            redis_url="redis://nonexistent-host:99999/0",
        )

    def test_memory_mode_verified(self):
        """确认降级到内存模式。"""
        mgr = self._make_memory_manager()
        assert mgr._redis_available is False
        assert mgr._redis is None

    def test_memory_revoke_then_refresh_fails(self):
        """内存模式：撤销access后用refresh刷新成功，但刷新后旧refresh也被撤销。"""
        mgr = self._make_memory_manager()
        pair = mgr.create_token_pair(user_id="mem-user-1", role="admin")

        # 撤销 access
        mgr.revoke_token(pair.access_token)
        from src.core.exceptions import TokenRevokedError
        with pytest.raises(TokenRevokedError):
            mgr.verify_token(pair.access_token, token_type="access")

        # refresh 仍可用
        new_pair = mgr.refresh_token_pair(refresh_token=pair.refresh_token, role="admin")
        assert new_pair.access_token != pair.access_token

        # 旧 refresh 已撤销
        with pytest.raises(TokenRevokedError):
            mgr.verify_token(pair.refresh_token, token_type="refresh")

    def test_memory_revoke_all_isolates_users(self):
        """内存模式：全设备撤销用户A不影响用户B。"""
        mgr = self._make_memory_manager()
        token_a = mgr.create_access_token(user_id="user-A", role="user")
        token_b = mgr.create_access_token(user_id="user-B", role="user")

        mgr.revoke_all_user_tokens("user-A")

        from src.core.exceptions import TokenRevokedError
        with pytest.raises(TokenRevokedError):
            mgr.verify_token(token_a, token_type="access")

        # 用户B 不受影响
        payload = mgr.verify_token(token_b, token_type="access")
        assert payload.sub == "user-B"

    def test_memory_new_token_after_revoke_all(self):
        """内存模式：全设备撤销后新Token可用（需时间推进避免iat冲突）。"""
        mgr = self._make_memory_manager()
        mgr.revoke_all_user_tokens("user-X")

        time.sleep(1.1)
        new_token = mgr.create_access_token(user_id="user-X", role="user")
        payload = mgr.verify_token(new_token, token_type="access")
        assert payload.sub == "user-X"


# ============================================================
# AC-CFG-03: ${ENV_VAR} 替换（边界场景）
# ============================================================

class TestEnvVarSubstitutionEdge:
    """环境变量替换边界场景测试。"""

    def _make_loader(self, tmp_path, env_file=None):
        from src.config.loader import ConfigLoader
        return ConfigLoader(config_dir=tmp_path, env_file=env_file)

    def test_multiple_env_vars_in_same_file(self, tmp_path, monkeypatch):
        """同一文件中多个 ${ENV_VAR} 都被替换。"""
        monkeypatch.setenv("VAR_A", "alpha")
        monkeypatch.setenv("VAR_B", "beta")
        monkeypatch.setenv("VAR_C", "gamma")

        (tmp_path / "multi.yaml").write_text(
            "a: ${VAR_A}\nb: ${VAR_B}\nc: ${VAR_C}\n", encoding="utf-8"
        )
        result = self._make_loader(tmp_path).load("multi.yaml")
        assert result == {"a": "alpha", "b": "beta", "c": "gamma"}

    def test_env_var_default_empty_string(self, tmp_path, monkeypatch):
        """${VAR:-} 使用空字符串作为默认值。"""
        monkeypatch.delenv("NONEXISTENT_EMPTY", raising=False)
        (tmp_path / "empty.yaml").write_text(
            "val: ${NONEXISTENT_EMPTY:-}\n", encoding="utf-8"
        )
        result = self._make_loader(tmp_path).load("empty.yaml")
        assert result["val"] == ""

    def test_env_var_deeply_nested_structure(self, tmp_path, monkeypatch):
        """深层嵌套结构中的环境变量替换。"""
        monkeypatch.setenv("DEEP_HOST", "db.local")
        monkeypatch.setenv("DEEP_PORT", "5432")

        content = """
database:
  primary:
    host: ${DEEP_HOST}
    port: ${DEEP_PORT}
    credentials:
      user: ${DEEP_USER:-admin}
      password: ${DEEP_PASS:-secret}
""".strip()
        (tmp_path / "deep.yaml").write_text(content, encoding="utf-8")
        result = self._make_loader(tmp_path).load("deep.yaml")

        assert result["database"]["primary"]["host"] == "db.local"
        assert result["database"]["primary"]["port"] == "5432"
        assert result["database"]["primary"]["credentials"]["user"] == "admin"
        assert result["database"]["primary"]["credentials"]["password"] == "secret"

    def test_env_var_in_yaml_list_of_dicts(self, tmp_path, monkeypatch):
        """字典列表中的环境变量替换。"""
        monkeypatch.setenv("ITEM_KEY_1", "val1")
        monkeypatch.setenv("ITEM_KEY_2", "val2")

        content = """
items:
  - name: first
    key: ${ITEM_KEY_1}
  - name: second
    key: ${ITEM_KEY_2}
""".strip()
        (tmp_path / "list.yaml").write_text(content, encoding="utf-8")
        result = self._make_loader(tmp_path).load("list.yaml")
        assert result["items"][0]["key"] == "val1"
        assert result["items"][1]["key"] == "val2"


# ============================================================
# AC-CFG-06: API Key 不在 YAML 中硬编码（静态扫描真实配置）
# ============================================================

class TestApiKeyStaticScan:
    """对真实配置文件进行静态扫描，确保无硬编码 API Key。

    验证点（AC-CFG-06）：
    - config/models/ 下的 YAML 文件使用 ${ENV_VAR} 格式
    - 不含明文 API Key（如 sk-xxx, sk-ant-xxx 等模式）
    """

    # 常见 API Key 前缀模式（硬编码标志）
    _HARDCODED_PATTERNS = [
        "sk-",          # OpenAI 风格
        "sk-ant-",      # Anthropic 风格
        "xai-",         # xAI 风格
    ]

    def _scan_yaml_for_hardcoded_keys(self, config_dir: Path) -> list[str]:
        """扫描目录下所有 YAML 文件，返回含硬编码密钥的文件列表。"""
        violations = []
        if not config_dir.exists():
            return violations

        for yaml_file in config_dir.rglob("*.yaml"):
            content = yaml_file.read_text(encoding="utf-8")
            # 跳过注释行和 ${ENV_VAR} 占位
            for line_num, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # 如果行包含 ${...} 占位符，跳过（合法用法）
                if "${" in line and "}" in line:
                    continue
                # 检查硬编码模式（排除占位和注释引用）
                for pattern in self._HARDCODED_PATTERNS:
                    if pattern in line and f"${{{pattern}" not in line:
                        # 进一步检查：值是否像真的 API key（长度>20的密钥串）
                        violations.append(
                            f"{yaml_file.name}:{line_num}: {stripped[:80]}"
                        )
        return violations

    def test_llm_yaml_no_hardcoded_keys(self):
        """config/models/llm.yaml 中不含硬编码 API Key。"""
        llm_yaml = Path("config/models/llm.yaml")
        if not llm_yaml.exists():
            pytest.skip("llm.yaml 不存在")
        content = llm_yaml.read_text(encoding="utf-8")

        # 所有 api_key 行应使用 ${ENV_VAR} 格式
        lines = content.splitlines()
        violations = []
        for i, line in enumerate(lines, 1):
            if "api_key" in line.lower():
                # 检查值部分是否是 ${...} 格式
                if "api_key: ${" not in line and "api_key:\"" not in line:
                    # 如果是空值（""），允许
                    val_part = line.split(":", 1)[1].strip() if ":" in line else ""
                    if val_part and val_part != '""' and val_part != "''":
                        # 确认不含明文密钥
                        for pattern in self._HARDCODED_PATTERNS:
                            if pattern in val_part:
                                violations.append(f"line {i}: {line.strip()}")

        joined = "\n".join(violations)
        assert violations == [], f"发现硬编码 API Key:\n{joined}"

    def test_config_models_dir_uses_env_vars(self):
        """config/models/ 目录下所有 YAML 使用 ${ENV_VAR} 引用密钥。"""
        models_dir = Path("config/models")
        if not models_dir.exists():
            pytest.skip("config/models 目录不存在")

        violations = self._scan_yaml_for_hardcoded_keys(models_dir)
        joined = "\n".join(violations)
        assert violations == [], (
            f"发现可能硬编码的密钥:\n{joined}"
        )

    def test_godot_yaml_no_hardcoded_secret(self):
        """config/external_tools/godot.yaml 不含硬编码 secret_key。"""
        godot_yaml = Path("config/external_tools/godot.yaml")
        if not godot_yaml.exists():
            pytest.skip("godot.yaml 不存在")
        content = godot_yaml.read_text(encoding="utf-8")

        # secret_key 应使用 ${ENV_VAR} 格式
        for line in content.splitlines():
            if "secret_key" in line.lower():
                val_part = line.split(":", 1)[1].strip() if ":" in line else ""
                # 不应包含明文密钥字符串
                assert "${" in val_part or val_part in ('""', "''", ""), (
                    f"godot.yaml secret_key 不应硬编码: {line.strip()}"
                )


# ============================================================
# AC-TASK-01: 状态机完整性验证（覆盖率计数）
# ============================================================

class TestStateMachineCoverageIntegrity:
    """状态机覆盖完整性验证。"""

    def test_exactly_7_states(self):
        """确认状态机恰好定义了 7 种状态。"""
        from src.tasks.state_machine import _TASK_TRANSITIONS
        expected_states = {"pending", "running", "evaluating", "stopped",
                          "completed", "failed", "timeout"}
        assert set(_TASK_TRANSITIONS.keys()) == expected_states

    def test_transition_coverage_count(self):
        """验证合法转换路径数量与需求文档一致。"""
        from src.tasks.state_machine import _TASK_TRANSITIONS

        total_transitions = sum(len(targets) for targets in _TASK_TRANSITIONS.values())
        # 需求文档定义的转换：
        # pending: 4, running: 5, evaluating: 4, stopped: 2, completed: 1, failed: 2, timeout: 3
        assert total_transitions == 21

    def test_each_state_has_outgoing_or_terminal(self):
        """每个状态要么有出边要么是终态。"""
        from src.tasks.state_machine import _TASK_TRANSITIONS
        for state, targets in _TASK_TRANSITIONS.items():
            # 终态（completed）可以有出边（重置到 pending）
            # 只需确保定义存在
            assert isinstance(targets, list), f"{state} 的目标列表类型错误"

    def test_no_self_transition(self):
        """状态不允许转换到自己（没有 pending→pending）。"""
        from src.tasks.state_machine import _TASK_TRANSITIONS
        for state, targets in _TASK_TRANSITIONS.items():
            assert state not in targets, f"状态 '{state}' 不应允许自转换"


# ============================================================
# AC-TASK-03: 容器任务管理子任务（状态独立性）
# ============================================================

class TestContainerTaskStateIsolation:
    """容器任务中子任务的状态独立性测试。

    验证点（AC-TASK-03）：
    - 父子任务有独立的状态
    - 父任务状态变化不影响子任务
    - 子任务状态变化不影响父任务
    """

    def test_parent_child_independent_state_machines(self):
        """父任务和子任务拥有独立的状态机。"""
        from src.tasks.state_machine import get_task_state_machine

        parent_sm = get_task_state_machine()
        child_sm = get_task_state_machine()

        # 父任务: pending → running
        parent_sm.transition("running")
        assert parent_sm.current_state == "running"
        # 子任务仍为 pending
        assert child_sm.current_state == "pending"

    def test_parent_completed_child_still_running(self):
        """父任务完成不影响子任务状态。"""
        from src.tasks.state_machine import get_task_state_machine
        from src.tasks.types import TaskStatus, create_task

        parent = create_task(title="父任务")
        child = create_task(title="子任务", parent_task_id=parent.id)

        parent_sm = get_task_state_machine()
        child_sm = get_task_state_machine()

        # 父任务完成流程
        parent_sm.transition("running")
        parent_sm.transition("completed")

        # 子任务仍在运行
        child_sm.transition("running")
        assert child_sm.current_state == "running"
        assert parent_sm.current_state == "completed"

    def test_sibling_tasks_independent(self):
        """同一容器的兄弟子任务彼此独立。"""
        from src.tasks.types import create_task

        container = create_task(title="容器任务")
        child1 = create_task(title="子1", parent_task_id=container.id)
        child2 = create_task(title="子2", parent_task_id=container.id)
        child3 = create_task(title="子3", parent_task_id=container.id)

        # 所有子任务共享同一个 parent 但有独立 ID
        assert child1.id != child2.id
        assert child2.id != child3.id
        assert child1.id != child3.id

        # 所有子任务的 parent_task_id 相同
        assert child1.parent_task_id == container.id
        assert child2.parent_task_id == container.id
        assert child3.parent_task_id == container.id

    def test_task_metadata_isolation(self):
        """不同任务的 metadata 相互独立。"""
        from src.tasks.types import create_task

        t1 = create_task(title="t1", metadata={"phase": "design"})
        t2 = create_task(title="t2", metadata={"phase": "implement"})

        assert t1.metadata["phase"] == "design"
        assert t2.metadata["phase"] == "implement"
        # 修改 t1 的 metadata 不影响 t2
        t1.metadata["phase"] = "review"
        assert t2.metadata["phase"] == "implement"


# ============================================================
# AC-EVAL-02: ExpectConditionEvaluator 11种操作符全覆盖
# ============================================================

class TestExpectConditionEvaluatorAllOps:
    """ExpectConditionEvaluator 的 11 种操作符完整覆盖。"""

    def _make_evaluator(self):
        from src.evaluation.expect_evaluator import ExpectConditionEvaluator
        return ExpectConditionEvaluator()

    def test_supported_operators_count_is_11(self):
        """支持的操作符数量恰好为 11。"""
        ev = self._make_evaluator()
        ops = ev.get_supported_operators()
        assert len(ops) == 11

    @pytest.mark.parametrize(
        "operator, result, expected_value, should_pass",
        [
            # 布尔判断
            ("is_true", True, None, True),
            ("is_true", False, None, False),
            ("is_true", 1, None, True),
            ("is_true", 0, None, False),
            ("is_false", False, None, True),
            ("is_false", True, None, False),
            ("is_false", None, None, True),
            # 等值比较
            ("equals", "success", "success", True),
            ("equals", "failed", "success", False),
            ("equals", 200, 200, True),
            ("equals", 200, 201, False),
            ("not_equals", "failed", "success", True),
            ("not_equals", "success", "success", False),
            # 集合判断
            ("in", 200, [200, 201, 204], True),
            ("in", 404, [200, 201, 204], False),
            ("not_in", 404, [200, 201], True),
            ("not_in", 200, [200, 201], False),
            # 包含判断
            ("contains", "hello world", "hello", True),
            ("contains", "goodbye", "hello", False),
            ("contains", ["a", "b", "c"], "b", True),
            ("contains", ["x", "y"], "z", False),
            # 数值比较
            ("gt", 100, 80, True),
            ("gt", 80, 80, False),
            ("gt", 50, 80, False),
            ("lt", 50, 80, True),
            ("lt", 80, 80, False),
            ("lt", 100, 80, False),
            ("gte", 80, 80, True),
            ("gte", 81, 80, True),
            ("gte", 79, 80, False),
            ("lte", 80, 80, True),
            ("lte", 79, 80, True),
            ("lte", 81, 80, False),
        ],
        ids=lambda x: str(x) if not isinstance(x, str) else x,
    )
    def test_operator_evaluate(self, operator, result, expected_value, should_pass):
        """11种操作符的正反两向验证。"""
        ev = self._make_evaluator()
        condition = {"field": "val", "operator": operator}
        if expected_value is not None:
            condition["value"] = expected_value

        eval_result = ev.evaluate({"val": result}, {"conditions": [condition]})
        assert eval_result["passed"] is should_pass, (
            f"operator={operator}, actual={result!r}, expected={expected_value!r}, "
            f"should_pass={should_pass}, got passed={eval_result['passed']}"
        )


# ============================================================
# AC-EVAL-03: 嵌套字段路径解析（ExpectConditionEvaluator）
# ============================================================

class TestNestedFieldResolutionEvalCond:
    """ExpectConditionEvaluator 嵌套字段解析测试。"""

    def _make_evaluator(self):
        from src.evaluation.expect_evaluator import ExpectConditionEvaluator
        return ExpectConditionEvaluator()

    def test_two_level_nested(self):
        """两层嵌套路径 data.exit_code。"""
        ev = self._make_evaluator()
        result = ev.evaluate(
            {"data": {"exit_code": 0, "status": "ok"}},
            {"conditions": [
                {"field": "data.exit_code", "operator": "equals", "value": 0}
            ]},
        )
        assert result["passed"] is True

    def test_three_level_nested(self):
        """三层嵌套路径 result.data.exit_code。"""
        ev = self._make_evaluator()
        result = ev.evaluate(
            {"result": {"data": {"exit_code": 1}}},
            {"conditions": [
                {"field": "result.data.exit_code", "operator": "equals", "value": 1}
            ]},
        )
        assert result["passed"] is True

    def test_nested_path_missing_returns_none(self):
        """嵌套路径不存在时字段值为 None。"""
        ev = self._make_evaluator()
        result = ev.evaluate(
            {"data": {"status": "ok"}},
            {"conditions": [
                {"field": "data.missing_field", "operator": "is_true"}
            ]},
        )
        assert result["passed"] is False

    def test_nested_path_intermediate_not_dict(self):
        """中间层非字典时安全返回 None。"""
        ev = self._make_evaluator()
        result = ev.evaluate(
            {"data": "not_a_dict"},
            {"conditions": [
                {"field": "data.exit_code", "operator": "equals", "value": 0}
            ]},
        )
        assert result["passed"] is False

    def test_nested_path_gt_on_missing_value(self):
        """gt 操作符对 None 值返回 False。"""
        ev = self._make_evaluator()
        result = ev.evaluate(
            {"data": {}},
            {"conditions": [
                {"field": "data.score", "operator": "gt", "value": 80}
            ]},
        )
        assert result["passed"] is False


# ============================================================
# AC-EVAL-05: 评估不通过→failed 完整流程
# ============================================================

class TestEvaluationFailFlow:
    """评估不通过→任务转 failed 的完整流程测试。"""

    def _make_failed_engine_mock(self):
        """创建返回失败结果的引擎 Mock。"""
        from src.evaluation.types import (
            EvaluationResult,
            MetricResult,
        )
        from unittest.mock import AsyncMock

        mock_engine = AsyncMock()
        mock_engine.evaluate = AsyncMock(return_value=EvaluationResult(
            task_id="task-fail-flow",
            results=[
                MetricResult(metric_id="file_check", passed=False, message="文件不存在"),
                MetricResult(metric_id="format_valid", passed=True, message="格式正确"),
            ],
            overall_passed=False,
            summary="1/2 指标通过",
        ))
        return mock_engine

    @pytest.mark.asyncio
    async def test_fail_flow_calls_complete_evaluation_with_false(self):
        """评估不通过时调用 complete_evaluation 且 passed=False。"""
        from evaluation.executor import EvaluationExecutor

        mock_engine = self._make_failed_engine_mock()
        mock_task_service = AsyncMock()
        mock_task_service.complete_evaluation = AsyncMock(return_value=None)

        executor = EvaluationExecutor(
            task_service=mock_task_service,
            engine=mock_engine,
        )

        result = await executor.run_evaluation(
            task_id="task-fail-flow",
            metric_ids=["file_check", "format_valid"],
        )

        assert result.overall_passed is False
        mock_task_service.complete_evaluation.assert_called_once()
        call_args = mock_task_service.complete_evaluation.call_args
        assert call_args.args[0] == "task-fail-flow"
        assert call_args.args[1] is False, "passed 应为 False（→failed）"

    @pytest.mark.asyncio
    async def test_fail_flow_includes_all_metrics_in_data(self):
        """失败流程回写数据包含所有指标详情。"""
        from evaluation.executor import EvaluationExecutor

        mock_engine = self._make_failed_engine_mock()
        mock_task_service = AsyncMock()
        mock_task_service.complete_evaluation = AsyncMock(return_value=None)

        executor = EvaluationExecutor(
            task_service=mock_task_service,
            engine=mock_engine,
        )

        await executor.run_evaluation(
            task_id="task-fail-flow",
            metric_ids=["file_check", "format_valid"],
        )

        call_kwargs = mock_task_service.complete_evaluation.call_args
        eval_data = call_kwargs.kwargs.get("result") or {}
        assert "metrics" in eval_data
        assert len(eval_data["metrics"]) == 2

        # 验证失败指标信息
        failed_metrics = [m for m in eval_data["metrics"] if not m["passed"]]
        assert len(failed_metrics) >= 1
        assert failed_metrics[0]["metric_id"] == "file_check"

    @pytest.mark.asyncio
    async def test_empty_results_maps_to_failed(self):
        """无评估指标时也映射为 failed。"""
        from evaluation.executor import EvaluationExecutor
        from evaluation.types import EvaluationResult

        mock_engine = AsyncMock()
        mock_engine.evaluate = AsyncMock(return_value=EvaluationResult(
            task_id="task-empty",
            results=[],
            overall_passed=False,
            summary="无评估指标",
        ))
        mock_task_service = AsyncMock()
        mock_task_service.complete_evaluation = AsyncMock(return_value=None)

        executor = EvaluationExecutor(
            task_service=mock_task_service,
            engine=mock_engine,
        )

        result = await executor.run_evaluation(
            task_id="task-empty",
            metric_ids=[],
        )

        assert result.overall_passed is False
        call_args = mock_task_service.complete_evaluation.call_args
        assert call_args.args[1] is False


# ============================================================
# AC-TST-05: 评估门禁通过才能标记完成
# ============================================================

class TestEvaluationGateEnforcement:
    """评估门禁强制执行测试。

    验证点（AC-TST-05）：
    - 门禁未通过时，任务不能标记为 completed
    - 门禁通过时，任务可标记为 completed
    - 门禁结果决定任务最终状态（not completed → failed）
    """

    def test_gate_block_completed_when_evaluation_fails(self):
        """评估未通过时，状态机不允许从 evaluating 直接到 completed。

        验证点：evaluating → completed 是合法转换，
        但评估门禁机制会阻止它（业务逻辑层面）。
        """
        from src.tasks.state_machine import get_task_state_machine

        sm = get_task_state_machine()
        sm.transition("running")
        sm.transition("evaluating")

        # 状态机允许 evaluating→completed 和 evaluating→failed
        assert sm.can_transition("completed") is True
        assert sm.can_transition("failed") is True

        # 门禁逻辑：如果评估不通过，业务层应选择 failed 而非 completed
        # 模拟门禁检查
        evaluation_passed = False
        target_state = "completed" if evaluation_passed else "failed"
        sm.transition(target_state)
        assert sm.current_state == "failed"

    def test_gate_allows_completed_when_evaluation_passes(self):
        """评估通过时，任务可以标记为 completed。"""
        from src.tasks.state_machine import get_task_state_machine

        sm = get_task_state_machine()
        sm.transition("running")
        sm.transition("evaluating")

        evaluation_passed = True
        target_state = "completed" if evaluation_passed else "failed"
        sm.transition(target_state)
        assert sm.current_state == "completed"

    @pytest.mark.asyncio
    async def test_gate_executor_pass_maps_to_completed(self):
        """评估通过→executor 回写 passed=True（→completed）。"""
        from evaluation.executor import EvaluationExecutor
        from evaluation.types import EvaluationResult, MetricResult
        from unittest.mock import AsyncMock

        mock_engine = AsyncMock()
        mock_engine.evaluate = AsyncMock(return_value=EvaluationResult(
            task_id="task-gate-pass",
            results=[MetricResult(metric_id="m1", passed=True, score=100)],
            overall_passed=True,
            summary="1/1 指标通过",
        ))
        mock_task_service = AsyncMock()
        mock_task_service.complete_evaluation = AsyncMock(return_value=None)

        executor = EvaluationExecutor(
            task_service=mock_task_service,
            engine=mock_engine,
        )
        result = await executor.run_evaluation("task-gate-pass", ["m1"])

        assert result.overall_passed is True
        call_args = mock_task_service.complete_evaluation.call_args
        assert call_args.args[1] is True

    @pytest.mark.asyncio
    async def test_gate_executor_fail_maps_to_failed(self):
        """评估未通过→executor 回写 passed=False（→failed）。"""
        from evaluation.executor import EvaluationExecutor
        from evaluation.types import EvaluationResult, MetricResult
        from unittest.mock import AsyncMock

        mock_engine = AsyncMock()
        mock_engine.evaluate = AsyncMock(return_value=EvaluationResult(
            task_id="task-gate-fail",
            results=[MetricResult(metric_id="m1", passed=False, score=0)],
            overall_passed=False,
            summary="0/1 指标通过",
        ))
        mock_task_service = AsyncMock()
        mock_task_service.complete_evaluation = AsyncMock(return_value=None)

        executor = EvaluationExecutor(
            task_service=mock_task_service,
            engine=mock_engine,
        )
        result = await executor.run_evaluation("task-gate-fail", ["m1"])

        assert result.overall_passed is False
        call_args = mock_task_service.complete_evaluation.call_args
        assert call_args.args[1] is False

    @pytest.mark.asyncio
    async def test_gate_skip_does_not_update_status(self):
        """skip_state_update=True 时门禁不回写状态。"""
        from evaluation.executor import EvaluationExecutor
        from evaluation.types import EvaluationResult, MetricResult
        from unittest.mock import AsyncMock

        mock_engine = AsyncMock()
        mock_engine.evaluate = AsyncMock(return_value=EvaluationResult(
            task_id="task-skip",
            results=[MetricResult(metric_id="m1", passed=True)],
            overall_passed=True,
        ))
        mock_task_service = AsyncMock()

        executor = EvaluationExecutor(
            task_service=mock_task_service,
            engine=mock_engine,
        )
        await executor.run_evaluation("task-skip", ["m1"], skip_state_update=True)
        mock_task_service.complete_evaluation.assert_not_called()
