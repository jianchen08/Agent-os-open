# @feature: FP-0.2.〇 管道引擎与插件执行模型（条件注入引擎） | @ci: python-coverage
"""prompt_build 条件注入引擎（contributions.py）行为测试。

契约（方案 docs/working/提示词条件注入引擎方案_20261018.md §3）：
- 单文件自包含规则：when（任务稳定键条件）+ inject（order + 内联内容）；
- 条件键白名单外键 / 未知 when 键 / 未知规则键 → 整条 fail-closed 拒载；
- order 撞号 → 整批拒载（零注入）；单条/总量预算超限 → 拒载该条；
- 文件缺失/畸形/超大小 → 零注入且永不抛错；
- 渲染确定性：同输入恒同输出；零命中零注入；enabled:false 不产出；
- 模型条件读落定键 model_id，回退候选 model_tier；
- 文件 mtime/size 变化触发重载（管理面热改即生效）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_THIS_DIR = Path(__file__).resolve().parent


def _load_engine() -> Any:
    mod_name = "prompt_build_contributions_engine_test"
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(mod_name, _THIS_DIR / "contributions.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def engine_factory(tmp_path: Path) -> Any:
    """写规则文本并构建引擎；返回 (engine, path) 供热改重载测试。"""
    mod = _load_engine()

    def _make(text: str, **kwargs: Any) -> Any:
        path = tmp_path / "contributions.yaml"
        path.write_text(text, encoding="utf-8")
        return mod.ContributionEngine(path, **kwargs)

    return _make


def _rule(
    name: str = "r1",
    *,
    when: str = "",
    content: str = "内容A",
    order: int | None = None,
    enabled: bool | None = None,
) -> str:
    lines = [f"  - name: {name}"]
    if enabled is not None:
        lines.append(f"    enabled: {'true' if enabled else 'false'}")
    if when:
        lines.append("    when:")
        lines.extend(f"      {w}" for w in when.splitlines())
    lines.append("    inject:")
    if order is not None:
        lines.append(f"      order: {order}")
    lines.append(f"      content: {content}")
    return "\n".join(lines)


def _doc(*rules: str) -> str:
    return "rules:\n" + "\n".join(rules) + "\n"


class TestModelCondition:
    def test_hit_on_model_id(self, engine_factory: Any) -> None:
        eng = engine_factory(_doc(_rule(when='model: "deepseek-v4-*"')))

        out = eng.render({"model_id": "deepseek-v4-pro"})

        assert out == "内容A"

    @pytest.mark.parametrize("model_id", ["minimax-m3.1-flash-preview", "glm-4.7"])
    def test_miss_on_other_model(self, engine_factory: Any, model_id: str) -> None:
        eng = engine_factory(_doc(_rule(when='model: "deepseek-v4-*"')))

        assert eng.render({"model_id": model_id}) == ""

    def test_falls_back_to_model_tier_when_pin_missing(self, engine_factory: Any) -> None:
        """落定键缺席时回退候选 model_tier（同为任务稳定值）。"""
        eng = engine_factory(_doc(_rule(when='model: "large"')))

        assert eng.render({"model_tier": "large"}) == "内容A"

    def test_model_id_wins_when_both_present(self, engine_factory: Any) -> None:
        """性质断言：候选并集匹配——显式 id 与档位任一命中即命中。"""
        eng = engine_factory(_doc(_rule(when='model: "deepseek-*"')))

        assert eng.render({"model_id": "deepseek-v4-flash", "model_tier": "large"}) == "内容A"


class TestAgentAndToolConditions:
    def test_agents_fn_match(self, engine_factory: Any) -> None:
        eng = engine_factory(_doc(_rule(when="agents: [\"main\", \"executor/*\"]")))

        assert eng.render({"agent_id": "executor/general_agent"}) == "内容A"
        assert eng.render({"agent_id": "community_ops"}) == ""

    def test_tools_require_all_present(self, engine_factory: Any) -> None:
        eng = engine_factory(_doc(_rule(when='tools: ["file_read", "file_write"]')))

        assert eng.render({"tool_ids": ["file_read", "file_write", "bash"]}) == "内容A"
        assert eng.render({"tool_ids": ["file_read"]}) == ""
        assert eng.render({}) == ""


class TestStateConditionWatchlist:
    def test_whitelisted_stable_key_matches(self, engine_factory: Any) -> None:
        eng = engine_factory(_doc(_rule(when='state: {core_type: "*"}')))

        assert eng.render({"core_type": "main"}) == "内容A"

    def test_nested_execution_context_key(self, engine_factory: Any) -> None:
        eng = engine_factory(
            _doc(_rule(when='state: {"execution_context.isolation.level": "container"}'))
        )

        assert (
            eng.render({"execution_context": {"isolation": {"level": "container"}}})
            == "内容A"
        )
        assert eng.render({"execution_context": {"isolation": {"level": "none"}}}) == ""

    def test_non_whitelisted_key_rejects_whole_rule(self, engine_factory: Any) -> None:
        """可变 state 键（iteration 等）不在白名单 → 整条拒载（fail-closed）。"""
        eng = engine_factory(
            _doc(
                _rule(name="good", when="state: {core_type: \"*\"}"),
                _rule(name="bad", content="内容B", when="state: {iteration: \"*\"}"),
            )
        )

        assert eng.render({"core_type": "main", "iteration": 3}) == "内容A"

    def test_unknown_when_key_rejects_rule(self, engine_factory: Any) -> None:
        """when 里的未知键（拼写错）必须拒载——静默忽略会让规则比声明面更宽。"""
        eng = engine_factory(_doc(_rule(when='modell: "deepseek-*"')))

        assert eng.render({"model_id": "deepseek-v4-pro"}) == ""

    def test_unknown_rule_key_rejects_rule(self, engine_factory: Any) -> None:
        eng = engine_factory("rules:\n  - name: r1\n    injecte:\n      content: 内容A\n")

        assert eng.render({"model_id": "deepseek-v4-pro"}) == ""


class TestOrderingAndJoin:
    def test_orders_ascending_join(self, engine_factory: Any) -> None:
        eng = engine_factory(
            _doc(
                _rule(name="late", content="后", order=200),
                _rule(name="early", content="前", order=100),
            )
        )

        assert eng.render({}) == "前\n\n后"

    def test_duplicate_order_rejects_whole_file(self, engine_factory: Any) -> None:
        eng = engine_factory(
            _doc(
                _rule(name="a", content="A", order=100),
                _rule(name="b", content="B", order=100),
            )
        )

        assert eng.render({}) == ""


class TestEnabled:
    def test_disabled_rule_not_rendered(self, engine_factory: Any) -> None:
        eng = engine_factory(_doc(_rule(enabled=False)))

        assert eng.render({}) == ""

    def test_disabled_and_enabled_mixed(self, engine_factory: Any) -> None:
        eng = engine_factory(
            _doc(
                _rule(name="disabled-rule", content="关闭", enabled=False),
                _rule(name="enabled-rule", content="开启"),
            )
        )

        assert eng.render({}) == "开启"


class TestBudget:
    def test_oversize_rule_dropped_others_kept(self, engine_factory: Any) -> None:
        eng = engine_factory(
            _doc(
                _rule(name="huge", content="X" * 50, order=100),
                _rule(name="fine", content="好", order=200),
            ),
            max_rule_chars=10,
        )

        assert eng.render({}) == "好"

    def test_total_budget_skips_overflowing_tail(self, engine_factory: Any) -> None:
        """总量预算按 order 累积，溢出者跳过（确定性：不受执行顺序影响）。"""
        eng = engine_factory(
            _doc(
                _rule(name="first", content="A" * 10, order=100),
                _rule(name="second", content="B" * 10, order=200),
            ),
            max_total_chars=15,
        )

        assert eng.render({}) == "A" * 10


class TestFatalParsing:
    @pytest.mark.parametrize(
        "text",
        [
            "rules: [\n",                        # yaml 语法错
            "- just\n- a\n- list\n",             # 顶层非映射
            "rules: {a: 1}\n",                   # rules 非列表
            "rules:\n  - not-a-mapping\n",       # 条目非映射（逐条拒载，整库仍可渲染）
        ],
        ids=["broken-yaml", "top-level-list", "rules-not-list", "item-not-mapping"],
    )
    def test_malformed_never_raises(self, engine_factory: Any, text: str) -> None:
        eng = engine_factory(text)

        assert eng.render({"model_id": "x"}) == ""

    def test_oversize_file_rejected(self, engine_factory: Any) -> None:
        eng = engine_factory(_doc(_rule()), max_file_bytes=10)

        assert eng.render({}) == ""

    def test_missing_file_zero_injection(self, tmp_path: Path) -> None:
        mod = _load_engine()
        eng = mod.ContributionEngine(tmp_path / "nope.yaml")

        assert eng.render({}) == ""


class TestDeterminism:
    def test_same_input_same_output(self, engine_factory: Any) -> None:
        """性质断言：同状态两次渲染逐字节一致（system 前缀缓存依赖此）。"""
        eng = engine_factory(
            _doc(_rule(name="b", content="B", order=20), _rule(name="a", content="A", order=10))
        )
        state = {"model_id": "deepseek-v4-pro", "tool_ids": ["x"]}

        assert eng.render(state) == eng.render(dict(state))
        assert eng.render(state) == "A\n\nB"

    def test_delete_value_experiment(self, engine_factory: Any) -> None:
        """删值实验：把模型条件改成不匹配值，断言渲染由命中变为零注入。"""
        text_hit = _doc(_rule(when='model: "deepseek-*"'))
        text_miss = _doc(_rule(when='model: "minimax-*"'))

        assert engine_factory(text_hit).render({"model_id": "deepseek-v4-pro"}) == "内容A"
        assert engine_factory(text_miss).render({"model_id": "deepseek-v4-pro"}) == ""


class TestReloadOnChange:
    def test_mtime_change_reloads(self, engine_factory: Any) -> None:
        eng = engine_factory(_doc(_rule(name="v1", content="版本一")))
        assert eng.render({}) == "版本一"

        import os
        import time

        time.sleep(0.01)
        eng.path.write_text(_doc(_rule(name="v2", content="版本二")), encoding="utf-8")
        os.utime(eng.path, (time.time() + 1, time.time() + 1))

        assert eng.render({}) == "版本二"
