# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""scene 插件（场景管理）单元测试。

覆盖（对齐插件目录 plugins/shared/system/scene/）：
1. ScenePersistence —— JSON 文件持久化：读写/删除/批量保存/损坏数据降级
2. SceneManager —— 创建（含模板）/切换/删除/更新/查询/活跃场景恢复
3. templates —— get_template / list_templates

测试不依赖真实内核——ScenePersistence 用 tmp_path 显式 storage_path，
不走多租户默认目录（避免污染仓库 data/）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_SYSTEM_DIR = _PLUGIN_DIR.parent  # plugins/shared/system/
if str(_SYSTEM_DIR) not in sys.path:
    sys.path.insert(0, str(_SYSTEM_DIR))


def _load_scene_package() -> Any:
    """把 scene.{models,persistence,templates,manager} 注册进 sys.modules。

    scene 是命名空间包（无 __init__.py），模块间用 ``from scene.X import``
    绝对导入，故必须用真实模块名加载并注册，保证 manager 的导入可解析。
    """
    for name in ("models", "persistence", "templates", "manager"):
        mod_name = f"scene.{name}"
        if mod_name in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
    return sys.modules


_MODS = _load_scene_package()
Scene = _MODS["scene.models"].Scene
if TYPE_CHECKING:
    from plugins.shared.system.scene.manager import SceneManager
    from plugins.shared.system.scene.persistence import ScenePersistence
else:
    ScenePersistence = _MODS["scene.persistence"].ScenePersistence
    SceneManager = _MODS["scene.manager"].SceneManager
get_template = _MODS["scene.templates"].get_template
list_templates = _MODS["scene.templates"].list_templates


def _make_persistence(tmp_path: Path) -> ScenePersistence:
    return ScenePersistence(storage_path=str(tmp_path / "scenes"))


def _make_manager(tmp_path: Path) -> SceneManager:
    return SceneManager(persistence=_make_persistence(tmp_path))


# ═══════════════════════════════════════════════════════════
# ScenePersistence：JSON 文件 CRUD
# ═══════════════════════════════════════════════════════════


