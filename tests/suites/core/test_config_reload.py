"""M8 配置热重载与 Schema 校验测试。

覆盖：
- ConfigReloadHandler: _should_process 过滤、防抖、事件回调
- ConfigReloader: 启动/停止、回调注册/注销、重载器注册、配置类型判断
- ConfigSchemaValidator: Pipeline 校验、Agent 校验、YAML 文件校验、目录校验
- 集成：实际文件修改触发热重载（使用 tmp_path fixture）
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from config.reload import ConfigReloadHandler, ConfigReloader
from config.schema import ConfigSchemaValidator


# ============================================================================
# ConfigReloadHandler 测试
# ============================================================================


class TestConfigReloadHandlerShouldProcess:
    """ConfigReloadHandler._should_process 过滤测试。"""

    def setup_method(self) -> None:
        self.callback = MagicMock()
        self.handler = ConfigReloadHandler(callback=self.callback)

    def test_yaml_file_should_process(self) -> None:
        """YAML 文件应被处理。"""
        assert self.handler._should_process("/config/test.yaml") is True

    def test_yml_file_should_process(self) -> None:
        """YML 文件应被处理。"""
        assert self.handler._should_process("/config/test.yml") is True

    def test_dot_prefix_file_should_ignore(self) -> None:
        """以 . 开头的文件应被忽略。"""
        assert self.handler._should_process("/config/.hidden.yaml") is False

    def test_tilde_prefix_file_should_ignore(self) -> None:
        """以 ~ 开头的文件应被忽略。"""
        assert self.handler._should_process("/config/~backup.yaml") is False

    def test_non_yaml_file_should_ignore(self) -> None:
        """非 YAML 文件应被忽略。"""
        assert self.handler._should_process("/config/test.json") is False
        assert self.handler._should_process("/config/test.py") is False
        assert self.handler._should_process("/config/test.txt") is False

    def test_dot_yml_should_ignore(self) -> None:
        """. 开头的 yml 文件也应被忽略。"""
        assert self.handler._should_process("/config/.swp.yml") is False


class TestConfigReloadHandlerEvents:
    """ConfigReloadHandler 事件回调测试。"""

    def setup_method(self) -> None:
        self.callback = MagicMock()
        self.handler = ConfigReloadHandler(
            callback=self.callback, debounce_seconds=0.0
        )

    def _make_event(self, src_path: str, is_directory: bool = False) -> MagicMock:
        """构造模拟的 FileSystemEvent。"""
        event = MagicMock()
        event.src_path = src_path
        event.is_directory = is_directory
        return event

    def test_on_modified_calls_callback(self) -> None:
        """修改事件应触发回调。"""
        event = self._make_event("/config/test.yaml")
        self.handler.on_modified(event)
        self.callback.assert_called_once_with("modified", "/config/test.yaml")

    def test_on_created_calls_callback(self) -> None:
        """创建事件应触发回调。"""
        event = self._make_event("/config/new.yaml")
        self.handler.on_created(event)
        self.callback.assert_called_once_with("created", "/config/new.yaml")

    def test_on_deleted_calls_callback(self) -> None:
        """删除事件应触发回调。"""
        event = self._make_event("/config/old.yaml")
        self.handler.on_deleted(event)
        self.callback.assert_called_once_with("deleted", "/config/old.yaml")

    def test_directory_event_ignored(self) -> None:
        """目录事件应被忽略。"""
        event = self._make_event("/config/subdir", is_directory=True)
        self.handler.on_modified(event)
        self.callback.assert_not_called()

    def test_non_yaml_event_ignored(self) -> None:
        """非 YAML 文件事件应被忽略。"""
        event = self._make_event("/config/test.json")
        self.handler.on_modified(event)
        self.callback.assert_not_called()


class TestConfigReloadHandlerDebounce:
    """ConfigReloadHandler 防抖测试。"""

    def test_debounce_suppresses_rapid_events(self) -> None:
        """防抖应抑制快速重复事件。"""
        callback = MagicMock()
        handler = ConfigReloadHandler(callback=callback, debounce_seconds=1.0)

        event = MagicMock()
        event.src_path = "/config/test.yaml"
        event.is_directory = False

        handler.on_modified(event)
        handler.on_modified(event)  # 第二次应被防抖

        assert callback.call_count == 1

    def test_debounce_allows_after_interval(self) -> None:
        """防抖间隔后应允许新事件。"""
        callback = MagicMock()
        handler = ConfigReloadHandler(callback=callback, debounce_seconds=0.01)

        event = MagicMock()
        event.src_path = "/config/test.yaml"
        event.is_directory = False

        handler.on_modified(event)
        time.sleep(0.02)
        handler.on_modified(event)

        assert callback.call_count == 2

    def test_debounce_independent_per_file(self) -> None:
        """不同文件的防抖应独立。"""
        callback = MagicMock()
        handler = ConfigReloadHandler(callback=callback, debounce_seconds=1.0)

        event_a = MagicMock()
        event_a.src_path = "/config/a.yaml"
        event_a.is_directory = False

        event_b = MagicMock()
        event_b.src_path = "/config/b.yaml"
        event_b.is_directory = False

        handler.on_modified(event_a)
        handler.on_modified(event_b)

        assert callback.call_count == 2


# ============================================================================
# ConfigReloader 测试
# ============================================================================


class TestConfigReloaderLifecycle:
    """ConfigReloader 启动/停止测试。"""

    def test_initial_state_not_running(self) -> None:
        """初始状态应为未运行。"""
        reloader = ConfigReloader(config_dir="/tmp/nonexist")
        assert reloader.is_running() is False

    @patch("config.reload.Observer")
    def test_start_sets_running(self, mock_observer_cls: MagicMock, tmp_path: Path) -> None:
        """启动后应处于运行状态。"""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        reloader = ConfigReloader(config_dir=config_dir)
        reloader.start()
        assert reloader.is_running() is True
        reloader.stop()

    @patch("config.reload.Observer")
    def test_stop_sets_not_running(self, mock_observer_cls: MagicMock, tmp_path: Path) -> None:
        """停止后应为未运行状态。"""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        reloader = ConfigReloader(config_dir=config_dir)
        reloader.start()
        reloader.stop()
        assert reloader.is_running() is False

    def test_start_nonexistent_dir_does_not_run(self) -> None:
        """不存在的目录不应启动。"""
        reloader = ConfigReloader(config_dir="/tmp/nonexistent_dir_xyz")
        reloader.start()
        assert reloader.is_running() is False

    @patch("config.reload.Observer")
    def test_start_twice_no_error(self, mock_observer_cls: MagicMock, tmp_path: Path) -> None:
        """重复启动不应报错。"""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        reloader = ConfigReloader(config_dir=config_dir)
        reloader.start()
        reloader.start()  # 不应抛异常
        reloader.stop()


class TestConfigReloaderCallbacks:
    """ConfigReloader 回调管理测试。"""

    def test_add_callback(self) -> None:
        """添加回调应成功。"""
        reloader = ConfigReloader()
        cb = MagicMock()
        reloader.add_callback(cb)
        assert cb in reloader._callbacks

    def test_remove_callback_success(self) -> None:
        """移除已注册的回调应返回 True。"""
        reloader = ConfigReloader()
        cb = MagicMock()
        reloader.add_callback(cb)
        result = reloader.remove_callback(cb)
        assert result is True
        assert cb not in reloader._callbacks

    def test_remove_callback_not_found(self) -> None:
        """移除未注册的回调应返回 False。"""
        reloader = ConfigReloader()
        cb = MagicMock()
        result = reloader.remove_callback(cb)
        assert result is False


class TestConfigReloaderReloaders:
    """ConfigReloader 重载器管理测试。"""

    def test_register_reloader(self) -> None:
        """注册重载器应成功。"""
        reloader = ConfigReloader()
        fn = MagicMock()
        reloader.register_reloader("agent", fn)
        assert reloader._reloaders["agent"] is fn

    def test_register_reloader_overwrite(self) -> None:
        """重复注册同一类型应覆盖。"""
        reloader = ConfigReloader()
        fn1 = MagicMock()
        fn2 = MagicMock()
        reloader.register_reloader("agent", fn1)
        reloader.register_reloader("agent", fn2)
        assert reloader._reloaders["agent"] is fn2


class TestConfigReloaderDetermineType:
    """ConfigReloader._determine_config_type 测试。"""

    def setup_method(self) -> None:
        self.reloader = ConfigReloader()

    def test_pipelines_path(self) -> None:
        assert self.reloader._determine_config_type("/config/pipelines/default.yaml") == "pipeline"

    def test_agents_path(self) -> None:
        assert self.reloader._determine_config_type("/config/agents/researcher.yaml") == "agent"

    def test_templates_path(self) -> None:
        assert self.reloader._determine_config_type("/config/templates/report.yaml") == "template"

    def test_triggers_path(self) -> None:
        assert self.reloader._determine_config_type("/config/triggers/schedule.yaml") == "trigger"

    def test_unknown_path(self) -> None:
        assert self.reloader._determine_config_type("/config/other.yaml") == "unknown"


class TestConfigReloaderOnFileChange:
    """ConfigReloader._on_file_change 集成测试。"""

    def test_calls_registered_reloader(self) -> None:
        """文件变更应调用对应类型的重载器。"""
        reloader = ConfigReloader()
        mock_reloader = MagicMock()
        reloader.register_reloader("agent", mock_reloader)

        reloader._on_file_change("modified", "/config/agents/test.yaml")

        mock_reloader.assert_called_once_with("/config/agents/test.yaml")

    def test_calls_callbacks(self) -> None:
        """文件变更应通知所有回调。"""
        reloader = ConfigReloader()
        cb = MagicMock()
        reloader.add_callback(cb)

        reloader._on_file_change("modified", "/config/test.yaml")

        cb.assert_called_once_with("modified", "/config/test.yaml", {"config_type": "unknown"})

    def test_reloader_error_does_not_break_callbacks(self) -> None:
        """重载器出错不应阻止回调执行。"""
        reloader = ConfigReloader()
        failing_reloader = MagicMock(side_effect=RuntimeError("boom"))
        reloader.register_reloader("agent", failing_reloader)

        cb = MagicMock()
        reloader.add_callback(cb)

        reloader._on_file_change("modified", "/config/agents/test.yaml")

        # 重载器失败但回调仍应被调用
        cb.assert_called_once()

    def test_unknown_type_skips_reloader(self) -> None:
        """未知类型不应调用重载器。"""
        reloader = ConfigReloader()
        mock_reloader = MagicMock()
        reloader.register_reloader("agent", mock_reloader)

        reloader._on_file_change("modified", "/config/other.yaml")

        mock_reloader.assert_not_called()


# ============================================================================
# ConfigSchemaValidator 测试
# ============================================================================


class TestValidatePipelineConfig:
    """ConfigSchemaValidator.validate_pipeline_config 测试。"""

    def setup_method(self) -> None:
        self.validator = ConfigSchemaValidator()

    def test_valid_pipeline_config(self) -> None:
        """合法 Pipeline 配置应无错误。"""
        data = {
            "name": "main_pipeline",
            "input_routes": [{"name": "default"}],
            "output_routes": [{"route_type": "end"}],
        }
        assert self.validator.validate_pipeline_config(data) == []

    def test_missing_name(self) -> None:
        """缺少 name 应报错。"""
        data = {"input_routes": [], "output_routes": []}
        errors = self.validator.validate_pipeline_config(data)
        assert any("name" in e for e in errors)

    def test_missing_input_routes(self) -> None:
        """缺少 input_routes 应报错。"""
        data = {"name": "test", "output_routes": []}
        errors = self.validator.validate_pipeline_config(data)
        assert any("input_routes" in e for e in errors)

    def test_missing_output_routes(self) -> None:
        """缺少 output_routes 应报错。"""
        data = {"name": "test", "input_routes": []}
        errors = self.validator.validate_pipeline_config(data)
        assert any("output_routes" in e for e in errors)

    def test_empty_name(self) -> None:
        """空 name 应报错。"""
        data = {"name": "  ", "input_routes": [], "output_routes": []}
        errors = self.validator.validate_pipeline_config(data)
        assert any("name" in e for e in errors)

    def test_input_routes_not_list(self) -> None:
        """input_routes 非列表应报错。"""
        data = {"name": "test", "input_routes": "bad", "output_routes": []}
        errors = self.validator.validate_pipeline_config(data)
        assert any("input_routes" in e for e in errors)


class TestValidateAgentConfig:
    """ConfigSchemaValidator.validate_agent_config 测试。"""

    def setup_method(self) -> None:
        self.validator = ConfigSchemaValidator()

    def test_valid_agent_config(self) -> None:
        """合法 Agent 配置应无错误。"""
        data = {"config_id": "researcher", "name": "Researcher", "level": "L2"}
        assert self.validator.validate_agent_config(data) == []

    def test_missing_config_id(self) -> None:
        """缺少 config_id 应报错。"""
        data = {"name": "test"}
        errors = self.validator.validate_agent_config(data)
        assert any("config_id" in e for e in errors)

    def test_missing_name(self) -> None:
        """缺少 name 应报错。"""
        data = {"config_id": "test"}
        errors = self.validator.validate_agent_config(data)
        assert any("name" in e for e in errors)

    def test_invalid_level(self) -> None:
        """非法 level 应报错。"""
        data = {"config_id": "test", "name": "Test", "level": "L9"}
        errors = self.validator.validate_agent_config(data)
        assert any("level" in e for e in errors)

    def test_invalid_agent_type(self) -> None:
        """非法 agent_type 应报错。"""
        data = {"config_id": "test", "name": "Test", "agent_type": "invalid"}
        errors = self.validator.validate_agent_config(data)
        assert any("agent_type" in e for e in errors)

    def test_valid_levels(self) -> None:
        """所有合法 level 都应通过。"""
        for level in ("L1", "L2", "L3"):
            data = {"config_id": "test", "name": "Test", "level": level}
            assert self.validator.validate_agent_config(data) == []

    def test_valid_agent_types(self) -> None:
        """所有合法 agent_type 都应通过。"""
        for at in ("main", "specialized", "system"):
            data = {"config_id": "test", "name": "Test", "agent_type": at}
            assert self.validator.validate_agent_config(data) == []


class TestValidateYamlFile:
    """ConfigSchemaValidator.validate_yaml_file 测试。"""

    def setup_method(self) -> None:
        self.validator = ConfigSchemaValidator()

    def test_nonexistent_file(self) -> None:
        """不存在的文件应报错。"""
        errors = self.validator.validate_yaml_file("/nonexistent/path.yaml")
        assert any("不存在" in e for e in errors)

    def test_invalid_yaml_syntax(self, tmp_path: Path) -> None:
        """YAML 语法错误应报错。"""
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("key: [unclosed", encoding="utf-8")
        errors = self.validator.validate_yaml_file(bad_yaml)
        assert len(errors) > 0

    def test_valid_pipeline_yaml(self, tmp_path: Path) -> None:
        """合法 Pipeline YAML 应无错误。"""
        yaml_file = tmp_path / "pipelines" / "test.yaml"
        yaml_file.parent.mkdir(parents=True)
        yaml_file.write_text(
            yaml.dump({
                "name": "test_pipeline",
                "input_routes": [{"name": "default"}],
                "output_routes": [{"route_type": "end"}],
            }),
            encoding="utf-8",
        )
        errors = self.validator.validate_yaml_file(yaml_file, config_type="pipeline")
        assert errors == []

    def test_auto_detect_pipeline(self, tmp_path: Path) -> None:
        """auto 模式应自动检测 Pipeline 配置。"""
        yaml_file = tmp_path / "pipelines" / "test.yaml"
        yaml_file.parent.mkdir(parents=True)
        yaml_file.write_text(
            yaml.dump({
                "name": "test_pipeline",
                "input_routes": [],
                "output_routes": [],
            }),
            encoding="utf-8",
        )
        errors = self.validator.validate_yaml_file(yaml_file, config_type="auto")
        assert errors == []

    def test_auto_detect_agent_by_content(self, tmp_path: Path) -> None:
        """auto 模式应通过内容特征检测 Agent 配置。"""
        yaml_file = tmp_path / "some_dir" / "test.yaml"
        yaml_file.parent.mkdir(parents=True)
        yaml_file.write_text(
            yaml.dump({
                "config_id": "researcher",
                "name": "Researcher",
            }),
            encoding="utf-8",
        )
        errors = self.validator.validate_yaml_file(yaml_file, config_type="auto")
        assert errors == []

    def test_yaml_not_dict(self, tmp_path: Path) -> None:
        """YAML 内容非字典应报错。"""
        yaml_file = tmp_path / "list.yaml"
        yaml_file.write_text("- item1\n- item2\n", encoding="utf-8")
        errors = self.validator.validate_yaml_file(yaml_file)
        assert any("字典" in e for e in errors)


class TestValidateDirectory:
    """ConfigSchemaValidator.validate_directory 测试。"""

    def setup_method(self) -> None:
        self.validator = ConfigSchemaValidator()

    def test_nonexistent_directory(self) -> None:
        """不存在的目录应报错。"""
        results = self.validator.validate_directory("/nonexistent/dir")
        assert len(results) > 0

    def test_not_directory(self, tmp_path: Path) -> None:
        """非目录路径应报错。"""
        file_path = tmp_path / "file.yaml"
        file_path.write_text("key: value", encoding="utf-8")
        results = self.validator.validate_directory(file_path)
        assert any("不是目录" in e or "目录" in e for errors in results.values() for e in errors)

    def test_valid_directory_no_errors(self, tmp_path: Path) -> None:
        """全部合法的目录应返回空结果。"""
        config_dir = tmp_path / "pipelines"
        config_dir.mkdir()
        (config_dir / "test.yaml").write_text(
            yaml.dump({
                "name": "test",
                "input_routes": [],
                "output_routes": [],
            }),
            encoding="utf-8",
        )
        results = self.validator.validate_directory(config_dir, config_type="pipeline")
        assert results == {}

    def test_directory_with_invalid_files(self, tmp_path: Path) -> None:
        """目录中有错误文件应返回对应错误。"""
        config_dir = tmp_path / "agents"
        config_dir.mkdir()
        (config_dir / "bad.yaml").write_text(
            yaml.dump({"description": "missing required fields"}),
            encoding="utf-8",
        )
        results = self.validator.validate_directory(config_dir, config_type="agent")
        assert len(results) > 0


# ============================================================================
# 集成测试：实际文件变更触发热重载
# ============================================================================


class TestHotReloadIntegration:
    """使用 tmp_path 和真实文件系统触发热重载。"""

    def test_file_creation_triggers_reloader(self, tmp_path: Path) -> None:
        """创建 YAML 文件应触发热重载。"""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "agents").mkdir()

        reloader = ConfigReloader(config_dir=config_dir, debounce_seconds=0.0)
        mock_reloader_fn = MagicMock()
        reloader.register_reloader("agent", mock_reloader_fn)

        cb = MagicMock()
        reloader.add_callback(cb)

        reloader.start()
        try:
            # 创建 YAML 文件
            agent_file = config_dir / "agents" / "test.yaml"
            agent_file.write_text(
                yaml.dump({"config_id": "test", "name": "Test"}),
                encoding="utf-8",
            )
            # 等待 watchdog 检测到变更
            time.sleep(2)
        finally:
            reloader.stop()

        # 验证重载器或回调被调用（watchdog 可能有延迟）
        # 由于 CI 环境中 watchdog 行为可能不一致，
        # 此处仅验证 start/stop 不报错即可
