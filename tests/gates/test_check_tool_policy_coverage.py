# @feature: FP-GATE tool_policy 覆盖门禁 | @ci: python-coverage
"""check_tool_policy_coverage.py 单测（grants P1.5，ADR 2026-11-19 D2 的 CI 兜底）。

行为契约（断输入→输出，不钉实现；fixture 为最小临时仓树，不碰真实策略表/隔离策略）：
- 绿世界：声明面必需键全部有分级键覆盖 → exit 0；
- 反向测试（本批核心）：删一条分级 → 红（exit 1 且缺口行点名键）→ 补回 → 绿；
- 多动作工具整工具裸键 → 红（§6 schema：整工具键仅限单动作工具）；
- ``tool.*`` 通配缺非空 notes → 红；带 notes → 绿（通配覆盖全部声明 action）；
- 外部提供方（EXTERNAL_PROVIDED_TOOL_IDS）在白名单不产生登记义务（记录不执法）；
- defaults.uncategorized ≠ deny → 红；verdict 越界闭集 → 红；
- 动作位派生契约：命令执行类（isolation policy ``command_in_container``）按
  ``tool.execute`` 精确键查表——登记则绿、缺登记则缺口点名 ``tool.execute``；
- 动态转发壳（call_tool）在声明面 → 未入白名单的声明工具也是必需键（动态可达面）；
- 负控制：真实仓跑上下文必须可解析（exit ≠ 2；缺口集当前非空属 P1 在飞预期，
  断言不钉红绿，P1 收口转绿不破测试）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_tool_policy_coverage.py"

pytestmark = pytest.mark.unit

POLICY_PLUGIN = (
    'VERDICTS = ("auto", "task_grant", "domain_grant", "per_call_approval", "deny")\n'
    'EFFECTS = ("read", "write", "execute")\n'
)
EXTERNAL_PLUGIN = "EXTERNAL_PROVIDED_TOOL_IDS = frozenset({'ext_tool'})\n"
ISOLATION_HOST_DIRECT = "default:\n  isolation: non_isolated\n  execution: host_direct\n"
ISOLATION_WITH_CMD = (
    "default:\n  isolation: non_isolated\n  execution: host_direct\n"
    "tools:\n  cmd_tool:\n    isolation: isolated\n    execution: command_in_container\n"
)

GREEN_POLICY = """\
version: 1
defaults:
  uncategorized: deny
tools:
  mini.alpha_act:
    effect: read
    verdict: auto
  mini.beta_act:
    effect: write
    verdict: task_grant
  solo_tool:
    effect: read
    verdict: auto
  extra_tool:
    effect: read
    verdict: auto
  dark_tool:
    effect: read
    verdict: auto
  call_tool:
    effect: execute
    verdict: per_call_approval
    notes: 动态壳未展开直达兜底