class TestScenePersistence:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        """save_scene → load_scenes 往返一致。"""
        p = _make_persistence(tmp_path)
        scene = Scene(name="测试场景", description="desc")
        p.save_scene(scene)

        loaded = p.load_scenes()
        assert len(loaded) == 1
        assert loaded[0].id == scene.id
        assert loaded[0].name == "测试场景"
        assert (p.scenes_file).exists()

    def test_save_updates_existing(self, tmp_path: Path) -> None:
        """同 id 再次保存 → 更新而非新增。"""
        p = _make_persistence(tmp_path)
        scene = Scene(name="a")
        p.save_scene(scene)
        scene.name = "b"
        p.save_scene(scene)
        loaded = p.load_scenes()
        assert len(loaded) == 1
        assert loaded[0].name == "b"

    def test_get_scene(self, tmp_path: Path) -> None:
        """按 id 查询；不存在返回 None。"""
        p = _make_persistence(tmp_path)
        scene = Scene(name="x")
        p.save_scene(scene)
        assert p.get_scene(scene.id).name == "x"  # type: ignore[union-attr]
        assert p.get_scene("missing") is None

    def test_delete_scene(self, tmp_path: Path) -> None:
        """删除存在的场景返回 True，不存在的返回 False。"""
        p = _make_persistence(tmp_path)
        scene = Scene(name="del")
        p.save_scene(scene)
        assert p.delete_scene(scene.id) is True
        assert p.delete_scene(scene.id) is False
        assert p.load_scenes() == []

    def test_missing_file_loads_empty(self, tmp_path: Path) -> None:
        """scenes.json 不存在 → 空列表。"""
        p = _make_persistence(tmp_path)
        assert p.load_scenes() == []

    def test_empty_and_corrupt_file_degrades(self, tmp_path: Path) -> None:
        """空文件 / 非法 JSON → 空列表，不抛异常。"""
        p = _make_persistence(tmp_path)
        p.scenes_file.write_text("", encoding="utf-8")
        assert p.load_scenes() == []
        p.scenes_file.write_text("{not json", encoding="utf-8")
        assert p.load_scenes() == []

    def test_invalid_scene_entry_skipped(self, tmp_path: Path) -> None:
        """单条场景数据损坏 → 跳过该条，其余正常加载。"""
        p = _make_persistence(tmp_path)
        good = Scene(name="ok")
        p.save_scene(good)
        raw = json.loads(p.scenes_file.read_text(encoding="utf-8"))
        raw["scenes"]["bad"] = {"name": 123, "layout": "not-a-layout"}
        p.scenes_file.write_text(json.dumps(raw), encoding="utf-8")

        loaded = p.load_scenes()
        assert [s.id for s in loaded] == [good.id]

    def test_save_all_scenes(self, tmp_path: Path) -> None:
        """批量保存全量覆盖。"""
        p = _make_persistence(tmp_path)
        a, b = Scene(name="a"), Scene(name="b")
        p.save_all_scenes([a, b])
        ids = {s.id for s in p.load_scenes()}
        assert ids == {a.id, b.id}

    def test_storage_path_priority_env_fallback(self, tmp_path: Path, monkeypatch) -> None:
        """未传 storage_path → 环境变量 SCENES_STORAGE_DIR 生效。"""
        env_dir = tmp_path / "env-scenes"
        monkeypatch.setenv("SCENES_STORAGE_DIR", str(env_dir))
        p = ScenePersistence()
        assert p.storage_path == env_dir
        assert p.scenes_file == env_dir / "scenes.json"

    def test_tenant_data_root_fallback(self, tmp_path: Path, monkeypatch) -> None:
        """storage_path 与 env 都未设置 → 走 tenant_data_root（monkeypatch 隔离）。"""
        import scene.persistence as persistence_mod

        monkeypatch.delenv("SCENES_STORAGE_DIR", raising=False)
        target = tmp_path / "tenant-scenes"
        monkeypatch.setattr(persistence_mod, "tenant_data_root", lambda tenant, kind: target)
        p = persistence_mod.ScenePersistence()
        assert p.storage_path == target


# ═══════════════════════════════════════════════════════════
# SceneManager：生命周期
# ═══════════════════════════════════════════════════════════


class TestSceneManagerCreate:
    def test_create_scene_defaults(self, tmp_path: Path) -> None:
        """创建场景：默认布局 + 空组件，落盘。"""
        mgr = _make_manager(tmp_path)
        scene = mgr.create_scene(name="工作台")
        assert scene.name == "工作台"
        assert scene.is_active is False
        assert mgr.get_scene(scene.id) is not None

    def test_create_with_widgets(self, tmp_path: Path) -> None:
        """传入 widgets 字典 → 转为 SceneWidgetConfig。"""
        mgr = _make_manager(tmp_path)
        scene = mgr.create_scene(name="w", widgets=[{"widget_type": "chat", "position": 1}])
        assert len(scene.widgets) == 1
        assert scene.widgets[0].widget_type == "chat"
        assert scene.widgets[0].position == 1

    def test_create_with_template(self, tmp_path: Path) -> None:
        """基于模板创建：widgets/description 继承模板。"""
        mgr = _make_manager(tmp_path)
        scene = mgr.create_scene(name="t", template_id="chat_workspace")
        assert scene.template_id == "chat_workspace"
        assert len(scene.widgets) == 2
        assert scene.description == "左侧聊天面板 + 右侧工作区，适合对话驱动的任务场景"

    def test_create_template_override_widgets(self, tmp_path: Path) -> None:
        """模板 + 显式 widgets → 显式优先。"""
        mgr = _make_manager(tmp_path)
        scene = mgr.create_scene(
            name="t2",
            template_id="dashboard",
            widgets=[{"widget_type": "table", "position": 0}],
        )
        assert len(scene.widgets) == 1
        assert scene.widgets[0].widget_type == "table"

    def test_create_unknown_template_raises(self, tmp_path: Path) -> None:
        """未知模板 id → ValueError。"""
        mgr = _make_manager(tmp_path)
        with pytest.raises(ValueError, match="模板不存在"):
            mgr.create_scene(name="x", template_id="nope")


