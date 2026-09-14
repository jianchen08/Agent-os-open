# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""scene 插件缺口分支补测（HEAD coverage.xml 缺行，2026-09-14）。

覆盖目标（行号语义经源码逐行确认）：
- manager.py:203/205/207（update_scene 的 layout/widgets/state 三字段写入）
- persistence.py:103-104（scenes.json 读失败 → SceneStorageError）、
  122-124（写失败 → error 日志后原样上抛）、184-186（get_scene 命中损坏
  行 → 警告并返回 None，其余场景不受影响）
- server.py:27（group_root 已在 sys.path 时的短路分支）、
  244（get_active 无活跃场景 → scene=None）
- routes_scene.py:50（_get_manager 冷启动惰性构造单例）

真实依赖纪律：持久化走真实 tmp_path 文件（含权限拒绝/目录占位两类真实 IO
失败），场景包经真实命名空间包加载（scene.manager / scene.persistence）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_SYSTEM_DIR = _PLUGIN_DIR.parent
if str(_SYSTEM_DIR) not in sys.path:
    sys.path.insert(0, str(_SYSTEM_DIR))


def _load_scene_package() -> dict[str, Any]:
    """加载真实 scene 命名空间包（与 test_scene.py 同款 setup）。"""
    out: dict[str, Any] = {}
    for name in ("models", "persistence", "templates", "manager", "routes_scene"):
        mod_name = f"scene.{name}"
        if mod_name in sys.modules:
            out[name] = sys.modules[mod_name]
            continue
        plugin_dir = _PLUGIN_DIR
        spec = importlib.util.spec_from_file_location(mod_name, plugin_dir / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        out[name] = module
    return out


_MODS = _load_scene_package()
ScenePersistence = _MODS["persistence"].ScenePersistence
SceneStorageError = _MODS["persistence"].SceneStorageError
SceneManager = _MODS["manager"].SceneManager


def _scene(
    scene_id: str = "s1",
    name: str = "场景",
    layout: dict[str, Any] | None = None,
    widgets: list[dict[str, Any]] | None = None,
    state: dict[str, Any] | None = None,
) -> Any:
    from scene.models import Scene

    return Scene(
        id=scene_id,
        name=name,
        layout=layout or {"type": "grid", "columns": 1},
        widgets=widgets or [],
        state=state or {"active_widget_id": None},
    )


def _field_value(scene: Any, field: str) -> Any:
    """三字段的可比较投影（pydantic 模型 → 纯数据）。"""
    value = getattr(scene, field)
    if isinstance(value, list):
        return [item.model_dump(mode="json") for item in value]
    return value.model_dump(mode="json")


def _assert_field_applied(scene: Any, field: str, expected: Any) -> None:
    """断言请求值真被写入（只校验请求携带的键，默认值不参与）。"""
    value = getattr(scene, field)
    if field == "widgets":
        assert [w.widget_type for w in value] == [w["widget_type"] for w in expected]
        return
    dumped = value.model_dump(mode="json")
    for key, want in expected.items():
        assert dumped[key] == want, f"{field}.{key} 未写入"

# ═══════════════════════════════════════════════════════════
# manager.update_scene：三字段写入
# ═══════════════════════════════════════════════════════════


class TestUpdateSceneFieldWrites:
    """update_scene 仅写入请求中显式提供的字段（203/205/207）。"""

    def test_layout_widgets_state_all_written(self, tmp_path: Path) -> None:
        persistence = ScenePersistence(tmp_path / "scenes")
        manager = SceneManager(persistence=persistence)
        persistence.save_scene(_scene("s1", "原名", state={"active_widget_id": "old"}))

        from scene.models import SceneUpdateRequest

        updated = manager.update_scene(
            "s1",
            SceneUpdateRequest(
                name="新名",
                layout={"type": "grid", "columns": 3},
                widgets=[{"widget_type": "chart", "position": 0}],
                state={"active_widget_id": "w9"},
            ),
        )
        assert updated is not None
        assert updated.name == "新名"
        assert updated.layout.columns == 3
        assert [w.widget_type for w in updated.widgets] == ["chart"]
        assert updated.state.active_widget_id == "w9"

        # 持久化落盘同样生效（不是只改内存对象）
        reloaded = ScenePersistence(tmp_path / "scenes").get_scene("s1")
        assert reloaded is not None
        assert reloaded.layout.columns == 3
        assert [w.widget_type for w in reloaded.widgets] == ["chart"]
        assert reloaded.state.active_widget_id == "w9"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("layout", {"type": "grid", "columns": 5}),
            ("widgets", [{"widget_type": "table"}]),
            ("state", {"active_widget_id": "w-edit"}),
        ],
    )
    def test_single_field_update_leaves_others_intact(
        self, tmp_path: Path, field: str, value: Any
    ) -> None:
        """单字段更新：其余字段保持原值（None 不覆盖）。"""
        persistence = ScenePersistence(tmp_path / "scenes")
        manager = SceneManager(persistence=persistence)
        original = _scene(
            "s1",
            layout={"type": "grid", "columns": 1},
            widgets=[{"widget_type": "old"}],
            state={"active_widget_id": "keep"},
        )
        persistence.save_scene(original)

        from scene.models import SceneUpdateRequest

        updated = manager.update_scene("s1", SceneUpdateRequest(**{field: value}))
        assert updated is not None
        _assert_field_applied(updated, field, value)
        for other in {"layout", "widgets", "state"} - {field}:
            assert _field_value(updated, other) == _field_value(original, other), (
                f"{other} 不应被覆盖"
            )

    def test_empty_request_keeps_all_fields(self, tmp_path: Path) -> None:
        """空请求：三字段全 None → 全保持（对照组，证明分支非恒真）。"""
        persistence = ScenePersistence(tmp_path / "scenes")
        manager = SceneManager(persistence=persistence)
        original = _scene("s1", layout={"type": "stack", "columns": 2}, widgets=[{"widget_type": "a"}], state={"active_widget_id": "keep"})
        persistence.save_scene(original)

        from scene.models import SceneUpdateRequest

        updated = manager.update_scene("s1", SceneUpdateRequest())
        assert updated is not None
        assert updated.layout.type.value == "stack"
        assert [w.widget_type for w in updated.widgets] == ["a"]
        assert updated.state.active_widget_id == "keep"

    def test_missing_scene_returns_none(self, tmp_path: Path) -> None:
        from scene.models import SceneUpdateRequest

        manager = SceneManager(persistence=ScenePersistence(tmp_path / "scenes"))
        assert manager.update_scene("ghost", SceneUpdateRequest(name="x")) is None


