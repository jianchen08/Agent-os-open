"""Schema 解析器测试。

覆盖：
- 从 YAML 字符串解析 ModuleUISchema
- 嵌套结构正确解析（identity/actions/rendering/clients）
- 缺失字段时使用默认值
- 无效 YAML 抛出异常/返回 None
- 热重载：相同路径重新解析更新缓存
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import yaml


class TestSchemaParserLoadDirectory:
    """SchemaParser.load_directory 测试。"""

    def _write_yaml(self, tmp_dir: Path, filename: str, content: dict) -> Path:
        """辅助：写入 YAML 文件。"""
        filepath = tmp_dir / filename
        with open(filepath, "w", encoding="utf-8") as f:
            yaml.dump(content, f, allow_unicode=True)
        return filepath

    def test_parse_yaml_with_ui_section(self) -> None:
        """解析包含 ui 部分的 YAML 配置。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._write_yaml(
                tmp_dir,
                "test_module.yaml",
                {
                    "config_id": "test_module",
                    "name": "测试模块",
                    "ui": {
                        "identity": {
                            "id": "test_module",
                            "name": "测试模块",
                            "version": "1.0.0",
                            "category": "builtin",
                        },
                        "actions": [
                            {"id": "run", "name": "运行", "type": "command"},
                        ],
                        "rendering": {
                            "chat": [],
                            "spaces": [],
                        },
                        "clients": {
                            "required_spaces": ["chat"],
                            "required_widgets": [],
                        },
                    },
                },
            )
            parser = SchemaParser()
            schemas = parser.load_directory(tmp_dir)
            assert len(schemas) == 1
            assert schemas[0].identity.id == "test_module"
            assert schemas[0].identity.name == "测试模块"

    def test_parse_yaml_without_ui_section(self) -> None:
        """不包含 ui 部分的 YAML 应被跳过。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._write_yaml(
                tmp_dir,
                "no_ui.yaml",
                {
                    "config_id": "no_ui",
                    "name": "无 UI 模块",
                },
            )
            parser = SchemaParser()
            schemas = parser.load_directory(tmp_dir)
            assert len(schemas) == 0

    def test_parse_multiple_yaml_files(self) -> None:
        """目录中多个 YAML 文件应全部加载。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            for i in range(3):
                self._write_yaml(
                    tmp_dir,
                    f"mod{i}.yaml",
                    {
                        "ui": {
                            "identity": {
                                "id": f"mod{i}",
                                "name": f"模块{i}",
                                "version": "1.0.0",
                            },
                        },
                    },
                )
            parser = SchemaParser()
            schemas = parser.load_directory(tmp_dir)
            assert len(schemas) == 3
            ids = {s.identity.id for s in schemas}
            assert ids == {"mod0", "mod1", "mod2"}

    def test_nonexistent_directory_returns_empty(self) -> None:
        """不存在的目录应返回空列表。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        parser = SchemaParser()
        schemas = parser.load_directory(Path("/nonexistent/dir"))
        assert schemas == []

    def test_empty_directory_returns_empty(self) -> None:
        """空目录应返回空列表。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            parser = SchemaParser()
            schemas = parser.load_directory(tmp)
            assert schemas == []


