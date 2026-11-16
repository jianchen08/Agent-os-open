# @feature: FP-GATE 共享根裸名导入防漂移闸 | @ci: python-coverage
"""check_shared_root_imports.py 单测（审查总册 E2 建闸，2026-10-16 拍板 API 化）。

行为契约（断输入→输出，不钉实现）：
- 命中样例红：生产码裸名 import 共享根模块（from/import 两形态、顶层与函数内
  惰性同管）→ exit 1，报真名 file:line；
- 出口形态绿：``from _root_api import ...`` → exit 0；
- 豁免面：共享根直下文件兄弟 import、出口包自身、测试文件（tests/ 目录、
  test_*.py、conftest.py）→ exit 0；
- 扫描面含 scripts/：脚本生产码裸名 import 红、出口形态绿（R5 遗留②清偿，
  scripts/ 并入扫描面）；
- 禁区动态枚举：共享根新落根模块未出口即入禁（对未知模块名同样红）；
- fail-closed：扫描面语法错 → exit 2 拒判；
- 负控制（防恒绿）：真实仓状态与出口切换后现状一致，检查器子进程实跑 exit 0。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_shared_root_imports", SCRIPTS / "check_shared_root_imports.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """临时仓树：按仓库相对路径落文件，模块三个根常量指向它。"""
    repo = tmp_path / "repo"
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return repo


def _patch_roots(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.setattr(mod, "ROOT", repo)
    monkeypatch.setattr(mod, "SHARED_ROOT", repo / "plugins" / "shared")
    monkeypatch.setattr(
        mod, "SCAN_ROOTS", (repo / "plugins", repo / "scripts")
    )


_OUTLET_INIT = '"""\n"""\nfrom http_json import json_response\n\n__all__ = ["json_response"]\n'
_SHARED_MOD = "def fake_shared():\n    return 1\n"


def test_from_import_hit_red(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """from 形态命中红：报真名与出口指引。"""
    repo = _make_repo(
        tmp_path,
        {
            "plugins/shared/http_json.py": _SHARED_MOD,
            "plugins/shared/foo/plugin.py": "from http_json import json_response\n",
        },
    )
    _patch_roots(monkeypatch, repo)
    assert mod.main() == 1


def test_import_forms_hit_red(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """import 两形态（顶层裸名 + 函数内惰性 + as 别名）均命中红。"""
    repo = _make_repo(
        tmp_path,
        {
            "plugins/shared/http_json.py": _SHARED_MOD,
            "plugins/shared/foo/plugin.py": (
                "import http_json\n"
                "import http_json as hj\n"
                "\n"
                "def go():\n"
                "    import http_json\n"
                "    return http_json\n"
            ),
        },
    )
    _patch_roots(monkeypatch, repo)
    assert mod.main() == 1


def test_outlet_form_green(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """出口形态绿：from _root_api import 不违规。"""
    repo = _make_repo(
        tmp_path,
        {
            "plugins/shared/http_json.py": _SHARED_MOD,
            "plugins/shared/_root_api/__init__.py": _OUTLET_INIT,
            "plugins/shared/foo/plugin.py": "from _root_api import json_response\n",
        },
    )
    _patch_roots(monkeypatch, repo)
    assert mod.main() == 0


@pytest.mark.parametrize(
    ("script_src", "expect"),
    [
        ("import http_json\n", 1),
        ("import http_json as hj\n", 1),
        ("from _root_api import http_json\n", 0),
    ],
    ids=["bare-red", "alias-red", "outlet-green"],
)
def test_scripts_face_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script_src: str, expect: int
) -> None:
    """scripts/ 在扫描面：脚本生产码裸名红、出口形态绿（豁免已摘除）。"""
    repo = _make_repo(
        tmp_path,
        {
            "plugins/shared/http_json.py": _SHARED_MOD,
            "plugins/shared/_root_api/__init__.py": _OUTLET_INIT,
            "scripts/some_ops.py": script_src,
        },
    )
    _patch_roots(monkeypatch, repo)
    assert mod.main() == expect


def test_shared_root_sibling_import_exempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """共享根直下文件兄弟 import 豁免（实现区内部）。"""
    repo = _make_repo(
        tmp_path,
        {
            "plugins/shared/user_space.py": _SHARED_MOD,
            "plugins/shared/tenant_data.py": "import user_space\n",
        },
    )
    _patch_roots(monkeypatch, repo)
    assert mod.main() == 0


def test_outlet_self_import_exempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """出口包自身裸名 import 豁免（出口本体）。"""
    repo = _make_repo(
        tmp_path,
        {
            "plugins/shared/http_json.py": _SHARED_MOD,
            "plugins/shared/_root_api/__init__.py": _OUTLET_INIT,
        },
    )
    _patch_roots(monkeypatch, repo)
    assert mod.main() == 0


@pytest.mark.parametrize(
    "rel",
    [
        "tests/test_x.py",
        "plugins/shared/foo/test_x.py",
        "plugins/shared/foo/tests/test_y.py",
        "plugins/shared/foo/conftest.py",
    ],
)
def test_test_files_exempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rel: str
) -> None:
    """测试文件白盒消费豁免（tests/ 目录、test_*.py、conftest.py）。"""
    repo = _make_repo(
        tmp_path,
        {
            "plugins/shared/http_json.py": _SHARED_MOD,
            rel: "from http_json import json_response\n",
        },
    )
    _patch_roots(monkeypatch, repo)
    assert mod.main() == 0


def test_new_root_module_dynamically_banned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删值实验面：禁区=共享根直下 *.py 动态枚举，新落根模块未出口即违规。"""
    repo = _make_repo(
        tmp_path,
        {
            "plugins/shared/brand_new_mod.py": _SHARED_MOD,
            "plugins/shared/foo/plugin.py": "from brand_new_mod import thing\n",
        },
    )
    _patch_roots(monkeypatch, repo)
    assert mod.main() == 1


def test_banned_set_covers_real_shared_root() -> None:
    """禁区推导真仓校验：动态枚举须覆盖存量根模块（推导被剪除即红）。"""
    banned = set(mod.banned_modules())
    assert {"http_json", "tenant_data", "user_space", "zone_policy"} <= banned


def test_syntax_error_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fail-closed：扫描面语法错 exit 2 拒判，不放行。"""
    repo = _make_repo(
        tmp_path,
        {
            "plugins/shared/foo/plugin.py": "def broken(:\n",
        },
    )
    _patch_roots(monkeypatch, repo)
    with pytest.raises(SystemExit) as excinfo:
        mod.main()
    assert excinfo.value.code == 2


def test_real_repo_green() -> None:
    """负控制（防恒绿）：真实仓（出口切换后现状）检查器实跑 exit 0。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_shared_root_imports.py")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
