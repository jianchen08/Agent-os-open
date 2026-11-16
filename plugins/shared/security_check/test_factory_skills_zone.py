# @feature: FP-0.2.批C-a-2 出厂 skills/ 写区收敛 | @ci: python-coverage
"""出厂 skills/ 写区收敛测试（ADR 2026-10-09 批 C-a 红线①，原则 7）。

三语义严格区分（判定单源 zone_policy.factory_skills_write_denied）：
1. 出厂收敛：仓库根一级 ``skills/`` 工具直写恒拒且不可授权（唯一写路径 =
   自进化提案管道）——任何档位含旁路档一致执法；
2. 项目根 skills/ 播种（用户域资产）：project_root 锚内写合法，不收敛；
3. 会话工作空间 skills/ 快照（workspace_lifecycle 写面）：workspace 锚内
   写合法，不收敛。

bash 旁路：命令串写动作词元扫（词法有界，见 plugin._BASH_WRITE_MARKER_RE），
读词面不扫；dev 场景 project_root 即仓库根时相对形态同拦。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_THIS_DIR = str(Path(__file__).resolve().parent)
_SHARED_DIR = str(Path(__file__).resolve().parents[1])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

import pytest  # noqa: E402
import repo_anchor  # noqa: E402


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "security_check_plugin_factory_skills", str(Path(_THIS_DIR) / "plugin.py")
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


class _NoCardCap:
    """授权卡哨兵：出厂 skills/ 拒绝必须走硬拒，任何卡片调用即失败。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        self.calls.append((method, params))
        raise AssertionError(f"出厂 skills/ 收敛为硬拒，不得弹卡: {method}")


@pytest.fixture
def factory_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Path]]:
    """假仓库锚（含出厂 skills/）+ 独立项目根 + 工作空间 + tmp 名单。"""
    repo = tmp_path / "repo"
    (repo / "config" / "kernel").mkdir(parents=True)
    (repo / "skills").mkdir()
    project = tmp_path / "proj"
    (project / "skills").mkdir(parents=True)
    ws = tmp_path / "sessions" / "s1"
    ws.mkdir(parents=True)
    users_dir = tmp_path / "cfgusers" / "default"
    users_dir.mkdir(parents=True)
    (users_dir / "project_whitelist.yaml").write_text("entries: []\n", encoding="utf-8")
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(repo / "config"))
    monkeypatch.setenv("AGENTOS_CONFIG_USERS_DIR", str(users_dir.parent))
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "usroot"))
    repo_anchor.reset_cache()
    yield {"repo": repo, "project": project, "ws": ws, "tmp": tmp_path}
    repo_anchor.reset_cache()


def _ctx(env: dict[str, Path], tool: str, args: dict[str, Any], **state: Any) -> Any:
    def _get_service(name: str) -> Any:
        raise KeyError(name)

    cap = _NoCardCap()
    _mod.set_human_interaction_cap(cap)
    return types.SimpleNamespace(
        state={
            "core_type": "tool_execute",
            "raw_tool_calls": [{"name": tool, "args": args, "id": "call-1"}],
            "messages": [],
            "session_id": "sess-factory",
            "pipeline_id": "pip-factory",
            "user_id": "u-1",
            **state,
        },
        get_service=_get_service,
    ), cap


async def _gate(env: dict[str, Path], tool: str, args: dict[str, Any], **state: Any):
    ctx, cap = _ctx(env, tool, args, **state)
    result = await _plugin_like()._do_work(ctx)
    return result, cap


def _plugin_like() -> Any:
    return SecurityCheckPlugin({})


# ── 语义 1：出厂收敛（正 = 工具直写拒 / 反 = 提案管道唯一写面不受影响） ──
async def test_factory_skills_tool_write_hard_denied(factory_env):
    """出厂 skills/ 工具直写：硬拒且不弹卡（唯一写路径 = 进化提案管道）。"""
    result, cap = await _gate(
        factory_env,
        "file_write",
        {"path": str(factory_env["repo"] / "skills" / "new.md"), "content": "x"},
        workspace=str(factory_env["ws"]),
    )
    assert cap.calls == [], "硬拒不弹授权卡"
    decision = result["pre_decided_results"][0]
    assert "出厂 skills/" in decision["error"]
    assert "提案" in decision["error"]
    assert not (factory_env["repo"] / "skills" / "new.md").exists()


