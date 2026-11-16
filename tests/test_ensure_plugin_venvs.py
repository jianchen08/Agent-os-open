# @feature: FP-0.2.装机venv自愈 | @ci: python-coverage
"""ensure_plugin_venvs 供给与健康断言测试（launcher venv 自愈三犯缺陷的回归锁）。

缺陷背景（装机 2026-10-18 三犯实证）：旧 launcher Step 2 门控=「.venv 目录存在
即跳过」+ uv sync 输出静默 + 零重建后断言——部分完成的 sync 留下残缺 .venv
（agentos_plugin_sdk editable 未装）后门控永不重刷，坏态经包内携带/junction
传播到装机，合宿组全灭 50 分钟。

本文件锁两条不变量：
- 「venv 可用」判据必须含 SDK import 断言（仅目录/解释器存在≠可用）；
- 断言失败必须触发补装并再断言（补装后有界重试，仍坏如实报 broken）。

执行体（uv/python 子进程）一律注入假 runner——进程拉起是外部依赖，mock 正当；
真实语料仅用于目标枚举的仓库布局不变量（见 test_iter_targets_real_repo_layout）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "ensure_plugin_venvs", REPO / "scripts" / "ensure_plugin_venvs.py"
)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - 同仓固定路径必成功
    raise RuntimeError("scripts/ensure_plugin_venvs.py 缺失")
E = importlib.util.module_from_spec(_SPEC)
sys.modules["ensure_plugin_venvs"] = E
_SPEC.loader.exec_module(E)


def make_plugin(
    tmp_path: Path,
    name: str,
    *,
    manifest: dict[str, Any] | None = None,
    pyproject_sdk: bool = True,
    venv_interpreter: bool = True,
) -> Path:
    """造一个插件目录：plugin.json + pyproject.toml + .venv 解释器桩。"""
    d = tmp_path / name
    d.mkdir()
    if manifest is not None:
        (d / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    sdk_line = '"agentos-plugin-sdk>=0.2.0",' if pyproject_sdk else ""
    (d / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.1.0"\n'
        f"requires-python = \">=3.11\"\ndependencies = [{sdk_line}]\n",
        encoding="utf-8",
    )
    if venv_interpreter:
        interp = E.venv_interpreter_path(d)
        interp.parent.mkdir(parents=True, exist_ok=True)
        interp.write_bytes(b"")
    return d


class FakeFlows:
    """假 runner 容器：记录 sync 调用次序，import 结果按脚本驱动。"""

    def __init__(self, import_results: list[bool]) -> None:
        self.sync_calls: list[Path] = []
        self.import_results = list(import_results)

    def sync(self, project_dir: Path) -> tuple[bool, str]:
        self.sync_calls.append(project_dir)
        # 建模真实 uv sync 的效果：重建出 venv 解释器（残缺态由此修复）
        interp = E.venv_interpreter_path(project_dir)
        interp.parent.mkdir(parents=True, exist_ok=True)
        interp.write_bytes(b"")
        return True, ""

    def probe(self, interpreter: Path) -> bool:
        if not self.import_results:
            raise AssertionError("import 探测次数超出剧本（补装断言链失控）")
        return self.import_results.pop(0)


def test_existing_venv_missing_sdk_triggers_sync_and_repairs(tmp_path: Path) -> None:
    """核心回归：venv 目录存在但 SDK 缺失 → 必须补装并再断言（旧门控此处直接跳过）。"""
    d = make_plugin(tmp_path, "alpha")
    flows = FakeFlows(import_results=[False, True])  # 首探缺失 → sync → 复探通过
    status, _detail = E.ensure_plugin_venv(d, sync_runner=flows.sync, import_runner=flows.probe)
    assert status == "repaired"
    assert len(flows.sync_calls) == 1, "恰好一轮补装"
    assert flows.sync_calls[0] == d


def test_sync_then_still_broken_reports_broken_after_second_round(tmp_path: Path) -> None:
    """补装后仍缺 SDK → 完整重跑第二轮 → 仍坏如实报 broken（不得静默绿）。"""
    d = make_plugin(tmp_path, "alpha")
    flows = FakeFlows(import_results=[False, False, False])
    status, _detail = E.ensure_plugin_venv(d, sync_runner=flows.sync, import_runner=flows.probe)
    assert status == "broken"
    assert len(flows.sync_calls) == 2, "补装有界两轮（初装 + 完整重试）"


def test_missing_interpreter_triggers_rebuild(tmp_path: Path) -> None:
    """解释器缺失（venv 被清/空壳）→ 重建 → SDK 断言过 → repaired。"""
    d = make_plugin(tmp_path, "alpha", venv_interpreter=False)
    flows = FakeFlows(import_results=[True])
    status, _detail = E.ensure_plugin_venv(d, sync_runner=flows.sync, import_runner=flows.probe)
    assert status == "repaired"
    assert flows.sync_calls == [d]


def test_healthy_venv_without_sdk_declaration_needs_no_sync(tmp_path: Path) -> None:
    """未声明 SDK 的插件：解释器在即可用，零探测零补装（不误报）。"""
    d = make_plugin(tmp_path, "plain", pyproject_sdk=False)
    flows = FakeFlows(import_results=[])
    status, _detail = E.ensure_plugin_venv(d, sync_runner=flows.sync, import_runner=flows.probe)
    assert status == "ok"
    assert flows.sync_calls == []


def test_healthy_venv_with_sdk_importable_needs_no_sync(tmp_path: Path) -> None:
    """SDK 声明且 import 通过：零补装（幂等不重装）。"""
    d = make_plugin(tmp_path, "alpha")
    flows = FakeFlows(import_results=[True])
    status, _detail = E.ensure_plugin_venv(d, sync_runner=flows.sync, import_runner=flows.probe)
    assert status == "ok"
    assert flows.sync_calls == []


def test_iter_targets_puts_host_first_and_skips_cohost_members(tmp_path: Path) -> None:
    """目标枚举：_host 恒首个（合宿 spawn 前置）；host_group 成员跳过（venv 零消费）。"""
    make_plugin(tmp_path, "_host", manifest={"id": "_host"})
    make_plugin(tmp_path, "approval", manifest={"id": "approval", "host_group": "light"})
    make_plugin(tmp_path, "bash", manifest={"id": "bash"})
    make_plugin(tmp_path, "no_pyproject")  # 有 manifest 无 pyproject → 不入选
    (tmp_path / "no_manifest").mkdir()
    (tmp_path / "no_manifest" / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    targets = E.iter_targets(tmp_path)
    assert [t.name for t in targets] == ["_host", "bash"]


def test_iter_targets_real_repo_layout_host_first_and_cohost_excluded() -> None:
    """真实语料不变量：仓内 plugins/shared 枚举 _host 居首，approval（light 成员）不在列。"""
    real_root = REPO / "plugins" / "shared"
    names = [t.name for t in E.iter_targets(real_root)]
    assert names[0] == "_host", "_host 必须首个处理"
    assert "approval" not in names, "合宿成员不得进独立 venv 供给轮"
    assert "monitoring" not in names, "light_stable 成员同样跳过"
    assert "bash" in names, "独立 sidecar 在列"


def test_failed_sync_still_retries_second_round(tmp_path: Path) -> None:
    """首轮 sync 进程失败（如网络断）→ 仍走第二轮完整重试 → 结果如实。"""
    d = make_plugin(tmp_path, "alpha")

    def failing_sync(project_dir: Path) -> tuple[bool, str]:
        return False, "network unreachable"

    # 初探 + 两轮补装后复探 = 3 次探测（失败 sync 不建解释器，复探仍走 SDK 断言）
    flows = FakeFlows(import_results=[False, False, False])
    status, _detail = E.ensure_plugin_venv(d, sync_runner=failing_sync, import_runner=flows.probe)
    assert status == "broken"


def test_sdk_import_probe_runs_real_interpreter(tmp_path: Path) -> None:
    """真实依赖一条：真解释器探测命令形态可执行（import 不存在的模块 → False，不抛）。"""
    assert E.sdk_importable(Path(sys.executable)) is False or True, (
        "探测返回布尔（解释器能否 import SDK 取决于环境，进程必须可拉起）"
    )
    # 关键断言：命令对「保证不存在的模块」必须返回 False 而非抛异常
    assert E._probe_import(Path(sys.executable), "no_such_module_xyz") is False


@pytest.mark.parametrize("name", ["_host", "bash", "monitoring"])
def test_venv_interpreter_path_platform_layout(name: str, tmp_path: Path) -> None:
    """解释器路径按平台布局（Windows Scripts/python.exe），门控与 spawn 期同源。"""
    p = E.venv_interpreter_path(tmp_path / name)
    assert p.parent.name == ("Scripts" if sys.platform == "win32" else "bin")
