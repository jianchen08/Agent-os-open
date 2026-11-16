# @feature: FP-GATE None | @ci: python-coverage
"""PILOT-01 签发验收测试：watchdog 单进程内存集成测试对宿主水位鲁棒。

@feature PILOT-01 签发分离试点·watchdog 内存水位测试加固 | @vision V1 可进化

需求来源：docs/working/ARM9bash进程管理器_台账.md「遗留与教训」遗留 1
（宿主内存 ≥85% 时 test_watchdog_no_kill_when_per_unit_memory_under_threshold
必红——高水位清理按 idle 杀掉测试内 50MB 进程，误报）。

起草方：签发分离试点起草轮（2026-10-28）。
状态：**已冻结 v1.0**（2026-10-28 签发轮：复检通过后由签发轮落位于本路径，断言与逻辑零改动）。
签发记录（复检方/复检结论/签发方/C1-C3 裁决/F3·F4' 处置留档）：
docs/working/签发分离试点_20261028/试点方案.md §九。

注入点契约：宿主内存水位经 psutil.virtual_memory monkeypatch 注入
（psutil 为 bash 插件声明依赖，见 plugins/shared/bash/uv.lock）。
若实现方加固时采用其他水位观测源，属注入点契约变更，须回签发复核调整
本件（不在实现轮内改动本件——签发件只读）。

可渲染约定：测试名含需求 ID + 场景；四段式结构；失败报告人类化（describe）；
场景分组；断言到契约粒度；高位水两组 + 低位水一组（≥2 组有区分度输入 +
性质断言，防无条件 skip 或硬编码刷绿）。
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

# 前置契约：被加固的目标测试与仓根（子进程以仓根为 rootdir 运行）。
# 仓根锚定：从本文件位置向上探测 pyproject.toml——对草案位（docs/working/...）
# 与落位形态（tests/signed/）均鲁棒，不硬编码层级（复检 F1 修正，方案 b）。


def _locate_repo_root() -> Path:
    """向上逐级探测含 pyproject.toml 的目录作为仓根，找不到即显式失败。"""
    for d in Path(__file__).resolve().parents:
        if (d / "pyproject.toml").is_file():
            return d
    raise RuntimeError(
        "未找到 pyproject.toml——本件应位于仓内（草案位或 tests/signed/ 落位）；"
        "移出仓外属落位契约破坏，回签发复核。"
    )


_REPO_ROOT = _locate_repo_root()
_TARGET_TEST = "tests/test_watchdog_per_process_memory.py::test_watchdog_no_kill_when_per_unit_memory_under_threshold"

# 注入用 conftest 模板：在目标测试加载前 patch psutil.virtual_memory
_INJECT_CONFTEST = """\
import psutil

class _FakeVM:
    def __init__(self, percent: float) -> None:
        self.percent = percent
        self.total = 32 * 1024**3
        self.available = int(self.total * (100 - percent) / 100)

def install_fake_watermark(percent: float) -> None:
    psutil.virtual_memory = lambda: _FakeVM(percent)  # noqa: BLE001 - 测试注入点
"""

_CONDUCTION_TEST = """\
import asyncio
import sys
from pathlib import Path

_REPO = Path({repo!r})
sys.path.insert(0, str(_REPO))
_BASH_DIR = str(_REPO / "plugins" / "shared" / "bash")
if _BASH_DIR not in sys.path:
    sys.path.insert(0, _BASH_DIR)

from install_injection import install_fake_watermark  # noqa: E402

install_fake_watermark({percent})

# import 目标测试模块并取被测用例，在注入水位下原样执行（不改断言/不跳用例）
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "target_watchdog_tests",
    _REPO / "tests" / "test_watchdog_per_process_memory.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_target = _mod.test_watchdog_no_kill_when_per_unit_memory_under_threshold


def test_target_under_injected_watermark():
    coro = _target.__wrapped__() if hasattr(_target, "__wrapped__") else _target()
    if asyncio.iscoroutine(coro):
        asyncio.run(coro)
