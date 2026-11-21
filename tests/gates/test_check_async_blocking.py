# @feature: FP-0.2.一 插件协议 | @ci: python-coverage
"""check_async_blocking.py 单测（样本树断言，不污染生产目录）。

行为契约（断输入→输出，不钉实现）：
- 正例爆红：async def 体内路径形态接收者的文件 I/O 原语（resolve/exists/
  read_text/open/os.walk）逐处命中，条目格式 file::func::call::line；
- 基线放行：存量键（file::func::call，行号漂移不影响匹配）在基线内 → 绿；
  基线外新增 → exit 1 且 stderr 列出新增项；
- 合法出口放行：``await asyncio.to_thread(...)`` / ``await run_blocking(...)``
  参数子树不下探；嵌套同步 def（扫描体提取习语）不下探；
  嵌套 async def 照常下探（await 时仍在事件循环执行）；
- 同名噪声排除：``dict.copy`` / ``str.replace`` / ``data.get`` 等非路径形态
  接收者不计（方法原语必须路径形态接收者才计）；
- 排除面：test_*.py 与 tests/** 目录不扫（pytest 独立进程不在事件循环）；
- 模块级原语无条件计：shutil.* / subprocess.* / time.sleep / os.listdir；
- --generate 产出基线（含当前全部命中）；--baseline-monotonic 对照 base
  版本：新增键红、只减放行、base 处无基线文件（adoption 前区间）跳过；
- 负控制（防恒绿）：本仓真实树上检查器实跑绿（存量都在基线内）。
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
    spec = importlib.util.spec_from_file_location("check_async_blocking", SCRIPTS / "check_async_blocking.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _make_tree(tmp_path: Path, rel: str, source: str) -> Path:
    """样本树：root/plugins/shared/<rel>（检查器扫描根的既有布局）。"""
    path = tmp_path / "plugins" / "shared" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _violations(tmp_path: Path) -> list:
    return mod.scan(tmp_path)


def _keys(violations: list) -> set[str]:
    return {v.key for v in violations}


# ── 正例爆红（逐原语族）──────────────────────────────────────────────


def test_path_method_primitives_flagged(tmp_path: Path) -> None:
    """路径形态接收者的方法原语逐处命中（resolve/exists/read_text/mkdir）。"""
    _make_tree(
        tmp_path,
        "sample.py",
        """
from pathlib import Path


async def handler(file_path: str) -> dict:
    p = Path(file_path)
    resolved = p.resolve()
    ok = resolved.exists()
    text = p.read_text("utf-8")
    p.parent.mkdir(parents=True, exist_ok=True)
    return {"ok": ok, "text": text, "resolved": str(resolved)}
""",
    )
    vs = _violations(tmp_path)
    keys = {(v.func, v.call) for v in vs}
    assert ("handler", "p.resolve") in keys
    assert ("handler", "resolved.exists") in keys
    assert ("handler", "p.read_text") in keys
    assert ("handler", "p.parent.mkdir") in keys
    assert all(v.file == "plugins/shared/sample.py" for v in vs)
    assert all("::" in v.entry for v in vs)


def test_module_primitives_flagged_unconditionally(tmp_path: Path) -> None:
    """模块级原语无条件计：os.walk / shutil.copytree / subprocess.run / time.sleep / open。"""
    _make_tree(
        tmp_path,
        "sample2.py",
        """
import os
import shutil
import subprocess
import time


async def handler(root: str) -> list:
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        hits.append(filenames)
    shutil.copytree(root, root + ".bak")
    subprocess.run(["ls"], capture_output=True)
    time.sleep(1)
    fh = open(root + "/x", "rb")
    fh.close()
    return hits
