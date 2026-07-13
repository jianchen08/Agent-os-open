"""
插件热加载系统测试。

覆盖功能点：
- PluginHotReloader 启停生命周期
- reload_plugin() 手动重载单个插件
- reload_all() 批量重载
- enable_plugin / disable_plugin 启停控制
- deleted 事件注销插件
- YAML 解析失败回滚
- 验证失败回滚
- 注册/注销到 Registry
- 回调通知
- ConfigCenter 集成
- 历史记录查询
"""
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from plugins.hot_reload import (
    PluginHotReloader,
    PluginRecord,
    PluginStatus,
    ReloadEvent,
    PluginConfigWatchHandler,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False)


@pytest.fixture
def config_dir(tmp_path):
    """临时 config 目录。"""
    d = tmp_path / "config"
    d.mkdir()
    return d


@pytest.fixture
def agent_registry():
    """Mock AgentRegistry。"""
    reg = MagicMock()
    reg.register = MagicMock()
    reg.unregister = MagicMock(return_value=True)
    return reg


@pytest.fixture
def reloader(config_dir, agent_registry):
    """创建 PluginHotReloader 实例。"""
    validator = MagicMock()
    validator.validate_agent_config = MagicMock(return_value=[])
    validator.validate_pipeline_config = MagicMock(return_value=[])
    validator.validate_model_config = MagicMock(return_value=[])
    return PluginHotReloader(
        config_dir=config_dir,
        agent_registry=agent_registry,
        validator=validator,
        debounce_seconds=0.05,
    )


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


class TestLifecycle:
    """PluginHotReloader 启停。"""

    def test_start_stop(self, reloader, config_dir):
        """start/stop 应正常工作。"""
        reloader.start()
        assert reloader.is_running is True
        reloader.stop()
        # stop 后 observer 被置 None
        assert reloader._observer is None

    def test_start_idempotent(self, reloader, config_dir):
        """重复 start 不应报错。"""
        reloader.start()
        reloader.start()  # 不应抛异常
        reloader.stop()

    def test_start_nonexistent_dir(self, tmp_path):
        """config 目录不存在时 start 应跳过。"""
        r = PluginHotReloader(config_dir=tmp_path / "no_such_dir")
        r.start()
        assert r.is_running is False

    def test_is_running_initially_false(self, reloader):
        """初始状态应为未运行。"""
        assert reloader.is_running is False


# ---------------------------------------------------------------------------
# reload_plugin() — 手动重载
# ---------------------------------------------------------------------------


class TestReloadPlugin:
    """手动重载单个插件。"""

    def test_reload_agent_config(self, reloader, config_dir, agent_registry):
        """重载 agent 配置应调用 agent_registry.register。"""
        agent_yaml = config_dir / "agents" / "test.yaml"
        _write_yaml(agent_yaml, {
            "config_id": "test_agent",
            "name": "Test Agent",
            "agent_type": "specialized",
            "level": "L3",
            "system_prompt": "Test",
        })

        event = reloader.reload_plugin(str(agent_yaml))

        assert event.success is True
        assert event.config_type == "agent"
        assert agent_registry.register.called

    def test_reload_nonexistent_file(self, reloader, config_dir):
        """重载不存在的文件应返回失败。"""
        event = reloader.reload_plugin(str(config_dir / "agents" / "missing.yaml"))

        assert event.success is False
        assert event.error is not None

    def test_reload_invalid_yaml(self, reloader, config_dir):
        """YAML 解析失败应返回失败事件。"""
        bad_yaml = config_dir / "agents" / "bad.yaml"
        bad_yaml.parent.mkdir(parents=True, exist_ok=True)
        bad_yaml.write_text("{{invalid yaml", encoding="utf-8")

        event = reloader.reload_plugin(str(bad_yaml))

        assert event.success is False
        assert "YAML" in event.error

    def test_reload_non_dict_yaml(self, reloader, config_dir):
        """非字典 YAML 应返回失败。"""
        list_yaml = config_dir / "agents" / "list.yaml"
        list_yaml.parent.mkdir(parents=True, exist_ok=True)
        list_yaml.write_text("- item1\n- item2", encoding="utf-8")

        event = reloader.reload_plugin(str(list_yaml))
        assert event.success is False

    def test_reload_relative_path(self, reloader, config_dir):
        """相对路径应被解析到 config_dir。"""
        agent_yaml = config_dir / "agents" / "test.yaml"
        _write_yaml(agent_yaml, {"config_id": "test", "name": "Test"})

        event = reloader.reload_plugin("agents/test.yaml")
        assert event.config_type == "agent"

    def test_reload_records_status(self, reloader, config_dir):
        """重载成功后应记录 PluginStatus.LOADED。"""
        agent_yaml = config_dir / "agents" / "test.yaml"
        _write_yaml(agent_yaml, {"config_id": "test", "name": "Test"})

        reloader.reload_plugin(str(agent_yaml))

        statuses = reloader.get_plugin_status()
        assert len(statuses) == 1
        assert statuses[0]["status"] == "loaded"


