# @feature: FP-0.2.〇 任务执行驱动 | @vision: V3 可嵌入 | @ci: python-coverage
"""task_submit 白话验收标准（扁平字段主形态）测试。

用户裁定（2026-09-29，ADR docs/decisions/2026-09-29-acceptance-flat-fields.md）：
- 主形态 = files/expect/command 扁平白话字段任选组合，零指标知识零嵌套；
- 老指标名 dict 形态静默兼容（既有调用方零破坏）；
- 深格式别名容错（expected_output→expected、criteria→并入 expected，透明告知）；
- 混用冲突拒绝；全空拒绝自描述（动态清单，复用 _metric_required_examples）。

映射目标（yaml 单源）：
- files → 逐文件 file_check（存在且非空，实例键 file_check::<路径>）；
- expect → semantic_check(expected=expect)；command → bash_check(command=command)。

装配：task_submit 平铺目录与 system/tasks 同 test_task_submit_dispatch.py 的
sys.path 装配；指标定义读真实 evaluation_metrics.yaml（与 task_evaluate 同源）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_TS_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "tools" / "task_submit"
_SYSTEM_ROOT = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "system"

for _d in [_SYSTEM_ROOT, _SYSTEM_ROOT / "tasks"]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


def _load_module() -> Any:
    """加载 task_submit/tool.py（唯一模块名，进程内缓存）。"""
    mod_name = "task_submit_tool_flat_acceptance_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _TS_DIR / "tool.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[mod_name]
        raise
    return module


@pytest.fixture
def mod() -> Any:
    return _load_module()


def _normalize(mod: Any, criteria: Any):
    """归一化入口捷径：返回 (normalized, fail)。"""
    return mod.TaskSubmitTool._normalize_acceptance_criteria(criteria)


# ── 白话三路映射（各 ≥2 组区分度） ──────────────────────────


class TestFilesMapping:
    def test_multi_files_map_per_file(self, mod: Any) -> None:
        """files 多文件 → 逐文件 file_check 实例（实例键 file_check::<路径>）。"""
        normalized, fail = _normalize(
            mod, {"files": ["chapters/chapter_025.md", "progress.md", "docs/总纲.md"]}
        )
        assert fail is None
        assert set(normalized.keys()) == {
            "file_check::chapters/chapter_025.md",
            "file_check::progress.md",
            "file_check::docs/总纲.md",
        }
        for key, config in normalized.items():
            path = key.split(mod._FLAT_INSTANCE_SEP, 1)[1]
            assert config["input_params"]["path"] == path
            assert config["input_params"]["check"] == "not_empty"

    def test_single_string_tolerated_and_stripped(self, mod: Any) -> None:
        """files 单字符串容错为单元素数组；路径去空白。"""
        normalized, fail = _normalize(mod, {"files": "  reports/summary.md  "})
        assert fail is None
        assert set(normalized.keys()) == {"file_check::reports/summary.md"}
        entry = normalized["file_check::reports/summary.md"]
        assert entry["input_params"] == {"path": "reports/summary.md", "check": "not_empty"}

    def test_files_count_property(self, mod: Any) -> None:
        """性质断言：展开实例数 = 文件数（一一对应，不合并不丢失）。"""
        files = [f"chapters/chapter_{i:03d}.md" for i in range(5)]
        normalized, fail = _normalize(mod, {"files": files})
        assert fail is None
        assert len(normalized) == len(files)


class TestExpectMapping:
    @pytest.mark.parametrize(
        "expect",
        [
            "总纲与25章每章1200-1800字剧情连续，progress.md 记录完整",
            "结尾必须回收第一章伏笔",
        ],
    )
    def test_expect_maps_to_semantic_expected(self, mod: Any, expect: str) -> None:
        """expect（含长中文）→ semantic_check(expected=expect)，零指标知识。"""
        normalized, fail = _normalize(mod, {"expect": expect})
        assert fail is None
        assert normalized == {"semantic_check": {"input_params": {"expected": expect}}}


class TestCommandMapping:
    @pytest.mark.parametrize(
        "command",
        [
            "pytest tests/ -q",
            "python -c \"import json;json.load(open('result.json'))\"",
        ],
    )
    def test_command_maps_to_bash_check(self, mod: Any, command: str) -> None:
        """command → bash_check(command=command)。"""
        normalized, fail = _normalize(mod, {"command": command})
        assert fail is None
        assert normalized == {"bash_check": {"input_params": {"command": command}}}


class TestCombination:
    def test_all_three_fields_combine(self, mod: Any) -> None:
        """三白话字段任选组合：files(2) + expect + command → 4 实例。"""
        normalized, fail = _normalize(
            mod,
            {
                "files": ["a.md", "b.md"],
                "expect": "剧情连续",
                "command": "pytest -q",
            },
        )
        assert fail is None
        # 性质断言：实例数 = 文件数 + expect(1) + command(1)
        assert len(normalized) == len(["a.md", "b.md"]) + 1 + 1
        assert "semantic_check" in normalized
        assert "bash_check" in normalized
        assert {k for k in normalized if k.startswith("file_check::")} == {
            "file_check::a.md",
            "file_check::b.md",
        }

    def test_expanded_criteria_pass_required_validation(self, mod: Any) -> None:
        """展开结果过提交期必填校验（映射目标与 yaml required 对齐）。"""
        normalized, fail = _normalize(
            mod,
            {"files": ["chapters/chapter_025.md"], "expect": "剧情连续", "command": "pytest -q"},
        )
        assert fail is None
        assert normalized  # 全部实例带齐必填参数


# ── 混用冲突 / 全空 / 形态非法 ──────────────────────────────


class TestConflicts:
    @pytest.mark.parametrize(
        "criteria",
        [
            {"files": ["a.md"], "file_check": {"input_params": {"path": "b.md"}}},
            {"expect": "剧情连续", "semantic_check": {"input_params": {"expected": "x"}}},
            {"command": "pytest -q", "human_review": {"input_params": {"mode": "choice", "title": "审核"}}},
        ],
    )
    def test_flat_and_deep_mixing_rejected(self, mod: Any, criteria: dict) -> None:
        """白话字段与指标名键混用 → MIXED_ACCEPTANCE_CRITERIA 拒绝。"""
        normalized, fail = _normalize(mod, criteria)
        assert fail is not None
        assert fail.error_code == "MIXED_ACCEPTANCE_CRITERIA"
        assert "不能同时使用" in fail.error
        assert normalized == {}

    @pytest.mark.parametrize(
        "criteria",
        [
            {"files": [], "expect": "  ", "command": ""},
            {"expect": ""},
        ],
    )
    def test_all_empty_rejected_with_dynamic_menu(self, mod: Any, criteria: dict) -> None:
        """白话字段全空 → EMPTY_ACCEPTANCE_CRITERIA，错误动态附全部指标+必填。"""
        normalized, fail = _normalize(mod, criteria)
        assert fail is not None
        assert fail.error_code == "EMPTY_ACCEPTANCE_CRITERIA"
        # 动态清单（真实 yaml）：指标 ID + 必填参数示例均在场
        assert "semantic_check" in fail.error
        assert '"expected"' in fail.error
        assert "file_check" in fail.error
        assert '"path"' in fail.error
        assert normalized == {}

    @pytest.mark.parametrize(
        ("criteria", "field"),
        [
            ({"files": 123}, "files"),
            ({"files": ["" ]}, "files"),
            ({"files": [42]}, "files"),
            ({"expect": 123}, "expect"),
            ({"command": 123}, "command"),
        ],
    )
    def test_invalid_flat_field_shape(self, mod: Any, criteria: dict, field: str) -> None:
        """白话字段类型非法 → INVALID_ACCEPTANCE_FIELD（自描述期望形态）。"""
        normalized, fail = _normalize(mod, criteria)
        assert fail is not None
        assert fail.error_code == "INVALID_ACCEPTANCE_FIELD"
        assert field in fail.error

    def test_null_valued_flat_fields_count_as_empty(self, mod: Any) -> None:
        """显式 null / 空白串视同缺席：仅剩空集 → EMPTY_ACCEPTANCE_CRITERIA（非类型错误）。"""
        for criteria in ({"expect": None}, {"expect": ""}, {"expect": "  "}):
            normalized, fail = _normalize(mod, criteria)
            assert fail is not None
            assert fail.error_code == "EMPTY_ACCEPTANCE_CRITERIA"
            assert normalized == {}


# ── 老深格式静默兼容（零回归） ──────────────────────────────


class TestDeepFormatCompat:
    def test_deep_metric_dict_passes_through_unchanged(self, mod: Any) -> None:
        """无白话字段的深格式原样通过（既有调用方零破坏）。"""
        criteria = {
            "file_check": {"input_params": {"path": "docs/report.md", "check": "exists"}},
            "human_review": {"input_params": {"mode": "choice", "title": "审核"}},
        }
        normalized, fail = _normalize(mod, criteria)
        assert fail is None
        assert normalized == criteria

    def test_deep_semantic_with_expected_passes(self, mod: Any) -> None:
        """深格式 semantic_check 带 expected（yaml required）→ 放行。"""
        normalized, fail = _normalize(
            mod, {"semantic_check": {"input_params": {"expected": "结构完整", "output": "正文"}}}
        )
        assert fail is None
        assert normalized["semantic_check"]["input_params"]["expected"] == "结构完整"


# ── 别名容错（≥2 组，透明归一） ─────────────────────────────


class TestLegacyAliases:
    def test_criteria_aliases_to_expected(self, mod: Any) -> None:
        """旧 criteria 形态（此前必然被拒）→ 归一为 expected 直接可用。"""
        normalized, fail = _normalize(
            mod, {"semantic_check": {"input_params": {"criteria": "必须包含结论"}}}
        )
        assert fail is None
        assert normalized["semantic_check"]["input_params"] == {"expected": "必须包含结论"}

    def test_expected_output_aliases_to_expected(self, mod: Any) -> None:
        """expected_output（旧 schema 字段）→ expected。"""
        normalized, fail = _normalize(
            mod, {"semantic_check": {"input_params": {"expected_output": "覆盖全部需求"}}}
        )
        assert fail is None
        assert normalized["semantic_check"]["input_params"] == {"expected": "覆盖全部需求"}

    def test_expected_output_dict_json_encoded(self, mod: Any) -> None:
        """expected_output 为 dict（旧 schema object 形态）→ json.dumps 归一为串。"""
        normalized, fail = _normalize(
            mod,
            {
                "semantic_check": {
                    "input_params": {
                        "expected_output": {"format": "章节", "word_range": [1200, 1800]}
                    }
                }
            },
        )
        assert fail is None
        expected = normalized["semantic_check"]["input_params"]["expected"]
        assert '"format"' in expected
        assert "章节" in expected
        assert "1200" in expected

    def test_non_string_criteria_coerced_to_str(self, mod: Any) -> None:
        """criteria 非串标量 → str 归一（透明归一不静默丢弃）。"""
        normalized, fail = _normalize(
            mod, {"semantic_check": {"input_params": {"criteria": 42}}}
        )
        assert fail is None
        assert normalized["semantic_check"]["input_params"]["expected"] == "42"

    def test_criteria_merges_into_existing_expected(self, mod: Any) -> None:
        """expected 与 criteria 同现 → 并入（换行拼接，不覆盖）。"""
        normalized, fail = _normalize(
            mod,
            {
                "semantic_check": {
                    "input_params": {"expected": "结构完整", "criteria": "无幻觉"}
                }
            },
        )
        assert fail is None
        assert normalized["semantic_check"]["input_params"]["expected"] == "结构完整\n无幻觉"

    def test_top_level_alias_creates_input_params(self, mod: Any) -> None:
        """别名在条目顶层（旧 schema 形态）→ 归一落 input_params（缺席则创建）。"""
        normalized, fail = _normalize(mod, {"semantic_check": {"criteria": "覆盖全部需求"}})
        assert fail is None
        assert normalized["semantic_check"]["input_params"] == {"expected": "覆盖全部需求"}

    def test_alias_scope_limited_to_semantic_check(self, mod: Any) -> None:
        """别名不越指标定义：file_check 的 criteria 不改写，缺 path 仍拒。"""
        normalized, fail = _normalize(
            mod, {"file_check": {"input_params": {"criteria": "存在即通过"}}}
        )
        assert fail is not None
        assert fail.error_code == "INVALID_METRIC_PARAMS"
        assert '"path"' in fail.error


# ── 失败自描述（动态清单复用） ──────────────────────────────


class TestFailureSelfDescription:
    def test_invalid_metric_id_error_lists_menu_and_flat_hint(self, mod: Any) -> None:
        """指标 ID 全无效 → 错误附全部可用指标+必填（动态生成）+ 白话字段指引。"""
        _, fail = _normalize(mod, {"ghost_metric": {"input_params": {}}})
        assert fail is not None
        assert fail.error_code == "INVALID_METRIC_ID"
        assert "ghost_metric" in fail.error
        # 动态清单（真实 yaml 单源）
        assert "file_check" in fail.error
        assert '"path"' in fail.error
        assert "semantic_check" in fail.error
        assert '"expected"' in fail.error
        # 白话扁平字段指引
        assert "files/expect/command" in fail.error

    def test_deep_criteria_rejection_message_has_no_criteria_hint(self, mod: Any) -> None:
        """semantic_check 缺 expected 的拒绝文案不再出现旧 {"criteria"} 硬编码示例。"""
        _, fail = _normalize(
            mod, {"semantic_check": {"input_params": {"check": "intent"}}}
        )
        assert fail is not None
        assert fail.error_code == "INVALID_METRIC_PARAMS"
        assert '"expected"' in fail.error
        assert '"criteria"' not in fail.error


# ── 端到端：白话标准随派发落 birth state 与 kickoff ──────────


class _FakeSender:
    """记录 chat.send_message 参数的派发器 fake。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, params: dict) -> dict:
        self.calls.append(params)
        return {"status": "created", "pipeline_id": "pipe_engine_gen_1"}


