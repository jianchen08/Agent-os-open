# @feature: FP-0.2.二 | @vision: V2 安全
# @ci: python-coverage
"""管道级免审批（v2 设计）行为锁：五选项 / 切档 / 血缘子树 / 终审保留。

契约（docs/working/审批管道级免审批_设计稿v2_2026-11-06.md）：
1. 审批卡五选项：approved_once / approved_remember / approved_pipeline_scope /
   approved_permanent_whitelist / denied（稳定 id + 封闭语义）；
2. 「本管道后续免审批」= 管道级权限档切旁路：先警告确认，confirm 才写覆盖表，
   cancel/超时/异常一律不切档且本次按仅本次放行；
3. 子树经血缘链查表继承（向下不向上），撤销后恢复弹卡；
4. 管道覆盖档为旁路时终审规则（final_approval）仍逐次人审；手动会话级旁路
   语义不变；
5. 规则降级态压制管道覆盖档（保守弹卡）；
6. 永久白名单：命令精确指纹跨管道跨会话生效；
7. 同命令免批记忆载体 = 管道级 state 键 task.approved_signatures（子任务可读）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_THIS_DIR = str(Path(__file__).resolve().parent)
_SHARED_DIR = str(Path(__file__).resolve().parents[1])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

_spec = importlib.util.spec_from_file_location(
    "security_check_plugin_pipeline_scope", str(Path(_THIS_DIR) / "plugin.py")
)
assert _spec is not None
assert _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# 每测试隔离目录（autouse fixture 写入；需要断盘上产物的用例读它）。
_STATE_DIR: Path | None = None

pytestmark = pytest.mark.unit


class FakeCap:
    """human-interaction 假 capability：按序回放选中项，并按卡上声明回语义。"""

    def __init__(self, answers: list[str] | list[dict[str, Any]]) -> None:
        self.answers = list(answers)
        self.cards: list[dict[str, Any]] = []
        self._last_options: list[dict[str, Any]] = []

    async def call(self, method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        if method == "create_choice":
            self.cards.append(params)
            self._last_options = list(params.get("options") or [])
            return {"request_id": f"req-{len(self.cards)}"}
        if method == "wait_for_choice":
            assert self.answers, "审批次数超出预期（answers 耗尽）"
            answer = self.answers.pop(0)
            if isinstance(answer, dict):
                # 自由文本应答等原样回放（selected_semantics 缺空 → 拒绝分支）
                return dict(answer)
            semantics = ""
            for opt in self._last_options:
                if opt.get("id") == answer:
                    semantics = str(opt.get("semantics") or "")
                    break
            return {
                "selected_option": answer,
                "selected_semantics": semantics,
                "response_type": "answered",
            }
        raise AssertionError(f"unexpected cap.call: {method}")


def _wire(answers: list[str] | list[dict[str, Any]]) -> FakeCap:
    cap = FakeCap(answers)
    _mod.set_human_interaction_cap(cap)
    return cap


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """每测试独立存储与表（不碰用户数据根/共享根）。"""
    global _STATE_DIR  # noqa: PLW0603
    _STATE_DIR = tmp_path
    scope_mod = sys.modules["approval_scope"]
    monkeypatch.setattr(_mod, "_PERMISSION_MODES_FILE", str(tmp_path / "permission_modes.json"))
    monkeypatch.setattr(_mod, "_PERMISSION_MODES", {})
    # 管道级表/白名单是 approval_scope 模块级对象（plugin 别名同引用）——原地清空。
    scope_mod.PIPELINE_PERMISSION_MODES.clear()
    scope_mod.PIPELINE_PARENTS.clear()
    scope_mod.PERMANENT_WHITELIST.clear()
    _mod.set_human_interaction_cap(None)
    yield tmp_path
    _mod.set_human_interaction_cap(None)
    scope_mod.PIPELINE_PERMISSION_MODES.clear()
    scope_mod.PIPELINE_PARENTS.clear()
    scope_mod.PERMANENT_WHITELIST.clear()
    _STATE_DIR = None


def _ctx(
    *,
    pipeline_id: str = "p1",
    session_id: str = "s1",
    parent_pipeline_id: str = "",
    command: str = "rm -rf /tmp/x",
    commands: list[str] | None = None,
    tool: str = "bash",
    state_extra: dict[str, Any] | None = None,
) -> Any:
    calls = [
        {"name": tool, "args": {"command": value}}
        for value in (commands if commands is not None else [command])
    ]
    state: dict[str, Any] = {
        "core_type": "tool_execute",
        "raw_tool_calls": calls,
        "execution_contexts": [{"tool_name": tool, "provider": "host"}],
        "pipeline_id": pipeline_id,
        "session_id": session_id,
        "user_id": "u1",
    }
    if parent_pipeline_id:
        state["lineage.parent_pipeline_id"] = parent_pipeline_id
    if state_extra:
        state.update(state_extra)
    return _mod.PluginContext(state=state, _services={})


def _rules() -> list[dict[str, Any]]:
    """最小非降级规则集（命中即弹审批，供切档/白名单用例走正常规则轨道）。"""
    return [
        {
            "name": "dangerous_commands",
            "tools": ["*"],
            "params": ["command", "cmd"],
            "action": "needs_approval",
            "patterns": [{"type": "keyword", "value": "rm -rf"}],
        }
    ]


def _blocked(result: Any) -> bool:
    return bool(result.state_updates.get("pre_decided_results"))


@pytest.mark.asyncio
async def test_approval_card_has_five_options() -> None:
    """审批卡下发五选项（稳定 id + 封闭语义），denied 保持末位。"""
    cap = _wire(["approved_once"])
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})

    await plugin.execute(_ctx())

    options = cap.cards[0]["options"]
    assert [o["id"] for o in options] == [
        "approved_once",
        "approved_remember",
        "approved_pipeline_scope",
        "approved_permanent_whitelist",
        "denied",
    ]
    assert [o["semantics"] for o in options] == [
        "approve_once",
        "approve_and_remember",
        "approve_pipeline_scope",
        "approve_permanent_whitelist",
        "deny",
    ]
    assert options[-1]["label"] == "拒绝执行"


@pytest.mark.asyncio
async def test_pipeline_scope_grant_switches_mode_and_persists() -> None:
    """confirm 后写覆盖表并落盘；同管道后续危险调用不再弹卡。"""
    cap = _wire(["approved_pipeline_scope", "confirm"])
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})

    first = await plugin.execute(_ctx())
    assert not _blocked(first)
    assert len(cap.cards) == 2, "先审批卡，再警告确认卡"
    assert cap.cards[1]["title"] == "管道免审批切换确认"
    assert _mod._PIPELINE_PERMISSION_MODES == {"p1": "bypass"}
    assert _STATE_DIR is not None
    assert (_STATE_DIR / "permission_modes_pipeline.json").is_file()
    audit = (_STATE_DIR / "security_audit.jsonl").read_text(encoding="utf-8")
    assert "mode_switch" in audit
    assert "grant" in audit

    # 同管道后续危险调用：整体放行，不再建卡
    second = await plugin.execute(_ctx(command="rm -rf /tmp/other"))
    assert not _blocked(second)
    assert len(cap.cards) == 2, "切档后不再弹审批"


@pytest.mark.asyncio
async def test_pipeline_scope_cancel_keeps_asking() -> None:
    """确认卡取消 → 不切档；本次按仅本次放行，下一次仍弹卡。"""
    cap = _wire(["approved_pipeline_scope", "cancel", "approved_once"])
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})

    first = await plugin.execute(_ctx())
    assert not _blocked(first), "取消切档仍按仅本次放行"
    assert _mod._PIPELINE_PERMISSION_MODES == {}

    await plugin.execute(_ctx())
    assert len(cap.cards) == 3, "未切档 → 下一次仍须审批"


@pytest.mark.asyncio
async def test_pipeline_scope_inherited_by_child_and_grandchild() -> None:
    """祖先覆盖档经血缘链命中：子/孙任务免审；兄弟管道不受影响。"""
    _mod._PIPELINE_PERMISSION_MODES["p-root"] = "bypass"
    cap = _wire(["approved_once"])
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})

    child = await plugin.execute(_ctx(pipeline_id="p-child", parent_pipeline_id="p-root"))
    assert not _blocked(child)
    assert cap.cards == [], "子任务应继承祖先覆盖档，不弹卡"

    grandchild = await plugin.execute(_ctx(pipeline_id="p-grand", parent_pipeline_id="p-child"))
    assert not _blocked(grandchild)
    assert cap.cards == [], "孙任务经血缘链查表继承"

    # 兄弟管道（父链不指向被授予管道）→ 照常弹卡（fail-closed）
    sibling = await plugin.execute(_ctx(pipeline_id="p-sibling", parent_pipeline_id="p-other"))
    assert len(cap.cards) == 1
    assert not _blocked(sibling)


@pytest.mark.asyncio
async def test_pipeline_override_revoked_restores_approval() -> None:
    """撤销覆盖表条目 → 本管道恢复弹卡（撤销端点等价行为）。"""
    _mod._PIPELINE_PERMISSION_MODES["p1"] = "bypass"
    cap = _wire(["approved_once"])
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})

    assert not _blocked(await plugin.execute(_ctx()))
    assert cap.cards == []

    _mod._PIPELINE_PERMISSION_MODES.pop("p1")
    assert not _blocked(await plugin.execute(_ctx()))
    assert len(cap.cards) == 1, "撤销后恢复弹卡"


@pytest.mark.asyncio
async def test_degraded_rules_suppress_pipeline_override() -> None:
    """规则降级态压制管道覆盖档（终审不可匹配 → 保守回弹卡）。"""
    _mod._PIPELINE_PERMISSION_MODES["p1"] = "bypass"
    cap = _wire(["approved_once"])
    # rules=[] → 无可用规则 → _rules_degraded=True（降级态压制覆盖档）
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": []})
    assert plugin._rules_degraded is True

    result = await plugin.execute(_ctx())
    assert len(cap.cards) == 1, "降级态必须保守弹卡"
    assert not _blocked(result)


@pytest.mark.asyncio
async def test_permanent_whitelist_grant_crosses_pipeline_and_session() -> None:
    """永久白名单：命令精确指纹落文件，新管道新会话同命令直接放行。"""
    cap = _wire(["approved_permanent_whitelist"])
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})
    await plugin.execute(_ctx())

    assert len(_mod._PERMANENT_WHITELIST) == 1
    assert _STATE_DIR is not None
    assert (_STATE_DIR / "permanent_whitelist.json").is_file()

    # 新实例 + 新管道 + 新会话：同命令不再询问
    fresh = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})
    result = await fresh.execute(_ctx(pipeline_id="p2", session_id="s2"))
    assert not _blocked(result)
    assert len(cap.cards) == 1, "永久白名单跨管道跨会话生效"


@pytest.mark.asyncio
async def test_permanent_whitelist_requires_exact_command() -> None:
    """指纹精确匹配：加白 A 命令不放行 B 命令。"""
    cap = _wire(["approved_permanent_whitelist", "approved_once"])
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})
    await plugin.execute(_ctx(command="rm -rf /tmp/a"))

    await plugin.execute(_ctx(command="rm -rf /tmp/b"))
    assert len(cap.cards) == 2, "不同命令仍须审批"


@pytest.mark.asyncio
async def test_final_approval_still_asks_in_pipeline_bypass() -> None:
    """管道覆盖档旁路：终审规则命中仍弹审批；未命中调用直接放行。"""
    rules = [
        {
            "name": "final_approval_publish",
            "final_approval": True,
            "tools": ["*"],
            "params": ["command"],
            "action": "needs_approval",
            "patterns": [{"type": "keyword", "value": "git push --force"}],
        }
    ]
    _mod._PIPELINE_PERMISSION_MODES["p1"] = "bypass"
    cap = _wire(["approved_once"])
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": rules})

    # 非终审命令：整体放行
    assert not _blocked(await plugin.execute(_ctx(command="ls -la")))
    assert cap.cards == []

    # 终审命令：仍逐次人审
    await plugin.execute(_ctx(command="git push --force"))
    assert len(cap.cards) == 1
    assert "终审" in cap.cards[0]["title"] or "终审" in cap.cards[0]["description"]


@pytest.mark.asyncio
async def test_manual_session_bypass_skips_final_approval() -> None:
    """手动会话级旁路语义不变：终审规则不进匹配（与管道覆盖档区分）。"""
    rules = [
        {
            "name": "final_approval_publish",
            "final_approval": True,
            "tools": ["*"],
            "params": ["command"],
            "action": "needs_approval",
            "patterns": [{"type": "keyword", "value": "git push --force"}],
        }
    ]
    _mod._PERMISSION_MODES["s1"] = "bypass"
    cap = _wire([])
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": rules})

    await plugin.execute(_ctx(command="git push --force"))
    assert cap.cards == [], "手动旁路档既有语义零改动"


@pytest.mark.asyncio
async def test_signature_memory_state_key_seeded_child() -> None:
    """管道级指纹记忆 state 键：子任务携带快照即命中免批。"""
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})
    signature = plugin._make_signature("bash", {"command": "rm -rf /tmp/x"})
    assert signature

    cap = _wire([])
    ctx = _ctx(state_extra={_mod.APPROVED_SIGNATURES_KEY: json.dumps([signature])})
    result = await plugin.execute(ctx)
    assert not _blocked(result)
    assert cap.cards == [], "已记忆指纹不应再弹卡"


# ── 命令解析：复合命令按子命令逐条授权（用户裁定 2026-11-12）──────────────


def test_split_command_segments_quote_aware() -> None:
    """分隔符切分引号/转义感知：引号内 && / ; / | 不切。"""
    segments = _mod.split_command_segments('echo "a && b" && rm -rf /tmp/x; ls | wc -l')
    assert segments == ['echo "a && b"', "rm -rf /tmp/x", "ls", "wc -l"]
    assert _mod.split_command_segments("   ") == []


@pytest.mark.asyncio
async def test_card_lists_all_parsed_commands() -> None:
    """审批卡明示解析出的全部命令（白名单/免批覆盖范围可见），同命令归并成一条。"""
    cap = _wire(["approved_once"])
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})

    await plugin.execute(_ctx(command="rm -rf /tmp/a && rm -rf /tmp/b"))

    description = cap.cards[0]["description"]
    parsed = _parsed_command_lines(description)
    assert len(parsed) == 1, f"同命令不同参数应归并为一条，实际 {parsed}"
    assert parsed[0].startswith("1) rm")
    assert "2 条已归并" in parsed[0]


def _parsed_command_lines(description: str) -> list[str]:
    """取卡面「解析出的免审批命令」段下的条目行（去掉标题行）。"""
    parts = description.split("解析出的免审批命令", 1)
    assert len(parts) == 2, f"卡面缺解析命令段: {description!r}"
    body = parts[1].split(":", 1)[1]
    return [line.strip() for line in body.splitlines() if line.strip()]


@pytest.mark.parametrize(
    ("segments", "expect"),
    [
        # 同命令不同参数 → 一条（可执行文件名做键）
        (["rm -rf /tmp/a", "rm -rf /tmp/b"], [("rm", 2)]),
        # 同子命令（在参数摘要里可辨，标签只到可执行文件名——G5：不做子命令升格）
        (["git status --short", "git status --porcelain"], [("git", 2)]),
        # 子命令不同 → 同样只到可执行文件名，仍只有一条
        (["git status", "git push --force"], [("git", 2)]),
        # 首个位置参数不同也归并（`mkdir a` / `mkdir b` 视为同命令）
        (["mkdir out_a", "mkdir out_b"], [("mkdir", 2)]),
        # 位置参数不得被当作子命令写进标签（G5：`grep -rn x ...` 的 `x`）
        (["grep -rn x src/a.py", "grep -rn x src/b.py"], [("grep", 2)]),
        # 不同命令不合并，单次出现的命令原样展示（参数信息不丢）
        (["ls -la", "grep -rn x src/a.py"], [("ls -la", 1), ("grep -rn x src/a.py", 1)]),
        # 绝对路径写法与裸命令名视为同一命令
        (["/usr/bin/git status -s", "git status -s"], [("git", 2)]),
        # 纯空白/畸形引号输入不抛（畸形段退化为按可执行文件名归并）
        ([], []),
        (['echo "unbalanced', 'echo "unbalanced'], [("echo", 2)]),
    ],
)
def test_merge_command_segments_command_level(
    segments: list[str], expect: list[tuple[str, int]]
) -> None:
    """命令级归并：键落可执行文件名，同命令不同参数不产生多条。"""
    scope_mod = sys.modules["approval_scope"]
    assert scope_mod.merge_command_segments(segments) == expect


@pytest.mark.parametrize(
    ("segments", "expect_key"),
    [
        # G5 回归锁：位置参数不得被认作子命令（键只到可执行文件名）
        (["grep -rn x src/a.py"], "grep"),
        (["grep -rn x src/a.py", "grep -rn y src/b.py"], "grep"),
        (["git status --short"], "git"),
        (["rm -rf /tmp/a"], "rm"),
        # 绝对路径 / .exe 写法归一
        (["/usr/bin/git status"], "git"),
        (["C:\\Windows\\System32\\cmd.exe /c dir"], "cmd"),
        # 无法分词的畸形段不抛，整段原文兜底做键
        (['echo "unbalanced'], "echo"),
    ],
)
def test_command_group_key_never_infers_subcommand(
    segments: list[str], expect_key: str
) -> None:
    """归并键只含可执行文件名：任何位置参数都不写进键/标签（G5 修正）。"""
    scope_mod = sys.modules["approval_scope"]
    for segment in segments:
        assert scope_mod.command_group_key(segment) == expect_key

    # 性质断言：归并标签不含第二个 token（位置参数不会污染标签）
    for label, _count in scope_mod.merge_command_segments(segments):
        assert label.split() == [expect_key] or label == segments[0]


@pytest.mark.asyncio
async def test_card_merges_same_command_across_calls() -> None:
    """同批多调用里同一命令的不同参数：卡面只出现一条。"""
    cap = _wire(["approved_once"])
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})

    await plugin.execute(
        _ctx(commands=["rm -rf /tmp/a", "rm -rf /tmp/b", "git status --short", "git status -u"])
    )

    parsed = _parsed_command_lines(cap.cards[0]["description"])
    assert len(parsed) == 2, f"两个命令应各出一条，实际 {parsed}"
    assert parsed[0].startswith("1) rm")
    assert parsed[1].startswith("2) git")
    # 参数差异不产生独立条目，但必须留在同一条目内可见（审批知情权）：
    # 归并行附参数摘要，两个参数版本都能在该行内看到。
    assert "/tmp/a" in parsed[0], f"归并行应含参数摘要: {parsed[0]}"
    assert "/tmp/b" in parsed[0], f"归并行应含参数摘要: {parsed[0]}"
    assert "--short" in parsed[1], f"归并行应含参数摘要: {parsed[1]}"
    assert "-u" in parsed[1], f"归并行应含参数摘要: {parsed[1]}"


@pytest.mark.parametrize(
    ("segments", "expect_fragments"),
    [
        # 同命令不同参数：两个参数版本都在归并行内可见
        (["rm -rf /tmp/a", "rm -rf /tmp/b"], ["/tmp/a", "/tmp/b"]),
        # 子命令不同：标签退回 exe，但子命令差异在参数摘要里可辨
        (["git status", "git push --force"], ["status", "push --force"]),
        # 同子命令不同参数：参数差异可辨
        (["git status --short", "git status --porcelain"], ["--short", "--porcelain"]),
        # 引号原样保留（摘要不改写命令语义）
        (['echo -m "a b"', 'echo -m "c d"'], ['-m "a b"', '-m "c d"']),
    ],
)
def test_merged_entry_keeps_parameter_summary(
    segments: list[str], expect_fragments: list[str]
) -> None:
    """归并行必须带可辨识参数摘要（G1/G2）：一条一行 + 参数可读。"""
    scope_mod = sys.modules["approval_scope"]
    rendered = scope_mod.format_parsed_command_list(segments)

    body = [line for line in rendered[1:] if line.strip()]
    assert len(body) == 1, f"同命令应只出一条，实际 {body}"
    for fragment in expect_fragments:
        assert fragment in body[0], f"归并行缺参数片段 {fragment!r}: {body[0]}"
    # 性质断言：条目数与归并键数一致（不因参数多寡再分裂）
    assert len(body) == len(scope_mod.merge_command_segments(segments))


def test_param_summary_truncates_and_counts() -> None:
    """超长参数按首 80 字符截断并标注全长；多于 3 条标「等 N 种」。"""
    scope_mod = sys.modules["approval_scope"]
    long_a = "rm -rf /tmp/" + "a" * 200
    long_b = "rm -rf /tmp/" + "b" * 200

    line = [ln for ln in scope_mod.format_parsed_command_list([long_a, long_b])[1:] if ln.strip()][0]
    assert "…（全长 " in line, f"超长参数应标全长: {line}"
    # 性质断言：预览短于原文（不刷屏），但保留足够可辨识的前缀（>= 60 字符）
    assert line.count("a") < 200, f"超长参数应被截断: {line}"
    assert line.count("b") < 200, f"超长参数应被截断: {line}"
    assert "-rf /tmp/" + "a" * 60 in line, f"截断后仍应保留可辨识前缀: {line}"
    assert "-rf /tmp/" + "b" * 60 in line, f"截断后仍应保留可辨识前缀: {line}"

    many = [f"rm -rf /tmp/p{i}" for i in range(6)]
    line_many = [ln for ln in scope_mod.format_parsed_command_list(many)[1:] if ln.strip()][0]
    assert "等 6 种" in line_many, f"超过 3 项应标总数: {line_many}"
    assert line_many.count(" | ") == 2, "一行最多列 3 项参数摘要"


@pytest.mark.asyncio
async def test_overlong_command_card_keeps_parameter_visibility() -> None:
    """超长命令（>500 字符，命令块被截断）时归并行仍可辨识参数（G1 核心场景）。"""
    cap = _wire(["approved_once"])
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})

    # 单条复合命令总长 > 1500 → `命令:` 块在 500 字符处截断，末段参数完全不可见
    seg_a = "rm -rf /tmp/" + "a" * 400
    seg_b = "rm -rf /tmp/" + "b" * 400
    seg_c = "rm -rf /tmp/" + "c" * 400
    await plugin.execute(_ctx(command=f"{seg_a} && {seg_b} && {seg_c}"))

    description = cap.cards[0]["description"]
    assert "... (命令过长，已截断)" in description, "前置条件：命令块确实被截断"
    assert seg_c not in description, "前置条件：末段完整参数不在卡面命令块中"

    parsed = _parsed_command_lines(description)
    assert len(parsed) == 1, f"同命令应归并成一条，实际 {parsed}"
    # 结论：命令块被截断时归并行仍带可辨识参数（不出现「任何位置都看不到参数」）
    assert "c" * 60 in parsed[0], f"末段参数摘要应可见: {parsed[0]}"
    assert "…（全长 " in parsed[0], f"超长参数应标全长: {parsed[0]}"


@pytest.mark.asyncio
async def test_overlong_command_fingerprints_still_per_segment() -> None:
    """超长命令的参数摘要只影响展示：授权面仍逐段精确指纹（回归锁）。"""
    cap = _wire(["approved_remember", "approved_once"])
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})

    long_a = "rm -rf /tmp/" + "a" * 300
    long_b = "rm -rf /tmp/" + "b" * 300
    first = await plugin.execute(_ctx(commands=[long_a, long_b]))
    remembered = json.loads(first.state_updates[_mod.APPROVED_SIGNATURES_KEY])
    assert len(remembered) == 2, "两条参数不同的命令各一枚指纹（授权面不随展示归并）"
    assert len(_parsed_command_lines(cap.cards[0]["description"])) == 1, "卡面仍只一条"

    seed = {_mod.APPROVED_SIGNATURES_KEY: first.state_updates[_mod.APPROVED_SIGNATURES_KEY]}
    long_c = "rm -rf /tmp/" + "c" * 300
    await plugin.execute(_ctx(command=long_c, state_extra=seed))
    assert len(cap.cards) == 2, "同命令的新参数版本仍须审批（fail-closed）"


@pytest.mark.asyncio
async def test_display_merge_does_not_merge_fingerprints() -> None:
    """归并只作用于卡面列表：指纹仍逐条精确，归并不放宽审批（fail-closed）。"""
    cap = _wire(["approved_remember", "approved_once"])
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})

    first = await plugin.execute(_ctx(commands=["rm -rf /tmp/a", "rm -rf /tmp/b"]))
    remembered = json.loads(first.state_updates[_mod.APPROVED_SIGNATURES_KEY])
    assert len(remembered) == 2, "两条参数不同的命令各有一个指纹，归并不合并授权面"
    assert len(_parsed_command_lines(cap.cards[0]["description"])) == 1, "卡面仍只一条"

    seed = {_mod.APPROVED_SIGNATURES_KEY: first.state_updates[_mod.APPROVED_SIGNATURES_KEY]}
    # 归并展示后，第三个参数版本仍须弹卡（未被归并成同一授权条目）
    result = await plugin.execute(_ctx(command="rm -rf /tmp/c", state_extra=seed))
    assert len(cap.cards) == 2, "同命令的参数新组合仍须审批"
    assert not _blocked(result)


@pytest.mark.asyncio
async def test_remember_grants_all_parsed_commands() -> None:
    """「同命令免批」一次记住复合命令解析出的每条子命令。"""
    cap = _wire(["approved_remember"])
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})

    first = await plugin.execute(_ctx(command="rm -rf /tmp/a && rm -rf /tmp/b"))
    remembered = json.loads(first.state_updates[_mod.APPROVED_SIGNATURES_KEY])
    assert len(remembered) == 2, "复合命令应解析出两条子命令指纹"

    # 子命令单独出现：均已记忆，不再弹卡（state 跨轮次由引擎合并）
    seed = {_mod.APPROVED_SIGNATURES_KEY: first.state_updates[_mod.APPROVED_SIGNATURES_KEY]}
    assert not _blocked(await plugin.execute(_ctx(command="rm -rf /tmp/a", state_extra=seed)))
    assert not _blocked(await plugin.execute(_ctx(command="rm -rf /tmp/b", state_extra=seed)))
    assert len(cap.cards) == 1


@pytest.mark.asyncio
async def test_permanent_whitelist_covers_all_parsed_commands() -> None:
    """永久白名单一次覆盖复合命令的每条子命令（跨管道跨会话可单独复用）。"""
    cap = _wire(["approved_permanent_whitelist"])
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})
    await plugin.execute(_ctx(command="rm -rf /tmp/a && rm -rf /tmp/b"))
    assert len(_mod._PERMANENT_WHITELIST) == 2

    fresh = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})
    assert not _blocked(await fresh.execute(_ctx(pipeline_id="p2", session_id="s2", command="rm -rf /tmp/a")))
    assert not _blocked(await fresh.execute(_ctx(pipeline_id="p2", session_id="s2", command="rm -rf /tmp/b")))
    assert len(cap.cards) == 1


@pytest.mark.asyncio
async def test_batch_calls_all_granted() -> None:
    """同批多条命令：一次授权覆盖整批（卡上全部命令都进授予面）。"""
    cap = _wire(["approved_remember"])
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})

    first = await plugin.execute(_ctx(commands=["rm -rf /tmp/a", "rm -rf /tmp/b"]))
    remembered = json.loads(first.state_updates[_mod.APPROVED_SIGNATURES_KEY])
    assert len(remembered) == 2, "同批两条命令都应被授予免批"

    seed = {_mod.APPROVED_SIGNATURES_KEY: first.state_updates[_mod.APPROVED_SIGNATURES_KEY]}
    assert not _blocked(await plugin.execute(_ctx(command="rm -rf /tmp/b", state_extra=seed)))
    assert len(cap.cards) == 1


@pytest.mark.asyncio
async def test_partial_remembered_still_requires_approval() -> None:
    """部分子命令已记忆 ≠ 整条放行：未覆盖的子命令仍需审批（fail-closed）。"""
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})
    sig_a = plugin._make_signature("bash", {"command": "rm -rf /tmp/a"})
    assert sig_a
    cap = _wire(["approved_once"])

    result = await plugin.execute(
        _ctx(
            command="rm -rf /tmp/a && rm -rf /tmp/b",
            state_extra={_mod.APPROVED_SIGNATURES_KEY: json.dumps([sig_a])},
        )
    )
    assert len(cap.cards) == 1, "未覆盖的 /tmp/b 必须打断并审批"
    assert not _blocked(result)


@pytest.mark.asyncio
async def test_free_text_feedback_returned_in_tool_result() -> None:
    """审批卡下方自由文本随 feedback 进入工具返回值（不再只剩笼统拒绝）。"""
    cap = _wire(
        [
            {
                "response_type": "answered",
                "selected_option": "",
                "selected_semantics": "",
                "feedback": "不要删这个目录，改删 /tmp/safe",
            }
        ]
    )
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})

    result = await plugin.execute(_ctx(command="rm -rf /tmp/x"))

    entries = result.state_updates.get("pre_decided_results") or []
    assert entries, "自由文本应答按拒绝软拦截"
    error = entries[0]["error"]
    assert "用户拒绝执行" in error
    assert "用户反馈: 不要删这个目录，改删 /tmp/safe" in error
    assert len(cap.cards) == 1