# ---------------------------------------------------------------------------
# reload_all() — 批量重载
# ---------------------------------------------------------------------------


class TestReloadAll:
    """批量重载所有 YAML 配置。"""

    def test_reload_all_multiple_files(self, reloader, config_dir):
        """批量重载应处理所有 YAML 文件。"""
        _write_yaml(config_dir / "agents" / "a1.yaml", {"config_id": "a1"})
        _write_yaml(config_dir / "agents" / "a2.yaml", {"config_id": "a2"})

        results = reloader.reload_all()

        assert len(results) == 2

    def test_reload_all_empty_dir(self, reloader, tmp_path):
        """空目录批量重载应返回空列表。"""
        r = PluginHotReloader(config_dir=tmp_path / "empty_config")
        (tmp_path / "empty_config").mkdir()
        results = r.reload_all()
        assert results == []

    def test_reload_all_ignores_temp_files(self, reloader, config_dir):
        """批量重载应跳过临时文件。"""
        _write_yaml(config_dir / "agents" / "a1.yaml", {"config_id": "a1"})
        temp = config_dir / "agents" / ".hidden.yaml"
        _write_yaml(temp, {"config_id": "hidden"})

        results = reloader.reload_all()
        # .hidden.yaml 应被跳过
        assert len(results) == 1


# ---------------------------------------------------------------------------
# enable / disable 插件
# ---------------------------------------------------------------------------


class TestEnableDisablePlugin:
    """按 enabled/disabled 状态控制热加载。"""

    def test_disable_plugin(self, reloader):
        """禁用插件应加入 disabled 集合。"""
        result = reloader.disable_plugin("test_agent")
        assert result is True
        assert "test_agent" in reloader._disabled_plugins

    def test_disable_already_disabled(self, reloader):
        """重复禁用应返回 False。"""
        reloader.disable_plugin("test_agent")
        result = reloader.disable_plugin("test_agent")
        assert result is False

    def test_enable_plugin(self, reloader):
        """启用已禁用的插件应从 disabled 集合移除。"""
        reloader.disable_plugin("test_agent")
        result = reloader.enable_plugin("test_agent")
        assert result is True
        assert "test_agent" not in reloader._disabled_plugins

    def test_enable_not_disabled(self, reloader):
        """启用未禁用的插件应返回 False。"""
        result = reloader.enable_plugin("test_agent")
        assert result is False

    def test_disabled_plugin_skips_reload(self, reloader, config_dir):
        """被禁用的插件不应被重载。"""
        reloader.disable_plugin("test_agent")

        agent_yaml = config_dir / "agents" / "test.yaml"
        _write_yaml(agent_yaml, {"config_id": "test_agent", "name": "Test"})

        event = reloader.reload_plugin(str(agent_yaml))

        # disabled 插件返回 success=True 但 error="disabled"
        assert event.success is True
        assert event.error == "disabled"


# ---------------------------------------------------------------------------
# 删除事件
# ---------------------------------------------------------------------------


class TestDeletedEvent:
    """文件删除事件应注销插件。"""

    def test_deleted_unregisters_from_registry(self, reloader, config_dir, agent_registry):
        """删除事件应调用 agent_registry.unregister。"""
        agent_yaml = config_dir / "agents" / "test.yaml"
        _write_yaml(agent_yaml, {"config_id": "test_agent", "name": "Test"})

        # 先加载
        reloader.reload_plugin(str(agent_yaml))
        # 模拟删除
        event = reloader._handle_deleted(str(agent_yaml), "agent")

        assert event.success is True
        assert agent_registry.unregister.called

    def test_deleted_updates_record_status(self, reloader, config_dir):
        """删除后记录状态应为 UNLOADED。"""
        agent_yaml = config_dir / "agents" / "test.yaml"
        _write_yaml(agent_yaml, {"config_id": "test_agent"})

        reloader.reload_plugin(str(agent_yaml))
        reloader._handle_deleted(str(agent_yaml), "agent")

        record = reloader._records.get(str(agent_yaml))
        assert record is not None
        assert record.status == PluginStatus.UNLOADED

    def test_deleted_unknown_file_noop(self, reloader):
        """删除未知文件应返回成功（无操作）。"""
        event = reloader._handle_deleted("/fake/path.yaml", "unknown")
        assert event.success is True