# ═══════════════════════════════════════════════════════════
# persistence：真实 IO 失败与损坏行隔离
# ═══════════════════════════════════════════════════════════


class TestPersistenceReadFailures:
    """_read_all_raw 的 IO 失败上抛（103-104）。"""

    def test_unreadable_scenes_file_raises_storage_error(self, tmp_path: Path) -> None:
        """scenes.json 是不可读实体（目录占位）→ SceneStorageError。

        真实 IO 失败（非 mock）：read_text 对目录抛 PermissionError（Windows）
        或 IsADirectoryError（POSIX），两者皆 OSError 子类。
        fail-closed 契约：读失败绝不当空库，否则 save_scene 的读改写会清空全量。
        """
        storage = tmp_path / "scenes"
        storage.mkdir()
        (storage / "scenes.json").mkdir()  # 目录占位

        persistence = ScenePersistence(storage)
        with pytest.raises(SceneStorageError, match="读取场景数据文件失败"):
            persistence.load_scenes()
        with pytest.raises(SceneStorageError, match="读取场景数据文件失败"):
            persistence.save_scene(_scene("s1"))

    def test_missing_file_is_valid_empty_library(self, tmp_path: Path) -> None:
        """对照组：文件不存在是合法空库（不落 fail-closed 分支）。"""
        persistence = ScenePersistence(tmp_path / "scenes")
        assert persistence.load_scenes() == []

    def test_blank_file_is_valid_empty_library(self, tmp_path: Path) -> None:
        """文件存在但内容空白 → 合法空库（有区分度输入）。"""
        storage = tmp_path / "scenes"
        storage.mkdir()
        (storage / "scenes.json").write_text("   \n", encoding="utf-8")
        assert ScenePersistence(storage).load_scenes() == []


