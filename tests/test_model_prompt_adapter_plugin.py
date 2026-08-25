# @feature: FP-0.2.二 模型适配插件（model_prompt_adapter） | @ci: python-coverage
"""pipeline_model_prompt_adapter 插件单元测试。

覆盖：首轮窗口判据（len(messages) <= 1）、规则命中与通配路由、
head insert op 构造、白名单过滤、rules.yaml 真实配置的结构校验。
"""

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("input", "model_prompt_adapter")

import asyncio  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402

import plugin as mpa_plugin  # noqa: E402
from pipeline.plugin import PluginContext  # noqa: E402


def _rule(**overrides):
    """基础测试规则：deepseek-v4-pro 命中。"""
    rule = {
        "name": "test_rule",
        "enabled": True,
        "when": {"model_id": "deepseek-v4-pro"},
        "inject": [
            {"role": "user", "content": "示例任务"},
            {
                "role": "assistant",
                "reasoning_content": "用户要求我……让我分析一下……",
                "content": "示例回答",
            },
        ],
    }
    rule.update(overrides)
    return rule


def _make_plugin(*rules, config=None):
    return mpa_plugin.ModelPromptAdapterPlugin(config={"rules": list(rules), **(config or {})})


def _run(p, state):
    return asyncio.run(p.execute(PluginContext(state=state, config={})))


def test_first_turn_head_insert_with_reasoning_content():
    """首轮（历史 1 条）+ 模型命中 → head 插入：at 递增、rc 透传、无标记。"""
    p = _make_plugin(_rule())
    result = _run(p, {"model_id": "deepseek-v4-pro", "messages": [{"role": "user", "content": "hi"}]})

    ops = result.state_updates["messages"]["_ops"]
    assert [op["at"] for op in ops] == [0, 1]
    assert ops[0]["msg"]["role"] == "user"
    assert ops[1]["msg"]["reasoning_content"].startswith("用户要求我")
    # 无状态设计：只更新 messages，不写任何标记
    assert set(result.state_updates.keys()) == {"messages"}


def test_empty_history_also_first_turn():
    """历史 0 条（事件触发无初始消息）→ 同属首轮窗口，注入。"""
    p = _make_plugin(_rule())
    result = _run(p, {"model_id": "deepseek-v4-pro", "messages": []})
    assert "messages" in result.state_updates


def test_history_grown_never_injects():
    """历史 ≥ 2 条（工具轮/多轮/run 恢复）→ 一律不注入，即使从未注入过。

    判据自幂等：不依赖标记，历史长度即真相（含示例已在历史中的场景）。
    """
    p = _make_plugin(_rule())
    grown = [
        {"role": "user", "content": "真实问题"},
        {"role": "assistant", "content": "回答"},
    ]
    result = _run(p, {"model_id": "deepseek-v4-pro", "messages": grown})
    assert result.state_updates == {}


def test_no_model_match_passthrough():
    """模型不命中 → 零副作用透传。"""
    p = _make_plugin(_rule())
    result = _run(p, {"model_id": "glm-5", "messages": [{"role": "user", "content": "hi"}]})
    assert result.state_updates == {}


def test_wildcard_and_list_model_match():
    """fnmatch 通配与列表任一命中。"""
    r1 = _run(
        _make_plugin(_rule(when={"model_id": "deepseek-*"})),
        {"model_id": "deepseek-v4-flash", "messages": []},
    )
    assert r1.state_updates != {}

    r2 = _run(
        _make_plugin(_rule(when={"model_id": ["glm-5", "deepseek-v4-pro"]})),
        {"model_id": "deepseek-v4-pro", "messages": []},
    )
    assert r2.state_updates != {}


def test_default_model_fallback():
    """state 无 model_id/model_tier → 回退 config.default_model 匹配。"""
    p = _make_plugin(_rule(), config={"default_model": "deepseek-v4-pro"})
    result = _run(p, {"messages": []})
    assert result.state_updates != {}


def test_real_state_shape_no_model_id():
    """真实运行时 state 形状（initial_state 键集，无 model_id）：
    model_tier=large + default_model=flash → pro 规则不命中；
    default_model 接为 pro → 命中（用户切 defaults.chat 后的接线）。
    """
    real_state = {
        "message": "帮我检查项目",
        "agent_id": "agentos",
        "core_type": "llm_call",
        "core_plugin": "pipeline_llm_core",
        "ended": False,
        "suspended": False,
        "model_tier": "large",
        "thinking_strength": "high",
        "messages": [],
    }
    # 未接线：default_model 缺省为空 → pro 规则永不命中（防"测试喂假 model_id"重演）
    p_unwired = _make_plugin(_rule())
    assert _run(p_unwired, real_state).state_updates == {}

    # 已接线：config default_model 与 llm.yaml defaults.chat 同步为 pro
    p_wired = _make_plugin(_rule(), config={"default_model": "deepseek-v4-pro"})
    assert _run(p_wired, real_state).state_updates != {}