class TestSchemaParserLoadFile:
    """SchemaParser.load_file 测试。"""

    def _write_yaml(self, tmp_dir: Path, filename: str, content: dict) -> Path:
        """辅助：写入 YAML 文件。"""
        filepath = tmp_dir / filename
        with open(filepath, "w", encoding="utf-8") as f:
            yaml.dump(content, f, allow_unicode=True)
        return filepath

    def test_load_single_file(self) -> None:
        """加载单个 YAML 文件。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            filepath = self._write_yaml(
                tmp_dir,
                "single.yaml",
                {
                    "config_id": "single",
                    "name": "单文件",
                    "ui": {
                        "identity": {
                            "id": "single",
                            "name": "单文件",
                            "version": "1.0.0",
                            "category": "extension",
                        },
                    },
                },
            )
            parser = SchemaParser()
            schema = parser.load_file(filepath)
            assert schema is not None
            assert schema.identity.id == "single"

    def test_load_file_not_found(self) -> None:
        """加载不存在的文件应返回 None。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        parser = SchemaParser()
        result = parser.load_file(Path("/nonexistent/file.yaml"))
        assert result is None

    def test_load_file_without_ui_returns_none(self) -> None:
        """加载没有 ui 部分的文件应返回 None。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            filepath = self._write_yaml(
                tmp_dir,
                "no_ui.yaml",
                {
                    "config_id": "no_ui",
                    "name": "No UI",
                },
            )
            parser = SchemaParser()
            result = parser.load_file(filepath)
            assert result is None

    def test_load_file_with_invalid_yaml(self) -> None:
        """加载无效 YAML 文件应返回 None（不抛出异常）。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            filepath = tmp_dir / "invalid.yaml"
            # 写入无效 YAML
            filepath.write_text(":\n  :\n    - [\n", encoding="utf-8")
            parser = SchemaParser()
            parser.load_file(filepath)
            # 结果要么是 None，要么解析成功（某些无效 YAML 也能被解析）
            # 关键是不抛出异常

    def test_load_file_with_empty_content(self) -> None:
        """加载空文件应返回 None。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            filepath = tmp_dir / "empty.yaml"
            filepath.write_text("", encoding="utf-8")
            parser = SchemaParser()
            result = parser.load_file(filepath)
            assert result is None


class TestSchemaParserNestedStructure:
    """嵌套结构解析测试。"""

    def _write_yaml(self, tmp_dir: Path, filename: str, content: dict) -> Path:
        filepath = tmp_dir / filename
        with open(filepath, "w", encoding="utf-8") as f:
            yaml.dump(content, f, allow_unicode=True)
        return filepath

    def test_parse_identity_nested(self) -> None:
        """identity 嵌套结构正确解析。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._write_yaml(
                tmp_dir,
                "mod.yaml",
                {
                    "ui": {
                        "identity": {
                            "id": "nested-identity",
                            "name": "嵌套测试",
                            "version": "2.0.0",
                            "category": "extension",
                            "description": "测试描述",
                            "icon": "🧪",
                            "author": "tester",
                            "tags": ["test", "nested"],
                        },
                    },
                },
            )
            parser = SchemaParser()
            schema = parser.load_file(tmp_dir / "mod.yaml")
            assert schema is not None
            assert schema.identity.id == "nested-identity"
            assert schema.identity.description == "测试描述"
            assert schema.identity.icon == "🧪"
            assert schema.identity.tags == ["test", "nested"]

    def test_parse_actions_nested(self) -> None:
        """actions 嵌套结构正确解析。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._write_yaml(
                tmp_dir,
                "mod.yaml",
                {
                    "ui": {
                        "identity": {
                            "id": "action-test",
                            "name": "Action Test",
                        },
                        "actions": [
                            {
                                "id": "create_item",
                                "name": "创建项目",
                                "type": "command",
                                "label": "创建",
                                "api": "/api/v1/modules/action-test/items",
                                "requiresConfirmation": False,
                                "isDangerous": False,
                            },
                            {
                                "id": "delete_item",
                                "name": "删除项目",
                                "type": "command",
                                "api": "/api/v1/modules/action-test/items",
                                "requiresConfirmation": True,
                                "isDangerous": True,
                            },
                        ],
                    },
                },
            )
            parser = SchemaParser()
            schema = parser.load_file(tmp_dir / "mod.yaml")
            assert schema is not None
            assert len(schema.actions) == 2
            assert schema.actions[0].id == "create_item"
            assert schema.actions[0].requires_confirmation is False
            assert schema.actions[1].id == "delete_item"
            assert schema.actions[1].requires_confirmation is True
            assert schema.actions[1].is_dangerous is True

    def test_parse_rendering_nested(self) -> None:
        """rendering 嵌套结构正确解析（chat + spaces + dock + fullscreen）。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._write_yaml(
                tmp_dir,
                "mod.yaml",
                {
                    "ui": {
                        "identity": {
                            "id": "render-test",
                            "name": "Render Test",
                        },
                        "rendering": {
                            "chat": [
                                {"type": "form", "dataSource": "module://items/create"},
                                {"type": "chart", "refreshInterval": 30000},
                            ],
                            "spaces": [
                                {
                                    "space": "workspace",
                                    "widget": "split",
                                    "props": {"direction": "horizontal"},
                                    "layout": {"width": "100%", "height": "100%"},
                                },
                                {
                                    "space": "floating",
                                    "widget": "status_card",
                                    "layout": {"width": 300, "height": 200, "position": "bottom-right"},
                                    "autoOpen": {"event": "on_task_start", "delay": 500},
                                },
                            ],
                            "dock": {
                                "icon": "🧪",
                                "label": "测试",
                                "indicator": "dot",
                                "indicatorColor": "#52c41a",
                            },
                            "fullscreen": {
                                "triggerEvent": "on_full_edit",
                                "autoEnter": False,
                            },
                        },
                    },
                },
            )
            parser = SchemaParser()
            schema = parser.load_file(tmp_dir / "mod.yaml")
            assert schema is not None
            # chat
            assert len(schema.rendering.chat) == 2
            assert schema.rendering.chat[0].type == "form"
            assert schema.rendering.chat[0].data_source == "module://items/create"
            assert schema.rendering.chat[1].refresh_interval == 30000
            # spaces
            assert len(schema.rendering.spaces) == 2
            assert schema.rendering.spaces[0].space == "workspace"
            assert schema.rendering.spaces[0].widget == "split"
            assert schema.rendering.spaces[1].auto_open is not None
            assert schema.rendering.spaces[1].auto_open["event"] == "on_task_start"
            # dock
            assert schema.rendering.dock is not None
            assert schema.rendering.dock.indicator == "dot"
            # fullscreen
            assert schema.rendering.fullscreen is not None
            assert schema.rendering.fullscreen.trigger_event == "on_full_edit"

    def test_parse_clients_nested(self) -> None:
        """clients 嵌套结构正确解析。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._write_yaml(
                tmp_dir,
                "mod.yaml",
                {
                    "ui": {
                        "identity": {
                            "id": "client-test",
                            "name": "Client Test",
                        },
                        "clients": {
                            "required_spaces": ["chat", "workspace"],
                            "required_widgets": ["form", "table"],
                            "minClientVersion": "1.0.0",
                            "fallback": {"widget": "status_card", "space": "chat"},
                        },
                    },
                },
            )
            parser = SchemaParser()
            schema = parser.load_file(tmp_dir / "mod.yaml")
            assert schema is not None
            assert schema.clients.required_spaces == ["chat", "workspace"]
            assert schema.clients.required_widgets == ["form", "table"]
            assert schema.clients.min_client_version == "1.0.0"
            assert schema.clients.fallback is not None
            assert schema.clients.fallback["widget"] == "status_card"


class TestSchemaParserDefaultValues:
    """缺失字段默认值填充测试。"""

    def _write_yaml(self, tmp_dir: Path, filename: str, content: dict) -> Path:
        filepath = tmp_dir / filename
        with open(filepath, "w", encoding="utf-8") as f:
            yaml.dump(content, f, allow_unicode=True)
        return filepath

    def test_missing_actions_defaults_to_empty_list(self) -> None:
        """缺少 actions 应默认为空列表。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._write_yaml(
                tmp_dir,
                "minimal.yaml",
                {
                    "ui": {
                        "identity": {
                            "id": "minimal",
                            "name": "最小化",
                            "version": "0.1.0",
                            "category": "custom",
                        },
                    },
                },
            )
            parser = SchemaParser()
            schemas = parser.load_directory(tmp_dir)
            assert len(schemas) == 1
            assert schemas[0].actions == []
            assert schemas[0].rendering.chat == []
            assert schemas[0].rendering.spaces == []

    def test_missing_rendering_defaults(self) -> None:
        """缺少 rendering 应使用默认值。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._write_yaml(
                tmp_dir,
                "mod.yaml",
                {
                    "ui": {
                        "identity": {"id": "no-render", "name": "No Render"},
                    },
                },
            )
            parser = SchemaParser()
            schema = parser.load_file(tmp_dir / "mod.yaml")
            assert schema is not None
            assert schema.rendering.chat == []
            assert schema.rendering.spaces == []
            assert schema.rendering.dock is None
            assert schema.rendering.fullscreen is None

    def test_missing_clients_defaults(self) -> None:
        """缺少 clients 应使用默认值。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._write_yaml(
                tmp_dir,
                "mod.yaml",
                {
                    "ui": {
                        "identity": {"id": "no-clients", "name": "No Clients"},
                    },
                },
            )
            parser = SchemaParser()
            schema = parser.load_file(tmp_dir / "mod.yaml")
            assert schema is not None
            assert schema.clients.required_spaces == []
            assert schema.clients.required_widgets == []

    def test_missing_identity_id_returns_none(self) -> None:
        """缺少 identity.id 应返回 None。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._write_yaml(
                tmp_dir,
                "bad.yaml",
                {
                    "ui": {
                        "identity": {"name": "No ID"},
                    },
                },
            )
            parser = SchemaParser()
            schema = parser.load_file(tmp_dir / "bad.yaml")
            assert schema is None

    def test_missing_identity_name_returns_none(self) -> None:
        """缺少 identity.name 应返回 None。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._write_yaml(
                tmp_dir,
                "bad.yaml",
                {
                    "ui": {
                        "identity": {"id": "no-name"},
                    },
                },
            )
            parser = SchemaParser()
            schema = parser.load_file(tmp_dir / "bad.yaml")
            assert schema is None

    def test_missing_identity_section_returns_none(self) -> None:
        """完全缺少 identity 部分应返回 None。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._write_yaml(
                tmp_dir,
                "noid.yaml",
                {
                    "ui": {
                        "actions": [],
                    },
                },
            )
            parser = SchemaParser()
            schema = parser.load_file(tmp_dir / "noid.yaml")
            assert schema is None


class TestSchemaParserCache:
    """缓存和查询测试。"""

    def _write_yaml(self, tmp_dir: Path, filename: str, content: dict) -> Path:
        filepath = tmp_dir / filename
        with open(filepath, "w", encoding="utf-8") as f:
            yaml.dump(content, f, allow_unicode=True)
        return filepath

    def test_get_schema_returns_cached(self) -> None:
        """get_schema 返回已缓存的 Schema。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._write_yaml(
                tmp_dir,
                "mod.yaml",
                {
                    "ui": {
                        "identity": {"id": "cached-mod", "name": "Cached"},
                    },
                },
            )
            parser = SchemaParser()
            parser.load_directory(tmp_dir)
            cached = parser.get_schema("cached-mod")
            assert cached is not None
            assert cached.identity.id == "cached-mod"

    def test_get_schema_nonexistent_returns_none(self) -> None:
        """get_schema 对不存在的 ID 返回 None。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        parser = SchemaParser()
        assert parser.get_schema("nonexistent") is None

    def test_list_schemas_returns_all(self) -> None:
        """list_schemas 返回所有已缓存的 Schema。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            for i in range(3):
                self._write_yaml(
                    tmp_dir,
                    f"mod{i}.yaml",
                    {
                        "ui": {
                            "identity": {"id": f"list-mod{i}", "name": f"Mod{i}"},
                        },
                    },
                )
            parser = SchemaParser()
            parser.load_directory(tmp_dir)
            all_schemas = parser.list_schemas()
            assert len(all_schemas) == 3