class TestSceneManagerSwitch:
    def test_switch_scene_activates_target(self, tmp_path: Path) -> None:
        """切换后目标场景 is_active=True，旧场景取消活跃。"""
        mgr = _make_manager(tmp_path)
        mgr.create_scene(name="a")
        b = mgr.create_scene(name="b")
        switched = mgr.switch_scene(b.id)
        assert switched.is_active is True
        assert mgr.get_active_scene().id == b.id  # type: ignore[union-attr]
        # 重新加载持久化：仅 b 活跃
        fresh = SceneManager(persistence=mgr._persistence)
        assert fresh.get_active_scene().id == b.id  # type: ignore[union-attr]

    def test_switch_to_same_scene(self, tmp_path: Path) -> None:
        """切换到当前活跃场景 → 幂等。"""
        mgr = _make_manager(tmp_path)
        a = mgr.create_scene(name="a")
        mgr.switch_scene(a.id)
        switched = mgr.switch_scene(a.id)
        assert switched.id == a.id

    def test_switch_missing_scene_raises(self, tmp_path: Path) -> None:
        """切换不存在的场景 → ValueError。"""
        mgr = _make_manager(tmp_path)
        with pytest.raises(ValueError, match="场景不存在"):
            mgr.switch_scene("missing")

    def test_switch_persists_previous_state(self, tmp_path: Path) -> None:
        """切换时前一活跃场景的 is_active 落盘为 False。"""
        mgr = _make_manager(tmp_path)
        a = mgr.create_scene(name="a")
        b = mgr.create_scene(name="b")
        mgr.switch_scene(a.id)
        mgr.switch_scene(b.id)
        prev = mgr.get_scene(a.id)
        assert prev is not None and prev.is_active is False


class TestSceneManagerDeleteUpdate:
    def test_delete_scene(self, tmp_path: Path) -> None:
        """删除场景；删除活跃场景时清除活跃状态。"""
        mgr = _make_manager(tmp_path)
        a = mgr.create_scene(name="a")
        mgr.switch_scene(a.id)
        assert mgr.delete_scene(a.id) is True
        assert mgr.get_active_scene() is None
        assert mgr.delete_scene(a.id) is False

    def test_list_scenes(self, tmp_path: Path) -> None:
        """list_scenes 返回全部。"""
        mgr = _make_manager(tmp_path)
        mgr.create_scene(name="a")
        mgr.create_scene(name="b")
        assert len(mgr.list_scenes()) == 2

    def test_update_scene_fields(self, tmp_path: Path) -> None:
        """update_scene 仅更新提供的字段。"""
        mgr = _make_manager(tmp_path)
        scene = mgr.create_scene(name="old", description="d")
        updated = mgr.update_scene(
            scene.id,
            sys.modules["scene.models"].SceneUpdateRequest(name="new"),
        )
        assert updated is not None
        assert updated.name == "new"
        assert updated.description == "d"
        assert updated.updated_at  # 更新了时间戳

    def test_update_missing_scene_returns_none(self, tmp_path: Path) -> None:
        """更新不存在的场景 → None。"""
        mgr = _make_manager(tmp_path)
        assert mgr.update_scene("missing", sys.modules["scene.models"].SceneUpdateRequest()) is None


# ═══════════════════════════════════════════════════════════
# templates
# ═══════════════════════════════════════════════════════════


class TestTemplates:
    def test_get_template_known(self) -> None:
        t = get_template("chat_workspace")
        assert t is not None and t.name == "聊天工作台"

    def test_get_template_unknown(self) -> None:
        assert get_template("nope") is None

    def test_list_templates(self) -> None:
        assert {t.id for t in list_templates()} >= {"chat_workspace", "media_gallery", "dashboard"}
