# @feature: FP-0.2.八 多租户核心系统 | @vision: V4 多用户 | @ci: python-plugins-test
"""F-TENANT-B-T2 其余插件按租户改造测试 — 方案 B 目录隔离。

覆盖 4 个插件存储层契约（同 multimodal/storage 的 T1 范式）：
- tasks/storage.TaskStorage：tenant_id → ``data/{tid}/tasks``；
- scene/persistence.ScenePersistence：tenant_id → ``data/{tid}/scenes``；
- memory/vector_retriever.SqliteVectorStore：tenant_id →
  ``data/{tid}/memory/memory.db``（sqlite 路径是文件非目录）；
- channel_api/routes_artifacts._get_uploads_dir：tenant_id →
  ``data/{tid}/uploads``。

每个插件断言四类不变量（§8 意图：方案 B 每租户独立数据根，避免跨租户读写串扰）：
1. 不同 tenant_id → 不同数据根；
2. 未传 tenant_id → default 租户（平滑回退，永不崩溃）；
3. 显式 base_dir / env 覆盖仍生效（向后兼容，存量测试不受影响）；
4. A 租户写入 B 租户不可见（隔离不变量）。

[来源: docs/test_traceability.md FP-0.2.八 / V4；config/rules/testing_rules.md §8/§9]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit  # TDD 分层：纯单测，零外部依赖（tests/plugins 强制）

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SYSTEM_DIR = _REPO_ROOT / "plugins" / "shared" / "system"
_TASKS_DIR = _SYSTEM_DIR / "tasks"
_CHANNEL_API_DIR = _SYSTEM_DIR / "channel_api"


# ── helpers ──────────────────────────────────────────────


def _import_from(module: str, src_dir: Path, pop: tuple[str, ...] = ()) -> Any:
    """从插件源目录导入平铺模块（裸名治理）。

    tests/plugins/conftest 的 pytest_runtest_setup 只清理 _COLLIDING_NAMES 中的
    裸名（storage/scene/vector_retriever/deps 等不在其中），同一 pytest 会话里
    先收集的测试文件会把同名裸模块缓存进 sys.modules（如 test_tenant_data.py
    缓存的 multimodal ``storage``）。本 helper 在导入前把源目录推到 sys.path
    最前并踢掉相关缓存，确保 ``from storage import TaskStorage`` 解析到本插件文件。
    注意：即使源目录已在 sys.path 中（其它测试可能已加入且被后续 insert(0) 挤到
    后面），也要重排到最前——否则 ``storage`` 可能解析到其它插件的同名平铺模块。
    """
    s = str(src_dir)
    if s in sys.path:
        sys.path.remove(s)
    sys.path.insert(0, s)
    for name in pop:
        sys.modules.pop(name, None)
    import importlib

    return importlib.import_module(module)


# ============================================================
# tasks/storage.TaskStorage
# ============================================================


def _make_task_storage(**kwargs: Any) -> Any:
    storage = _import_from(
        "storage", _TASKS_DIR, pop=("storage", "task_types", "enum_utils", "agents_types")
    )
    return storage.TaskStorage(**kwargs)


class TestTaskStorageTenantAware:
    """TaskStorage 的 data_dir 由 tenant_id 驱动（同 multimodal 范式）。"""

    def test_tenant_id_drives_data_dir(self, tmp_path, monkeypatch):
        """tenant_id=tenantA → data_dir 落在 data/tenantA/tasks。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("TASKS_STORAGE_DIR", raising=False)

        s = _make_task_storage(tenant_id="tenantA")
        assert s._data_dir == tmp_path / "tenantA" / "tasks"
        assert s._data_dir.is_dir()

    def test_different_tenants_different_data_dir(self, tmp_path, monkeypatch):
        """租户 A/B 的 TaskStorage data_dir 不同（隔离不变量）。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("TASKS_STORAGE_DIR", raising=False)

        s_a = _make_task_storage(tenant_id="tenantA")
        s_b = _make_task_storage(tenant_id="tenantB")
        assert s_a._data_dir != s_b._data_dir
        assert s_a._data_dir == tmp_path / "tenantA" / "tasks"
        assert s_b._data_dir == tmp_path / "tenantB" / "tasks"

    def test_no_tenant_id_uses_default(self, tmp_path, monkeypatch):
        """未传 tenant_id → default 租户目录。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("TASKS_STORAGE_DIR", raising=False)

        s = _make_task_storage()
        assert s._data_dir == tmp_path / "default" / "tasks"

    def test_explicit_data_dir_overrides_tenant(self, tmp_path, monkeypatch):
        """显式 data_dir 优先级最高，覆盖 tenant_id 与 env。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TASKS_STORAGE_DIR", str(tmp_path / "env_override"))

        explicit = tmp_path / "explicit_tasks"
        s = _make_task_storage(data_dir=str(explicit), tenant_id="tenantA")
        assert s._data_dir == explicit

    def test_env_tasks_storage_dir_overrides_tenant(self, tmp_path, monkeypatch):
        """TASKS_STORAGE_DIR 覆盖 tenant_id 默认（兼容存量部署）。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        env_dir = tmp_path / "env_storage"
        monkeypatch.setenv("TASKS_STORAGE_DIR", str(env_dir))

        s = _make_task_storage(tenant_id="tenantA")
        assert s._data_dir == env_dir

    def test_tenant_a_write_not_visible_to_tenant_b(self, tmp_path, monkeypatch):
        """租户 A 保存的任务，租户 B 读不到（文件级隔离不变量）。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("TASKS_STORAGE_DIR", raising=False)

        task_types = _import_from(
            "task_types", _TASKS_DIR, pop=("task_types", "enum_utils", "agents_types")
        )
        task = task_types.TaskModel(id="task-a", title="tenant A 的任务")

        s_a = _make_task_storage(tenant_id="tenantA")
        s_a.save(task)

        s_b = _make_task_storage(tenant_id="tenantB")
        assert s_b.get("task-a") is None
        # 文件确实落在 A 的目录下，B 的目录下不存在
        assert (tmp_path / "tenantA" / "tasks" / "tree_task-a" / "task-a.yaml").exists()
        assert not (tmp_path / "tenantB" / "tasks" / "tree_task-a" / "task-a.yaml").exists()


# ============================================================
# scene/persistence.ScenePersistence
# ============================================================


def _make_scene_persistence(**kwargs: Any) -> Any:
    persistence = _import_from("scene.persistence", _SYSTEM_DIR, pop=("scene",))
    return persistence.ScenePersistence(**kwargs)


class TestScenePersistenceTenantAware:
    """ScenePersistence 的 storage_path 由 tenant_id 驱动。"""

    def test_tenant_id_drives_storage_path(self, tmp_path, monkeypatch):
        """tenant_id=tenantA → storage_path 落在 data/tenantA/scenes。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("SCENES_STORAGE_DIR", raising=False)

        p = _make_scene_persistence(tenant_id="tenantA")
        assert p.storage_path == tmp_path / "tenantA" / "scenes"
        assert p.scenes_file == tmp_path / "tenantA" / "scenes" / "scenes.json"
        assert p.storage_path.is_dir()

    def test_different_tenants_different_storage_path(self, tmp_path, monkeypatch):
        """租户 A/B 的 storage_path 不同（隔离不变量）。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("SCENES_STORAGE_DIR", raising=False)

        p_a = _make_scene_persistence(tenant_id="tenantA")
        p_b = _make_scene_persistence(tenant_id="tenantB")
        assert p_a.storage_path != p_b.storage_path
        assert p_a.storage_path == tmp_path / "tenantA" / "scenes"
        assert p_b.storage_path == tmp_path / "tenantB" / "scenes"

    def test_no_tenant_id_uses_default(self, tmp_path, monkeypatch):
        """未传 tenant_id → default 租户目录。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("SCENES_STORAGE_DIR", raising=False)

        p = _make_scene_persistence()
        assert p.storage_path == tmp_path / "default" / "scenes"

    def test_explicit_storage_path_overrides_tenant(self, tmp_path, monkeypatch):
        """显式 storage_path 优先级最高，覆盖 tenant_id 与 env。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("SCENES_STORAGE_DIR", str(tmp_path / "env_override"))

        explicit = tmp_path / "explicit_scenes"
        p = _make_scene_persistence(storage_path=str(explicit), tenant_id="tenantA")
        assert p.storage_path == explicit

    def test_env_scenes_storage_dir_overrides_tenant(self, tmp_path, monkeypatch):
        """SCENES_STORAGE_DIR 覆盖 tenant_id 默认（兼容存量部署）。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        env_dir = tmp_path / "env_storage"
        monkeypatch.setenv("SCENES_STORAGE_DIR", str(env_dir))

        p = _make_scene_persistence(tenant_id="tenantA")
        assert p.storage_path == env_dir

    def test_tenant_a_write_not_visible_to_tenant_b(self, tmp_path, monkeypatch):
        """租户 A 保存的场景，租户 B 读不到（文件级隔离不变量）。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("SCENES_STORAGE_DIR", raising=False)

        scene_models = _import_from("scene.models", _SYSTEM_DIR, pop=("scene",))
        scene = scene_models.Scene(name="tenant A 的场景")

        p_a = _make_scene_persistence(tenant_id="tenantA")
        p_a.save_scene(scene)
        assert p_a.load_scenes() != []

        p_b = _make_scene_persistence(tenant_id="tenantB")
        assert p_b.load_scenes() == []
        assert not (tmp_path / "tenantB" / "scenes" / "scenes.json").exists()
        assert (tmp_path / "tenantA" / "scenes" / "scenes.json").exists()


# ============================================================
# channel_api/routes_artifacts._get_uploads_dir
# ============================================================


def _get_uploads_dir(tenant_id: str | None = None) -> str:
    routes_artifacts = _import_from(
        "routes_artifacts",
        _CHANNEL_API_DIR,
        pop=("routes_artifacts", "deps", "auth", "auth_token", "models"),
    )
    return routes_artifacts._get_uploads_dir(tenant_id=tenant_id)


class TestUploadsDirTenantAware:
    """上传目录由 tenant_id 驱动（``data/{tid}/uploads``）。"""

    def test_tenant_id_drives_uploads_dir(self, tmp_path, monkeypatch):
        """tenant_id=tenantA → data/tenantA/uploads。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("UPLOADS_DIR", raising=False)

        assert _get_uploads_dir("tenantA") == str(tmp_path / "tenantA" / "uploads")
        assert (tmp_path / "tenantA" / "uploads").is_dir()

    def test_different_tenants_different_uploads_dir(self, tmp_path, monkeypatch):
        """租户 A/B 上传目录不同（隔离不变量）。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("UPLOADS_DIR", raising=False)

        dir_a = _get_uploads_dir("tenantA")
        dir_b = _get_uploads_dir("tenantB")
        assert dir_a != dir_b
        assert dir_a == str(tmp_path / "tenantA" / "uploads")
        assert dir_b == str(tmp_path / "tenantB" / "uploads")

    def test_no_tenant_id_uses_default(self, tmp_path, monkeypatch):
        """未传 tenant_id → default 租户上传目录。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("UPLOADS_DIR", raising=False)

        assert _get_uploads_dir() == str(tmp_path / "default" / "uploads")

    def test_env_uploads_dir_overrides_tenant(self, tmp_path, monkeypatch):
        """UPLOADS_DIR 覆盖 tenant_id 默认（兼容存量部署）。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        env_dir = tmp_path / "env_uploads"
        monkeypatch.setenv("UPLOADS_DIR", str(env_dir))

        assert _get_uploads_dir("tenantA") == str(env_dir)

    def test_tenant_a_write_not_visible_to_tenant_b(self, tmp_path, monkeypatch):
        """租户 A 写入的上传文件，租户 B 目录下不存在（隔离不变量）。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("UPLOADS_DIR", raising=False)

        dir_a = Path(_get_uploads_dir("tenantA"))
        (dir_a / "secret.bin").write_bytes(b"tenant-a")
        dir_b = Path(_get_uploads_dir("tenantB"))

        assert (dir_a / "secret.bin").exists()
        assert not (dir_b / "secret.bin").exists()
