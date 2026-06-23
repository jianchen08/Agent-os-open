"""ConfigCenter 热加载单元测试。

覆盖范围：
1. ConfigCenter.get / reload / watch / unwatch 基本功能
2. 防抖和哈希去重
3. 并发安全（读写锁）
4. 加载失败回滚
5. Agent Registry 热更新方法（reload_agent / register / unregister）
6. REST reload 端点（routes_config）
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# ConfigCenter 相关导入
# ---------------------------------------------------------------------------
from config.config_center import (
    DEBOUNCE_SECONDS,
    MAX_AUDIT_HISTORY,
    AuditEntry,
    ConfigCenter,
    _RWLock,
    _ReadGuard,
    _WriteGuard,
)


# ===========================================================================
# 辅助工具
# ===========================================================================


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    """写入 YAML 文件的辅助函数。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, sort_keys=False)


def _content_hash(content: str) -> str:
    """计算与 ConfigCenter 相同算法的 SHA-256 哈希。"""
    return hashlib.sha256(content.encode()).hexdigest()


# ===========================================================================
# 1. ConfigCenter 基本功能
# ===========================================================================


class TestConfigCenterGet:
    """ConfigCenter.get 测试。"""

    def test_get_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        """get() 在文件不存在时返回 None。"""
        center = ConfigCenter(config_root=tmp_path)
        assert center.get("nonexistent.yaml") is None

    def test_get_reads_from_disk_and_caches(self, tmp_path: Path) -> None:
        """get() 首次从磁盘读取并缓存，第二次命中缓存。"""
        data = {"key": "value"}
        fpath = tmp_path / "test.yaml"
        _write_yaml(fpath, data)

        center = ConfigCenter(config_root=tmp_path)
        result = center.get("test.yaml")

        assert result == data

        # 第二次 get 应该命中缓存（删除文件后仍能返回缓存）
        fpath.unlink()
        cached = center.get("test.yaml")
        assert cached == data

    def test_get_returns_none_for_non_dict_yaml(self, tmp_path: Path) -> None:
        """get() 对非字典类型的 YAML 返回 None。"""
        fpath = tmp_path / "list.yaml"
        fpath.write_text("- item1\n- item2\n", encoding="utf-8")

        center = ConfigCenter(config_root=tmp_path)
        assert center.get("list.yaml") is None

    def test_get_returns_none_for_invalid_yaml(self, tmp_path: Path) -> None:
        """get() 对无效 YAML 文件返回 None。"""
        fpath = tmp_path / "bad.yaml"
        fpath.write_text("{{invalid yaml::", encoding="utf-8")

        center = ConfigCenter(config_root=tmp_path)
        assert center.get("bad.yaml") is None


