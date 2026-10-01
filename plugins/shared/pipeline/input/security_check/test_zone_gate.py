# @feature: FP-0.2.〇 管道引擎与插件执行模型（位置闸管道层执法） | @ci: python-coverage
"""位置闸测试（ADR 2026-09-24-read-deny-write-zones 修订：位置轴管道层统一执法）。

锁定契约（security_check._enforce_zone_policy，判定单源 zone_policy）：
1. 写区外 → 弹授权卡（两选项：仅本管道/永久），批准即放行且授权落盘；
2. 「仅本管道」→ state 键 task.authorized_write_zones 更新 + 当前调用参数
   就地补授权（param_inject 注入先于本阶段，工具层兜底靠它看到本次授权）；
3. 「永久」→ add_write_zone 落名单文件（env 钉 tmp）；
4. 拒绝/超时/未知选项 → 软拦截，不产生任何授权；
5. 仓库自保目录（config/ 等）对写直接拦不弹卡；
6. 读链（用户裁定 2026-09-28）：锚内读豁免（工作区/项目根不查黑名单）；
   锚外黑名单命中弹读授权卡（state 键 task.authorized_read_zones /
   名单 read_allow 节），普通路径放行不弹卡；
7. 根锚内（workspace/project_root）读写均不弹卡；
8. 旁路档（隔离未显式选档）不豁免位置闸——_do_work 层序在模式分流之前；
9. 未声明在 path_param_operations 里的工具不经位置闸；
10. 连续拒绝熔断署名 router.stop_reason=tool_fail_loop（终态落 failed）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_THIS_DIR = str(Path(__file__).resolve().parent)
_SHARED_DIR = str(Path(__file__).resolve().parents[3])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

import pytest  # noqa: E402
import repo_anchor  # noqa: E402
import yaml  # noqa: E402


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location("security_check_plugin_zone_gate", str(Path(_THIS_DIR) / "plugin.py"))
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_plugin_module()
SecurityCheckPlugin = _mod.SecurityCheckPlugin
zone_policy = _mod.zone_policy

pytestmark = pytest.mark.unit


class FakeHICap:
    """human-interaction 假 capability：卡片批指定选项或超时。"""

    def __init__(self, selected: str | None, fail: bool = False) -> None:
        self.selected = selected
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _declared_semantics(self) -> str:
        """按卡上声明回显语义（对齐 human 归一点：选中项声明什么语义就回什么）。"""
        for method, params in reversed(self.calls):
            if method == "create_choice":
                for opt in params.get("options", []):
                    if isinstance(opt, dict) and opt.get("id") == self.selected:
                        return str(opt.get("semantics", ""))
                break
        return "cancel"  # 未命中任何选项 = 未知词形（human 归一点按取消回语义）

    async def call(self, method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        self.calls.append((method, params))
        if method == "create_choice":
            if self.fail:
                return {"error": "boom"}
            assert [o["id"] for o in params["options"]] == ["pipeline", "permanent"]
            assert all(o.get("semantics") for o in params["options"]), "授权卡必须声明语义"
            return {"request_id": "req-1"}
        if method == "wait_for_choice":
            if self.selected is None:
                return {"error": "timeout", "error_code": "INTERACTION_TIMEOUT"}
            return {
                "selected_option": self.selected,
                "selected_semantics": self._declared_semantics(),
            }
        raise AssertionError(f"unexpected method {method}")


@pytest.fixture
def zone_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Path]]:
    """假仓库锚 + tmp 名单 + workspace/project_root 布局。"""
    repo = tmp_path / "repo"
    (repo / "config" / "kernel").mkdir(parents=True)
    (repo / "config" / "llm.yaml").write_text("api_key: x\n", encoding="utf-8")
    ws = tmp_path / "sessions" / "s1"
    ws.mkdir(parents=True)
    users_dir = tmp_path / "config" / "users"
    (users_dir / "default").mkdir(parents=True)
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(repo / "config"))
    monkeypatch.setenv("AGENTOS_CONFIG_USERS_DIR", str(users_dir))
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "usroot"))
    repo_anchor.reset_cache()
    yield {"repo": repo, "ws": ws, "users_dir": users_dir, "tmp": tmp_path}
    repo_anchor.reset_cache()


def _ctx(
    state_overrides: dict[str, Any],
    cap: FakeHICap | None,
) -> tuple[Any, FakeHICap | None]:
    def _get_service(name: str) -> Any:
        raise KeyError(name)

    state = {
        "core_type": "tool_execute",
        "raw_tool_calls": [],
        "messages": [],
        "session_id": "sess-zone",
        "pipeline_id": "pip-zone",
        "user_id": "u-1",
        **state_overrides,
    }
    ctx = types.SimpleNamespace(state=state, get_service=_get_service)
    _mod.set_human_interaction_cap(cap)
    return ctx, cap


def _plugin() -> Any:
    return SecurityCheckPlugin({})


def _tool_calls(name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"name": name, "args": args, "id": "call-1"}]


# ── 写区外 → 授权卡 ──────────────────────────────────────────────


async def test_write_outside_zone_pipeline_grant_allows_and_updates_state(
    zone_env: dict[str, Path],
) -> None:
    """「仅本管道」：state 键更新 + 当前调用参数就地补授权 + 放行。"""
    outsider = zone_env["tmp"] / "granted_dir"
    outsider.mkdir()
    cap = FakeHICap(selected="pipeline")
    ctx, _ = _ctx(
        {
            "raw_tool_calls": _tool_calls("file_write", {"path": str(outsider / "a.txt")}),
            "workspace": str(zone_env["ws"]),
            "project_root": "",
        },
        cap,
    )
    updates: dict[str, Any] = {}

    blocked = await _plugin()._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], updates)

    assert blocked is None
    assert updates[zone_policy.STATE_KEY] == json.dumps([str(outsider)])
    assert ctx.state[zone_policy.STATE_KEY] == json.dumps([str(outsider)])
    # 当前调用参数就地补授权（工具层兜底校验可见）
    args = ctx.state["raw_tool_calls"][0]["args"]
    assert json.loads(args["authorized_zones"]) == [str(outsider)]


async def test_write_outside_zone_permanent_grant_writes_whitelist(
    zone_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """「永久」：add_write_zone 落名单文件（真值 = env 钉的 tmp 用户空间）。"""
    import project_registry as pr

    monkeypatch.setattr(pr, "legacy_config_users_base", lambda: zone_env["tmp"] / "no_legacy")
    outsider = zone_env["tmp"] / "perm_dir"
    outsider.mkdir()
    cap = FakeHICap(selected="permanent")
    ctx, _ = _ctx(
        {
            "raw_tool_calls": _tool_calls("file_write", {"path": str(outsider / "b.txt")}),
            "workspace": str(zone_env["ws"]),
        },
        cap,
    )
    updates: dict[str, Any] = {}

    blocked = await _plugin()._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], updates)

    assert blocked is None
    assert updates == {}
    wl = zone_env["users_dir"] / "default" / "project_whitelist.yaml"
    assert wl.is_file()
    assert yaml.safe_load(wl.read_text(encoding="utf-8"))["entries"] == [str(outsider)]


async def test_write_outside_zone_denied_soft_blocks_without_grant(
    zone_env: dict[str, Path],
) -> None:
    """超时/拒绝：软拦截，state 与名单均无授权痕迹。"""
    outsider = zone_env["tmp"] / "denied_dir"
    outsider.mkdir()
    cap = FakeHICap(selected=None)
    ctx, _ = _ctx(
        {
            "raw_tool_calls": _tool_calls("file_write", {"path": str(outsider / "c.txt")}),
            "workspace": str(zone_env["ws"]),
        },
        cap,
    )
    updates: dict[str, Any] = {}

    blocked = await _plugin()._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], updates)

    assert blocked is not None
    assert blocked["pre_decided_results"], "软拦截=预定结果"
    assert zone_policy.STATE_KEY not in updates
    assert zone_policy.STATE_KEY not in ctx.state
    assert not (zone_env["users_dir"] / "default" / "project_whitelist.yaml").exists()


async def test_write_outside_zone_unknown_option_soft_blocks(
    zone_env: dict[str, Path],
) -> None:
    """未知选项（契约漂移）：按拒绝处理，不产生授权。"""
    outsider = zone_env["tmp"] / "unknown_dir"
    outsider.mkdir()
    ctx, _ = _ctx(
        {
            "raw_tool_calls": _tool_calls("file_write", {"path": str(outsider / "d.txt")}),
            "workspace": str(zone_env["ws"]),
        },
        FakeHICap(selected="whatever"),
    )
    updates: dict[str, Any] = {}

    blocked = await _plugin()._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], updates)

    assert blocked is not None
    assert zone_policy.STATE_KEY not in updates


async def test_zone_write_entries_zone_passes_without_card(
    zone_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """名单 entries 覆盖的路径：放行且不弹卡。"""
    entry = zone_env["tmp"] / "listed"
    entry.mkdir()
    users_dir = zone_env["users_dir"]
    (users_dir / "default" / "project_whitelist.yaml").write_text(
        yaml.safe_dump({"entries": [str(entry)]}), encoding="utf-8"
    )
    ctx, cap = _ctx(
        {
            "raw_tool_calls": _tool_calls("file_write", {"path": str(entry / "e.txt")}),
            "workspace": str(zone_env["ws"]),
        },
        FakeHICap(selected="pipeline"),
    )

    blocked = await _plugin()._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], {})

    assert blocked is None
    assert cap is not None
    assert cap.calls == []  # 名单内不弹卡


# ── 仓库自保 / 读链 / 根锚 ────────────────────────────────────────


async def test_repo_denied_dir_write_blocked_without_card(
    zone_env: dict[str, Path],
) -> None:
    """仓库自保目录（config/）对写恒拒：直接拦，不弹卡（系统自保不可授权）。"""
    target = zone_env["repo"] / "config" / "evil.yaml"
    ctx, cap = _ctx(
        {
            "raw_tool_calls": _tool_calls("file_write", {"path": str(target)}),
            "workspace": str(zone_env["ws"]),
        },
        FakeHICap(selected="pipeline"),
    )

    blocked = await _plugin()._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], {})

    assert blocked is not None
    assert "运行时/产物区" in blocked["pre_decided_results"][0]["error"]
    assert cap is not None
    assert cap.calls == []


async def test_read_deny_prefix_rejected(zone_env: dict[str, Path]) -> None:
    """read_deny 命中：弹读授权卡（用户裁定 2026-09-28），拒绝/超时 → 软拦截。"""
    denied = zone_env["tmp"] / "private"
    denied.mkdir()
    (denied / "diary.txt").write_text("x\n", encoding="utf-8")
    users_dir = zone_env["users_dir"]
    (users_dir / "default" / "project_whitelist.yaml").write_text(
        yaml.safe_dump({"read_deny": [str(denied)]}), encoding="utf-8"
    )
    ctx, cap = _ctx(
        {
            "raw_tool_calls": _tool_calls("file_read", {"path": str(denied / "diary.txt")}),
            "workspace": str(zone_env["ws"]),
        },
        FakeHICap(selected=None),
    )

    blocked = await _plugin()._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], {})

    assert blocked is not None
    assert "读黑名单" in blocked["pre_decided_results"][0]["error"]
    assert cap is not None
    assert cap.calls, "read_deny 命中应弹读授权卡交用户裁定，不再机器硬拒"


# ── 读面（用户裁定 2026-09-28）：黑名单命中 → 读授权卡 ────────────


async def test_read_blacklist_pipeline_grant_allows_and_updates_state(
    zone_env: dict[str, Path],
) -> None:
    """仓库拒绝集命中（config/）：批准「仅本管道」→ state 键 + args 就地补 + 放行。"""
    target = zone_env["repo"] / "config" / "llm.yaml"
    cap = FakeHICap(selected="pipeline")
    ctx, _ = _ctx(
        {
            "raw_tool_calls": _tool_calls("file_read", {"path": str(target)}),
            "workspace": str(zone_env["ws"]),
        },
        cap,
    )
    updates: dict[str, Any] = {}

    blocked = await _plugin()._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], updates)

    assert blocked is None
    grant_dir = str(zone_env["repo"] / "config")
    assert updates[zone_policy.STATE_KEY_READ] == json.dumps([grant_dir])
    assert ctx.state[zone_policy.STATE_KEY_READ] == json.dumps([grant_dir])
    args = ctx.state["raw_tool_calls"][0]["args"]
    assert json.loads(args["authorized_read_zones"]) == [grant_dir]
    assert cap.calls[0][1]["title"] == f"授权读取 {grant_dir}"


async def test_read_blacklist_permanent_grant_writes_read_allow(
    zone_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """「永久」：add_read_allow 落名单文件 read_allow 节（真值 = env 钉 tmp）。"""
    import project_registry as pr

    monkeypatch.setattr(pr, "legacy_config_users_base", lambda: zone_env["tmp"] / "no_legacy")
    target = zone_env["repo"] / "config" / "llm.yaml"
    ctx, _ = _ctx(
        {
            "raw_tool_calls": _tool_calls("file_read", {"path": str(target)}),
            "workspace": str(zone_env["ws"]),
        },
        FakeHICap(selected="permanent"),
    )

    blocked = await _plugin()._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], {})

    assert blocked is None
    wl = zone_env["users_dir"] / "default" / "project_whitelist.yaml"
    assert wl.is_file()
    data = yaml.safe_load(wl.read_text(encoding="utf-8"))
    assert data["read_allow"] == [str(zone_env["repo"] / "config")]


async def test_read_allow_entry_passes_without_card(zone_env: dict[str, Path]) -> None:
    """read_allow 名单命中：放行不弹卡（授权前缀先行于黑名单链）。"""
    cfg = zone_env["repo"] / "config"
    (zone_env["users_dir"] / "default" / "project_whitelist.yaml").write_text(
        yaml.safe_dump({"read_allow": [str(cfg)]}), encoding="utf-8"
    )
    ctx, cap = _ctx(
        {
            "raw_tool_calls": _tool_calls("file_read", {"path": str(cfg / "llm.yaml")}),
            "workspace": str(zone_env["ws"]),
        },
        FakeHICap(selected="pipeline"),
    )

    blocked = await _plugin()._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], {})

    assert blocked is None
    assert cap is not None
    assert cap.calls == []


async def test_root_anchored_read_inside_repo_denied_dir_passes(
    zone_env: dict[str, Path],
) -> None:
    """事故 2026-09-28 回归：workspace 落在仓库 .ai_workspaces 下，列自己的
    工作区（path="."）放行——锚内读不套仓库拒绝集，与写链锚内先行同序。"""
    aiws = zone_env["repo"] / ".ai_workspaces" / "sessions" / "s1"
    aiws.mkdir(parents=True)
    ctx, cap = _ctx(
        {
            "raw_tool_calls": _tool_calls("list_directory", {"path": "."}),
            "workspace": str(aiws),
        },
        FakeHICap(selected="pipeline"),
    )

    blocked = await _plugin()._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], {})

    assert blocked is None
    assert cap is not None
    assert cap.calls == []


async def test_reject_loop_signs_stop_reason(zone_env: dict[str, Path]) -> None:
    """连续拒绝熔断署名（控制状态键契约 ADR 2026-08-30）：第 3 次软拦截带
    ended + router.stop_reason=tool_fail_loop（内核终态映射落 failed，不再
    折算 completed）。"""
    target = zone_env["repo"] / "config" / "llm.yaml"
    plugin = _plugin()  # 同实例累积签名计数（单管道作用域）
    results = []
    for _ in range(3):
        ctx, _ = _ctx(
            {
                "raw_tool_calls": _tool_calls("file_read", {"path": str(target)}),
                "workspace": str(zone_env["ws"]),
            },
            None,  # 交互服务缺席 → 软拦截（不弹卡）
        )
        results.append(await plugin._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], {}))

    assert all(r is not None for r in results)
    assert "ended" not in results[0]
    assert "ended" not in results[1]
    assert results[2].get("ended") is True
    assert results[2].get("router.stop_reason") == "tool_fail_loop"
    assert "疑似死循环" in str(results[2].get("raw_error", ""))


async def test_read_outside_zone_passes_without_card(
    zone_env: dict[str, Path],
) -> None:
    """读黑名单制：名单外普通文件读放行，不弹卡。"""
    outsider = zone_env["tmp"] / "readable"
    outsider.mkdir()
    (outsider / "s.txt").write_text("x\n", encoding="utf-8")
    ctx, cap = _ctx(
        {
            "raw_tool_calls": _tool_calls("file_read", {"path": str(outsider / "s.txt")}),
            "workspace": str(zone_env["ws"]),
        },
        FakeHICap(selected="pipeline"),
    )

    blocked = await _plugin()._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], {})

    assert blocked is None
    assert cap is not None
    assert cap.calls == []


async def test_root_anchored_write_passes_without_card(
    zone_env: dict[str, Path],
) -> None:
    """根锚内写（任务工作区）：放行不弹卡（锚内不套自保拒绝）。"""
    ctx, cap = _ctx(
        {
            "raw_tool_calls": _tool_calls("file_write", {"path": "out.md"}),
            "workspace": str(zone_env["ws"]),
        },
        FakeHICap(selected="pipeline"),
    )

    blocked = await _plugin()._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], {})

    assert blocked is None
    assert cap is not None
    assert cap.calls == []


async def test_undeclared_tool_not_gated(zone_env: dict[str, Path]) -> None:
    """未声明在操作表里的工具：不经位置闸（bash 命令串边界，ADR 修订节）。"""
    outsider = zone_env["tmp"] / "bash_target"
    outsider.mkdir()
    ctx, cap = _ctx(
        {
            "raw_tool_calls": _tool_calls("bash_execute", {"command": f"touch {outsider}/x"}),
            "workspace": str(zone_env["ws"]),
        },
        FakeHICap(selected="pipeline"),
    )

    blocked = await _plugin()._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], {})

    assert blocked is None
    assert cap is not None
    assert cap.calls == []


# ── 层序：位置闸先于模式分流（旁路档不豁免） ──────────────────────


async def test_zone_gate_precedes_bypass_short_circuit(
    zone_env: dict[str, Path],
) -> None:
    """隔离会话免审批默认（旁路档）不豁免位置闸：区外写仍弹卡。"""
    outsider = zone_env["tmp"] / "bypass_dir"
    outsider.mkdir()
    cap = FakeHICap(selected="pipeline")
    ctx, _ = _ctx(
        {
            "raw_tool_calls": _tool_calls("file_write", {"path": str(outsider / "f.txt")}),
            "workspace": str(zone_env["ws"]),
            "execution_contexts": [{"provider": "docker", "level": "isolated", "task_isolated": True}],
        },
        cap,
    )

    result = await _plugin()._do_work(ctx)

    assert cap.calls, "旁路档必须先过位置闸（区外写仍弹卡）"
    assert result[zone_policy.STATE_KEY] == json.dumps([str(outsider)])
    # 隔离会话生效档=旁路：位置闸放行后仍走旁路免审批（授权已完成落盘）
    assert result.get("security.decision") is None, "旁路放行零决策产出"


async def test_no_interaction_cap_fails_closed(zone_env: dict[str, Path]) -> None:
    """交互服务不可用：区外写 fail-closed（软拦截），不静默放行。"""
    outsider = zone_env["tmp"] / "nocap_dir"
    outsider.mkdir()
    ctx, _ = _ctx(
        {
            "raw_tool_calls": _tool_calls("file_write", {"path": str(outsider / "g.txt")}),
            "workspace": str(zone_env["ws"]),
        },
        None,
    )
    updates: dict[str, Any] = {}

    blocked = await _plugin()._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], updates)

    assert blocked is not None
    assert "交互服务不可用" in blocked["pre_decided_results"][0]["error"]
    assert zone_policy.STATE_KEY not in updates


# ── WSL 挂载混入形态归一 + sessions 子树豁免 + 批准别名 ──


def test_normalize_windows_mixed_path_parametrized() -> None:
    """污染形态 → Windows 形态；正常路径原样（POSIX 形态仅 nt 重写）。"""
    f = zone_policy.normalize_windows_mixed_path
    assert f(r"D:\mnt\d\repo\docs\a.md") == r"D:\repo\docs\a.md"
    assert f("D:/mnt/e/work/x.txt") == r"E:\work\x.txt"
    assert f(r"\mnt\z\root.md") == r"Z:\root.md"
    if os.name == "nt":
        assert f("/mnt/d/repo/f.txt") == r"D:\repo\f.txt"
    else:
        assert f("/mnt/d/repo/f.txt") == "/mnt/d/repo/f.txt"
    # 非命中原样：普通 Windows 路径 / /workspace 约定 / 相对路径 / 空串
    assert f(r"D:\repo\f.txt") == r"D:\repo\f.txt"
    assert f("/workspace/sub/f.txt") == "/workspace/sub/f.txt"
    assert f("relative/f.txt") == "relative/f.txt"
    assert f("") == ""


async def test_mixed_mount_write_within_workspace_no_card(
    zone_env: dict[str, Path],
) -> None:
    r"""脏形态 `T:\mnt\t\...` 归一后落在根锚内 → 放行且不弹卡。"""
    tmp = zone_env["tmp"]
    drive = tmp.drive[0]
    target = tmp / "sessions" / "s1" / "docs" / "out.md"
    dirty = rf"{tmp.drive}\mnt\{drive}\{str(target)[3:]}"
    cap = FakeHICap(selected="pipeline")
    ctx, _ = _ctx(
        {
            "raw_tool_calls": _tool_calls("file_write", {"path": dirty}),
            "workspace": str(zone_env["ws"]),
            "project_root": "",
        },
        cap,
    )
    updates: dict[str, Any] = {}

    blocked = await _plugin()._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], updates)

    assert blocked is None
    assert cap.calls == []  # 锚内不弹卡


async def test_repo_sessions_tree_write_grantable_not_hard_rejected(
    zone_env: dict[str, Path],
) -> None:
    """`.ai_workspaces/sessions/` 会话项目树：不再自保硬拒，落入写区链弹卡可授权。"""
    target = (
        zone_env["repo"] / ".ai_workspaces" / "sessions" / "thread-1" / "projects" / "proj" / "docs"
    )
    cap = FakeHICap(selected="pipeline")
    ctx, _ = _ctx(
        {
            "raw_tool_calls": _tool_calls("file_write", {"path": str(target / "a.md")}),
            "workspace": str(zone_env["ws"]),
            "project_root": "",
        },
        cap,
    )
    updates: dict[str, Any] = {}

    blocked = await _plugin()._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], updates)

    assert blocked is None
    assert cap.calls[0][0] == "create_choice"


async def test_repo_task_runtime_dir_write_still_hard_rejected(
    zone_env: dict[str, Path],
) -> None:
    """任务运行时面 `.ai_workspaces/<task_id>/` 维持自保恒拒（不弹卡）。"""
    target = zone_env["repo"] / ".ai_workspaces" / "abc123task" / "out.txt"
    cap = FakeHICap(selected="pipeline")
    ctx, _ = _ctx(
        {
            "raw_tool_calls": _tool_calls("file_write", {"path": str(target)}),
            "workspace": str(zone_env["ws"]),
            "project_root": "",
        },
        cap,
    )
    updates: dict[str, Any] = {}

    blocked = await _plugin()._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], updates)

    assert blocked is not None
    assert "运行时/产物区" in blocked["pre_decided_results"][0]["error"]
    assert cap.calls == []  # 硬拒不弹卡


async def test_grant_semantics_axis_mismatch_fails_closed(
    zone_env: dict[str, Path],
) -> None:
    """跨轴语义违约 fail-closed（ADR 2026-10-01 决策 2）：读卡收到 grant_write
    语义按拒绝结算，不落授权。approve/approved 等历史别名在 human 归一点收敛，
    消费端只读语义——别名回归用例随别名表退役（归一行为由 human 契约测试覆盖）。"""
    denied = zone_env["repo"] / "config"

    class _CrossAxisCap(FakeHICap):
        """读卡但回写轴语义（模拟契约违约通道）。"""

        def _declared_semantics(self) -> str:
            return "grant_write"

    cross = _CrossAxisCap(selected="pipeline")
    ctx, _ = _ctx(
        {
            "raw_tool_calls": _tool_calls("file_read", {"path": str(denied / "llm.yaml")}),
            "workspace": str(zone_env["ws"]),
        },
        cross,
    )
    updates: dict[str, Any] = {}

    blocked = await _plugin()._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], updates)

    assert blocked is not None, "跨轴语义必须按拒绝软拦截"
    assert zone_policy.STATE_KEY_READ not in updates
    assert zone_policy.STATE_KEY not in updates


# ── 用户根形态地界（ADR 2026-10-01-workspace-root-user-root-anchor）──────────


class TestUserRootWorkspacesGate:
    """装机形态工作空间根迁 ``<user_root>/workspaces`` 后的位置闸回归。

    判定单源 zone_policy 地界识别含 ``workspaces`` 用户根锚定，位置闸零改动
    自动继承——
    1. 自己任务工作区内写放行不弹卡（根锚内先行）；
    2. 其他任务工作树写仍弹授权卡（跨任务写污染防线）；
    3. 同任务兄弟树读经内建地界放行不弹卡（读授权链携锚派生）。
    """

    @pytest.fixture
    def user_workspaces(self, zone_env: dict[str, Path]) -> Path:
        """用户根下 workspaces 树（zone_env 已钉 AGENTOS_USER_ROOT）。"""
        ur = zone_env["tmp"] / "usroot" / "workspaces"
        ur.mkdir(parents=True)
        return ur

    async def test_write_in_own_task_workspace_no_card(
        self, zone_env: dict[str, Path], user_workspaces: Path
    ) -> None:
        """自己任务工作区内 file_write：根锚内放行，零卡片（事故回归）。"""
        ws = user_workspaces / "01ed89f3952e"
        ws.mkdir()
        cap = FakeHICap(selected=None)  # 若误走卡片路径 → 超时软拦截
        ctx, _ = _ctx(
            {
                "raw_tool_calls": _tool_calls(
                    "file_write", {"path": str(ws / "tests" / "a.txt")}
                ),
                "workspace": str(ws),
                "project_root": "",
            },
            cap,
        )
        updates: dict[str, Any] = {}

        blocked = await _plugin()._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], updates)

        assert blocked is None
        assert cap.calls == [], "锚内写不得弹任何卡片"
        assert zone_policy.STATE_KEY not in updates

    async def test_write_other_task_tree_still_cards(
        self, zone_env: dict[str, Path], user_workspaces: Path
    ) -> None:
        """其他任务工作树写（地界内但锚外）：仍弹授权卡待批。"""
        own = user_workspaces / "taskA"
        own.mkdir()
        other = user_workspaces / "taskB"
        other.mkdir()
        cap = FakeHICap(selected=None)
        ctx, _ = _ctx(
            {
                "raw_tool_calls": _tool_calls("file_write", {"path": str(other / "x.txt")}),
                "workspace": str(own),
            },
            cap,
        )
        updates: dict[str, Any] = {}

        blocked = await _plugin()._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], updates)

        assert blocked is not None, "跨任务写必须弹卡（跨会话写污染防线）"
        assert blocked["pre_decided_results"]
        assert zone_policy.STATE_KEY not in updates

    async def test_read_sibling_tree_in_own_task_no_card(
        self, zone_env: dict[str, Path], user_workspaces: Path
    ) -> None:
        """同任务兄弟树读（锚外地界内）：内建地界放行，零卡片。"""
        ws = user_workspaces / "taskA" / "sessions" / "thread-1"
        sibling = user_workspaces / "taskA" / "docs"
        sibling.mkdir(parents=True)
        ws.mkdir(parents=True)
        (sibling / "note.md").write_text("sibling doc", encoding="utf-8")
        cap = FakeHICap(selected=None)
        ctx, _ = _ctx(
            {
                "raw_tool_calls": _tool_calls(
                    "file_read", {"path": str(sibling / "note.md")}
                ),
                "workspace": str(ws),
                "project_root": str(ws),
            },
            cap,
        )
        updates: dict[str, Any] = {}

        blocked = await _plugin()._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], updates)

        assert blocked is None
        assert cap.calls == [], "地界内读不得弹读授权卡"
        assert zone_policy.STATE_KEY_READ not in updates
