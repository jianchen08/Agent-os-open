# @feature: FP-0.2.〇 项目 = 文件夹 + 登记 | @ci: python-coverage
"""projects 域 create_project 端点测试（幂等复用 + created 标志）。

登记簿实现测试自 T6a 起随 project_registry 实现迁 project_manager
（test_project_registry.py）；本文件只留 tasks 侧端点消费面——经
project.* 服务桩 + 真实登记簿实例双轨验证（关键路径走真实依赖）。
"""

from __future__ import annotations

import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

# 共享根自举（plugins/shared/ —— project_manager 包所在）。本文件在
# plugins/shared/tasks/ 下，共享根 = 上溯 1 级；写 2 级落到 plugins/，
# project_manager 永不可达（孤跑/组合车道伪影，2026-10-27 同刀修正）。
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)

from project_manager import project_registry as pr  # noqa: E402


@pytest.fixture
def reg_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """隔离登记目录 + 工作空间基目录（不落仓库根 .ai_workspaces）。"""
    tasks_root = tmp_path / "tasks_data"
    ws_base = tmp_path / "ws"
    monkeypatch.setenv("TASKS_STORAGE_DIR", str(tasks_root))
    monkeypatch.setattr("project_manager.project_registry.workspace_base_dir", lambda: ws_base)
    return {"tasks_root": tasks_root, "ws_base": ws_base}


class TestCreateProjectApi:
    """projects 域 create_project 端点（http_api）：幂等复用 + created 标志。"""

    @staticmethod
    def _load_tasks_http_api() -> Any:
        """按本插件目录解析 http_api 并返回模块。

        ``http_api`` 是 0.2 平铺共享裸名（conftest 收集期逐出后按 sys.path
        重解析）；先跑的插件测试（triggers_ext/test_triggers.py 等）会把
        自家目录残留在 sys.path[0]，逐出后在残序上重解析即劫持到别家
        http_api（其顶层 from tool import 又落到 bash/tool.py → ImportError）。
        导入前置顶本目录，与 triggers_ext/test_triggers.py 同款防御。
        """
        _HERE = os.path.dirname(os.path.abspath(__file__))
        if _HERE in sys.path:
            sys.path.remove(_HERE)
        sys.path.insert(0, _HERE)
        sys.modules.pop("http_api", None)
        import http_api

        return http_api

    @staticmethod
    def _stub_project_call(
        monkeypatch: pytest.MonkeyPatch, http_api: Any, data_dir: Path
    ) -> None:
        """project.* 服务桩：create 分支走真实登记簿（隔离 data_dir）——
        关键路径消费真实依赖，只桩跨插件 RPC 这一层。"""
        from dataclasses import asdict as _asdict

        async def _fake_project_call(method: str, args: dict[str, Any]) -> dict[str, Any]:
            assert method == "create"
            registry = pr.ProjectRegistry(data_dir=data_dir)
            project, created = pr.ensure_project_registered(
                title=str(args.get("title") or ""),
                explicit_path=str(args.get("path") or ""),
                session_id=str(args.get("session_id") or ""),
                submitted_by=str(args.get("submitted_by") or ""),
                auto_execute=bool(args.get("auto_execute", False)),
                registry=registry,
            )
            return {"project": _asdict(project), "created": created}

        monkeypatch.setattr(http_api, "_project_call", _fake_project_call)

    async def test_create_project_idempotent_by_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reg_env: dict[str, Any]
    ) -> None:
        http_api = self._load_tasks_http_api()
        self._stub_project_call(monkeypatch, http_api, reg_env["tasks_root"])
        target = reg_env["ws_base"] / "proj"
        target.mkdir(parents=True)

        r1 = await http_api.create_project({"goal": "P", "path": str(target)})
        r2 = await http_api.create_project({"goal": "P", "path": str(target)})

        assert r1["created"] is True
        assert r2["created"] is False
        assert r2["project"]["id"] == r1["project"]["id"]
        assert r2["project"]["metadata"]["path"] == str(target)
        # 登记目录只有一条
        assert len(list(pr.load_project_paths())) == 1

    async def test_create_project_reuses_registered_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reg_env: dict[str, Any]
    ) -> None:
        """会话目录已登记 → 复用既有登记，不新建（会话保存多次幂等）。"""
        http_api = self._load_tasks_http_api()
        self._stub_project_call(monkeypatch, http_api, reg_env["tasks_root"])
        target = reg_env["ws_base"] / "proj"
        target.mkdir(parents=True)

        r1 = await http_api.create_project(
            {"goal": "P", "path": str(target), "session_id": "sess-1"}
        )
        r2 = await http_api.create_project(
            {"goal": "P", "path": str(target), "session_id": "sess-2"}
        )

        assert r1["created"] is True
        assert r2["created"] is False
        assert r2["project"]["id"] == r1["project"]["id"]
