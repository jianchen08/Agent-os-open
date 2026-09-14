# @feature: FP-0.2.可观测性 dsh_adapter 插件 | @ci: python-coverage
"""translator._project_root 缺口补测：cwd 为文件系统根时的上溯终止分支（456）。

契约（``_project_root`` docstring）：
1. ``AGENTOS_PROJECT_ROOT`` 指向真实目录 → 采纳；
2. 否则以 ``__file__`` 锚定（``parents[4]`` 含 ``config/`` → 采纳）；
3. 否则 cwd 向父目录上溯最多 6 次找 ``config/``，撞到「父目录 == 自身」
   （文件系统根）即终止；
4. 全部落空 → 返回 ``str(Path.cwd())``。

覆盖点：456 行的 ``break``——cwd 自身就是文件系统根（``parent == cur``）
且根下无 ``config/`` 时循环提前终止，不再无谓迭代（也避免根目录下的
无限/无效上溯）。

不可达说明（逐条）：
- 无——456 行可达：真实 chdir 到驱动器根（``Path.cwd().anchor``）并以
  真实文件树锚定 ``__file__``（``parents[4]`` 指向无 ``config/`` 的临时目录）。
  若该根下恰有 ``config/``（极端环境），用例显式 skip 而非伪绿。

文件系统为真实依赖（真实 chdir / 真实目录树），不 mock。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import translator as tr  # noqa: E402

pytestmark = pytest.mark.unit


def test_project_root_breaks_at_filesystem_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cwd 为文件系统根且无 config/ → 上溯循环在 ``parent == cur`` 处 break。

    锚定 ``__file__`` 到无 config/ 的真实临时目录（绕开 449 行的锚定命中），
    再 chdir 到驱动器根——此时 cwd 的父目录等于自身，456 行必然执行，
    最终回退返回 cwd 字符串。
    """
    root = Path(Path.cwd().anchor)
    if (root / "config").is_dir():
        pytest.skip(f"驱动器根 {root} 下存在 config/，无法构造「根下无 config」形态")

    anchor_dir = tmp_path / "a" / "b" / "c" / "d"
    anchor_dir.mkdir(parents=True)
    monkeypatch.setattr(tr, "__file__", str(anchor_dir / "translator.py"))
    monkeypatch.delenv("AGENTOS_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(root)

    # 前提校验：根下确无 config/，且 cwd 的父目录等于自身（456 行触发条件）
    assert not (root / "config").is_dir()
    assert Path.cwd().parent == Path.cwd()

    assert tr._project_root() == str(root)


def test_project_root_cwd_hit_returns_cwd_without_break(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """区分度输入（正例）：cwd 自身含 config/ → 首轮即命中，不走上溯终止分支。

    与上一条仅「cwd 下是否有 config/」一面不同，锁定 452 行先于 456 行判定。
    """
    cfg_root = tmp_path / "proj"
    (cfg_root / "config").mkdir(parents=True)
    anchor_dir = tmp_path / "x1" / "x2" / "x3" / "x4"
    anchor_dir.mkdir(parents=True)
    monkeypatch.setattr(tr, "__file__", str(anchor_dir / "translator.py"))
    monkeypatch.delenv("AGENTOS_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(cfg_root)

    assert tr._project_root() == str(cfg_root)


def test_project_root_env_wins_when_directory_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """环境变量指向真实目录 → 最高优先级采纳（不进入 cwd 上溯）。"""
    env_root = tmp_path / "env-proj"
    env_root.mkdir()
    monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(env_root))
    monkeypatch.setattr(tr, "__file__", str(tmp_path / "ghost" / "p1" / "p2" / "p3" / "p4" / "translator.py"))

    assert tr._project_root() == str(env_root)


def test_project_root_env_ignored_when_directory_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """环境变量指向不存在目录 → 不采纳，继续锚定链（fail-open 到真实探测）。"""
    anchor_dir = tmp_path / "a" / "b" / "c" / "d"
    anchor_dir.mkdir(parents=True)
    monkeypatch.setattr(tr, "__file__", str(anchor_dir / "translator.py"))
    monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(tmp_path / "does-not-exist"))
    monkeypatch.chdir(tmp_path)

    assert tr._project_root() == str(tmp_path)