class TestPersistenceWriteFailures:
    """_write_all_raw 的写失败：记日志后原样上抛（122-124）。"""

    def test_write_failure_logged_and_reraised(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """scenes.json 只读（可读不可写）→ OSError 上抛 + error 日志（122-124）。"""
        storage = tmp_path / "scenes"
        storage.mkdir()
        target = storage / "scenes.json"
        target.write_text('{"scenes": {}}', encoding="utf-8")
        target.chmod(0o444)  # 只读：读得通、写被拒（真实 IO 失败，非 mock）
        persistence = ScenePersistence(storage)
        assert persistence.load_scenes() == [], "只读文件应仍可读（读失败不在此分支）"

        try:
            with caplog.at_level("ERROR", logger="scene.persistence"):
                with pytest.raises(OSError):
                    persistence.save_scene(_scene("s1"))
        finally:
            target.chmod(0o666)
        assert any("写入场景数据失败" in r.getMessage() for r in caplog.records)

    def test_write_success_persists_and_is_readable(self, tmp_path: Path) -> None:
        """对照组：正常写入后可由新实例读回（证明上抛路径非恒定）。"""
        storage = tmp_path / "scenes"
        ScenePersistence(storage).save_scene(_scene("s1", name="持久化场景"))
        reloaded = ScenePersistence(storage).get_scene("s1")
        assert reloaded is not None and reloaded.name == "持久化场景"


class TestPersistenceCorruptRowIsolation:
    """get_scene/load_scenes 对损坏行逐条隔离（184-186）。"""

    def test_corrupt_row_returns_none_and_keeps_other_scenes(self, tmp_path: Path) -> None:
        """单个场景数据非法 → get_scene 返回 None + warning；其余场景照常可读。"""
        storage = tmp_path / "scenes"
        persistence = ScenePersistence(storage)
        persistence.save_scene(_scene("good", name="好场景"))
        raw = json.loads((storage / "scenes.json").read_text(encoding="utf-8"))
        raw["scenes"]["bad"] = {"id": "bad", "name": None, "layout": "not-a-dict"}
        (storage / "scenes.json").write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8"
        )

        fresh = ScenePersistence(storage)
        with pytest.MonkeyPatch.context() as mp:
            records: list[str] = []
            mp.setattr(
                _MODS["persistence"].logger,
                "warning",
                lambda msg, *a, **kw: records.append(msg % a if a else msg),
            )
            assert fresh.get_scene("bad") is None
        assert records, "损坏行应留 warning 痕迹"

        good = fresh.get_scene("good")
        assert good is not None and good.name == "好场景"
        # load_scenes 同样逐条跳过损坏行，不整体失败
        names = {s.id for s in fresh.load_scenes()}
        assert "good" in names and "bad" not in names

    def test_absent_scene_returns_none_without_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """对照组：场景本就不存在 → None 且不留 warning（区分两种 None）。"""
        persistence = ScenePersistence(tmp_path / "scenes")
        with caplog.at_level("WARNING", logger="scene.persistence"):
            assert persistence.get_scene("never-created") is None
        assert not [r for r in caplog.records if "场景数据无效" in r.getMessage()]


# ═══════════════════════════════════════════════════════════
# server / routes_scene：导入短路与冷启动单例
# ═══════════════════════════════════════════════════════════