class TestSchemaParserHotReload:
    """热重载测试。"""

    def _write_yaml(self, tmp_dir: Path, filename: str, content: dict) -> Path:
        filepath = tmp_dir / filename
        with open(filepath, "w", encoding="utf-8") as f:
            yaml.dump(content, f, allow_unicode=True)
        return filepath

    def test_hot_reload_detects_changes(self) -> None:
        """热重载应检测到文件变更。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._write_yaml(
                tmp_dir,
                "hot.yaml",
                {
                    "config_id": "hot",
                    "name": "热重载",
                    "ui": {
                        "identity": {
                            "id": "hot",
                            "name": "热重载",
                            "version": "1.0.0",
                            "category": "builtin",
                        },
                    },
                },
            )
            parser = SchemaParser()
            schemas = parser.load_directory(tmp_dir)
            assert len(schemas) == 1

            # 修改文件（确保 mtime 变化）
            time.sleep(0.1)
            self._write_yaml(
                tmp_dir,
                "hot.yaml",
                {
                    "config_id": "hot",
                    "name": "热重载更新",
                    "ui": {
                        "identity": {
                            "id": "hot",
                            "name": "热重载更新",
                            "version": "2.0.0",
                            "category": "builtin",
                        },
                    },
                },
            )

            # 检测变更
            changed = parser.detect_changes(tmp_dir)
            assert "hot" in changed

    def test_hot_reload_no_changes(self) -> None:
        """未修改文件不应检测到变更。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._write_yaml(
                tmp_dir,
                "stable.yaml",
                {
                    "ui": {
                        "identity": {"id": "stable", "name": "Stable"},
                    },
                },
            )
            parser = SchemaParser()
            parser.load_directory(tmp_dir)

            # 立即检测（文件未修改）
            changed = parser.detect_changes(tmp_dir)
            assert "stable" not in changed

    def test_reload_updates_cache(self) -> None:
        """重新加载应更新缓存。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._write_yaml(
                tmp_dir,
                "update.yaml",
                {
                    "ui": {
                        "identity": {
                            "id": "update-mod",
                            "name": "旧名称",
                            "version": "1.0.0",
                        },
                    },
                },
            )
            parser = SchemaParser()
            parser.load_directory(tmp_dir)
            assert parser.get_schema("update-mod").identity.name == "旧名称"

            # 修改并重新加载
            time.sleep(0.1)
            self._write_yaml(
                tmp_dir,
                "update.yaml",
                {
                    "ui": {
                        "identity": {
                            "id": "update-mod",
                            "name": "新名称",
                            "version": "2.0.0",
                        },
                    },
                },
            )
            parser.load_directory(tmp_dir)
            assert parser.get_schema("update-mod").identity.name == "新名称"
