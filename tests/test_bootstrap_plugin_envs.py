"""bootstrap_plugin_envs 分类与状态判据测试（纯函数级，不依赖真实插件 venv）。

另含一条真实语料不变量（test_cohost_members_deps_subset_of_host_declaration）：
合宿成员的依赖必须落在 `_host/pyproject.toml` 声明内——成员 .venv 运行期零消费，
共享 venv 缺一项即该成员在宿主进程内 import 失败，且宿主 fail-fast 连坐全组
（实证：monitoring 缺 psutil → light_stable 组 monitor 面全体 502）。
"""

import importlib.util
import sys
import tomllib
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


@pytest.mark.parametrize("group", ["light", "light_stable", "review_pool"])
def test_classify_any_declared_group_is_cohost_member(tmp_path, group):
    """与内核 is_cohost_member 同源：声明任意组名即合宿成员（多组扩展）。

    只认 "light" 会把 light_stable 成员误判 independent——漏删冗余 venv 且
    launcher 回潮重建（kernel 侧 venv_provision 自愈按 is_cohost_member 跳过，
    两面不一致即磁盘回潮缺口）。
    """
    d = make_plugin(tmp_path, {"id": "x", "host_group": group}, [])
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


# ─── 真实语料不变量：合宿成员依赖 ⊆ _host 声明闭包 ─────────────────────


def _host_importable_names() -> set[str]:
    """`_host` 共享 venv 装得出的 import 名全集 = uv.lock 全部 registry 包 + 分发名→import 名映射。

    用 lock 闭包（含传递依赖）而非 pyproject 直依赖：pyyaml 等由依赖链带入的包
    同样可 import，判据应认。
    """
    lock = REPO / "plugins" / "shared" / "_host" / "uv.lock"
    data = tomllib.loads(lock.read_text(encoding="utf-8"))
    names = {
        B.norm(p["name"])
        for p in data.get("package", [])
        if "registry" in (p.get("source") or {})
    }
    # 分发名 → import 名的常见不一致（仅列本仓出现过的）
    dist_to_import = {"pyyaml": "yaml", "pillow": "PIL", "python-dateutil": "dateutil"}
    names |= {"agentos_plugin_sdk"}
    names |= {imp for dist, imp in dist_to_import.items() if dist in names}
    return names


def _repo_importable_names() -> set[str]:
    """仓库内可被合宿成员 import 的名字（非第三方依赖，不由 pip 提供）。

    合宿成员经 bootstrap_plugin 把「插件目录 + plugins/shared 根 + 组根（如
    tools/）」入 sys.path，故这些目录下的 .py 与子目录（含命名空间包，如
    tools/human/ 被 import 成 human.*）都是仓内可导入面，不属依赖缺口。
    """
    shared_root = REPO / "plugins" / "shared"
    names = {p.stem for p in shared_root.glob("*.py")}
    for parent in (shared_root, *[d for d in shared_root.iterdir() if d.is_dir()]):
        names |= {p.stem for p in parent.glob("*.py")}
        names |= {p.name for p in parent.iterdir() if p.is_dir() and p.name != "__pycache__"}
    return names


def _server_top_third_party(plugin_dir: Path) -> set[str]:
    """server.py 模块顶层的第三方 import 名（host.py 只 exec server.py，故 fail-fast 触发面在此）。

    剔除：标准库、成员自身目录下的模块（成员子包/平铺同目录模块）、仓库内可导入
    名字（plugins/shared 面）。
    """
    import ast

    local = {p.stem for p in plugin_dir.glob("*.py")}
    local |= {p.name for p in plugin_dir.iterdir() if p.is_dir()}
    stl = set(sys.stdlib_module_names)
    repo_mods = _repo_importable_names()

    server = plugin_dir / "server.py"
    if not server.exists():
        return set()
    tree = ast.parse(server.read_text(encoding="utf-8", errors="replace"))
    mods: set[str] = set()
    for node in tree.body:  # 仅模块顶层：函数/方法内的 lazy import 不触发 fail-fast
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
    return {m for m in mods if m not in stl and m not in local and m not in repo_mods}


def test_cohost_members_deps_subset_of_host_declaration():
    """每个合宿成员的 server.py 顶层第三方 import，必须能被 _host 共享 venv 装载。

    成员 .venv 运行期零消费（合宿成员跑在 _host 共享 venv 里），故 _host 的依赖
    闭包是成员依赖的唯一生效来源。缺一项 = 该成员 import 失败 → 宿主 fail-fast →
    同组全部成员同时不可用（fail-fast 连坐，非单插件降级）。
    """
    importable = _host_importable_names()

    members: list[tuple[str, Path]] = []
    for pattern in B.PLUGIN_GLOBS:
        for d in sorted(REPO.glob(pattern)):
            manifest = d / "plugin.json"
            if not manifest.exists():
                continue
            import json

            meta = json.loads(manifest.read_text(encoding="utf-8"))
            if meta.get("host_group"):
                members.append((str(meta.get("id")), d))

    assert members, "真实语料应扫到合宿成员（host_group 声明），实际 0——扫描判据失效"

    gaps: dict[str, list[str]] = {}
    for pid, d in members:
        missing = sorted(
            m
            for m in _server_top_third_party(d)
            if m not in importable and B.norm(m) not in importable
        )
        if missing:
            gaps[pid] = missing

    assert not gaps, (
        "合宿成员依赖未落在 plugins/shared/_host/ 的依赖闭包内（共享 venv 装不上 → "
        f"宿主 fail-fast 连坐全组；补进 _host/pyproject.toml 后重跑 uv lock）: {gaps}"
    )