class TestSceneServerBranches:
    def test_group_root_absent_is_injected(self) -> None:
        """group_root 不在 sys.path 时由 server.py 注入（server.py:27）。"""
        group_root = str(_SYSTEM_DIR.resolve())
        while group_root in sys.path:
            sys.path.remove(group_root)
        try:
            mod_name = "scene_server_gaps_probe"
            sys.modules.pop(mod_name, None)
            spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            assert group_root in sys.path, "server.py 应把组根注入 sys.path"
            # 包导入在注入后才能解析（scene.manager 已成功绑定）
            assert module.SceneManager is SceneManager
        finally:
            while group_root in sys.path:
                sys.path.remove(group_root)
            sys.path.insert(0, group_root)

    def test_group_root_present_not_duplicated(self) -> None:
        """对照组：组根已在 sys.path → 短路不重复插入（26 的 False 分支）。"""
        group_root = str(_SYSTEM_DIR.resolve())
        while group_root in sys.path:
            sys.path.remove(group_root)
        sys.path.insert(0, group_root)
        before = sys.path.count(group_root)
        try:
            mod_name = "scene_server_gaps_probe_dup"
            sys.modules.pop(mod_name, None)
            spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            assert sys.path.count(group_root) == before
        finally:
            while group_root in sys.path:
                sys.path.remove(group_root)
            sys.path.insert(0, group_root)

    async def test_get_active_returns_none_scene_when_no_active(self) -> None:
        """无活跃场景 → success=True 且 scene=None（server.py:244）。"""
        mod_name = "scene_server_gaps_probe2"
        sys.modules.pop(mod_name, None)
        spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            module._manager = SceneManager(persistence=ScenePersistence(Path(tmp) / "s"))
            result = await module.scene_get_active()
        assert result["success"] is True
        assert result["scene"] is None

    async def test_get_active_returns_scene_when_switched(self) -> None:
        """对照组：切换后 get_active 返回场景数据（证明 None 非恒返回）。"""
        mod_name = "scene_server_gaps_probe3"
        sys.modules.pop(mod_name, None)
        spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            manager = SceneManager(persistence=ScenePersistence(Path(tmp) / "s"))
            created = manager.create_scene(name="活跃场景")
            manager.switch_scene(created.id)
            module._manager = manager
            result = await module.scene_get_active()
        assert result["success"] is True
        assert result["scene"] is not None
        assert result["scene"]["name"] == "活跃场景"


class TestRoutesSceneManagerSingleton:
    def test_get_manager_lazily_constructs_singleton(self) -> None:
        """_get_manager：_scene_manager 为 None 时惰性构造，二次取用同一实例（50）。"""
        routes = _MODS["routes_scene"]
        original = routes._scene_manager
        routes._scene_manager = None
        try:
            first = routes._get_manager()
            assert isinstance(first, SceneManager)
            assert routes._get_manager() is first
        finally:
            routes._scene_manager = original

    def test_create_scene_uses_injected_manager(self, tmp_path: Path) -> None:
        """注入 manager 后 create_scene 落到注入实例（单例解析可观察效果）。"""
        routes = _MODS["routes_scene"]
        original = routes._scene_manager
        routes._scene_manager = SceneManager(persistence=ScenePersistence(tmp_path / "s"))
        try:
            created = routes.create_scene({"name": "经路由创建"})
            assert created["name"] == "经路由创建"
            assert routes.list_scenes()["total"] == 1
        finally:
            routes._scene_manager = original

    def test_create_scene_invalid_template_raises_http_error(self, tmp_path: Path) -> None:
        """无效模板 → SceneHTTPError 400（错误面可观察契约）。"""
        routes = _MODS["routes_scene"]
        original = routes._scene_manager
        routes._scene_manager = SceneManager(persistence=ScenePersistence(tmp_path / "s"))
        try:
            with pytest.raises(routes.SceneHTTPError) as excinfo:
                routes.create_scene({"name": "x", "template_id": "no-such-template"})
            assert excinfo.value.status_code == 400
        finally:
            routes._scene_manager = original
