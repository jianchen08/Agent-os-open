"""场景管理系统单元测试。

覆盖 SceneManager 的核心功能：创建、切换、删除、列表、查询场景，
以及场景模板库和持久化逻辑。

测试场景覆盖：
- AC1: SceneManager 支持 create_scene/switch_scene/delete_scene/list_scenes/get_scene
- AC2: 场景支持布局引擎（grid、split、stack、tab 等布局模式）
- AC3: 场景支持嵌入 AudioPlayer 和 ImageGallery 组件
- AC4: 场景模板库包含至少3个预设场景
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scene import SceneManager, ScenePersistence
from scene.models import (
    Scene,
    SceneCreateRequest,
    SceneLayoutConfig,
    SceneLayoutType,
    SceneState,
    SceneTemplate,
    SceneUpdateRequest,
    SceneWidgetConfig,
)
from scene.templates import PRESET_TEMPLATES, get_template, list_templates


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def tmp_storage(tmp_path: Path) -> Path:
    """创建临时存储目录。"""
    storage = tmp_path / "test_scenes"
    storage.mkdir()
    return storage


@pytest.fixture
def persistence(tmp_storage: Path) -> ScenePersistence:
    """创建持久化实例。"""
    return ScenePersistence(storage_path=str(tmp_storage))


@pytest.fixture
def manager(persistence: ScenePersistence) -> SceneManager:
    """创建场景管理器实例。"""
    return SceneManager(persistence=persistence)


# ============================================================
# AC1: 场景管理器 CRUD + 切换
# ============================================================


class TestSceneManagerCreate:
    """场景创建测试。"""

    def test_create_scene(self, manager: SceneManager) -> None:
        """创建场景，验证返回 Scene 对象包含正确字段。"""
        scene = manager.create_scene(name="测试场景")

        assert scene.id  # 自动生成 UUID
        assert scene.name == "测试场景"
        assert scene.description == ""
        assert scene.template_id is None
        assert scene.is_active is False
        assert scene.created_at
        assert scene.updated_at

    def test_create_scene_with_description(self, manager: SceneManager) -> None:
        """创建场景时指定描述。"""
        scene = manager.create_scene(
            name="工作台",
            description="我的工作台场景",
        )

        assert scene.name == "工作台"
        assert scene.description == "我的工作台场景"

    def test_create_scene_with_template(self, manager: SceneManager) -> None:
        """基于模板创建场景，验证继承模板布局和组件。"""
        scene = manager.create_scene(
            name="聊天工作台",
            template_id="chat_workspace",
        )

        assert scene.template_id == "chat_workspace"
        assert scene.layout.type == SceneLayoutType.SPLIT
        assert len(scene.widgets) == 2
        assert scene.widgets[0].widget_type == "chat"
        assert scene.widgets[1].widget_type == "workspace"

    def test_create_scene_invalid_template(self, manager: SceneManager) -> None:
        """使用不存在的模板 ID 创建场景应抛出 ValueError。"""
        with pytest.raises(ValueError, match="模板不存在"):
            manager.create_scene(name="测试", template_id="nonexistent")

    def test_create_scene_with_custom_layout(self, manager: SceneManager) -> None:
        """创建场景时指定自定义布局覆盖模板默认布局。"""
        layout = SceneLayoutConfig(
            type=SceneLayoutType.GRID,
            columns=3,
        )
        scene = manager.create_scene(name="网格场景", layout=layout)

        assert scene.layout.type == SceneLayoutType.GRID
        assert scene.layout.columns == 3

    def test_create_scene_with_custom_widgets(self, manager: SceneManager) -> None:
        """创建场景时指定自定义组件列表覆盖模板默认组件。"""
        widgets: list[dict[str, Any]] = [
            {"widget_type": "chart", "props": {"chartType": "bar"}, "position": 0},
            {"widget_type": "table", "props": {}, "position": 1},
        ]
        scene = manager.create_scene(name="图表场景", widgets=widgets)

        assert len(scene.widgets) == 2
        assert scene.widgets[0].widget_type == "chart"


class TestSceneManagerSwitch:
    """场景切换测试。"""

    def test_switch_scene(self, manager: SceneManager) -> None:
        """切换场景，验证新场景 is_active=True，旧场景 is_active=False。"""
        s1 = manager.create_scene(name="场景1")
        s2 = manager.create_scene(name="场景2")

        # 先激活 s1
        result1 = manager.switch_scene(s1.id)
        assert result1.is_active is True
        assert result1.id == s1.id

        active = manager.get_active_scene()
        assert active is not None
        assert active.id == s1.id

        # 再切换到 s2，验证 s1 变为非活跃
        result2 = manager.switch_scene(s2.id)
        assert result2.is_active is True
        assert result2.id == s2.id

        # 验证旧场景 s1 不再活跃
        prev = manager.get_scene(s1.id)
        assert prev is not None
        assert prev.is_active is False

    def test_switch_scene_auto_save_previous(self, manager: SceneManager) -> None:
        """切换场景时自动保存前一场景（取消活跃标记）。"""
        s1 = manager.create_scene(name="场景A")
        s2 = manager.create_scene(name="场景B")

        manager.switch_scene(s1.id)
        manager.switch_scene(s2.id)

        # 验证前一场景不再活跃
        prev = manager.get_scene(s1.id)
        assert prev is not None
        assert prev.is_active is False

        # 验证新场景是活跃的
        current = manager.get_scene(s2.id)
        assert current is not None
        assert current.is_active is True

    def test_switch_nonexistent_scene(self, manager: SceneManager) -> None:
        """切换不存在的场景应抛出 ValueError。"""
        with pytest.raises(ValueError, match="场景不存在"):
            manager.switch_scene("nonexistent-id")


class TestSceneManagerDelete:
    """场景删除测试。"""

    def test_delete_scene(self, manager: SceneManager) -> None:
        """删除场景，验证不再存在。"""
        scene = manager.create_scene(name="待删除")
        result = manager.delete_scene(scene.id)

        assert result is True
        assert manager.get_scene(scene.id) is None

    def test_delete_nonexistent_scene(self, manager: SceneManager) -> None:
        """删除不存在的场景返回 False。"""
        result = manager.delete_scene("nonexistent-id")
        assert result is False

    def test_delete_active_scene(self, manager: SceneManager) -> None:
        """删除活跃场景后 _active_scene_id 被清除。"""
        scene = manager.create_scene(name="活跃场景")
        manager.switch_scene(scene.id)

        # 确认场景是活跃的
        assert manager.get_active_scene() is not None

        manager.delete_scene(scene.id)

        # 验证活跃场景被清除
        assert manager.get_active_scene() is None


class TestSceneManagerListAndGet:
    """场景列表和查询测试。"""

    def test_list_scenes_empty(self, manager: SceneManager) -> None:
        """空列表。"""
        scenes = manager.list_scenes()
        assert scenes == []

    def test_list_scenes(self, manager: SceneManager) -> None:
        """创建多个场景后列表返回全部。"""
        manager.create_scene(name="场景1")
        manager.create_scene(name="场景2")
        manager.create_scene(name="场景3")

        scenes = manager.list_scenes()
        assert len(scenes) == 3

        names = {s.name for s in scenes}
        assert names == {"场景1", "场景2", "场景3"}

    def test_get_scene(self, manager: SceneManager) -> None:
        """获取场景详情，验证字段正确。"""
        created = manager.create_scene(name="详情场景", description="场景描述")
        found = manager.get_scene(created.id)

        assert found is not None
        assert found.name == "详情场景"
        assert found.description == "场景描述"
        assert found.id == created.id
        assert found.created_at == created.created_at

    def test_get_nonexistent_scene(self, manager: SceneManager) -> None:
        """获取不存在的场景返回 None。"""
        result = manager.get_scene("nonexistent")
        assert result is None


class TestSceneManagerUpdate:
    """场景更新测试。"""

    def test_update_scene_name(self, manager: SceneManager) -> None:
        """更新场景名称。"""
        scene = manager.create_scene(name="旧名称")

        updated = manager.update_scene(
            scene.id,
            SceneUpdateRequest(name="新名称"),
        )

        assert updated is not None
        assert updated.name == "新名称"

    def test_update_scene_description(self, manager: SceneManager) -> None:
        """更新场景描述。"""
        scene = manager.create_scene(name="场景")

        updated = manager.update_scene(
            scene.id,
            SceneUpdateRequest(description="新描述"),
        )

        assert updated is not None
        assert updated.description == "新描述"

    def test_update_scene_name_and_description(self, manager: SceneManager) -> None:
        """同时更新场景名称和描述。"""
        scene = manager.create_scene(name="旧名称", description="旧描述")

        updated = manager.update_scene(
            scene.id,
            SceneUpdateRequest(name="新名称", description="新描述"),
        )

        assert updated is not None
        assert updated.name == "新名称"
        assert updated.description == "新描述"
        # 验证 updated_at 被更新
        assert updated.updated_at >= scene.updated_at

    def test_update_nonexistent_scene(self, manager: SceneManager) -> None:
        """更新不存在的场景返回 None。"""
        result = manager.update_scene(
            "nonexistent",
            SceneUpdateRequest(name="测试"),
        )
        assert result is None


# ============================================================
# AC2 + AC3: 布局引擎 & 嵌入组件
# ============================================================


class TestSceneLayoutAndWidgets:
    """布局引擎和组件嵌入测试。"""

    def test_grid_layout(self) -> None:
        """验证 grid 布局配置。"""
        config = SceneLayoutConfig(type=SceneLayoutType.GRID, columns=4)
        assert config.type == SceneLayoutType.GRID
        assert config.columns == 4

    def test_split_layout(self) -> None:
        """验证 split 布局配置。"""
        config = SceneLayoutConfig(
            type=SceneLayoutType.SPLIT,
            direction="vertical",
            ratio=[1, 2],
        )
        assert config.type == SceneLayoutType.SPLIT
        assert config.direction == "vertical"
        assert config.ratio == [1, 2]

    def test_stack_layout(self) -> None:
        """验证 stack 布局配置。"""
        config = SceneLayoutConfig(type=SceneLayoutType.STACK)
        assert config.type == SceneLayoutType.STACK

    def test_tab_layout(self) -> None:
        """验证 tab 布局配置。"""
        config = SceneLayoutConfig(type=SceneLayoutType.TAB, default_tab=1)
        assert config.type == SceneLayoutType.TAB
        assert config.default_tab == 1

    def test_audio_player_widget(self) -> None:
        """验证 AudioPlayer 组件配置。"""
        widget = SceneWidgetConfig(
            widget_type="audio_player",
            props={"autoplay": False, "showControls": True},
            position=0,
        )
        assert widget.widget_type == "audio_player"
        assert widget.props["showControls"] is True

    def test_image_gallery_widget(self) -> None:
        """验证 ImageGallery 组件配置。"""
        widget = SceneWidgetConfig(
            widget_type="image_gallery",
            props={"columns": 3, "enableLightbox": True},
            position=0,
        )
        assert widget.widget_type == "image_gallery"
        assert widget.props["columns"] == 3


# ============================================================
# AC4: 场景模板库
# ============================================================


class TestSceneTemplates:
    """场景模板库测试。"""

    def test_list_templates(self) -> None:
        """验证返回至少 3 个预设模板。"""
        templates = list_templates()
        assert len(templates) >= 3

    def test_get_template_chat_workspace(self) -> None:
        """验证 chat_workspace 模板存在且包含正确组件。"""
        template = get_template("chat_workspace")
        assert template is not None
        assert template.name == "聊天工作台"
        assert template.layout.type == SceneLayoutType.SPLIT
        assert len(template.widgets) == 2
        widget_types = [w.widget_type for w in template.widgets]
        assert "chat" in widget_types
        assert "workspace" in widget_types

    def test_get_template_media_gallery(self) -> None:
        """验证 media_gallery 模板包含 image_gallery 和 audio_player 组件。"""
        template = get_template("media_gallery")
        assert template is not None
        assert template.name == "媒体展示"
        assert template.layout.type == SceneLayoutType.STACK
        widget_types = [w.widget_type for w in template.widgets]
        assert "image_gallery" in widget_types
        assert "audio_player" in widget_types

    def test_get_template_dashboard(self) -> None:
        """验证 dashboard 模板包含 chart、table、status_card 组件。"""
        template = get_template("dashboard")
        assert template is not None
        assert template.name == "仪表盘"
        assert template.layout.type == SceneLayoutType.GRID
        assert template.layout.columns == 3
        widget_types = [w.widget_type for w in template.widgets]
        assert "chart" in widget_types
        assert "table" in widget_types
        assert "status_card" in widget_types

    def test_get_nonexistent_template(self) -> None:
        """获取不存在的模板返回 None。"""
        assert get_template("nonexistent") is None

    def test_template_ids_are_unique(self) -> None:
        """验证所有模板 ID 唯一。"""
        ids = [t.id for t in PRESET_TEMPLATES]
        assert len(ids) == len(set(ids))


# ============================================================
# 持久化测试
# ============================================================


class TestScenePersistence:
    """场景持久化测试。"""

    def test_save_and_load_scene(self, persistence: ScenePersistence) -> None:
        """保存后加载，验证数据完整。"""
        scene = Scene(name="持久化测试", description="测试描述")
        persistence.save_scene(scene)

        loaded = persistence.load_scenes()
        assert len(loaded) == 1
        assert loaded[0].name == "持久化测试"
        assert loaded[0].description == "测试描述"
        assert loaded[0].id == scene.id

    def test_delete_scene_persistence(self, persistence: ScenePersistence) -> None:
        """删除后加载，验证不存在。"""
        scene = Scene(name="待删除")
        persistence.save_scene(scene)

        result = persistence.delete_scene(scene.id)
        assert result is True
        assert persistence.load_scenes() == []

    def test_load_empty(self, tmp_storage: Path) -> None:
        """无数据时加载，验证返回空列表。"""
        empty_persistence = ScenePersistence(storage_path=str(tmp_storage / "empty"))
        scenes = empty_persistence.load_scenes()
        assert scenes == []

    def test_get_scene(self, persistence: ScenePersistence) -> None:
        """按 ID 获取场景。"""
        scene = Scene(name="查询测试")
        persistence.save_scene(scene)

        found = persistence.get_scene(scene.id)
        assert found is not None
        assert found.name == "查询测试"

    def test_get_nonexistent_scene(self, persistence: ScenePersistence) -> None:
        """获取不存在的场景返回 None。"""
        assert persistence.get_scene("nonexistent") is None

    def test_json_file_format(self, tmp_storage: Path) -> None:
        """验证 JSON 文件格式正确。"""
        persistence = ScenePersistence(storage_path=str(tmp_storage))
        scene = Scene(name="JSON测试")
        persistence.save_scene(scene)

        # 直接读取 JSON 文件验证格式
        json_file = tmp_storage / "scenes.json"
        assert json_file.exists()

        content = json.loads(json_file.read_text(encoding="utf-8"))
        assert "scenes" in content
        assert scene.id in content["scenes"]

    def test_save_all_scenes(self, persistence: ScenePersistence) -> None:
        """批量保存所有场景。"""
        scenes = [
            Scene(name="场景A"),
            Scene(name="场景B"),
        ]
        persistence.save_all_scenes(scenes)

        loaded = persistence.load_scenes()
        assert len(loaded) == 2

    def test_overwrite_scene(self, persistence: ScenePersistence) -> None:
        """保存同一 ID 场景会覆盖旧数据。"""
        scene = Scene(name="原始名称")
        persistence.save_scene(scene)

        scene.name = "更新名称"
        persistence.save_scene(scene)

        loaded = persistence.load_scenes()
        assert len(loaded) == 1
        assert loaded[0].name == "更新名称"


# ============================================================
# 数据模型测试
# ============================================================


class TestSceneModels:
    """数据模型测试。"""

    def test_scene_model_defaults(self) -> None:
        """验证 Scene 默认字段值。"""
        scene = Scene(name="测试")

        assert scene.id  # UUID 自动生成
        assert scene.description == ""
        assert scene.template_id is None
        assert scene.layout.type == SceneLayoutType.SPLIT
        assert scene.widgets == []
        assert scene.is_active is False
        assert scene.state.active_widget_id is None
        assert scene.state.scroll_position == {"x": 0, "y": 0}
        assert scene.created_at
        assert scene.updated_at

    def test_scene_layout_config_grid(self) -> None:
        """验证 grid 布局配置枚举值。"""
        config = SceneLayoutConfig(type=SceneLayoutType.GRID, columns=4)
        assert config.type == SceneLayoutType.GRID
        assert config.columns == 4

    def test_scene_layout_config_split(self) -> None:
        """验证 split 布局配置。"""
        config = SceneLayoutConfig(
            type=SceneLayoutType.SPLIT,
            direction="vertical",
            ratio=[1, 1],
        )
        assert config.type == SceneLayoutType.SPLIT
        assert config.direction == "vertical"
        assert config.ratio == [1, 1]

    def test_scene_layout_config_stack(self) -> None:
        """验证 stack 布局配置。"""
        config = SceneLayoutConfig(type=SceneLayoutType.STACK)
        assert config.type == SceneLayoutType.STACK

    def test_scene_layout_config_tab(self) -> None:
        """验证 tab 布局配置。"""
        config = SceneLayoutConfig(type=SceneLayoutType.TAB, default_tab=2)
        assert config.type == SceneLayoutType.TAB
        assert config.default_tab == 2

    def test_scene_layout_config_default_direction(self) -> None:
        """验证布局配置默认分割方向为 horizontal。"""
        config = SceneLayoutConfig()
        assert config.direction == "horizontal"

    def test_scene_widget_config(self) -> None:
        """验证组件配置结构。"""
        widget = SceneWidgetConfig(
            widget_type="audio_player",
            props={"src": "test.mp3", "title": "测试音频"},
            data_source="media_lib",
            position=1,
        )
        assert widget.widget_type == "audio_player"
        assert widget.props["src"] == "test.mp3"
        assert widget.data_source == "media_lib"
        assert widget.position == 1

    def test_scene_widget_config_defaults(self) -> None:
        """验证组件配置默认值。"""
        widget = SceneWidgetConfig(widget_type="chart")
        assert widget.props == {}
        assert widget.data_source is None
        assert widget.position == 0

    def test_scene_state_default(self) -> None:
        """验证 SceneState 默认值。"""
        state = SceneState()
        assert state.active_widget_id is None
        assert state.scroll_position == {"x": 0, "y": 0}
        assert state.widget_states == {}
        assert state.custom_data == {}

    def test_scene_template_model(self) -> None:
        """验证 SceneTemplate 模型。"""
        template = SceneTemplate(
            id="test_template",
            name="测试模板",
            description="测试用",
            icon="🧪",
        )
        assert template.id == "test_template"
        assert template.icon == "🧪"
        assert template.category == "general"

    def test_scene_create_request_validation(self) -> None:
        """验证创建请求校验。"""
        req = SceneCreateRequest(name="测试场景")
        assert req.name == "测试场景"
        assert req.template_id is None
        assert req.description == ""

    def test_scene_update_request_partial(self) -> None:
        """验证更新请求支持部分更新。"""
        req = SceneUpdateRequest(name="新名称")
        assert req.name == "新名称"
        assert req.description is None
        assert req.layout is None

    def test_scene_layout_type_enum_values(self) -> None:
        """验证 SceneLayoutType 枚举包含所有布局类型。"""
        assert SceneLayoutType.GRID == "grid"
        assert SceneLayoutType.SPLIT == "split"
        assert SceneLayoutType.STACK == "stack"
        assert SceneLayoutType.TAB == "tab"
        assert len(SceneLayoutType) == 4

    def test_scene_model_serialization(self) -> None:
        """验证 Scene 模型可正确序列化和反序列化。"""
        scene = Scene(
            name="序列化测试",
            description="测试序列化",
            layout=SceneLayoutConfig(type=SceneLayoutType.GRID, columns=3),
            widgets=[
                SceneWidgetConfig(widget_type="chart", position=0),
            ],
        )
        data = scene.model_dump(mode="json")
        restored = Scene.model_validate(data)

        assert restored.name == scene.name
        assert restored.layout.type == SceneLayoutType.GRID
        assert len(restored.widgets) == 1
        assert restored.id == scene.id