async def test_factory_skills_denied_even_in_bypass_tier(factory_env):
    """旁路档（隔离免审批默认）不豁免出厂 skills/ 收敛——任何档一致执法。"""
    result, cap = await _gate(
        factory_env,
        "file_write",
        {"path": str(factory_env["repo"] / "skills" / "new.md"), "content": "x"},
        workspace=str(factory_env["ws"]),
        execution_contexts=[{"provider": "docker", "level": "isolated", "task_isolated": True}],
    )
    assert cap.calls == []
    assert "出厂 skills/" in result["pre_decided_results"][0]["error"]


async def test_factory_skills_deny_not_masked_by_project_anchor(factory_env):
    """语义 2 反例：project_root 即仓库根（dev 场景）时锚内快捷放行不遮蔽收敛。"""
    result, cap = await _gate(
        factory_env,
        "file_write",
        {"path": "skills/leak.md", "content": "x"},
        project_root=str(factory_env["repo"]),
        workspace=str(factory_env["ws"]),
    )
    assert cap.calls == []
    assert "出厂 skills/" in result["pre_decided_results"][0]["error"]


async def test_delete_move_into_factory_skills_denied(factory_env):
    """写类操作族（delete/move 落点）同收敛；delete 源在出厂 skills/ 亦拒。"""
    src = factory_env["repo"] / "skills" / "old.md"
    src.write_text("x\n", encoding="utf-8")
    result, cap = await _gate(
        factory_env, "delete_file", {"path": str(src)}, workspace=str(factory_env["ws"])
    )
    assert cap.calls == []
    assert "出厂 skills/" in result["pre_decided_results"][0]["error"]
    assert src.exists(), "删除被拒，文件保留"


# ── 语义 2：项目根 skills/ 播种（正 = 写合法不收敛） ────────────────────
async def test_project_root_skills_seeding_write_passes(factory_env):
    """用户项目根 skills/（播种资产）：锚内写放行不弹卡（不收敛）。"""
    target = factory_env["project"] / "skills" / "seeded.md"
    result, cap = await _gate(
        factory_env,
        "file_write",
        {"path": str(target), "content": "x"},
        project_root=str(factory_env["project"]),
        workspace=str(factory_env["ws"]),
    )
    assert result.get("pre_decided_results") is None
    assert cap.calls == []


# ── 语义 3：会话工作空间 skills/ 快照（正 = 写合法 / 反 = 不外溢出厂面） ─
async def test_workspace_skills_snapshot_write_passes(factory_env):
    """工作空间 skills/ 快照：锚内写放行（workspace_lifecycle 写面合法）。"""
    target = factory_env["ws"] / "skills" / "snapshot.md"
    result, cap = await _gate(
        factory_env,
        "file_write",
        {"path": str(target), "content": "x"},
        workspace=str(factory_env["ws"]),
    )
    assert result.get("pre_decided_results") is None
    assert cap.calls == []


async def test_repo_root_prefix_is_exactly_skills(factory_env):
    """仓库内工作空间（.ai_workspaces 形态）skills 写合法——收敛前缀恰为
    仓库根一级 skills/，不外溢到仓库根下其他路径。"""
    ws_in_repo = factory_env["repo"] / "workspace-t1"
    (ws_in_repo / "skills").mkdir(parents=True)
    ok, cap = await _gate(
        factory_env,
        "file_write",
        {"path": str(ws_in_repo / "skills" / "x.md"), "content": "x"},
        workspace=str(ws_in_repo),
    )
    assert ok.get("pre_decided_results") is None
    assert cap.calls == []


