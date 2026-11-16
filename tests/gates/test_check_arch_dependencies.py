# @feature: FP-GATE 架构依赖巡检（P-18） | @ci: python-coverage
"""check_arch_dependencies.py 单测（三清单清单二 11 项 / P-18，方案 §9.2 步骤 1）。

行为契约（断输入→输出，不钉实现）：
- 四种导入形态样例各检出违规（方案 §9.2：函数体内导入 / 条件导入 / 动态导入 /
  TYPE_CHECKING；外加顶层静态基线形态）→ exit 1，报 file:line + 形态标签 + 目标；
- 公共接口跨包导入绿（v3 §3.3 禁的是「非法导入私有接口」，非一切跨包依赖）；
- 同包私有导入绿（PEP 8 私有属包内实现细节）；
- 平铺布局歧义消解：顶层名在本包目录直下可解析（.py / 包）→ 视为同包内部
  导入（isolation/server.py 经 sys.path 注入消费本包平铺模块的实况同构）；
- 动态导入非常量实参（import_module(var)）静态不可达 → 不判违规，位点显性计数
  （non_constant_dynamic_imports 字段，数值精确断言防空转）；
- 测试文件豁免（test_*.py / conftest.py / tests/ 目录剪枝，白盒消费同参照闸）；
- 相对导入（level>0）恒同包，绿；
- fail-closed：扫描面语法错 → exit 2 拒判；
- 负控制（防恒绿）：真实仓检查器子进程实跑 exit 0（现库零违规基线）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = SCRIPTS / "check_arch_dependencies.py"

pytestmark = pytest.mark.unit


def _make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """临时仓树：按 plugins/shared 相对路径落文件，返回其根。"""
    shared = tmp_path / "plugins" / "shared"
    for rel, text in files.items():
        target = shared / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return tmp_path


def _run(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(repo / "plugins" / "shared")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


# 违规目标跨两形态组（防拟合：非单一字面输入）
_PRIV_FROM_B = "from pkg_b import _hidden"
_PRIV_MOD_B = "import pkg_b._impl as impl"


def test_top_level_static_import_detected(tmp_path: Path) -> None:
    """顶层静态跨包私有导入（基线形态）→ 红，报顶层标签与目标名。"""
    repo = _make_repo(
        tmp_path,
        {
            "pkg_a/plugin.py": _PRIV_FROM_B + "\n" + _PRIV_MOD_B + "\n",
            "pkg_b/__init__.py": "PUBLIC_API = 1\n",
        },
    )
    proc = _run(repo)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "pkg_a/plugin.py:1" in proc.stdout
    assert "_hidden" in proc.stdout
    assert "pkg_b._impl" in proc.stdout
    assert "顶层" in proc.stdout


def test_function_body_import_detected(tmp_path: Path) -> None:
    """函数体内惰性导入（方案 §9.2 形态一）→ 红。"""
    repo = _make_repo(
        tmp_path,
        {
            "pkg_a/plugin.py": (
                "def handler():\n"
                "    from pkg_b import _hidden\n"
                "    return _hidden\n"
            ),
            "pkg_b/__init__.py": "PUBLIC_API = 1\n",
        },
    )
    proc = _run(repo)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "pkg_a/plugin.py:2" in proc.stdout
    assert "函数体内" in proc.stdout


def test_conditional_import_detected(tmp_path: Path) -> None:
    """条件导入 try/except 与 if 两形态（方案 §9.2 形态二）→ 红。"""
    repo = _make_repo(
        tmp_path,
        {
            "pkg_a/plugin.py": (
                "try:\n"
                "    from pkg_b import _hidden\n"
                "except ImportError:\n"
                "    _hidden = None\n"
                "DEBUG = True\n"
                "if DEBUG:\n"
                "    import pkg_b._impl\n"
            ),
            "pkg_b/__init__.py": "PUBLIC_API = 1\n",
        },
    )
    proc = _run(repo)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "条件" in proc.stdout
    assert "_hidden" in proc.stdout
    assert "pkg_b._impl" in proc.stdout


def test_type_checking_import_detected(tmp_path: Path) -> None:
    """TYPE_CHECKING 块导入（方案 §9.2 形态四）→ 红。"""
    repo = _make_repo(
        tmp_path,
        {
            "pkg_a/plugin.py": (
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n"
                "    from pkg_b import _Secret\n"
            ),
            "pkg_b/__init__.py": "PUBLIC_API = 1\n",
        },
    )
    proc = _run(repo)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "TYPE_CHECKING" in proc.stdout
    assert "_Secret" in proc.stdout


def test_dynamic_import_detected(tmp_path: Path) -> None:
    """动态导入常量实参（方案 §9.2 形态三）：import_module 与 __import__ → 红。"""
    repo = _make_repo(
        tmp_path,
        {
            "pkg_a/plugin.py": (
                "import importlib\n"
                "mod = importlib.import_module('pkg_b._impl')\n"
                "legacy = __import__('pkg_b._impl', fromlist=['x'])\n"
            ),
            "pkg_b/__init__.py": "PUBLIC_API = 1\n",
        },
    )
    proc = _run(repo)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "动态" in proc.stdout
    assert "pkg_b._impl" in proc.stdout


def test_public_cross_import_green(tmp_path: Path) -> None:
    """跨包公共接口导入合法（v3 §3.3 禁私有非禁跨包）→ 绿。"""
    repo = _make_repo(
        tmp_path,
        {
            "pkg_a/plugin.py": "from pkg_b import PUBLIC_API\nimport pkg_b\n",
            "pkg_b/__init__.py": "PUBLIC_API = 1\n",
        },
    )
    proc = _run(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_same_pkg_private_import_green(tmp_path: Path) -> None:
    """同包私有消费合法（含相对导入与平铺裸名两形态）→ 绿。"""
    repo = _make_repo(
        tmp_path,
        {
            "pkg_a/plugin.py": (
                "from pkg_a import _impl\n"
                "from .helpers import _secret\n"
                "from pkg_a.helpers import _secret\n"
            ),
            "pkg_a/_impl.py": "_X = 1\n",
            "pkg_a/helpers.py": "_secret = 2\n",
        },
    )
    proc = _run(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_flat_layout_local_module_green(tmp_path: Path) -> None:
    """平铺歧义消解：顶层名在本包直下可解析 → 同包内部导入，绿。

    同构 isolation/server.py（sys.path 注入消费本包平铺模块 workspace.py）。
    """
    repo = _make_repo(
        tmp_path,
        {
            "pkg_flat/server.py": "from workspace import _isolation_config_path\n",
            "pkg_flat/workspace.py": "def _isolation_config_path():\n    return None\n",
        },
    )
    proc = _run(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_unresolved_dynamic_target_counted(tmp_path: Path) -> None:
    """动态导入非常量实参：静态不可达 → 不判违规但位点显性计数（数值精确断言）。

    场景三动态位点：函数体内非常量 + 顶层非常量（两处均计入）+ 顶层常量
    （常量走 R1 解析，不得混入未解析计数）→ non_constant_dynamic_imports=2。
    """
    repo = _make_repo(
        tmp_path,
        {
            "pkg_a/plugin.py": (
                "import importlib\n"
                "def load(name):\n"
                "    return importlib.import_module(name)\n"
                "mod = importlib.import_module('pkg_b')\n"
                "legacy = __import__(dynamic_name)\n"
            ),
            "pkg_b/__init__.py": "PUBLIC_API = 1\n",
        },
    )
    proc = _run(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "non_constant_dynamic_imports=2" in proc.stdout


def test_test_files_exempt(tmp_path: Path) -> None:
    """测试文件白盒豁免（test_*.py / conftest.py / tests/ 目录）→ 绿。"""
    repo = _make_repo(
        tmp_path,
        {
            "pkg_a/test_probe.py": "from pkg_b import _hidden\n",
            "pkg_a/conftest.py": "from pkg_b import _hidden\n",
            "pkg_a/tests/whitebox.py": "from pkg_b import _hidden\n",
            "pkg_b/__init__.py": "PUBLIC_API = 1\n",
        },
    )
    proc = _run(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_syntax_error_fail_closed(tmp_path: Path) -> None:
    """fail-closed：扫描面语法错 exit 2 拒判，不放行。"""
    repo = _make_repo(
        tmp_path,
        {
            "pkg_a/plugin.py": "def broken(:\n",
            "pkg_b/__init__.py": "PUBLIC_API = 1\n",
        },
    )
    proc = _run(repo)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "fail-closed" in (proc.stdout + proc.stderr)


def test_form_coverage_counts_visible(tmp_path: Path) -> None:
    """干净树输出形态计数：各静态形态样例在扫即计数可见（车道深度可审计）。"""
    repo = _make_repo(
        tmp_path,
        {
            "pkg_a/plugin.py": (
                "from typing import TYPE_CHECKING\n"
                "def f():\n"
                "    from pkg_b import PUBLIC_API\n"
                "if True:\n"
                "    from pkg_b import PUBLIC_API as A2\n"
                "if TYPE_CHECKING:\n"
                "    from pkg_b import PUBLIC_API as A3\n"
                "import importlib\n"
                "mod = importlib.import_module('pkg_b')\n"
            ),
            "pkg_b/__init__.py": "PUBLIC_API = 1\n",
        },
    )
    proc = _run(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for label in ("函数体内", "条件", "TYPE_CHECKING", "动态", "顶层"):
        assert label in proc.stdout


def test_real_repo_green() -> None:
    """负控制（防恒绿）：真实仓检查器实跑 exit 0（现库零违规基线）。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