""",
    )
    calls = {v.call for v in _violations(tmp_path)}
    assert "os.walk" in calls
    assert "shutil.copytree" in calls
    assert "subprocess.run" in calls
    assert "time.sleep" in calls
    assert "open" in calls


def test_builtin_open_with_binding_flags_handle_reads(tmp_path: Path) -> None:
    """with open(...) 绑定名上的 .read 与 open 同窗计（真实 I/O 面）。"""
    _make_tree(
        tmp_path,
        "sample3.py",
        """
async def handler(path: str) -> bytes:
    with open(path, "rb") as fh:
        head = fh.read(64 * 1024)
    return head
""",
    )
    calls = {v.call for v in _violations(tmp_path)}
    assert "open" in calls
    assert "fh.read" in calls


# ── 同名噪声排除（接收者必须路径形态）───────────────────────────────


def test_non_path_receiver_noise_not_flagged(tmp_path: Path) -> None:
    """dict.copy / str.replace / dict.get 等同名噪声必须排除。"""
    _make_tree(
        tmp_path,
        "sample4.py",
        """
async def handler(data: dict, text: str, config: dict) -> dict:
    merged = data.copy()
    replaced = text.replace("a", "b")
    value = config.get("path", "")
    items = list(data.items())
    return {"merged": merged, "replaced": replaced, "value": value, "items": items}
""",
    )
    assert _violations(tmp_path) == []


def test_non_path_receiver_replace_not_flagged(tmp_path: Path) -> None:
    """路径形态名变量上的 str.replace 不误报（replace 不在原语集）。"""
    _make_tree(
        tmp_path,
        "sample4b.py",
        """
async def handler(path: str) -> str:
    return path.replace("\\\\", "/")
""",
    )
    assert _violations(tmp_path) == []


# ── 合法出口放行 ─────────────────────────────────────────────────────


def test_to_thread_and_run_blocking_offload(tmp_path: Path) -> None:
    """to_thread 参数子树与 run_blocking 调用放行（推荐习语零命中）。"""
    _make_tree(
        tmp_path,
        "sample5.py",
        """
import asyncio
from agentos_plugin_sdk import run_blocking


async def handler(file_path: str) -> str:
    resolved = await asyncio.to_thread(lambda: __import__("pathlib").Path(file_path).resolve())
    text = await run_blocking(_read_sync, file_path)
    return text


def _read_sync(path: str) -> str:
    return open(path, "rb").read().decode("utf-8")
""",
    )
    vs = _violations(tmp_path)
    async_hits = [v for v in vs if v.func == "handler"]
    assert async_hits == [], f"合法出口不应命中: {[v.entry for v in async_hits]}"


def test_nested_sync_def_offloaded_not_flagged(tmp_path: Path) -> None:
    """嵌套同步 def（扫描体提取习语）不下探；外层直接阻塞照报。"""
    _make_tree(
        tmp_path,
        "sample6.py",
        """
import os
from pathlib import Path


async def handler(root: str) -> list:
    results: list = []

    def _scan_sync() -> None:
        for dirpath, dirnames, filenames in os.walk(root):
            results.append((dirpath, Path(dirpath).resolve()))

    import asyncio

    await asyncio.to_thread(_scan_sync)
    bad = Path(root).exists()
    return results if bad else []
""",
    )
    calls = {v.call for v in _violations(tmp_path)}
    assert "Path(root).exists" in calls or any("exists" in c for c in calls)
    assert "os.walk" not in calls, "嵌套同步 def 体内不应命中（下放线程池习语）"


def test_nested_async_def_still_flagged(tmp_path: Path) -> None:
    """嵌套 async def await 时仍在事件循环执行 → 照常下探命中。"""
    _make_tree(
        tmp_path,
        "sample7.py",
        """
from pathlib import Path


async def outer(path: str) -> None:
    async def inner() -> None:
        Path(path).resolve()

    await inner()
