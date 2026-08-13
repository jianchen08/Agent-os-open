# @feature: FP-0.2.八 多租户核心系统 | @vision: V4 多用户 | @ci: python-plugins-test
"""F-TENANT-B-T1 多租户数据根咽喉点测试 — 方案 B 目录隔离地基。

覆盖咽喉点 ``plugins/shared/tenant_data.py`` 的全部契约：
- ``tenant_data_root(tenant_id, subdir)`` 返回 ``{base}/{tenant_id}/{subdir}/`` 并自动创建；
- 租户隔离：A/B 路径不同、互不可见；
- ``get_current_tenant_id`` 经 tenant-context capability 解析；未注入/失败 → ``default``；
- ``migrate_legacy_data_to_default`` 幂等迁移 ``data/*`` → ``data/default/*``；
- 示范改造 ``multimodal/storage.DiskFileStorage``：tenant_id 驱动 base_dir，显式/env 覆盖优先。

意图（§8）：方案 B 多租户隔离的 WHY 是「每租户独立数据根，避免跨租户读写串扰」；
本测试把这条不变量编码为路径隔离 + 回退 default + 迁移幂等三类断言。

[来源: docs/test_traceability.md FP-0.2.八 / V4；config/rules/testing_rules.md §8/§9]
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit  # TDD 分层：纯单测，零外部依赖（tests/plugins 强制）

from tenant_data import (  # noqa: E402  (conftest 已把 plugins/shared 推上 sys.path)
    DEFAULT_TENANT,
    get_current_tenant_id,
    migrate_legacy_data_to_default,
    tenant_config_dir,
    tenant_data_root,
)


# ── helpers ──────────────────────────────────────────────


def _async_run(coro):
    """同步运行 async 函数（兼容已有/无事件循环）。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result(timeout=10)


# ============================================================
# tenant_data_root — 路径构造 + 自动创建
# ============================================================


