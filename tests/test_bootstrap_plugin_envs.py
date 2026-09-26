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
    另有成员在代码内自行 sys.path 注入兄弟插件目录 / scripts 子目录
    （实证：task_evaluate 注入 system/tasks 解析 task_types；eval_harness
    的 ws_batch 注入 scripts/eval_bench 解析 kernel_client）——凡仓内同面
    .py 均计入（有界定深扫，禁递归 glob：dsh_adapter 自嵌套 junction 会挂死）。
    """
    shared_root = REPO / "plugins" / "shared"
    names = {p.stem for p in shared_root.glob("*.py")}
    for parent in (shared_root, *[d for d in shared_root.iterdir() if d.is_dir()]):
        names |= {p.stem for p in parent.glob("*.py")}
        names |= {p.name for p in parent.iterdir() if p.is_dir() and p.name != "__pycache__"}
    # 插件目录层（system/*/ tools/*/ modes/*/ pipeline/*/*/）与 scripts 两层：
    # 兄弟插件模块与 eval_bench 脚本面
    for sub in ("system/*", "tools/*", "modes/*", "pipeline/*/*"):
        for d in shared_root.glob(sub):
            if d.is_dir():
                names |= {p.stem for p in d.glob("*.py")}
    scripts = REPO / "scripts"
    names |= {p.stem for p in scripts.glob("*.py")}
    for d in scripts.iterdir():
        if d.is_dir() and d.name != "__pycache__":
            names |= {p.stem for p in d.glob("*.py")}
    return names


def _plugin_top_third_party(plugin_dir: Path) -> set[str]:
    """成员目录全部非测试 .py 模块顶层的第三方 import 名。

    扫 server.py 之外还要扫其余顶层 .py：成员可在调用时才 exec 加载模块
    （实证 web_ext `_load_web_tool()` 懒加载 tool.py，其顶层 `import httpx`
    在每次工具调用时才触发 ModuleNotFoundError——load 期 fail-fast 抓不到，
    只能靠静态扫面兜住）。剔除：test_*/conftest.py（只在 pytest 车道运行，
    不进宿主 venv）、标准库、成员自身目录下的模块（成员子包/平铺同目录
    模块）、仓库内可导入名字（plugins/shared 面）；函数/方法内的 lazy import
    静态扫不到，属登记面不在本门禁。
    """
    import ast

    local = {p.stem for p in plugin_dir.glob("*.py")}
    local |= {p.name for p in plugin_dir.iterdir() if p.is_dir()}
    stl = set(sys.stdlib_module_names)
    repo_mods = _repo_importable_names()

    mods: set[str] = set()
    for py in sorted(plugin_dir.glob("*.py")):
        if py.stem.startswith("test") or py.stem == "conftest":
            continue
        tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        for node in tree.body:  # 仅模块顶层
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
            for m in _plugin_top_third_party(d)
            if m not in importable and B.norm(m) not in importable
        )
        if missing:
            gaps[pid] = missing

    assert not gaps, (
        "合宿成员依赖未落在 plugins/shared/_host/ 的依赖闭包内（共享 venv 装不上 → "
        f"宿主 fail-fast 连坐全组；补进 _host/pyproject.toml 后重跑 uv lock）: {gaps}"
    )


def _host_installed_dists() -> set[str]:
    """`_host/.venv` 实装发行包归一名全集（site-packages/*.dist-info 扫描）。

    与 uv.lock 判据互补：lock 是「声明闭包」，实装是「磁盘真值」——lock 更新了
    但忘 `uv sync` 时本判据仍红（打包 extraResources 整目录照搬 _host/.venv，
    磁盘真值就是装机版运行时真值）。
    """
    import re

    sp = REPO / "plugins" / "shared" / "_host" / ".venv" / "Lib" / "site-packages"
    if not sp.is_dir():  # Unix 布局
        candidates = sorted((REPO / "plugins" / "shared" / "_host" / ".venv").glob("lib/python*/site-packages"))
        assert candidates, "_host/.venv site-packages 不存在，先 uv sync --project plugins/shared/_host"
        sp = candidates[0]
    dist_re = re.compile(r"^(.+)-(\d.*)\.dist-info$", re.IGNORECASE)
    return {B.norm(m.group(1)) for e in sp.iterdir() if (m := dist_re.match(e.name))}


def test_cohost_members_pyproject_deps_installed_in_host_venv():
    """每个合宿成员 pyproject 声明的依赖，必须已实装进 _host/.venv（磁盘真值）。

    lock 判据（上一条）抓「声明缺依赖」，本条抓「声明/锁已补但 venv 没 sync」——
    两者任一漏网，装机版都按同样方式炸（包内 _host/.venv 整目录照搬 dev 磁盘）。
    SDK 是 editable 本地源不在 dist-info 口径内，豁免。
    """
    installed = _host_installed_dists()

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
            name
            for name in B.pyproject_dep_names(d)
            if name != "agentos-plugin-sdk" and name not in installed
        )
        if missing:
            gaps[pid] = missing

    assert not gaps, (
        "合宿成员 pyproject 依赖未实装进 plugins/shared/_host/.venv（打包整目录照搬该 "
        f"venv，装机版将 ModuleNotFoundError；在 _host 目录跑 uv sync）: {gaps}"
    )


# ---------- _plugin_top_third_party 扫面单元用例 ----------


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_scan_catches_lazy_loaded_module_import(tmp_path):
    """调用时才 exec 的模块（tool.py）顶层 import 必须在扫面内——web_ext 实证形态。

    回归锚：仅扫 server.py 时 web_ext 的 `import httpx` 藏在懒加载 tool.py 里，
    18 次搜索调用期 ModuleNotFoundError 门禁全绿。
    """
    d = tmp_path / "lazy_member"
    d.mkdir()
    _write(d / "plugin.json", '{"id": "lazy_member", "host_group": "light"}')
    _write(d / "server.py", "def _load():\n    pass\n")
    _write(d / "tool.py", "import httpx\nimport os\n\n\ndef run():\n    return httpx\n")

    mods = _plugin_top_third_party(d)
    assert "httpx" in mods, "懒加载模块顶层第三方 import 必须被扫出"
    assert "os" not in mods, "标准库不算依赖缺口"


def test_scan_ignores_test_and_conftest_files(tmp_path):
    """test_*.py / conftest.py 只在 pytest 车道运行，不进宿主 venv，必须豁免。"""
    d = tmp_path / "tested_member"
    d.mkdir()
    _write(d / "server.py", "import yaml\n")
    _write(d / "test_web_tool.py", "import httpx\nimport pytest\n")
    _write(d / "conftest.py", "import pytest\n")

    mods = _plugin_top_third_party(d)
    assert mods == {"yaml"}, f"测试面 import 不得计入依赖缺口，实际 {mods}"


def test_scan_never_fires_without_member_python_files(tmp_path):
    """空目录/无 .py 成员返回空集（不误报）。"""
    d = tmp_path / "empty_member"
    d.mkdir()
    assert _plugin_top_third_party(d) == set()


def test_scan_importform_top_level(tmp_path):
    """from-import 顶层形态同样入扫（与 server.py 时代同口径）。"""
    d = tmp_path / "fromform_member"
    d.mkdir()
    _write(d / "server.py", "from PIL import Image\nfrom . import helper\n")

    mods = _plugin_top_third_party(d)
    assert "PIL" in mods
    assert "helper" not in mods, "相对导入非第三方依赖"
