# @feature: FP-0.2.二 mode_personal 个人助理模式包 | @ci: python-coverage
"""pim 数据根外移单测：用户空间数据根解析 / 包内 data 启动迁移（幂等非破坏）。

数据根 = <user_root>/data/mode_personal/（副本无关——同 id 副本更替数据不丢）；
迁移 = 包内 data/ 实体文件一次性拷入用户根（目标缺名才拷，绝不覆盖用户侧文件）。
env 与文件面为外部依赖：env 经 monkeypatch 隔离，文件走真实 tmp_path（测试纪律：
关键路径真实依赖，零 mock 文件面）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PKG = Path(__file__).resolve().parents[1]
_SHARED = _PKG.parents[1]  # plugins/shared/


def _load(name: str, mod_name: str) -> Any:
    for d in (_PKG, _SHARED):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    path = _PKG / f"{name}.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


server = _load("server", "mode_personal_test_data_root_server")

_ENV = "AGENTOS_USER_ROOT"


@pytest.fixture(autouse=True)
def _reset_store_singleton():
    """get_store 单例是模块级全局：每用例前后复位，防跨用例串数据。"""
    server.set_store(None)
    yield
    server.set_store(None)


def test_user_data_dir_under_user_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, str(tmp_path))
    assert server.user_data_dir() == tmp_path / "data" / "mode_personal"


def test_migrate_copies_entity_files_skips_gitkeep(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg" / "data"
    pkg.mkdir(parents=True)
    (pkg / "todos.jsonl").write_text(
        json.dumps({"id": "a1", "title": "卡一"}, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (pkg / "partner.json").write_text('{"level": 2}', encoding="utf-8")
    (pkg / ".gitkeep").write_text("", encoding="utf-8")
    (pkg / "README.md").write_text("非实体文件", encoding="utf-8")
    target = tmp_path / "user" / "data" / "mode_personal"

    migrated = server.migrate_package_data(pkg, target)

    assert sorted(migrated) == ["partner.json", "todos.jsonl"]
    assert "卡一" in (target / "todos.jsonl").read_text(encoding="utf-8")
    assert (target / "partner.json").read_text(encoding="utf-8") == '{"level": 2}'
    assert not (target / ".gitkeep").exists(), "骨架标记不迁"
    assert not (target / "README.md").exists(), "非实体文件不迁"


def test_migrate_never_overwrites_existing_target(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg" / "data"
    pkg.mkdir(parents=True)
    (pkg / "todos.jsonl").write_text('{"id": "pkg"}\n', encoding="utf-8")
    (pkg / "habits.jsonl").write_text('{"id": "h1"}\n', encoding="utf-8")
    target = tmp_path / "user" / "data" / "mode_personal"
    target.mkdir(parents=True)
    (target / "todos.jsonl").write_text('{"id": "user"}\n', encoding="utf-8")

    migrated = server.migrate_package_data(pkg, target)

    assert migrated == ["habits.jsonl"], "目标已有同名文件 = 用户侧在用，跳过"
    assert (target / "todos.jsonl").read_text(encoding="utf-8") == '{"id": "user"}\n'


def test_migrate_idempotent_second_run_is_noop(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg" / "data"
    pkg.mkdir(parents=True)
    (pkg / "todos.jsonl").write_text('{"id": "a1"}\n', encoding="utf-8")
    target = tmp_path / "user" / "data" / "mode_personal"

    assert server.migrate_package_data(pkg, target) == ["todos.jsonl"]
    assert server.migrate_package_data(pkg, target) == []


def test_migrate_missing_package_dir_is_noop(tmp_path: Path) -> None:
    assert server.migrate_package_data(tmp_path / "nope", tmp_path / "target") == []


def test_migrate_io_failure_skips_file_continues_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = tmp_path / "pkg" / "data"
    pkg.mkdir(parents=True)
    (pkg / "todos.jsonl").write_text('{"id": "a1"}\n', encoding="utf-8")
    (pkg / "habits.jsonl").write_text('{"id": "h1"}\n', encoding="utf-8")
    target = tmp_path / "user" / "data" / "mode_personal"

    real_copyfile = server.shutil.copyfile

    def flaky_copy(src: object, dst: object) -> None:
        if str(src).endswith("todos.jsonl"):
            raise OSError("disk full")
        real_copyfile(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(server.shutil, "copyfile", flaky_copy)

    migrated = server.migrate_package_data(pkg, target)

    assert migrated == ["habits.jsonl"], "单文件 IO 失败降级跳过，其余继续迁"
    assert not (target / "todos.jsonl").exists()
    assert (target / "habits.jsonl").is_file()


def test_get_store_reads_migrated_package_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, str(tmp_path))
    pkg = tmp_path / "pkg" / "data"
    pkg.mkdir(parents=True)
    (pkg / "todos.jsonl").write_text(
        json.dumps(
            {"id": "mig1", "title": "迁移卡", "status": "backlog", "progress": 0,
             "priority": "P2", "direction": "none", "created": "2026-10-19T00:00:00"},
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "_PACKAGE_DATA_DIR", pkg)

    store = server.get_store()

    assert "迁移卡" in [t["title"] for t in store.list_records("todo")]
    assert server.user_data_dir() == tmp_path / "data" / "mode_personal"
    assert (tmp_path / "data" / "mode_personal" / "todos.jsonl").is_file()