class TestTenantDataRoot:
    """``tenant_data_root`` 路径契约。"""

    def test_path_structure_and_creation(self, tmp_path):
        """tenant_data_root(tenantA, multimodal) == base/tenantA/multimodal/ 且目录被创建。"""
        root = tenant_data_root("tenantA", "multimodal", base=tmp_path)

        assert root == tmp_path / "tenantA" / "multimodal"
        assert root.is_dir(), "咽喉点应自动 mkdir(parents=True)"

    def test_default_base_uses_env(self, tmp_path, monkeypatch):
        """未显式传 base 时，优先 env AGENTOS_DATA_DIR（避免写真实 data/）。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        root = tenant_data_root("acme", "tasks")

        assert root == tmp_path / "acme" / "tasks"
        assert root.is_dir()

    def test_subdir_nested_creation(self, tmp_path):
        """多层 subdir 正确切分（不含路径分隔符的合法 subdir）。"""
        root = tenant_data_root("t1", "uploads", base=tmp_path)
        assert root == tmp_path / "t1" / "uploads"
        assert root.is_dir()


class TestTenantIsolation:
    """负向隔离：不同租户路径不同、互不可见。"""

    def test_different_tenants_different_paths(self, tmp_path):
        """租户 A 与租户 B 的 multimodal 数据根必须不同。"""
        root_a = tenant_data_root("tenantA", "multimodal", base=tmp_path)
        root_b = tenant_data_root("tenantB", "multimodal", base=tmp_path)

        assert root_a != root_b
        assert root_a == tmp_path / "tenantA" / "multimodal"
        assert root_b == tmp_path / "tenantB" / "multimodal"

    def test_tenant_data_not_visible_to_other_tenant(self, tmp_path):
        """租户 A 写入的文件，在租户 B 的数据根下不存在（隔离不变量）。"""
        root_a = tenant_data_root("tenantA", "multimodal", base=tmp_path)
        root_b = tenant_data_root("tenantB", "multimodal", base=tmp_path)

        (root_a / "secret.json").write_text('{"tenant": "A"}', encoding="utf-8")

        assert (root_a / "secret.json").exists()
        assert not (root_b / "secret.json").exists(), "租户 B 不得见到租户 A 的数据"

    def test_default_tenant_isolated_from_named_tenant(self, tmp_path):
        """default 租户与具名租户路径不同。"""
        root_default = tenant_data_root(DEFAULT_TENANT, "multimodal", base=tmp_path)
        root_named = tenant_data_root("tenantA", "multimodal", base=tmp_path)

        assert root_default == tmp_path / "default" / "multimodal"
        assert root_default != root_named


# ============================================================
# get_current_tenant_id — capability 解析 + default 回退
# ============================================================


class TestGetCurrentTenantId:
    """``get_current_tenant_id`` capability 契约 + 韧性回退。

    意图：未接入 capability 的调用方（如旧插件/单测）暂用 default 租户，
    保证平滑迁移——永不因 tenant-context 缺失而崩溃。
    """

    @staticmethod
    def _caller(return_value):
        """构造 mock capability_caller（tenant-context 绑定，短方法名）。"""

        async def _call(method, params):
            return return_value

        return _call

    def test_dict_result_with_tenant_id(self):
        """capability 返回 {"tenant_id": "t1"} → 解析为 t1。"""
        caller = self._caller({"tenant_id": "t1", "session_id": "s1"})
        assert _async_run(get_current_tenant_id(caller)) == "t1"

    def test_string_result(self):
        """capability 返回裸字符串 tenant_id。"""
        caller = self._caller("tenant-str")
        assert _async_run(get_current_tenant_id(caller)) == "tenant-str"

    def test_empty_dict_falls_back_to_default(self):
        """返回空 dict → default。"""
        caller = self._caller({})
        assert _async_run(get_current_tenant_id(caller)) == DEFAULT_TENANT

    def test_none_caller_falls_back_to_default(self):
        """capability_caller 未注入（None）→ default。"""
        assert _async_run(get_current_tenant_id(None)) == DEFAULT_TENANT

    def test_caller_raises_falls_back_to_default(self):
        """capability 调用抛异常（如内核未实现 tenant-context.get）→ default。"""

        async def _boom(method, params):
            raise RuntimeError("capability method not implemented: tenant-context.get")

        assert _async_run(get_current_tenant_id(_boom)) == DEFAULT_TENANT

    def test_calls_tenant_context_get(self):
        """应经 tenant-context 命名空间调用（method='get'，tenant-context 绑定）。"""
        captured: dict = {}

        async def _spy(method, params):
            captured["method"] = method
            captured["params"] = params
            return {"tenant_id": "spy"}

        tid = _async_run(get_current_tenant_id(_spy))
        assert tid == "spy"
        assert captured["method"] == "get"
        assert captured["params"] == {}


# ============================================================
# migrate_legacy_data_to_default — 幂等迁移
# ============================================================


class TestMigrateLegacyData:
    """``migrate_legacy_data_to_default`` 幂等迁移逻辑。

    意图：方案 B 上线时把全局共享的 ``data/{memory,multimodal,...}`` 平滑迁到
    ``data/default/{...}``；幂等保证部署钩子重复调用不重复移动/不丢数据。
    本测试用 tempdir 模拟，绝不触碰真实 data/。
    """

    def test_migrates_legacy_subdirs_into_default(self, tmp_path):
        """data/{memory,multimodal,tasks} → data/default/{memory,multimodal,tasks}。"""
        # 模拟旧布局：data/ 直接子目录
        (tmp_path / "memory").mkdir()
        (tmp_path / "memory" / "memory.db").write_text("x", encoding="utf-8")
        (tmp_path / "multimodal").mkdir()
        (tmp_path / "tasks").mkdir()
        (tmp_path / "tasks" / "t1.json").write_text("{}", encoding="utf-8")

        moved = migrate_legacy_data_to_default(data_root=tmp_path)

        assert (tmp_path / "default" / "memory" / "memory.db").exists()
        assert (tmp_path / "default" / "multimodal").is_dir()
        assert (tmp_path / "default" / "tasks" / "t1.json").exists()
        # 原位置已移走
        assert not (tmp_path / "memory").exists()
        assert not (tmp_path / "multimodal").exists()
        # 返回被移动条目
        assert "memory" in moved and "multimodal" in moved and "tasks" in moved

    def test_migrates_top_level_files_too(self, tmp_path):
        """data/ 顶层的散落文件也一并迁入 default/。"""
        (tmp_path / "diag.log").write_text("log", encoding="utf-8")
        (tmp_path / "uploads").mkdir()

        migrate_legacy_data_to_default(data_root=tmp_path)

        assert (tmp_path / "default" / "diag.log").exists()
        assert (tmp_path / "default" / "uploads").is_dir()
        assert not (tmp_path / "diag.log").exists()

    def test_idempotent_second_call_is_noop(self, tmp_path):
        """已有 data/default/ 时再次调用不重复移动、不报错。"""
        (tmp_path / "memory").mkdir()
        (tmp_path / "default").mkdir()  # 已存在 default → 视为已迁移

        moved = migrate_legacy_data_to_default(data_root=tmp_path)

        # default 已存在 → 跳过：memory 仍在原位（不被移动），返回空
        assert moved == [] or moved is None or len(moved) == 0
        assert (tmp_path / "memory").exists(), "已有 default 时不应移动 legacy 目录"
        assert (tmp_path / "default").is_dir()

    def test_empty_data_root(self, tmp_path):
        """空 data/ 调用迁移应安全（创建 default/ 或无操作）。"""
        moved = migrate_legacy_data_to_default(data_root=tmp_path)
        # 无 legacy 内容 → 无需移动；行为幂等即可
        assert isinstance(moved, list)


# ============================================================
# config/users/{tenant_id}/ 配置覆盖层咽喉点（F-TENANT-B 配置面）
# ============================================================


class TestTenantConfigDir:
    """``tenant_config_dir`` 配置覆盖层契约。

    意图（§8）：方案 B 配置面——每租户独立配置覆盖目录
    ``config/users/{tenant_id}/``，目录存在 = 有覆盖，不存在 = 回退全局配置。
    与存储层（tenant_data_root 自动创建）刻意不同：配置覆盖层**不因读取而生成
    空目录**，避免空覆盖层掩盖"无覆盖 → 回退"语义。
    """

    def test_path_structure(self, tmp_path):
        """tenant_config_dir 返回 {base}/{tenant_id}（不含 subdir 层级）。"""
        base = tmp_path / "users"
        (base / "default").mkdir(parents=True)
        (base / "default" / "profile.md").write_text("p", encoding="utf-8")

        d = tenant_config_dir("default", base=base)

        assert d == base / "default"
        assert (d / "profile.md").exists()

    def test_no_auto_create(self, tmp_path):
        """覆盖层目录不存在时**不自动创建**（区别于数据根）。"""
        base = tmp_path / "users"
        d = tenant_config_dir("tenant-x", base=base)

        assert not d.exists(), "配置覆盖层不应因读取而自动创建目录"
        assert not (base / "tenant-x").exists()

    def test_default_base_uses_env(self, tmp_path, monkeypatch):
        """env AGENTOS_CONFIG_USERS_DIR 覆盖默认 base。"""
        monkeypatch.setenv("AGENTOS_CONFIG_USERS_DIR", str(tmp_path / "cfg"))
        d = tenant_config_dir("default")
        assert str(d).startswith(str(tmp_path / "cfg"))

    def test_traversal_rejected(self, tmp_path):
        """tenant_id 含 .. / 分隔符 → 拒绝（与数据根同款防穿越）。"""
        with pytest.raises(ValueError):
            tenant_config_dir("../../etc", base=tmp_path)
        with pytest.raises(ValueError):
            tenant_config_dir("a/b", base=tmp_path)

    def test_tenant_isolation(self, tmp_path):
        """A 租户配置目录与 B 租户不同（隔离不变量）。"""
        base = tmp_path / "users"
        (base / "a").mkdir(parents=True)
        (base / "b").mkdir(parents=True)

        da = tenant_config_dir("a", base=base)
        db = tenant_config_dir("b", base=base)

        assert da != db
        assert da.parent == db.parent


# ============================================================
# 示范改造：multimodal/storage.DiskFileStorage
# ============================================================

# multimodal 模块内部平铺 import（from storage import ...），需把其源目录推上 sys.path。
_REPO_ROOT = Path(__file__).resolve().parents[3]
_MULTIMODAL_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "multimodal"
_mm = str(_MULTIMODAL_DIR)
if _mm not in sys.path:
    sys.path.insert(0, _mm)

from storage import DiskFileStorage  # noqa: E402


class TestMultimodalStorageTenantAware:
    """示范改造：DiskFileStorage 的 base_dir 由 tenant_id 驱动。

    意图：把全局 ``./data/multimodal`` 改为 ``data/{tenant_id}/multimodal``，
    从存储层落实方案 B 目录隔离；同时保留 env/显式 base_dir 覆盖以兼容存量调用。
    """

    def test_tenant_id_drives_base_dir(self, tmp_path, monkeypatch):
        """tenant_id=tenantA → base_dir 落在 data/tenantA/multimodal。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        # 确保未设 MULTIMODAL_STORAGE_DIR 以外的覆盖
        monkeypatch.delenv("MULTIMODAL_STORAGE_DIR", raising=False)

        s = DiskFileStorage(tenant_id="tenantA")
        assert s._base_dir == tmp_path / "tenantA" / "multimodal"

    def test_different_tenants_different_base_dir(self, tmp_path, monkeypatch):
        """租户 A/B 的 DiskFileStorage base_dir 不同（隔离不变量）。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("MULTIMODAL_STORAGE_DIR", raising=False)

        s_a = DiskFileStorage(tenant_id="tenantA")
        s_b = DiskFileStorage(tenant_id="tenantB")
        assert s_a._base_dir != s_b._base_dir
        assert s_a._base_dir == tmp_path / "tenantA" / "multimodal"
        assert s_b._base_dir == tmp_path / "tenantB" / "multimodal"

    def test_no_tenant_id_uses_default(self, tmp_path, monkeypatch):
        """未传 tenant_id → default 租户目录。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("MULTIMODAL_STORAGE_DIR", raising=False)

        s = DiskFileStorage()
        assert s._base_dir == tmp_path / "default" / "multimodal"

    def test_explicit_base_dir_overrides_tenant(self, tmp_path, monkeypatch):
        """显式 base_dir 优先级最高，覆盖 tenant_id 与 env。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MULTIMODAL_STORAGE_DIR", str(tmp_path / "env_override"))

        explicit = tmp_path / "explicit_storage"
        s = DiskFileStorage(base_dir=str(explicit), tenant_id="tenantA")
        assert s._base_dir == explicit

    def test_env_multimodal_storage_dir_overrides_tenant(self, tmp_path, monkeypatch):
        """MULTIMODAL_STORAGE_DIR 覆盖 tenant_id 默认（兼容存量部署）。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        env_dir = tmp_path / "env_storage"
        monkeypatch.setenv("MULTIMODAL_STORAGE_DIR", str(env_dir))

        s = DiskFileStorage(tenant_id="tenantA")
        assert s._base_dir == env_dir