class TestConfigCenterReload:
    """ConfigCenter.reload 测试。"""

    def test_reload_reads_file_and_updates_cache(self, tmp_path: Path) -> None:
        """reload() 成功读取文件并更新缓存。"""
        data = {"model": "gpt-4"}
        fpath = tmp_path / "models" / "model.yaml"
        _write_yaml(fpath, data)

        center = ConfigCenter(config_root=tmp_path)
        result = center.reload("models/model.yaml")

        assert result["success"] is True
        assert result["error"] is None
        assert result["rolled_back"] is False
        assert result["config_type"] == "model"

        # 缓存已更新
        cached = center.get("models/model.yaml")
        assert cached == data

    def test_reload_raises_for_missing_file(self, tmp_path: Path) -> None:
        """reload() 对不存在的文件抛出 FileNotFoundError。"""
        center = ConfigCenter(config_root=tmp_path)
        with pytest.raises(FileNotFoundError):
            center.reload("nonexistent.yaml")

    def test_reload_returns_error_for_non_dict_yaml(self, tmp_path: Path) -> None:
        """reload() 对非字典类型的 YAML 返回错误结果。"""
        fpath = tmp_path / "list.yaml"
        fpath.write_text("- item1\n", encoding="utf-8")

        center = ConfigCenter(config_root=tmp_path)
        result = center.reload("list.yaml")

        assert result["success"] is False
        assert "不是字典类型" in result["error"]
        assert result["rolled_back"] is False

    def test_reload_detects_config_type(self, tmp_path: Path) -> None:
        """reload() 根据路径判断配置类型。"""
        test_cases = [
            ("agents/test.yaml", "agent"),
            ("pipelines/default.yaml", "pipeline"),
            ("tools/bash.yaml", "tool"),
            ("models/llm.yaml", "model"),
            ("templates/test.yaml", "template"),
            ("triggers/cron.yaml", "trigger"),
            ("evaluation_metrics/test.yaml", "evaluation_metric"),
            ("evaluation/test.yaml", "evaluation"),
            ("isolation/config.yaml", "isolation"),
            ("modules/test.yaml", "module"),
            ("other/file.yaml", "unknown"),
        ]
        center = ConfigCenter(config_root=tmp_path)

        for rel_path, expected_type in test_cases:
            fpath = tmp_path / rel_path
            _write_yaml(fpath, {"test": True})
            result = center.reload(rel_path)
            assert result["config_type"] == expected_type, (
                f"Expected {expected_type} for {rel_path}, got {result['config_type']}"
            )

    def test_reload_with_absolute_path(self, tmp_path: Path) -> None:
        """reload() 接受绝对路径。"""
        fpath = tmp_path / "abs_test.yaml"
        _write_yaml(fpath, {"abs": True})

        center = ConfigCenter(config_root=tmp_path)
        result = center.reload(str(fpath))

        assert result["success"] is True

    def test_reload_writes_audit_log(self, tmp_path: Path) -> None:
        """reload() 成功后写入审计日志。"""
        fpath = tmp_path / "audit.yaml"
        _write_yaml(fpath, {"audited": True})

        center = ConfigCenter(config_root=tmp_path)
        center.reload("audit.yaml")

        log = center.get_audit_log()
        assert len(log) >= 1
        entry = log[0]
        assert entry["success"] is True
        assert entry["event_type"] == "manual_reload"

    def test_reload_invalid_yaml_returns_failure(self, tmp_path: Path) -> None:
        """reload() 对 YAML 解析错误返回失败结果。"""
        fpath = tmp_path / "invalid.yaml"
        fpath.write_text(":\n  :\n    - {\n", encoding="utf-8")

        center = ConfigCenter(config_root=tmp_path)
        result = center.reload("invalid.yaml")

        assert result["success"] is False
        assert "YAML 解析失败" in result["error"]