""",
    )
    funcs = {v.func for v in _violations(tmp_path)}
    assert "outer.inner" in funcs


# ── 排除面 ───────────────────────────────────────────────────────────


def test_test_files_and_dirs_excluded(tmp_path: Path) -> None:
    """test_*.py 文件与 tests/** 目录不扫（pytest 独立进程不在事件循环）。"""
    _make_tree(tmp_path, "test_sample.py", "from pathlib import Path\n\n\nasync def t() -> None:\n    Path('x').resolve()\n")
    _make_tree(tmp_path, "tests/helper.py", "from pathlib import Path\n\n\nasync def t() -> None:\n    Path('x').resolve()\n")
    _make_tree(tmp_path, "other_test.py", "from pathlib import Path\n\n\nasync def t() -> None:\n    Path('x').resolve()\n")
    assert _violations(tmp_path) == []


def test_non_async_def_not_scanned(tmp_path: Path) -> None:
    """同步 def 不在扫描面（SDK _invoke_callable 已对同步函数下放线程池）。"""
    _make_tree(
        tmp_path,
        "sample8.py",
        """
from pathlib import Path


def sync_handler(path: str) -> str:
    return str(Path(path).resolve())
""",
    )
    assert _violations(tmp_path) == []


# ── 基线机制 ─────────────────────────────────────────────────────────


def _write_baseline(tmp_path: Path, violations: list) -> Path:
    baseline = tmp_path / ".github" / "async-blocking-baseline.txt"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(
        "# async 内同步阻塞基线（逐项棘轮，只减不增）\n" + "".join(f"{v.entry}\n" for v in violations),
        encoding="utf-8",
    )
    return baseline


def test_baseline_admission_and_line_drift_tolerance(tmp_path: Path) -> None:
    """基线内存量放行；行号漂移不产生假红（匹配键 = 前 3 段）。"""
    _make_tree(
        tmp_path,
        "sample9.py",
        """
from pathlib import Path


async def handler(path: str) -> str:
    return str(Path(path).resolve())
""",
    )
    vs = _violations(tmp_path)
    assert len(vs) == 1
    baseline = _write_baseline(tmp_path, vs)
    assert mod.check(tmp_path, baseline) == 0

    # 行号漂移：基线条目行号改写后仍按 3 段键匹配
    drifted = baseline.read_text(encoding="utf-8").replace(f"::{vs[0].line}", "::99999")
    baseline.write_text(drifted, encoding="utf-8")
    assert mod.check(tmp_path, baseline) == 0


def test_new_violation_outside_baseline_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """基线外新增违规 → exit 1，stderr 列出新增项。"""
    _make_tree(
        tmp_path,
        "sample10.py",
        """
from pathlib import Path


async def handler(path: str) -> str:
    return str(Path(path).resolve())


async def other(path: str) -> bool:
    return Path(path).exists()
""",
    )
    vs = _violations(tmp_path)
    baseline = _write_baseline(tmp_path, vs[:1])  # 只收编一处，另一处为新增
    rc = mod.check(tmp_path, baseline)
    assert rc == 1
    err = capsys.readouterr().err
    assert "新增违规" in err
    assert "handler" in err or "other" in err


def test_stale_baseline_entries_advisory_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """存量修复后基线残留 → 只提示收缩，不阻断（基线只减不增）。"""
    baseline = tmp_path / ".github" / "async-blocking-baseline.txt"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(
        "# 已修复面残留\nplugins/shared/ghost.py::handler::Path(...).resolve::10\n",
        encoding="utf-8",
    )
    rc = mod.check(tmp_path, baseline)
    out = capsys.readouterr().out
    assert rc == 0
    assert "收缩" in out


def _git(tmp_path: Path, *args: str) -> None:
    proc = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_generate_then_monotonic_ratchet(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--generate 产出基线；--baseline-monotonic 对照 base：新增键红、只减放行。

    adoption 前区间（base 提交处无基线文件）跳过放行。
    """
    baseline = tmp_path / ".github" / "async-blocking-baseline.txt"
    _make_tree(
        tmp_path,
        "sample11.py",
        """
from pathlib import Path


async def handler(path: str) -> str:
    return str(Path(path).resolve())
""",
    )
    _git(tmp_path, "init")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base-without-baseline")

    # base（HEAD）处无基线文件 → 账本棘轮跳过（adoption 前区间）
    assert mod.baseline_monotonic(tmp_path, baseline, "HEAD") == 0

    baseline.parent.mkdir(parents=True, exist_ok=True)
    assert mod.generate(tmp_path, baseline) == 0
    assert baseline.exists()
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "adopt-baseline")

    # 基线覆盖当前树：账本棘轮绿 + 常规执法绿
    assert mod.baseline_monotonic(tmp_path, baseline, "HEAD") == 0

    # 新增违规而不扩基线 → 账本棘轮红（只许减不许增）
    _make_tree(
        tmp_path,
        "sample12.py",
        """
import os


async def walker(root: str) -> list:
    return list(os.walk(root))
""",
    )
    assert mod.baseline_monotonic(tmp_path, baseline, "HEAD") == 1
    out = capsys.readouterr()
    assert "棘轮" in out.out + out.err or "新增" in out.out + out.err


def test_main_exit_zero_on_clean_tree(tmp_path: Path) -> None:
    """干净树（无 plugins 扫描面）执法绿（main 全链路）。"""
    rc = mod.main(["--root", str(tmp_path), "--baseline", str(tmp_path / "b.txt")])
    assert rc == 0


# ── 调用图穿透（1d，2026-11-18 装机事故同款习语）────────────────────


def test_call_graph_penetration_flags_hidden_scan_helper(tmp_path: Path) -> None:
    """真实案例夹具（_reload→_scan_plugin_dirs 形态）：旧口径漏检、新口径爆红。

    本仓习语「扫描体提取模块级同步函数 + async 体只留直调」让字面原语扫描
    完全隐身（2026-11-18 两个实锤卡点即此形态且从未被检出）。穿透语义：
    async 体直调同模块模块级同步 helper → helper 体内原语归因回 async 函数，
    条目 = file::func::via:helper::原语::helper内行号（完整链路一眼能修）。
    """
    _make_tree(
        tmp_path,
        "host_like.py",
        """
import os


def _scan_plugin_dirs(root: str) -> list:
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        hits.append((dirpath, filenames))
    return hits


async def _reload(plugin_id: str, root: str) -> list:
    return _scan_plugin_dirs(root)
""",
    )
    vs = _violations(tmp_path)
    # 旧口径（async 体字面原语）对此样本零命中——async 体只有直调无原语
    literal = [v for v in vs if not v.call.startswith("via:")]
    assert literal == [], f"旧口径字面命中应为空（习语隐身实证）: {[v.entry for v in literal]}"
    # 新口径穿透爆红：归因回 _reload，helper 名可见
    assert [(v.func, v.call) for v in vs] == [("_reload", "via:_scan_plugin_dirs")]
    v = vs[0]
    assert v.via_call == "os.walk"
    # 完整链路（async 函数 → helper → 原语）在条目里一眼能修
    assert "_reload" in v.entry and "via:_scan_plugin_dirs" in v.entry and "os.walk" in v.entry
    # 键稳定：helper 内原语形态/行号不参与（增减原语不产生基线漂移）
    assert v.key == f"plugins/shared/host_like.py::_reload::via:_scan_plugin_dirs"


def test_to_thread_offloaded_helper_not_penetrated(tmp_path: Path) -> None:
    """to_thread(helper) 合法出口不穿透（参数下探跳过规则照抄现有实现）。"""
    _make_tree(
        tmp_path,
        "offloaded.py",
        """
import asyncio
import os


def _scan_plugin_dirs(root: str) -> list:
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        hits.append((dirpath, filenames))
    return hits


async def _reload(root: str) -> list:
    return await asyncio.to_thread(_scan_plugin_dirs, root)
""",
    )
    assert _violations(tmp_path) == []


def test_each_helper_attributed_once_per_async_func(tmp_path: Path) -> None:
    """护栏：同一 helper 被同一 async 函数多次直调只归因一次（不刷屏）。"""
    _make_tree(
        tmp_path,
        "multi_call.py",
        """
import os


def _scan(root: str) -> list:
    return [d for d, _, _ in os.walk(root)]


async def handler(root: str) -> list:
    first = _scan(root)
    second = _scan(root)
    return first + second
""",
    )
    vs = _violations(tmp_path)
    assert [(v.func, v.call) for v in vs] == [("handler", "via:_scan")]


def test_penetration_entry_roundtrips_through_baseline(tmp_path: Path) -> None:
    """穿透条目写入基线后可解析回放（5 段形态），执法绿；普通 4 段同行共存。"""
    _make_tree(
        tmp_path,
        "roundtrip.py",
        """
import os


def _scan(root: str) -> list:
    return [d for d, _, _ in os.walk(root)]


async def handler(root: str) -> list:
    return _scan(root)


async def direct(path: str) -> bool:
    from pathlib import Path

    return Path(path).exists()
""",
    )
    vs = _violations(tmp_path)
    via = [v for v in vs if v.call.startswith("via:")]
    plain = [v for v in vs if not v.call.startswith("via:")]
    assert len(via) == 1 and len(plain) == 1
    baseline = _write_baseline(tmp_path, vs)
    assert mod.check(tmp_path, baseline) == 0
    # 解析键：穿透键 = file::func::via:helper（helper 内原语形态不参与）
    keys = mod.parse_baseline(baseline)
    assert f"plugins/shared/roundtrip.py::handler::via:_scan" in keys
    assert any("::direct::Path(...).exists" in k for k in keys)


def test_helper_without_io_primitives_not_penetrated(tmp_path: Path) -> None:
    """helper 体内无 IO 原语（纯计算）→ 直调不归因（穿透只盯原语命中面）。"""
    _make_tree(
        tmp_path,
        "pure.py",
        """
def _transform(items: list) -> list:
    return [x.upper() for x in items]


async def handler(items: list) -> list:
    return _transform(items)
""",
    )
    assert _violations(tmp_path) == []


def test_reverted_reload_direct_call_form_fails(tmp_path: Path) -> None:
    """人为回退验证：_reload 直调形态（1b 修复前）必须爆红——闸门顶塞语义。

    对当前真实仓 host.py（to_thread 形态）不报由 test_checker_runs_green_as_
    subprocess 覆盖；本条用最小样本锁定「回退即红」，防穿透语义回退。
    """
    _make_tree(
        tmp_path,
        "reverted.py",
        """
import os


def _scan_plugin_dirs(roots) -> dict:
    out = {}
    for dirpath, dirnames, filenames in os.walk(roots):
        if "plugin.json" in filenames:
            out[dirpath] = dirpath
    return out


async def _reload(plugin_id: str, roots: str) -> dict:
    return _scan_plugin_dirs(roots)
""",
    )
    vs = _violations(tmp_path)
    assert [v.call for v in vs] == ["via:_scan_plugin_dirs"], "回退直调形态必须爆红（闸门顶塞）"


# ── 负控制（防恒绿）：真实仓实跑 ─────────────────────────────────────


def test_real_repo_gate_green_and_baseline_exists() -> None:
    """真实仓：车道对当前树绿（存量都在基线内）+ 基线文件在位。"""
    baseline_path = REPO_ROOT / ".github" / "async-blocking-baseline.txt"
    assert baseline_path.exists(), "基线文件缺失（车道 adoption 未完成）"
    rc = mod.check(REPO_ROOT, baseline_path)
    assert rc == 0, "真实树存在基线外新增违规"


def test_checker_runs_green_as_subprocess() -> None:
    """检查器子进程实跑退出 0（与 run_gates 车道同口径）。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_async_blocking.py")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0, f"检查器实跑红:\n{proc.stdout}\n{proc.stderr}"
