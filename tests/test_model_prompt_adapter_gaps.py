# @feature: FP-0.2.二 模型适配插件（model_prompt_adapter） | @ci: python-coverage
"""model_prompt_adapter 缺口分支补测（coverage.xml 缺口行靶单）。

行为契约（断输入→输出/副作用）：
- 插件身份：name 恒 "model_prompt_adapter"、priority 恒 55（挂载序契约）。
- 规则加载优先级：config 内联 rules > rules_path > 插件目录 rules.yaml；
  rules_path 相对路径按插件目录解析（非 CWD）；绝对路径原样。
- 规则文件缺失 → warning 留痕 + 空规则集（零副作用透传，不抛）。
- 规则文件 rules 字段非列表 / 顶层非 dict → warning + 空规则集。
- rules 数组内非 dict 条目剔除；内联 rules 非 list（如字符串）→ 走文件路径分支。
- 注入体过滤：非 dict 条目跳过；缺 content 且缺 reasoning_content 的消息丢弃
  （API 拒收空内容体）；白名单角色之外丢弃并留痕。
- 多规则顺序：先命中的生效；命中但注入体过滤后为空 → 继续尝试后续规则。

防拟合：同一分支以 ≥2 组有区分度输入（正常/边界/形态相反）验证；
字面值断言配性质断言（ops 数 = 有效注入条数、at 严格递增）。

不可达/环境依赖残留（逐条说明）：无。本文件覆盖的均为可达分支。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PLUGIN_DIR = _REPO_ROOT / "plugins" / "shared" / "pipeline" / "input" / "model_prompt_adapter"
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
for _p in (str(_SHARED_DIR), str(_PLUGIN_DIR)):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
# 裸名自防御：整车道里先导测试缓存的别家 `plugin` 模块会遮蔽本插件目录
# （与 test_isolation_docker_recheck.py 同款处置），逐出后再 import。
for _bare in ("plugin", "tool", "models", "service"):
    sys.modules.pop(_bare, None)

import plugin as mpa_plugin  # noqa: E402


def _run(p: Any, state: dict[str, Any]) -> Any:
    return asyncio.run(p.execute(mpa_plugin.PluginContext(state=state, config={})))


def _rule(inject: list[Any] | None = None, **overrides: Any) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "name": "gaps_rule",
        "enabled": True,
        "when": {"model_id": "probe-*"},
        "inject": inject if inject is not None else [{"role": "user", "content": "示例"}],
    }
    rule.update(overrides)
    return rule


def _state(model: str = "probe-v4", **extra: Any) -> dict[str, Any]:
    return {"model_id": model, "messages": [], **extra}


# ═══════════════ 插件身份 ═══════════════


class TestIdentity:
    def test_name_and_priority_constant(self) -> None:
        """name 恒为插件 id；priority 恒 55（prepare 链挂载序契约）。"""
        p = mpa_plugin.ModelPromptAdapterPlugin(config={})

        assert p.name == "model_prompt_adapter"
        assert isinstance(p.priority, int)
        assert p.priority == 55
        assert p.name == mpa_plugin.ModelPromptAdapterPlugin(config={"rules": []}).name


# ═══════════════ 规则加载优先级与形态 ═══════════════


class TestRuleLoading:
    def test_inline_rules_win_over_rules_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """config 内联 rules 最高优先级：rules_path 指向的文件不被读取。"""
        file_path = tmp_path / "from_file.yaml"
        file_path.write_text(
            yaml.safe_dump({"rules": [_rule(inject=[{"role": "user", "content": "文件"}] )]}),
            encoding="utf-8",
        )
        p = mpa_plugin.ModelPromptAdapterPlugin(config={
            "rules": [_rule(inject=[{"role": "user", "content": "内联"}])],
            "rules_path": str(file_path),
        })

        result = _run(p, _state())

        assert result.state_updates["messages"]["_ops"][0]["msg"]["content"] == "内联"

    @pytest.mark.parametrize("relative", ["rules_abs.yaml", "nested/rules_abs.yaml"])
    def test_relative_rules_path_resolved_against_plugin_dir(
        self, relative: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """相对 rules_path 基于插件目录解析（sidecar cwd 是插件目录，非 CWD）。"""
        target = _PLUGIN_DIR / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            yaml.safe_dump({"rules": [_rule(inject=[{"role": "user", "content": relative}])]}),
            encoding="utf-8",
        )
        # CWD 指向别处：解析若错误地跟随 CWD 则加载失败（零注入）
        monkeypatch.chdir(_REPO_ROOT)
        try:
            p = mpa_plugin.ModelPromptAdapterPlugin(config={"rules_path": relative})
            result = _run(p, _state())
        finally:
            target.unlink()

        assert result.state_updates["messages"]["_ops"][0]["msg"]["content"] == relative

    def test_absolute_rules_path_used_verbatim(self, tmp_path: Path) -> None:
        """绝对 rules_path 原样使用（不受插件目录影响）。"""
        file_path = tmp_path / "abs_rules.yaml"
        file_path.write_text(
            yaml.safe_dump({"rules": [_rule(inject=[{"role": "assistant", "content": "绝对"}])]}),
            encoding="utf-8",
        )
        p = mpa_plugin.ModelPromptAdapterPlugin(config={"rules_path": str(file_path)})

        result = _run(p, _state())

        assert result.state_updates["messages"]["_ops"][0]["msg"]["content"] == "绝对"

    def test_inline_rules_non_list_falls_through_to_file(
        self, tmp_path: Path,
    ) -> None:
        """内联 rules 非 list（字符串/None）→ 视为未配置，改走 rules_path。"""
        file_path = tmp_path / "fallback.yaml"
        file_path.write_text(
            yaml.safe_dump({"rules": [_rule(inject=[{"role": "user", "content": "回落"}])]}),
            encoding="utf-8",
        )
        for bad_inline in ("a-string", 42, {"not": "list"}):
            p = mpa_plugin.ModelPromptAdapterPlugin(config={
                "rules": bad_inline, "rules_path": str(file_path),
            })
            result = _run(p, _state())
            assert result.state_updates["messages"]["_ops"][0]["msg"]["content"] == "回落"

    def test_missing_rule_file_warns_and_degrades(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """规则文件不存在 → warning 留痕 + 空规则集（透传，不抛）。"""
        p = mpa_plugin.ModelPromptAdapterPlugin(config={"rules_path": str(tmp_path / "nope.yaml")})

        with caplog.at_level("WARNING"):
            result = _run(p, _state())

        assert result.state_updates == {}
        assert any("规则文件不存在" in r.message for r in caplog.records)

    @pytest.mark.parametrize(
        ("payload", "expect_warning"),
        [
            ({"rules": "not-a-list"}, True),       # rules 字段非列表
            ({"rules": None}, False),              # 缺省空 → 走 `or []`，无告警
            ({"other": 1}, False),                 # 无 rules 键 → 空集
        ],
    )
    def test_malformed_rule_document_degrades(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
        payload: Any, expect_warning: bool,
    ) -> None:
        """规则文件形态非法 → 空规则集透传；rules 显式非列表额外留痕。"""
        file_path = tmp_path / "malformed.yaml"
        file_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
        p = mpa_plugin.ModelPromptAdapterPlugin(config={"rules_path": str(file_path)})

        with caplog.at_level("WARNING"):
            result = _run(p, _state())

        assert result.state_updates == {}
        warned = any("rules 字段非列表" in r.message for r in caplog.records)
        assert warned is expect_warning

    def test_toplevel_non_mapping_document_degrades(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """规则文档顶层非映射（列表/标量）→ 告警 + 空规则集透传。

        yaml.safe_load 对整份文档写成列表/标量的形态返回非 dict，缺同一防线
        会让插件**构造期抛 AttributeError**（曾锁现状记录，修复后反转断言）。
        顶层非映射与"rules 字段非列表"同一契约：畸形文档一律透传。
        """
        for payload in (["rule-a", "rule-b"], "plain-scalar"):
            file_path = tmp_path / f"non_dict_doc_{len(str(payload))}.yaml"
            file_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

            with caplog.at_level("WARNING"):
                result = _run(
                    mpa_plugin.ModelPromptAdapterPlugin(config={"rules_path": str(file_path)}),
                    _state(),
                )

            assert result.state_updates == {}, "畸形文档必须零副作用透传（不抛、不注入）"
            assert any("规则文档顶层非映射" in r.message for r in caplog.records)
            caplog.clear()

    def test_non_dict_rule_entries_filtered(self, tmp_path: Path) -> None:
        """rules 数组内非 dict 条目剔除，合法条目照常生效。"""
        file_path = tmp_path / "mixed.yaml"
        file_path.write_text(
            yaml.safe_dump({"rules": [
                "junk",
                7,
                None,
                _rule(inject=[{"role": "user", "content": "合法"}]),
            ]}),
            encoding="utf-8",
        )
        p = mpa_plugin.ModelPromptAdapterPlugin(config={"rules_path": str(file_path)})

        result = _run(p, _state())

        assert result.state_updates["messages"]["_ops"][0]["msg"]["content"] == "合法"


# ═══════════════ 注入体过滤 ═══════════════


class TestInjectSanitization:
    @pytest.mark.parametrize(
        ("inject", "expected_bodies"),
        [
            # 缺 content 且缺 reasoning_content → API 拒收，丢弃
            ([{"role": "user"}], []),
            ([{"role": "user"}, {"role": "assistant", "content": "有效"}], ["有效"]),
            # 只带 reasoning_content（无 content）合法：tool 轮回传 rc 的原生格式
            ([{"role": "assistant", "reasoning_content": "想"}], [None]),
        ],
    )
    def test_contentless_messages_dropped(
        self, inject: list[Any], expected_bodies: list[Any],
    ) -> None:
        """缺内容体的注入消息被丢弃；有 rc 无 content 属合法形态保留。"""
        p = mpa_plugin.ModelPromptAdapterPlugin(config={"rules": [_rule(inject=inject)]})

        result = _run(p, _state())

        if not expected_bodies:
            assert result.state_updates == {}
            return
        ops = result.state_updates["messages"]["_ops"]
        assert len(ops) == len(expected_bodies)
        assert [op["msg"].get("content") for op in ops] == expected_bodies

    def test_non_dict_inject_entries_skipped(self) -> None:
        """注入数组内非 dict 条目跳过，其余保留（配置作者写错单条不废整组）。"""
        p = mpa_plugin.ModelPromptAdapterPlugin(config={"rules": [_rule(inject=[
            "junk",
            None,
            3,
            {"role": "user", "content": "保留"},
        ])]})

        result = _run(p, _state())

        ops = result.state_updates["messages"]["_ops"]
        assert len(ops) == 1
        assert ops[0]["msg"]["content"] == "保留"

    def test_all_invalid_inject_continues_to_next_rule(self) -> None:
        """命中规则过滤后注入体为空 → 不产出空 ops，继续尝试后续规则。"""
        p = mpa_plugin.ModelPromptAdapterPlugin(config={"rules": [
            _rule(name="empty", inject=[{"role": "user"}, {"role": "system", "content": "x"}]),
            _rule(name="usable", inject=[{"role": "user", "content": "后手"}]),
        ]})

        result = _run(p, _state())

        ops = result.state_updates["messages"]["_ops"]
        assert len(ops) == 1
        assert ops[0]["msg"]["content"] == "后手"

    def test_dropped_role_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """非法角色丢弃时 warning 留痕（配置作者可发现规则失效）。"""
        p = mpa_plugin.ModelPromptAdapterPlugin(config={"rules": [_rule(inject=[
            {"role": "tool", "content": "不应注入"},
            {"role": "user", "content": "ok"},
        ])]})

        with caplog.at_level("WARNING"):
            result = _run(p, _state())

        assert any("丢弃非法角色消息" in r.message for r in caplog.records)
        assert len(result.state_updates["messages"]["_ops"]) == 1

    def test_insert_positions_strictly_increasing(self) -> None:
        """head 插入位置从 0 严格递增（引擎按序应用，原历史 seq 顺延）。"""
        bodies = ["a", "b", "c"]
        p = mpa_plugin.ModelPromptAdapterPlugin(config={"rules": [_rule(inject=[
            {"role": "user", "content": body} for body in bodies
        ])]})

        result = _run(p, _state())
        ops = result.state_updates["messages"]["_ops"]

        assert [op["at"] for op in ops] == [0, 1, 2]
        assert [op["msg"]["content"] for op in ops] == bodies
        assert all(op["op"] == "insert" for op in ops)