"""


@dataclass(frozen=True)
class _RunResult:
    """子进程 pytest 的可渲染结果。"""

    outcome: str  # "passed" / "skipped" / "failed" / "no-tests"
    report: str  # 人类可读输出（失败报告素材）


def _describe(scenario: str, requirement_id: str, injected_percent: float,
              expected: str, actual: _RunResult) -> str:
    """失败报告人类化：场景/需求ID/注入输入/期望/实际/定位，不出裸堆栈。"""
    tail = "\n".join(actual.report.splitlines()[-25:])
    return (
        f"[{requirement_id}] 场景: {scenario}\n"
        f"  输入: 注入宿主水位={injected_percent}%\n"
        f"  期望: {expected}\n"
        f"  实际: {actual.outcome}\n"
        f"  定位: 目标测试 {_TARGET_TEST} 在注入水位下的运行结果\n"
        f"  子进程输出尾部:\n{tail}"
    )


def _run_target_under_injected_watermark(percent: float, tmp_path: Path) -> _RunResult:
    """在注入水位环境下子进程运行目标测试，返回可渲染结果。

    副作用观测接口：仅写 tmp_path（pytest 管理的临时目录），
    不写仓库任何状态、不触碰生产配置——无其他副作用。
    """
    (tmp_path / "install_injection.py").write_text(_INJECT_CONFTEST, encoding="utf-8")
    driver = tmp_path / "test_conduction.py"
    driver.write_text(
        _CONDUCTION_TEST.format(repo=str(_REPO_ROOT), percent=percent), encoding="utf-8"
    )
    proc = subprocess.run(  # noqa: S603 - 固定参数子进程，无用户输入
        [sys.executable, "-m", "pytest", str(driver), "-v", "--no-header", "-x",
         "-p", "pytest_asyncio", "--rootdir", str(_REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(_REPO_ROOT),
        check=False,
    )
    output = proc.stdout + proc.stderr
    if "PASSED" in output:
        return _RunResult("passed", output)
    if "SKIPPED" in output or "SKIPPED[" in output:
        return _RunResult("skipped", output)
    if "no tests ran" in output:
        return _RunResult("no-tests", output)
    return _RunResult("failed", output)


class TestPILOT01注入高水位:
    """高水位注入组：目标测试不得因宿主水位误报。

    AC-1 语义：加固后，宿主水位 ≥85% 时目标测试要么原样通过
    （隔离方案），要么以带水位原因的方式跳过（前置跳过方案）——
    不得 failed。两个注入值（92%/96%）为有区分度输入，防实现
    只在某一个水位值上正确。
    """

    @pytest.mark.parametrize("injected_percent", [92.0, 96.0])
    def test_PILOT_01_注入水位下反例测试不误报(self, injected_percent: float,
                                                tmp_path: Path) -> None:
        # --- 输入数据 ---
        scenario = f"宿主水位注入{injected_percent}%_小内存进程反例"

        # --- 执行 ---
        result = _run_target_under_injected_watermark(injected_percent, tmp_path)

        # --- 输出断言 ---
        # 性质断言（兼容两个合法修法）：passed，或 skipped 且原因带水位语义
        if result.outcome == "skipped":
            assert any(
                key in result.report for key in ("水位", "watermark", "water")
            ), _describe(
                scenario=scenario,
                requirement_id="PILOT-01",
                injected_percent=injected_percent,
                expected="passed 或 skipped(原因含水位语义)",
                actual=result,
            )
            return
        assert result.outcome == "passed", _describe(
            scenario=scenario,
            requirement_id="PILOT-01",
            injected_percent=injected_percent,
            expected="passed 或 skipped(原因含水位语义)",
            actual=result,
        )

        # --- 副作用断言 ---
        # 无生产副作用：子进程仅读仓内代码；临时文件由 tmp_path 管理（见 _run_target_under_injected_watermark 声明）


class TestPILOT01注入低水位:
    """低水位注入组：目标测试必须原样通过。

    AC-2 语义：该组挡住"无条件 skip 整个测试"的平凡实现——
    若实现为绕开高水位问题而永久 skip，本组必红（low 水位下
    skipped 不被接受，必须 passed）。
    """

    def test_PILOT_01_注入水位40百分比下反例测试必须通过(self, tmp_path: Path) -> None:
        # --- 输入数据 ---
        scenario = "宿主水位注入40%_小内存进程反例"

        # --- 执行 ---
        result = _run_target_under_injected_watermark(40.0, tmp_path)

        # --- 输出断言 ---
        assert result.outcome == "passed", _describe(
            scenario=scenario,
            requirement_id="PILOT-01",
            injected_percent=40.0,
            expected="passed（低水位下不得跳过、不得失败）",
            actual=result,
        )

        # --- 副作用断言 ---
        # 无生产副作用（同上）


class TestPILOT01水位无关性:
    """性质断言组：目标测试结论与注入水位无关（AC-1 ∪ AC-2 的联合语义）。

    「高注入水位下不误报 + 低注入水位下必通过」联合构成鲁棒性：
    同一组测试行为在跨水位输入下保持契约结论（不杀小内存进程），
    实现无法通过只对单个水位值特化而骗过。
    """

    @pytest.mark.parametrize("injected_percent", [40.0, 88.0, 99.0])
    def test_PILOT_01_跨水位输入下契约结论一致_小内存进程不被杀(
        self, injected_percent: float, tmp_path: Path
    ) -> None:
        # --- 输入数据 ---
        scenario = f"跨水位一致性_注入{injected_percent}%"

        # --- 执行 ---
        result = _run_target_under_injected_watermark(injected_percent, tmp_path)

        # --- 输出断言 ---
        # 性质断言：allowed 集合随水位单调放宽，但 failed 恒非法
        allowed = {"passed"} if injected_percent < 85 else {"passed", "skipped"}
        assert result.outcome in allowed, _describe(
            scenario=scenario,
            requirement_id="PILOT-01",
            injected_percent=injected_percent,
            expected=f"结论 ∈ {sorted(allowed)}（跨水位契约一致，failed 恒非法）",
            actual=result,
        )

        # --- 副作用断言 ---
        # 无生产副作用（同上）