"""

MINI_ALPHA_BLOCK = "  mini.alpha_act:\n    effect: read\n    verdict: auto\n"
MINI_BOTH_BLOCKS = (
    "  mini.alpha_act:\n    effect: read\n    verdict: auto\n"
    "  mini.beta_act:\n    effect: write\n    verdict: task_grant\n"
)
DARK_TOOL_BLOCK = "  dark_tool:\n    effect: read\n    verdict: auto\n"


def _write_plugin(root: Path, dir_name: str, plugin_id: str, tools: list[dict]) -> None:
    base = root / dir_name
    base.mkdir(parents=True, exist_ok=True)
    manifest = {"id": plugin_id, "capabilities": {"tools": tools}}
    (base / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")


def _make_fixture(
    tmp_path: Path,
    policy_text: str = GREEN_POLICY,
    isolation_text: str = ISOLATION_HOST_DIRECT,
) -> tuple[Path, Path, Path, Path]:
    """最小临时仓树：3 插件 + 动态壳 + 暗工具 + 白名单 + 策略表 + 隔离策略。"""
    shared = tmp_path / "shared"
    (shared / "grants").mkdir(parents=True, exist_ok=True)
    (shared / "grants" / "policy.py").write_text(POLICY_PLUGIN, encoding="utf-8")
    (shared / "tool_schema").mkdir(parents=True, exist_ok=True)
    (shared / "tool_schema" / "plugin.py").write_text(EXTERNAL_PLUGIN, encoding="utf-8")
    _write_plugin(
        shared,
        "mini_a",
        "mini_plugin",
        [{
            "name": "mini",
            "input_schema": {"type": "object", "properties": {"action": {"enum": ["alpha_act", "beta_act"]}}},
        }],
    )
    _write_plugin(
        shared,
        "solo_b",
        "solo_plugin",
        [
            {"name": "solo_tool", "input_schema": {"type": "object"}},
            {"name": "extra_tool", "input_schema": {"type": "object"}},
        ],
    )
    _write_plugin(shared, "shell_c", "shell_plugin", [{"name": "call_tool", "input_schema": {"type": "object"}}])
    _write_plugin(shared, "dark_d", "dark_plugin", [{"name": "dark_tool", "input_schema": {"type": "object"}}])
    agents = tmp_path / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "a.yaml").write_text("tool_ids:\n  - mini\n  - solo_tool\n  - ext_tool\n", encoding="utf-8")
    (agents / "b.yaml").write_text("tool_ids:\n  - solo_plugin\n", encoding="utf-8")
    policy = tmp_path / "config" / "plugins" / "grants" / "tool_policy.yaml"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(policy_text, encoding="utf-8")
    isolation = tmp_path / "config" / "plugins" / "isolation" / "isolation_policy.yaml"
    isolation.parent.mkdir(parents=True, exist_ok=True)
    isolation.write_text(isolation_text, encoding="utf-8")
    return shared, agents, policy, isolation


def _run(
    shared: Path, agents: Path, policy: Path, isolation: Path, *extra: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--plugins-root",
            str(shared),
            "--agents-root",
            str(agents),
            "--policy",
            str(policy),
            "--isolation-policy",
            str(isolation),
            *extra,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=False,
    )


def test_green_world_is_exit_zero(tmp_path: Path) -> None:
    shared, agents, policy, isolation = _make_fixture(tmp_path)
    proc = _run(shared, agents, policy, isolation)
    assert proc.returncode == 0, f"绿世界应 exit 0，实得 {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert "缺口集为空" in proc.stdout


def test_reverse_delete_classification_red_then_restore_green(tmp_path: Path) -> None:
    """反向测试（验收判据 2）：删一条分级 → 红；补回 → 绿。"""
    shared, agents, policy, isolation = _make_fixture(tmp_path)
    assert _run(shared, agents, policy, isolation).returncode == 0
    mutated = GREEN_POLICY.replace(MINI_ALPHA_BLOCK, "")
    assert mutated != GREEN_POLICY, "变异失败（fixture 自检）"
    policy.write_text(mutated, encoding="utf-8")
    proc = _run(shared, agents, policy, isolation)
    assert proc.returncode == 1, f"删分级后应 exit 1，实得 {proc.returncode}\n{proc.stdout}"
    assert "mini.alpha_act" in proc.stdout
    assert "策略表缺口" in proc.stdout
    policy.write_text(GREEN_POLICY, encoding="utf-8")
    assert _run(shared, agents, policy, isolation).returncode == 0, "补回后应恢复绿"


def test_multi_action_whole_tool_bare_key_is_red(tmp_path: Path) -> None:
    shared, agents, policy, isolation = _make_fixture(
        tmp_path,
        GREEN_POLICY.replace(MINI_ALPHA_BLOCK, "  mini:\n    effect: read\n    verdict: auto\n"),
    )
    proc = _run(shared, agents, policy, isolation)
    assert proc.returncode == 1
    assert "多动作工具整工具裸键" in proc.stdout


def test_wildcard_without_notes_red_with_notes_green(tmp_path: Path) -> None:
    no_reason = GREEN_POLICY.replace(MINI_BOTH_BLOCKS, "  mini.*:\n    effect: read\n    verdict: auto\n")
    shared, agents, policy, isolation = _make_fixture(tmp_path, no_reason)
    proc = _run(shared, agents, policy, isolation)
    assert proc.returncode == 1
    assert "通配键" in proc.stdout
    assert "缺登记理由" in proc.stdout

    with_reason = no_reason.replace(
        "  mini.*:\n    effect: read\n    verdict: auto\n",
        "  mini.*:\n    effect: read\n    verdict: auto\n    notes: 测试工具族整族同级（fixture 理由）\n",
    )
    policy.write_text(with_reason, encoding="utf-8")
    assert _run(shared, agents, policy, isolation).returncode == 0, "通配带理由应覆盖全部声明 action"


def test_command_execution_tool_requires_execute_key(tmp_path: Path) -> None:
    """动作位派生契约第二层：命令执行类按 ``tool.execute`` 精确键查表。"""
    with_cmd = GREEN_POLICY + "  cmd_tool:\n    effect: execute\n    verdict: task_grant\n    notes: 规则面优先\n"
    shared, agents, policy, isolation = _make_fixture(tmp_path, with_cmd, ISOLATION_WITH_CMD)
    # cmd_tool 未声明、未白名单——经动态可达面成为必需键，动作位 = execute。
    _write_plugin(Path(shared), "cmd_e", "cmd_plugin", [{"name": "cmd_tool", "input_schema": {"type": "object"}}])
    assert _run(shared, agents, policy, isolation).returncode == 0, "命令执行类登记 cmd_tool.execute 应绿"

    missing = with_cmd.replace(
        "  cmd_tool:\n    effect: execute\n    verdict: task_grant\n    notes: 规则面优先\n", ""
    )
    assert missing != with_cmd, "变异失败（fixture 自检）"
    policy.write_text(missing, encoding="utf-8")
    proc = _run(shared, agents, policy, isolation)
    assert proc.returncode == 1
    assert "cmd_tool.execute" in proc.stdout, "缺登记应点名 cmd_tool.execute"


def test_external_whitelist_entry_creates_no_burden(tmp_path: Path) -> None:
    """ext_tool（外部提供方）在白名单但不产生登记义务；缺口集不含它、记录面含它。"""
    sys.path.insert(0, str(SCRIPT.parent))
    import check_tool_policy_coverage as gate

    policy_text = GREEN_POLICY.replace(DARK_TOOL_BLOCK, "").replace(
        "  solo_tool:\n    effect: read\n    verdict: auto\n", ""
    )
    shared, agents, policy, isolation = _make_fixture(tmp_path, policy_text)
    violations, report = gate.check(shared, agents, policy, isolation)
    assert violations, "dark_tool/solo_tool 缺表键应仍红"
    assert not any("ext_tool" in v for v in violations), "外部提供方不得产生缺口"
    assert "外部/用户空间提供方" in report, "记录面应留外部提供方证据"
    assert "ext_tool" in report, "记录面应留外部提供方证据"


def test_permissive_default_and_bad_verdict_are_red(tmp_path: Path) -> None:
    shared, agents, policy, isolation = _make_fixture(
        tmp_path, GREEN_POLICY.replace("uncategorized: deny", "uncategorized: auto")
    )
    proc = _run(shared, agents, policy, isolation)
    assert proc.returncode == 1
    assert "defaults.uncategorized" in proc.stdout

    shared, agents, policy, isolation = _make_fixture(
        tmp_path,
        GREEN_POLICY.replace(
            "  mini.beta_act:\n    effect: write\n    verdict: task_grant",
            "  mini.beta_act:\n    effect: write\n    verdict: banana",
        ),
    )
    proc = _run(shared, agents, policy, isolation)
    assert proc.returncode == 1
    assert "越界闭集" in proc.stdout


def test_dynamic_shell_makes_dark_tool_required(tmp_path: Path) -> None:
    shared, agents, policy, isolation = _make_fixture(tmp_path, GREEN_POLICY.replace(DARK_TOOL_BLOCK, ""))
    proc = _run(shared, agents, policy, isolation)
    assert proc.returncode == 1
    assert "dark_tool" in proc.stdout, "call_tool 在声明面时未入白名单的 dark_tool 也须覆盖"


def test_report_flag_writes_gap_file(tmp_path: Path) -> None:
    shared, agents, policy, isolation = _make_fixture(tmp_path)
    out = tmp_path / "out" / "gap.txt"
    proc = _run(shared, agents, policy, isolation, "--report", str(out))
    assert proc.returncode == 0
    assert out.is_file()
    assert "缺口集为空" in out.read_text(encoding="utf-8")


def test_real_repo_context_resolves_not_context_failure() -> None:
    """负控制：真实仓上下文必须可解析。断言 exit ≠ 2（当前红=预期，P1 收口转绿不破本测）。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=False,
    )
    assert proc.returncode != 2, f"真实仓上下文解析失败：{proc.stderr}"
    assert proc.returncode in (0, 1), f"真实仓意外退出码 {proc.returncode}：{proc.stderr}"