def _base_inputs(**over: Any) -> dict:
    base = {
        "goal_title": "小说章节产出",
        "goal_description": "写第二十五章，1200-1800 字",
        "target_type": "agent",
        "target_id": "main",
        "parent_agent_level": 1,
        "pipeline_id": "pipe_parent_9",
        "user_id": "user-1",
        "acceptance_criteria": {
            "files": ["chapters/chapter_025.md"],
            "expect": "总纲与25章每章1200-1800字剧情连续，progress.md 记录完整",
        },
    }
    base.update(over)
    return base


def _make_tool(mod: Any) -> Any:
    """构造工具实例并 stub 纯参数校验（同 test_task_submit_dispatch 口径）。"""
    tool = mod.TaskSubmitTool()

    async def _ok(target: Any, level: Any) -> tuple[bool, str, str]:
        return (True, "", "")

    tool._validate_target_agent = _ok  # type: ignore[method-assign]

    async def _base_config(target_id: str) -> dict[str, Any] | None:
        return {
            "level": "L2",
            "is_active": True,
            "tool_ids": ["file_read"],
            "system_prompt": "stub-persona",
        }

    tool._get_agent_base_config = _base_config  # type: ignore[method-assign]
    return tool


class TestFlatDispatchEndToEnd:
    @pytest.mark.asyncio
    async def test_flat_criteria_expand_into_birth_state_and_kickoff(self, mod: Any) -> None:
        """白话标准经 execute 展开为指标实例：birth state 与 kickoff 均为实例键形态。"""
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        try:
            result = await tool.execute(_base_inputs())
        finally:
            mod._chat_sender = None
        assert result.success is True, result.error
        # 出生登记调用（birth state）与 kickoff 派发调用分别定位
        birth_call = next(
            c for c in sender.calls if "task.acceptance_criteria" in (c.get("state") or {})
        )
        state = birth_call["state"]
        criteria = state["task.acceptance_criteria"]
        assert criteria == {
            "file_check::chapters/chapter_025.md": {
                "input_params": {"path": "chapters/chapter_025.md", "check": "not_empty"}
            },
            "semantic_check": {
                "input_params": {
                    "expected": "总纲与25章每章1200-1800字剧情连续，progress.md 记录完整"
                }
            },
        }
        # 派发指令展开按定义 ID 解析实例键（评估指标详情头 + 实例键注入）
        kickoff_call = next(
            c for c in sender.calls if mod._EVALUATION_PROMPT_HEADER in str(c.get("message") or "")
        )
        assert "file_check::chapters/chapter_025.md" in kickoff_call["message"]

    @pytest.mark.asyncio
    async def test_schema_description_promotes_flat_form_only(self, mod: Any) -> None:
        """工具 schema 的 acceptance_criteria description 只写白话形态+用户示例。"""
        definition = mod.TaskSubmitTool.get_tool_definition()
        prop = definition.input_schema["properties"]["acceptance_criteria"]
        assert "files" in prop["description"]
        assert "expect" in prop["description"]
        assert "command" in prop["description"]
        assert "chapters/chapter_025.md" in prop["description"]
        assert "additionalProperties" not in prop
        # plugin.json 声明同口径（双面同步，防漂移）
        import json as _json

        manifest = _json.loads((_TS_DIR / "plugin.json").read_text(encoding="utf-8"))
        manifest_desc = manifest["capabilities"]["tools"][0]["input_schema"]["properties"][
            "acceptance_criteria"
        ]["description"]
        assert manifest_desc == prop["description"]
