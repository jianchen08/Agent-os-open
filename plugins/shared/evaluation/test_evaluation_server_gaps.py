# @feature: FP-0.2.〇 管道引擎与插件执行模型(内核地基) | @vision: V3 可嵌入 | @ci: python-coverage
"""evaluation server.py `_project_root` 缺口补测（第 57 行 break）。

`_project_root` 在 AGENTOS_PROJECT_ROOT 未命中时从 cwd 上溯最多 6 层找 config/；
走到卷根（parent == cur）时 break 退出循环。本文件钉死该早退路径与随后
`os.getcwd()` 兜底返回。

环境守卫：若卷根下真存在 config/ 目录，上溯会在卷根命中返回（而非 break），
用例语义不成立 → skip（当前 D:\\ / C:\\ 均无 config/）。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent


def _load_server_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    mod_name = "evaluation_server_gaps_under_test"
    spec = importlib.util.spec_from_file_location(mod_name, str(_DIR / "server.py"))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _volume_root() -> str:
    return os.path.abspath(os.sep)


@pytest.mark.parametrize(
    "cwd",
    [
        _volume_root(),
        os.path.join(_volume_root(), "no_such_dir_for_gaps_probe"),
    ],
)
def test_walk_to_volume_root_breaks_and_falls_back_to_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cwd: str
) -> None:
    """卷根本身 / 卷根下不存在子目录：两种入参都能走到 parent == cur 的 break。"""
    volume_root = _volume_root()
    if os.path.isdir(os.path.join(volume_root, "config")):
        pytest.skip(f"卷根 {volume_root} 下存在 config/，上溯会命中而非 break")
    monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(tmp_path / "does_not_exist"))
    monkeypatch.setattr(os, "getcwd", lambda: cwd)
    mod = _load_server_module(monkeypatch)

    assert mod._project_root() == cwd


def test_existing_project_root_env_is_preferred(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """对照组：AGENTOS_PROJECT_ROOT 有效目录时直接返回（不走上溯）。"""
    monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(tmp_path))
    mod = _load_server_module(monkeypatch)

    assert mod._project_root() == str(tmp_path)


def test_walk_stops_at_nearest_config_ancestor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """对照组：上溯途中命中 config/ 的祖先 → 返回该祖先（不是卷根）。"""
    project = tmp_path / "proj"
    nested = project / "a" / "b"
    (project / "config").mkdir(parents=True)
    nested.mkdir(parents=True)
    monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(tmp_path / "missing"))
    monkeypatch.setattr(os, "getcwd", lambda: str(nested))
    mod = _load_server_module(monkeypatch)

    assert mod._project_root() == str(project)
