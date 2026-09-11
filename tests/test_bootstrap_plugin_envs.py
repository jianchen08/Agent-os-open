"""bootstrap_plugin_envs 分类与状态判据测试（纯函数级，不依赖真实插件 venv）。"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "bootstrap_plugin_envs", REPO / "scripts" / "bootstrap_plugin_envs.py"
)
assert _SPEC is not None and _SPEC.loader is not None  # 同仓固定路径，装配必成功
B = importlib.util.module_from_spec(_SPEC)
sys.modules["bootstrap_plugin_envs"] = B  # dataclass 装饰器需按模块名查 sys.modules
_SPEC.loader.exec_module(B)


def make_plugin(tmp_path: Path, manifest: dict | None, lock: dict | None) -> Path:
    d = tmp_path / "some_plugin"
    d.mkdir()
    if manifest is not None:
        import json

        (d / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    if lock is not None:
        lines = []
        for name, ver, source in lock:
            lines.append(f'[[package]]\nname = "{name}"\nversion = "{ver}"\nsource = {source}\n')
        (d / "uv.lock").write_text("\n".join(lines), encoding="utf-8")
    return d


SHARED = {"mcp": "2.1.1", "pydantic": "2.13.4", "pyyaml": "6.0.2"}


def test_norm_pep503():
    assert B.norm("PyYAML") == "pyyaml"
    assert B.norm("mcp_types") == "mcp-types"
    assert B.norm("AgentOS--Plugin__SDK") == "agentos-plugin-sdk"


def test_lock_packages_only_registry_entries(tmp_path):
    d = make_plugin(
        tmp_path,
        None,
        [
            ("some-plugin", "1.0.0", '{ virtual = "." }'),
            ("agentos-plugin-sdk", "0.2.0", '{ editable = "../../sdk" }'),
            ("mcp", "2.1.1", '{ registry = "https://pypi.org/simple" }'),
            ("pyyaml", "6.0.2", '{ registry = "https://pypi.org/simple" }'),
        ],
    )
    assert B.lock_packages(d) == {"mcp": "2.1.1", "pyyaml": "6.0.2"}


def test_lock_packages_git_source_excluded_fail_closed(tmp_path):
    d = make_plugin(tmp_path, None, [("gitpkg", "1.0", '{ git = "https://x" }')])
    assert B.lock_packages(d) == {}
    assert B.classify(d, {}) == "independent"


def test_lock_packages_missing_lock_is_none(tmp_path):
    d = make_plugin(tmp_path, {"id": "x"}, None)
    assert B.lock_packages(d) is None
    assert B.classify(d, SHARED) == "independent"


def test_classify_light_by_manifest_regardless_of_lock(tmp_path):
    d = make_plugin(tmp_path, {"id": "x", "host_group": "light"}, [])
    assert B.classify(d, {}) == "light"


@pytest.mark.parametrize(
    ("locked", "shared", "expect"),
    [
        ({"mcp": "2.1.1", "pydantic": "2.13.4"}, SHARED, "linkable"),
        ({"mcp": "2.1.1"}, {"mcp": "2.1.1", "pyyaml": "6.0.2"}, "linkable"),
        ({"mcp": "2.0.0"}, SHARED, "independent"),
        ({"httpx": "1.0"}, SHARED, "independent"),
    ],
)
def test_classify_linkable_requires_version_subset(tmp_path, locked, shared, expect):
    d = make_plugin(
        tmp_path,
        {"id": "x"},
        [(n, v, '{ registry = "https://pypi.org/simple" }') for n, v in locked.items()],
    )
    assert B.classify(d, shared) == expect


def test_classify_invalid_manifest_is_independent(tmp_path):
    d = tmp_path / "broken"
    d.mkdir()
    (d / "plugin.json").write_text("{not json", encoding="utf-8")
    assert B.classify(d, SHARED) == "independent"


def test_venv_state_absent_and_real(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    assert B.venv_state(d) == "absent"
    (d / ".venv").mkdir()
    (d / ".venv" / "marker.txt").write_text("x", encoding="utf-8")
    assert B.venv_state(d) == "real"


@pytest.mark.skipif(sys.platform != "win32", reason="junction 为 Windows 机制")
def test_venv_state_junction(tmp_path):
    import _winapi

    d = tmp_path / "p"
    d.mkdir()
    _winapi.CreateJunction(str(B.SHARED_ENV), str(d / ".venv"))
    assert B.venv_state(d) == "junction"