class TestConfigCenterWatch:
    """ConfigCenter.watch / unwatch 测试。"""

    def test_watch_registers_callback(self, tmp_path: Path) -> None:
        """watch() 注册回调后，reload 触发回调。"""
        center = ConfigCenter(config_root=tmp_path)
        callback = MagicMock()

        center.watch("agents/", callback)

        fpath = tmp_path / "agents" / "test.yaml"
        _write_yaml(fpath, {"name": "test_agent"})
        center.reload(str(fpath))

        callback.assert_called_once()
        call_args = callback.call_args
        assert call_args[0][0] == "manual_reload"
        assert "test.yaml" in call_args[0][1]

    def test_unwatch_removes_callback(self, tmp_path: Path) -> None:
        """unwatch() 取消回调后，reload 不再触发。"""
        center = ConfigCenter(config_root=tmp_path)
        callback = MagicMock()

        center.watch("agents/", callback)
        assert center.unwatch("agents/", callback) is True

        fpath = tmp_path / "agents" / "test.yaml"
        _write_yaml(fpath, {"name": "test"})
        center.reload(str(fpath))

        callback.assert_not_called()

    def test_unwatch_returns_false_for_unknown_callback(
        self, tmp_path: Path,
    ) -> None:
        """unwatch() 对未注册的回调返回 False。"""
        center = ConfigCenter(config_root=tmp_path)
        assert center.unwatch("agents/", MagicMock()) is False

    def test_watch_multiple_callbacks(self, tmp_path: Path) -> None:
        """同一前缀注册多个回调，全部被触发。"""
        center = ConfigCenter(config_root=tmp_path)
        cb1 = MagicMock()
        cb2 = MagicMock()

        center.watch("agents/", cb1)
        center.watch("agents/", cb2)

        fpath = tmp_path / "agents" / "multi.yaml"
        _write_yaml(fpath, {"multi": True})
        center.reload(str(fpath))

        cb1.assert_called_once()
        cb2.assert_called_once()

    def test_watch_callback_exception_does_not_break_others(
        self, tmp_path: Path,
    ) -> None:
        """一个回调异常不影响其他回调执行。"""
        center = ConfigCenter(config_root=tmp_path)

        def bad_callback(event_type: str, file_path: str, ctx: dict) -> None:
            raise RuntimeError("callback error")

        good_callback = MagicMock()
        center.watch("agents/", bad_callback)
        center.watch("agents/", good_callback)

        fpath = tmp_path / "agents" / "safe.yaml"
        _write_yaml(fpath, {"safe": True})
        center.reload(str(fpath))

        good_callback.assert_called_once()


# ===========================================================================
# 2. 防抖和哈希去重
# ===========================================================================


class TestDebounce:
    """防抖逻辑测试。"""

    def test_check_debounce_allows_first_call(self, tmp_path: Path) -> None:
        """首次调用 _check_debounce 返回 True。"""
        center = ConfigCenter(config_root=tmp_path)
        assert center._check_debounce("/some/path.yaml") is True

    def test_check_debounce_rejects_rapid_call(self, tmp_path: Path) -> None:
        """防抖窗口内的重复调用返回 False。"""
        center = ConfigCenter(config_root=tmp_path, debounce_seconds=1.0)
        center._check_debounce("/some/path.yaml")
        # 立即再调用，应被防抖拦截
        assert center._check_debounce("/some/path.yaml") is False

    def test_check_debounce_allows_after_window(self, tmp_path: Path) -> None:
        """防抖窗口过期后的调用返回 True。"""
        center = ConfigCenter(
            config_root=tmp_path,
            debounce_seconds=0.01,  # 极短窗口
        )
        center._check_debounce("/some/path.yaml")
        time.sleep(0.02)
        assert center._check_debounce("/some/path.yaml") is True

    def test_check_debounce_independent_per_file(self, tmp_path: Path) -> None:
        """不同文件的防抖状态互不影响。"""
        center = ConfigCenter(config_root=tmp_path, debounce_seconds=1.0)
        center._check_debounce("/file1.yaml")
        # file2 应该不受 file1 防抖影响
        assert center._check_debounce("/file2.yaml") is True


class TestHashDedup:
    """内容哈希去重测试。"""

    def test_reload_skips_unchanged_content(self, tmp_path: Path) -> None:
        """reload() 对内容未变化的文件跳过更新。"""
        data = {"unchanged": True}
        fpath = tmp_path / "same.yaml"
        _write_yaml(fpath, data)

        center = ConfigCenter(config_root=tmp_path)

        # 首次加载
        result1 = center.reload("same.yaml")
        assert result1["success"] is True

        # 再次加载相同内容
        result2 = center.reload("same.yaml")
        assert result2["success"] is True
        assert result2["rolled_back"] is False

        # 审计日志应该只有一条成功记录（第二次去重不写新审计）
        log = center.get_audit_log(limit=10)
        success_entries = [e for e in log if e["success"]]
        assert len(success_entries) == 1

    def test_reload_updates_on_content_change(self, tmp_path: Path) -> None:
        """reload() 在内容变化时更新缓存。"""
        fpath = tmp_path / "changing.yaml"

        _write_yaml(fpath, {"version": 1})
        center = ConfigCenter(config_root=tmp_path)
        center.reload("changing.yaml")

        # 修改文件内容
        _write_yaml(fpath, {"version": 2})
        result = center.reload("changing.yaml")

        assert result["success"] is True
        cached = center.get("changing.yaml")
        assert cached["version"] == 2