# ---------------------------------------------------------------------------
# 回滚
# ---------------------------------------------------------------------------


class TestRollback:
    """加载失败时应回滚。"""

    def test_parse_error_rollback(self, reloader, config_dir):
        """YAML 解析失败时应保留旧版本（如果有）。"""
        agent_yaml = config_dir / "agents" / "test.yaml"
        _write_yaml(agent_yaml, {"config_id": "test", "name": "Good"})
        reloader.reload_plugin(str(agent_yaml))

        # 写入无效 YAML
        agent_yaml.write_text("{{bad", encoding="utf-8")
        event = reloader.reload_plugin(str(agent_yaml))

        assert event.success is False
        assert event.rolled_back is True
        # 旧记录仍保持 LOADED
        record = reloader._records.get(str(agent_yaml))
        assert record.status == PluginStatus.LOADED

    def test_apply_reload_failure_rollback(self, reloader, config_dir, agent_registry):
        """注册失败时应回滚到旧数据。"""
        agent_yaml = config_dir / "agents" / "test.yaml"
        _write_yaml(agent_yaml, {"config_id": "test", "name": "V1"})
        reloader.reload_plugin(str(agent_yaml))

        # 让 register 第一次调用失败（新注册），第二次调用成功（回滚注册）
        call_count = [0]
        def register_side_effect(config):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ValueError("registration failed")
            # 回滚时正常注册
            return None

        agent_registry.register.side_effect = register_side_effect

        _write_yaml(agent_yaml, {"config_id": "test", "name": "V2"})
        event = reloader.reload_plugin(str(agent_yaml))

        assert event.success is False
        assert event.rolled_back is True


# ---------------------------------------------------------------------------
# 回调通知
# ---------------------------------------------------------------------------


class TestCallbacks:
    """重载事件回调通知。"""

    def test_add_callback(self, reloader):
        """添加回调后应被调用。"""
        cb = MagicMock()
        reloader.add_callback(cb)

        agent_yaml = reloader._config_dir / "agents" / "test.yaml"
        _write_yaml(agent_yaml, {"config_id": "test", "name": "Test"})
        reloader.reload_plugin(str(agent_yaml))

        assert cb.called

    def test_remove_callback(self, reloader):
        """移除回调后不应再被调用。"""
        cb = MagicMock()
        reloader.add_callback(cb)
        reloader.remove_callback(cb)

        agent_yaml = reloader._config_dir / "agents" / "test.yaml"
        _write_yaml(agent_yaml, {"config_id": "test"})
        reloader.reload_plugin(str(agent_yaml))

        assert not cb.called

    def test_remove_nonexistent_callback(self, reloader):
        """移除未注册的回调应返回 False。"""
        result = reloader.remove_callback(MagicMock())
        assert result is False


# ---------------------------------------------------------------------------
# 历史记录
# ---------------------------------------------------------------------------


class TestHistory:
    """重载历史记录。"""

    def test_reload_history(self, reloader, config_dir):
        """重载后应有历史记录。"""
        agent_yaml = config_dir / "agents" / "test.yaml"
        _write_yaml(agent_yaml, {"config_id": "test"})

        reloader.reload_plugin(str(agent_yaml))
        history = reloader.get_reload_history()

        assert len(history) >= 1
        assert history[0]["success"] is True

    def test_reload_history_limit(self, reloader, config_dir):
        """历史记录应有上限。"""
        reloader._max_history = 5
        agent_yaml = config_dir / "agents" / "test.yaml"

        for i in range(10):
            _write_yaml(agent_yaml, {"config_id": f"test_{i}"})
            reloader.reload_plugin(str(agent_yaml))

        assert len(reloader._history) <= 5