# ── bash 旁路面（词法有界，读词面不扫） ─────────────────────────────────
async def test_bash_write_to_factory_skills_blocked(factory_env):
    """bash 重定向写出厂 skills/：位置闸硬拒（fs 工具闸不被 bash 绕穿）。"""
    result, cap = await _gate(
        factory_env,
        "bash_execute",
        {"command": f"echo x > {factory_env['repo'] / 'skills' / 'x.md'}"},
        workspace=str(factory_env["ws"]),
    )
    assert cap.calls == []
    assert "出厂 skills/" in result["pre_decided_results"][0]["error"]


async def test_bash_relative_write_blocked_when_project_is_repo(factory_env):
    """dev 场景（project_root = 仓库根）：相对形态 skills/x 同拦。"""
    result, cap = await _gate(
        factory_env,
        "bash_execute",
        {"command": "echo x > skills/x.md"},
        project_root=str(factory_env["repo"]),
        workspace=str(factory_env["ws"]),
    )
    assert "出厂 skills/" in result["pre_decided_results"][0]["error"]


async def test_bash_write_to_project_skills_passes(factory_env):
    """bash 写用户项目 skills/：合法（语义 2 不被 bash 面误伤）。

    旁路档运行（隔离免审批默认）：规则轨道的重定向审批是既有独立闸，不在
    本测试靶面——旁路档跳过规则轨道而位置闸照常执法，可干净隔离位置轴。
    """
    result, cap = await _gate(
        factory_env,
        "bash_execute",
        {"command": "echo x > skills/seed.md"},
        project_root=str(factory_env["project"]),
        workspace=str(factory_env["ws"]),
        execution_contexts=[{"provider": "docker", "level": "isolated", "task_isolated": True}],
    )
    assert result.get("pre_decided_results") is None
    assert cap.calls == []


async def test_bash_read_of_factory_skills_passes(factory_env):
    """bash 读出厂 skills/：读面不收敛（skills/ 是可读源码区）。旁路档隔离规则轨道。"""
    target = factory_env["repo"] / "skills" / "doc.md"
    target.write_text("doc\n", encoding="utf-8")
    result, cap = await _gate(
        factory_env,
        "bash_execute",
        {"command": f"cat {target}"},
        workspace=str(factory_env["ws"]),
        execution_contexts=[{"provider": "docker", "level": "isolated", "task_isolated": True}],
    )
    assert result.get("pre_decided_results") is None
    assert cap.calls == []


async def test_bash_write_marker_prefix_noise_not_blocked(factory_env):
    """写标记前文词元不扫：读出厂技能文件并重定向到别处（读 + 外写）放行。"""
    result, cap = await _gate(
        factory_env,
        "bash_execute",
        {"command": f"cat {factory_env['repo'] / 'skills' / 'doc.md'} > {factory_env['ws'] / 'out.md'}"},
        workspace=str(factory_env["ws"]),
        execution_contexts=[{"provider": "docker", "level": "isolated", "task_isolated": True}],
    )
    assert result.get("pre_decided_results") is None
    assert cap.calls == []


# ── 判定单源单元（repo_anchor / zone_policy 同源出口） ──────────────────
def test_zone_policy_factory_verdict_anchors(factory_env, monkeypatch):
    """无仓库锚 → None（行为退回，不报错）；非 skills 一等目录 → None。"""
    zone_policy = _mod.zone_policy
    assert zone_policy.factory_skills_write_denied(factory_env["repo"] / "docs" / "x.md") is None
    assert zone_policy.factory_skills_write_denied(factory_env["tmp"] / "elsewhere" / "skills" / "x.md") is None
    # 无锚分支：在仓布局内探测恒命中（root is None 不可自然到达），按既有
    # 先例 monkeypatch resolve_repo_root → None（test_repo_read_anchor 同款）。
    monkeypatch.setattr(repo_anchor, "resolve_repo_root", lambda: None)
    assert zone_policy.factory_skills_write_denied(Path("D:/nowhere/skills/x.md")) is None
    assert repo_anchor.factory_skills_write_denied(factory_env["repo"] / "skills" / "x.md") is None