# ===========================================================================
# 3. 并发安全（读写锁）
# ===========================================================================


class TestRWLock:
    """读写锁测试。"""

    def test_rwlock_allows_concurrent_reads(self) -> None:
        """多个线程可以同时获取读锁。"""
        rwlock = _RWLock()
        active_readers = 0
        max_readers = 0
        lock = threading.Lock()

        def reader() -> None:
            nonlocal active_readers, max_readers
            with _ReadGuard(rwlock):
                with lock:
                    active_readers += 1
                    max_readers = max(max_readers, active_readers)
                time.sleep(0.05)
                with lock:
                    active_readers -= 1

        threads = [threading.Thread(target=reader) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert max_readers > 1, "应允许多个并发读"

    def test_rwlock_blocks_readers_during_write(self) -> None:
        """写锁持有时，读锁应被阻塞。"""
        rwlock = _RWLock()
        write_started = threading.Event()
        read_attempted = threading.Event()
        read_completed = threading.Event()

        def writer() -> None:
            rwlock.acquire_write()
            write_started.set()
            read_attempted.wait(timeout=2)
            rwlock.release_write()

        def reader() -> None:
            write_started.wait(timeout=2)
            read_attempted.set()
            rwlock.acquire_read()
            rwlock.release_read()
            read_completed.set()

        t_writer = threading.Thread(target=writer)
        t_reader = threading.Thread(target=reader)

        t_writer.start()
        time.sleep(0.05)
        t_reader.start()

        # 读不应该在写完成前完成
        assert not read_completed.is_set()

        t_writer.join(timeout=3)
        t_reader.join(timeout=3)
        assert read_completed.is_set()

    def test_rwlock_write_is_exclusive(self) -> None:
        """写锁是独占的，不能同时有两个写者。"""
        rwlock = _RWLock()
        write_count = 0
        max_concurrent_writes = 0
        lock = threading.Lock()

        def writer() -> None:
            nonlocal write_count, max_concurrent_writes
            rwlock.acquire_write()
            with lock:
                write_count += 1
                max_concurrent_writes = max(max_concurrent_writes, write_count)
            time.sleep(0.05)
            with lock:
                write_count -= 1
            rwlock.release_write()

        threads = [threading.Thread(target=writer) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert max_concurrent_writes == 1, "写锁应该是独占的"


class TestConfigCenterConcurrency:
    """ConfigCenter 并发操作测试。"""

    def test_concurrent_get_and_reload(self, tmp_path: Path) -> None:
        """多个线程同时 get 和 reload 不引发异常。"""
        fpath = tmp_path / "concurrent.yaml"
        _write_yaml(fpath, {"counter": 0})

        center = ConfigCenter(config_root=tmp_path)
        center.reload("concurrent.yaml")

        errors: list[Exception] = []

        def reader() -> None:
            try:
                for _ in range(50):
                    center.get("concurrent.yaml")
            except Exception as e:
                errors.append(e)

        def writer() -> None:
            try:
                for i in range(50):
                    _write_yaml(fpath, {"counter": i})
                    center.reload("concurrent.yaml")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=reader),
            threading.Thread(target=reader),
            threading.Thread(target=writer),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"并发操作发生错误: {errors}"


# ===========================================================================
# 4. 加载失败回滚
# ===========================================================================


class TestLoadFailureRollback:
    """加载失败回滚测试。"""

    def test_rollback_preserves_old_config_on_failure(self, tmp_path: Path) -> None:
        """加载失败时保留旧配置（回滚）。"""
        fpath = tmp_path / "rollback.yaml"
        _write_yaml(fpath, {"version": 1})

        center = ConfigCenter(config_root=tmp_path)
        center.reload("rollback.yaml")

        # 破坏文件内容（非字典 YAML）
        fpath.write_text("- not_a_dict\n", encoding="utf-8")

        result = center.reload("rollback.yaml")
        assert result["success"] is False
        assert result["rolled_back"] is True

        # 缓存应保留旧值
        cached = center.get("rollback.yaml")
        assert cached == {"version": 1}

    def test_failure_without_old_config(self, tmp_path: Path) -> None:
        """加载失败且无旧配置时，rolled_back 为 False。"""
        fpath = tmp_path / "no_old.yaml"
        fpath.write_text("- not_a_dict\n", encoding="utf-8")

        center = ConfigCenter(config_root=tmp_path)
        result = center.reload("no_old.yaml")

        assert result["success"] is False
        assert result["rolled_back"] is False

    def test_handle_load_failure_writes_audit(self, tmp_path: Path) -> None:
        """加载失败时写入审计日志。"""
        fpath = tmp_path / "audit_fail.yaml"
        fpath.write_text("bad: [yaml: {{\n", encoding="utf-8")

        center = ConfigCenter(config_root=tmp_path)
        try:
            center.reload("audit_fail.yaml")
        except Exception:
            pass

        log = center.get_audit_log()
        failed = [e for e in log if not e["success"]]
        assert len(failed) >= 1

    def test_audit_log_respects_max_history(self, tmp_path: Path) -> None:
        """审计日志超过最大条数时自动淘汰旧条目。"""
        center = ConfigCenter(config_root=tmp_path)

        for i in range(MAX_AUDIT_HISTORY + 50):
            center._write_audit(AuditEntry(
                file_path=f"/test/{i}.yaml",
                event_type="test",
                config_type="test",
                success=True,
            ))

        assert len(center._audit_log) <= MAX_AUDIT_HISTORY


# ===========================================================================
# 5. Agent Registry 热更新方法
# ===========================================================================


class TestAgentRegistryHotReload:
    """AgentRegistry 热更新方法测试。"""

    @pytest.fixture()
    def _agent_yaml(self, tmp_path: Path) -> Path:
        """创建一个最小可用的 Agent YAML 配置文件。"""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        data = {
            "config_id": "test_agent",
            "name": "Test Agent",
            "display_name": "测试 Agent",
            "description": "A test agent",
            "agent_type": "SPECIALIZED",
            "category": "test",
            "level": "L3",
            "model_tier": "small",
            "system_prompt": "You are a test agent.",
        }
        fpath = agent_dir / "test_agent.yaml"
        _write_yaml(fpath, data)
        return fpath

    def test_register_and_unregister(self, tmp_path: Path) -> None:
        """register/unregister 基本流程。"""
        from agents.registry import AgentRegistry
        from agents.types import AgentConfig, AgentLevel, AgentType

        registry = AgentRegistry()
        config = AgentConfig(
            config_id="reg_test",
            name="reg_test",
            display_name="Reg Test",
            description="test",
            agent_type=AgentType.SPECIALIZED,
            category="test",
            level=AgentLevel.L3_ATOMIC,
        )

        registry.register(config)
        assert registry.get("reg_test") is not None
        assert registry.count() == 1

        assert registry.unregister("reg_test") is True
        assert registry.get("reg_test") is None
        assert registry.count() == 0

    def test_unregister_nonexistent_returns_false(self) -> None:
        """注销不存在的 Agent 返回 False。"""
        from agents.registry import AgentRegistry

        registry = AgentRegistry()
        assert registry.unregister("ghost") is False

    def test_register_rejects_empty_id(self) -> None:
        """register 拒绝空 config_id。"""
        from agents.registry import AgentRegistry
        from agents.types import AgentConfig, AgentLevel, AgentType

        registry = AgentRegistry()
        config = AgentConfig(
            config_id="",
            name="empty_id",
            display_name="Empty",
            description="test",
            agent_type=AgentType.SPECIALIZED,
            category="test",
            level=AgentLevel.L3_ATOMIC,
        )
        with pytest.raises(ValueError, match="不能为空"):
            registry.register(config)

    def test_reload_agent_from_disk(self, tmp_path: Path) -> None:
        """reload_agent 从磁盘重新加载配置。"""
        from agents.registry import AgentRegistry

        registry = AgentRegistry()
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()

        # 初始配置
        data = {
            "config_id": "hot_agent",
            "name": "hot_agent",
            "display_name": "Hot Agent",
            "description": "before reload",
            "agent_type": "SPECIALIZED",
            "category": "test",
            "level": "L3",
            "model_tier": "small",
            "system_prompt": "old prompt",
        }
        fpath = agent_dir / "hot_agent.yaml"
        _write_yaml(fpath, data)

        # 加载目录
        registry.load_directory(agent_dir)
        old_config = registry.get("hot_agent")
        assert old_config is not None
        assert old_config.description == "before reload"

        # 修改文件
        data["description"] = "after reload"
        data["system_prompt"] = "new prompt"
        _write_yaml(fpath, data)

        # 热重载
        new_config = registry.reload_agent("hot_agent")
        assert new_config is not None
        assert new_config.description == "after reload"

    def test_reload_agent_nonexistent_returns_none(self, tmp_path: Path) -> None:
        """reload_agent 对不存在的 config_id 返回 None。"""
        from agents.registry import AgentRegistry

        registry = AgentRegistry()
        registry._config_dir = tmp_path
        assert registry.reload_agent("ghost_agent") is None


# ===========================================================================
# 6. REST reload 端点
# ===========================================================================


class TestReloadEndpoint:
    """routes_config.reload_config 端点测试。"""

    def test_reload_returns_503_when_no_config_center(self) -> None:
        """ConfigCenter 未注入时返回 503。"""
        from fastapi import HTTPException

        from channels.api.routes_config import reload_config

        # 确保未注入
        import channels.api.routes_config as mod
        original = mod._config_center
        mod._config_center = None

        try:
            with pytest.raises(HTTPException) as exc_info:
                reload_config("test/config")
            assert exc_info.value.status_code == 503
        finally:
            mod._config_center = original

    def test_reload_calls_config_center(self, tmp_path: Path) -> None:
        """reload_config 正确调用 ConfigCenter.reload。"""
        import channels.api.routes_config as mod

        mock_center = MagicMock()
        mock_center.reload.return_value = {
            "success": True,
            "error": None,
            "rolled_back": False,
            "config_type": "model",
        }

        original = mod._config_center
        mod._config_center = mock_center

        try:
            # 使用白名单路径
            whitelist_key = list(mod._GENERIC_CONFIG_WHITELIST.keys())[0]
            result = mod.reload_config(whitelist_key)

            assert result["success"] is True
            mock_center.reload.assert_called_once()
        finally:
            mod._config_center = original

    def test_reload_rejects_path_outside_config(self) -> None:
        """reload_config 拒绝 config/ 目录外的路径。"""
        from fastapi import HTTPException

        import channels.api.routes_config as mod

        mock_center = MagicMock()
        original = mod._config_center
        mod._config_center = mock_center

        try:
            with pytest.raises(HTTPException) as exc_info:
                mod.reload_config("../../etc/passwd")
            assert exc_info.value.status_code in (403, 404)
        finally:
            mod._config_center = original

    def test_set_config_center(self) -> None:
        """set_config_center 正确注入实例。"""
        import channels.api.routes_config as mod

        mock = MagicMock()
        original = mod._config_center

        mod.set_config_center(mock)
        assert mod._config_center is mock

        # 恢复
        mod._config_center = original

    def test_reload_returns_404_for_missing_file(self) -> None:
        """reload_config 对不存在的文件返回 404。"""
        from fastapi import HTTPException

        import channels.api.routes_config as mod

        mock_center = MagicMock()
        mock_center.reload.side_effect = FileNotFoundError("not found")

        original = mod._config_center
        mod._config_center = mock_center

        try:
            with pytest.raises(HTTPException) as exc_info:
                whitelist_key = list(mod._GENERIC_CONFIG_WHITELIST.keys())[0]
                mod.reload_config(whitelist_key)
            assert exc_info.value.status_code == 404
        finally:
            mod._config_center = original

    def test_reload_returns_400_for_invalid_yaml(self) -> None:
        """reload_config 对无效 YAML 返回 400。"""
        from fastapi import HTTPException

        import channels.api.routes_config as mod

        mock_center = MagicMock()
        mock_center.reload.side_effect = ValueError("YAML 解析失败")

        original = mod._config_center
        mod._config_center = mock_center

        try:
            with pytest.raises(HTTPException) as exc_info:
                whitelist_key = list(mod._GENERIC_CONFIG_WHITELIST.keys())[0]
                mod.reload_config(whitelist_key)
            assert exc_info.value.status_code == 400
        finally:
            mod._config_center = original


# ===========================================================================
# 辅助功能测试
# ===========================================================================


class TestConfigCenterHelpers:
    """ConfigCenter 辅助方法测试。"""

    def test_determine_config_type(self) -> None:
        """_determine_config_type 正确识别配置类型。"""
        cases = {
            "/config/agents/main/灵汐.yaml": "agent",
            "/config/pipelines/default.yaml": "pipeline",
            "/config/tools/bash.yaml": "tool",
            "/config/models/llm.yaml": "model",
            "/config/templates/test.yaml": "template",
            "/config/triggers/cron.yaml": "trigger",
            "/config/evaluation_metrics/m1.yaml": "evaluation_metric",
            "/config/evaluation/config.yaml": "evaluation",
            "/config/isolation/config.yaml": "isolation",
            "/config/modules/test.yaml": "module",
            "/random/path.yaml": "unknown",
        }
        for path, expected in cases.items():
            assert ConfigCenter._determine_config_type(path) == expected

    def test_is_excluded(self, tmp_path: Path) -> None:
        """_is_excluded 正确过滤排除文件。"""
        center = ConfigCenter(config_root=tmp_path)

        excluded = [
            tmp_path / ".env",
            tmp_path / ".env.local",
            tmp_path / ".env.production",
            tmp_path / "redis.conf",
            tmp_path / "docker-compose.yml",
            tmp_path / "Dockerfile",
            tmp_path / "backup.bak",
        ]
        for p in excluded:
            assert center._is_excluded(p) is True, f"{p.name} 应被排除"

        included = [
            tmp_path / "agents.yaml",
            tmp_path / "config.yaml",
        ]
        for p in included:
            assert center._is_excluded(p) is False, f"{p.name} 不应被排除"

    def test_is_running_property(self, tmp_path: Path) -> None:
        """is_running 属性正确反映状态。"""
        center = ConfigCenter(config_root=tmp_path)
        assert center.is_running is False

    def test_get_audit_log_empty(self, tmp_path: Path) -> None:
        """空审计日志返回空列表。"""
        center = ConfigCenter(config_root=tmp_path)
        assert center.get_audit_log() == []

    def test_get_audit_log_limit(self, tmp_path: Path) -> None:
        """get_audit_log 限制返回条数。"""
        center = ConfigCenter(config_root=tmp_path)
        for i in range(10):
            center._write_audit(AuditEntry(
                file_path=f"/test/{i}.yaml",
                event_type="test",
                config_type="test",
                success=True,
            ))

        log = center.get_audit_log(limit=3)
        assert len(log) == 3
