# @feature: FP-0.2.〇 管道引擎与插件执行模型（切片投喂契约执法） | @ci: python-coverage
"""`state.reads` 声明完备性机械闸自测 + 基线双向棘轮回归。

执法器 `scripts/check_state_reads_declaration.py`：内核按 manifest `state.reads`
切片投喂（`kernel/crates/invoker/src/shared.rs` `state_feed_payload`），**未声明
键插件永远读不到且无日志**——两起实证事故（duplicate_check 漏 `task.id` 致失败
归因静默不写；task_reminder 漏 `reminder_seen_result_fp` 致 5 条任务误判
`reminder_exhausted`）都是纯静态可判的漏声明，故用机械闸拦。

本文件锁三件事：
1. **扫描器正反例**——三形态读点必中；注释/文档串、写语境下标、动态键不误判；
2. **控制键单一真源**——白名单直接解析 Rust 侧 `STATE_SLICE_CONTROL_KEYS`，
   且 `_libs/testing/state_slice.py` 的镜像与之逐字相等（测试夹具漂移即红）；
3. **基线双向棘轮**——实测违规集与 `.github/state-reads-baseline.txt` 必须
   逐条相等：基线外新增 = 红（新漏声明），条目失效 = 红（须同刀摘除）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = str(ROOT / "scripts")
_SHARED = str(ROOT / "plugins" / "shared")
for _p in (_SCRIPTS, _SHARED):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import check_state_reads_declaration as csr  # noqa: E402
from _libs.testing import state_slice  # noqa: E402


def _reads(source: str) -> set[str]:
    keys, _dynamic = csr.collect_state_reads(source)
    return keys


class TestScanLiteralForms:
    def test_bare_state_get_hit(self) -> None:
        assert _reads('x = state.get("trigger.origin")') == {"trigger.origin"}

    def test_ctx_state_get_hit(self) -> None:
        assert _reads('x = ctx.state.get("tool_ids", [])') == {"tool_ids"}

    def test_self_state_get_hit(self) -> None:
        assert _reads("x = self.state.get('a.b')") == {"a.b"}

    def test_subscript_read_hit(self) -> None:
        assert _reads('x = state["isolation.container_name"]') == {"isolation.container_name"}

    def test_nested_state_attribute_hit(self) -> None:
        assert _reads('x = self.ctx.state.get("user_id")') == {"user_id"}


class TestScanNegativeForms:
    def test_subscript_write_not_a_read(self) -> None:
        assert _reads('state["raw_result"] = "x"') == set()

    def test_augmented_subscript_not_a_read(self) -> None:
        assert _reads('state["n"] += 1') == set()

    def test_comment_and_docstring_not_a_read(self) -> None:
        source = '"""doc: state["ghost.key"] 只是文档。"""\n# state.get("also.ghost")\nx = 1\n'
        assert _reads(source) == set()

    def test_non_state_get_not_a_read(self) -> None:
        assert _reads('x = row.get("task_status")') == set()

    def test_existence_check_not_a_read(self) -> None:
        """``"k" in state`` 属写面自检（ADR 2026-09-10），不算读。"""
        assert _reads('if "k" in state:\n    x = 1') == set()

    def test_nested_get_on_state_value_not_a_read(self) -> None:
        assert _reads('x = state.get("a", {}).get("b")') == {"a"}


class TestScanConstantForms:
    def test_module_string_constant_resolved(self) -> None:
        source = '_K = "llm_loop_streak"\nx = state.get(_K)\n'
        assert _reads(source) == {"llm_loop_streak"}

    def test_annotated_module_constant_resolved(self) -> None:
        source = '_K: str = "task.ws_meta"\nx = state.get(_K, None)\n'
        assert _reads(source) == {"task.ws_meta"}

    def test_statekeys_attribute_resolved(self) -> None:
        assert _reads("x = ctx.state.get(StateKeys.TASK_ID)") == {"task.id"}

    def test_flat_shared_module_constant_resolved(self) -> None:
        """`zone_policy.STATE_KEY` 这类平铺共享模块常量。"""
        source = "x = ctx.state.get(zone_policy.STATE_KEY)\n"
        assert _reads(source) == {"task.authorized_write_zones"}

    def test_dynamic_key_reported_not_guessed(self) -> None:
        keys, dynamic = csr.collect_state_reads("x = state.get(key)\n")
        assert keys == set()
        assert dynamic, "动态键应进 unresolved 面"
        assert "动态键" in dynamic[0]


class TestManifestVisibleKeys:
    def test_messages_tail_normalized_to_messages(self, tmp_path: Path) -> None:
        plugin = tmp_path / "p1"
        plugin.mkdir()
        (plugin / "plugin.json").write_text('{"state": {"reads": ["messages_tail:20", "task.id"]}}', encoding="utf-8")

        assert csr.manifest_visible_keys(plugin) == {"messages", "task.id"}

    def test_empty_reads_means_full_feed(self, tmp_path: Path) -> None:
        plugin = tmp_path / "p2"
        plugin.mkdir()
        (plugin / "plugin.json").write_text('{"state": {"reads": []}}', encoding="utf-8")

        assert csr.manifest_visible_keys(plugin) == set()


class TestControlKeysSingleSource:
    def test_control_keys_parsed_from_kernel_source(self) -> None:
        keys = csr.load_control_keys()
        assert {"pipeline_id", "agent.id", "ended"} <= keys
        assert len(keys) >= 10

    def test_test_helper_mirror_matches_kernel_source(self) -> None:
        """`_libs/testing/state_slice.py` 的控制键镜像必须与 Rust 真源一致。"""
        assert set(state_slice.CONTROL_KEYS) == csr.load_control_keys(), (
            "测试夹具控制键镜像与 kernel/crates/invoker/src/shared.rs 漂移——行为锁用例投喂面会与生产切片不一致"
        )


class TestRepositoryStateRatchet:
    def test_no_new_undeclared_reads(self) -> None:
        found = csr.format_entries(csr.find_violations())
        baseline = csr.load_baseline()
        new, stale = csr.diff_baseline(found, baseline)

        assert new == [], f"基线外新增漏声明（切片投喂下静默读不到）：{new}"
        assert stale == [], f"基线条目已失效（须同刀摘除，防基线腐化）：{stale}"

    def test_repaired_plugins_have_no_undeclared_reads(self) -> None:
        """本刀修复的九个插件不得再出现在违规面（防基线回填）。"""
        violations = csr.find_violations()
        repaired = {
            "llm_core",
            "stop_check",
            "agent_config_load",
            "tool_args_inject",
            "environment_lifecycle",
            "notify_judge",
            "security_check",
            "prompt_build",
            "tool_cache_writer",
        }
        # prompt_build 的读点在 contributions.py（plugin.py 之外），本闸不覆盖
        # —— 由 plugins/shared/prompt_build/test_prompt_build_state_reads_slice.py 锁
        hit = sorted(repaired & set(violations))
        assert hit == [], f"已修复插件回填漏声明：{hit}"
        leaked = sorted(e for e in csr.load_baseline() if e.startswith("prompt_build:"))
        assert leaked == [], f"prompt_build 不得进基线（读点由插件行为锁覆盖）：{leaked}"

    def test_checker_cli_green(self) -> None:
        assert csr.main([]) == 0