# ---------------------------------------------------------------------------
# 插件状态查询
# ---------------------------------------------------------------------------


class TestPluginStatus:
    """插件状态查询。"""

    def test_get_plugin_status(self, reloader, config_dir):
        """查询所有跟踪插件的状态。"""
        agent_yaml = config_dir / "agents" / "test.yaml"
        _write_yaml(agent_yaml, {"config_id": "test", "name": "Test"})

        reloader.reload_plugin(str(agent_yaml))
        statuses = reloader.get_plugin_status()

        assert len(statuses) == 1
        s = statuses[0]
        assert "config_path" in s
        assert "config_type" in s
        assert "status" in s
        assert s["status"] == "loaded"


# ---------------------------------------------------------------------------
# 配置类型判断
# ---------------------------------------------------------------------------


class TestConfigTypeDetection:
    """根据文件路径判断配置类型。"""

    @pytest.mark.parametrize("path,expected", [
        ("/config/agents/test.yaml", "agent"),
        ("/config/pipelines/default.yaml", "pipeline"),
        ("/config/tools/search.yaml", "tool"),
        ("/config/models/llm.yaml", "model"),
        ("/config/evaluation_metrics/m.yaml", "evaluation_metric"),
        ("/config/templates/t.yaml", "template"),
        ("/config/triggers/cron.yaml", "trigger"),
        ("/random/file.yaml", "unknown"),
    ])
    def test_determine_config_type(self, path, expected):
        """各路径应正确识别配置类型。"""
        assert PluginHotReloader._determine_config_type(path) == expected


# ---------------------------------------------------------------------------
# WatchHandler 防抖
# ---------------------------------------------------------------------------


class TestWatchHandler:
    """文件监听处理器防抖。"""

    def test_handler_filters_non_yaml(self):
        """非 YAML 文件应被过滤。"""
        cb = MagicMock()
        handler = PluginConfigWatchHandler(cb)
        assert handler._should_process("test.txt") is False
        assert handler._should_process("test.py") is False
        assert handler._should_process("test.yaml") is True
        assert handler._should_process("test.yml") is True

    def test_handler_filters_temp_files(self):
        """临时文件应被过滤。"""
        cb = MagicMock()
        handler = PluginConfigWatchHandler(cb)
        assert handler._should_process(".hidden.yaml") is False
        assert handler._should_process("~temp.yaml") is False

    def test_handler_debounces(self):
        """防抖窗口内的重复事件应被过滤。"""
        cb = MagicMock()
        handler = PluginConfigWatchHandler(cb, debounce_seconds=0.5)
        handler._debounce_and_notify("modified", "/fake/test.yaml")
        handler._debounce_and_notify("modified", "/fake/test.yaml")

        assert cb.call_count == 1


# ---------------------------------------------------------------------------
# ConfigCenter 集成
# ---------------------------------------------------------------------------


class TestConfigCenterIntegration:
    """PluginHotReloader 与 ConfigCenter 的集成。"""

    def test_integrate_registers_watchers(self, reloader):
        """集成后应在 ConfigCenter 上注册监听。"""
        mock_center = MagicMock()
        reloader.integrate_with_config_center(mock_center)

        assert mock_center.watch.call_count >= 4  # agents/tools/pipelines/models
        watched_prefixes = [call[0][0] for call in mock_center.watch.call_args_list]
        assert "agents/" in watched_prefixes
        assert "tools/" in watched_prefixes
        assert "pipelines/" in watched_prefixes
        assert "models/" in watched_prefixes

    def test_config_center_callback_delegates_to_reload(self, reloader, config_dir):
        """ConfigCenter 回调应委托给 _on_file_change。"""
        mock_center = MagicMock()
        reloader.integrate_with_config_center(mock_center)

        # 获取注册的回调
        agents_cb = None
        for call in mock_center.watch.call_args_list:
            prefix = call[0][0]
            if prefix == "agents/":
                agents_cb = call[0][1]
                break

        assert agents_cb is not None

        # 验证回调能正确委托
        agent_yaml = config_dir / "agents" / "test.yaml"
        _write_yaml(agent_yaml, {"config_id": "test", "name": "Test"})

        agents_cb("modified", str(agent_yaml), {"config_type": "agent"})
        # 应在 records 中有记录（异步线程池执行，等待短暂时间）
        time.sleep(0.2)
        assert str(agent_yaml) in reloader._records