def test_boolean_state_match():
    """when.state 布尔匹配：yaml 写 true 匹配 state 布尔 True（规范化）。"""
    rule = _rule(when={"model_id": "deepseek-*", "state": {"ended": "false"}})
    hit = _run(_make_plugin(rule), {"model_id": "deepseek-v4-pro", "ended": False, "messages": []})
    assert hit.state_updates != {}

    miss = _run(_make_plugin(rule), {"model_id": "deepseek-v4-pro", "ended": True, "messages": []})
    assert miss.state_updates == {}


def test_missing_key_matches_empty():
    """state 键缺失 → 规范化空串，规则写 "" 或 * 可匹配缺键。"""
    p = _make_plugin(_rule(when={"model_id": "deepseek-*", "state": {"nope": ""}}))
    result = _run(p, {"model_id": "deepseek-v4-pro", "messages": []})
    assert result.state_updates != {}


def test_numeric_state_match():
    """when.state 数字匹配：yaml 写 '3' 匹配 state 数字 3。"""
    p = _make_plugin(_rule(when={"model_id": "deepseek-*", "state": {"iter": "3"}}))
    result = _run(p, {"model_id": "deepseek-v4-pro", "iter": 3, "messages": []})
    assert result.state_updates != {}


def test_state_condition_gate():
    """when.state 全部满足才命中；缺键/不匹配都不注入。"""
    rule = _rule(when={"model_id": "deepseek-*", "state": {"core_plugin": "pipeline_llm_core"}})
    hit = _run(_make_plugin(rule), {"model_id": "deepseek-v4-pro", "core_plugin": "pipeline_llm_core", "messages": []})
    assert hit.state_updates != {}

    miss = _run(
        _make_plugin(rule), {"model_id": "deepseek-v4-pro", "core_plugin": "pipeline_tool_core", "messages": []}
    )
    assert miss.state_updates == {}

    absent = _run(_make_plugin(rule), {"model_id": "deepseek-v4-pro", "messages": []})
    assert absent.state_updates == {}


def test_sanitize_drops_disallowed_role_and_fields():
    """非法角色丢弃、白名单外字段丢弃，合法消息保留。"""
    rule = _rule(
        inject=[
            {"role": "system", "content": "不应注入"},
            {"role": "assistant", "content": "ok", "tool_calls": [{"id": "x"}], "seq": 99},
        ]
    )
    p = _make_plugin(rule)
    result = _run(p, {"model_id": "deepseek-v4-pro", "messages": []})
    ops = result.state_updates["messages"]["_ops"]
    assert len(ops) == 1
    assert set(ops[0]["msg"].keys()) == {"role", "content"}


def test_first_matching_rule_wins():
    """多规则命中取首个（enabled=false 跳过）。"""
    p = _make_plugin(
        _rule(name="disabled", enabled=False, inject=[{"role": "user", "content": "A"}]),
        _rule(name="second", inject=[{"role": "user", "content": "B"}]),
    )
    result = _run(p, {"model_id": "deepseek-v4-pro", "messages": []})
    ops = result.state_updates["messages"]["_ops"]
    assert len(ops) == 1
    assert ops[0]["msg"]["content"] == "B"


def test_shipped_rules_yaml_structure():
    """仓库自带 rules.yaml 可加载，deepseek-v4-pro 规则结构完整。

    防止规则文件被改坏（示例丢失/字段漂移）导致静默失效。
    """
    rules_path = Path(mpa_plugin.__file__).resolve().parent / "rules.yaml"
    rules = mpa_plugin.load_rules_from(rules_path)
    assert rules, "rules.yaml 应至少含一条规则"

    ds = next(r for r in rules if "deepseek" in str(r.get("when", {}).get("model_id", "")))
    inject = ds.get("inject") or []
    assert len(inject) == 2
    assert inject[0]["role"] == "user"
    assert inject[1]["role"] == "assistant"
    # 思维链示例风格断言（从 DSH 完整环境 v4-pro 实跑 rc 改写，
    # 素材 ~/.dsh/reasoning_samples/）：中文对话 + 英文 rc；
    # 实跑样本骨架句式在（识别语言复述开头 → 任务类型判断 → 事实
    # 先行 → Hmm/Let me consider 权衡 → Let me 收尾）；
    # 内容不绑定任何具体任务（无具体对象/动作引用）
    rc = inject[1].get("reasoning_content") or ""
    assert rc.startswith("The user is asking me, in Chinese, to handle a new task")
    assert "build task" in rc
    assert "fix task" in rc
    assert "Let me consider:" in rc
    assert "Let me start by" in rc
    assert "current state of the project" not in rc  # 不绑具体事情
    assert "请帮我处理一个新任务" in inject[0]["content"]  # user 消息中性化
    assert "Let me start by" in rc
    # 中文对话 + 中文回答（真实分布同构）
    assert re.search(r"[\u4e00-\u9fff]", inject[0]["content"])
    assert re.search(r"[\u4e00-\u9fff]", inject[1]["content"])
    # 经 sanitize 后仍完整存活
    sanitized = mpa_plugin.ModelPromptAdapterPlugin._sanitize(inject)
    assert sanitized == inject
