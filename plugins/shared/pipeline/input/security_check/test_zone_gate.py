# @feature: FP-0.2.〇 管道引擎与插件执行模型（位置闸管道层执法） | @ci: python-coverage
"""位置闸测试（ADR 2026-09-24-read-deny-write-zones 修订：位置轴管道层统一执法）。

锁定契约（security_check._enforce_zone_policy，判定单源 zone_policy）：
1. 写区外 → 弹授权卡（两选项：仅本管道/永久），批准即放行且授权落盘；
2. 「仅本管道」→ state 键 task.authorized_write_zones 更新 + 当前调用参数
   就地补授权（param_inject 注入先于本阶段，工具层兜底靠它看到本次授权）；
3. 「永久」→ add_write_zone 落名单文件（env 钉 tmp）；
4. 拒绝/超时/未知选项 → 软拦截，不产生任何授权；
5. 仓库自保目录（config/ 等）→ 直接拦，不弹卡；
6. 读黑名单链：read_deny 命中拒绝，普通路径放行不弹卡；
7. 根锚内（workspace/project_root）读写均不弹卡；
8. 旁路档（隔离未显式选档）不豁免位置闸——_do_work 层序在模式分流之前；
9. 未声明在 path_param_operations 里的工具不经位置闸。
"""

from __future__ import annotations

import importlib.util
import json
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
    spec = importlib.util.spec_from_file_location(
        "security_check_plugin_zone_gate", str(Path(_THIS_DIR) / "plugin.py")
    )
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

    async def call(self, method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        self.calls.append((method, params))
        if method == "create_choice":
            if self.fail:
                return {"error": "boom"}
            assert [o["id"] for o in params["options"]] == ["pipeline", "permanent"]
            return {"request_id": "req-1"}
        if method == "wait_for_choice":
            if self.selected is None:
                return {"error": "timeout", "error_code": "INTERACTION_TIMEOUT"}
            return {"selected_option": self.selected}
        raise AssertionError(f"unexpected method {method}")


@pytest.fixture
def zone_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Path]]:
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

    blocked = await _plugin()._enforce_zone_policy(
        ctx, ctx.state["raw_tool_calls"], updates
    )

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
    assert blocked["security.decision"]["reason"].startswith("soft_block")
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

    blocked = await _plugin()._enforce_zone_policy(
        ctx, ctx.state["raw_tool_calls"], {}
    )

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
    assert "运行时/产物区" in blocked["tool_results"][0]["error"]
    assert cap is not None
    assert cap.calls == []


async def test_read_deny_prefix_rejected(zone_env: dict[str, Path]) -> None:
    """read_deny 命中：读拒绝（软拦截），不弹卡。"""
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
        FakeHICap(selected="pipeline"),
    )

    blocked = await _plugin()._enforce_zone_policy(ctx, ctx.state["raw_tool_calls"], {})

    assert blocked is not None
    assert "read_deny" in blocked["tool_results"][0]["error"]
    assert cap is not None
    assert cap.calls == []


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
            "execution_contexts": [
                {"provider": "docker", "level": "isolated", "task_isolated": True}
            ],
        },
        cap,
    )

    result = await _plugin()._do_work(ctx)

    assert cap.calls, "旁路档必须先过位置闸（区外写仍弹卡）"
    assert result[zone_policy.STATE_KEY] == json.dumps([str(outsider)])
    # 隔离会话生效档=旁路：位置闸放行后仍走旁路免审批（授权已完成落盘）
    assert result["security.decision"]["reason"] == "bypass: base checks passed"


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
    assert "交互服务不可用" in blocked["tool_results"][0]["error"]
    assert zone_policy.STATE_KEY not in updates
