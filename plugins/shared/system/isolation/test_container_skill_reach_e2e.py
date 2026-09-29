# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""真容器技能可达验收（2026-09-29 R300 装机悬空引用修复验收①）。

验收契约：workspace_lifecycle 同步进会话工作空间的 skills/ 快照，经隔离
容器的工作空间整体挂载（-v {ws}:/workspace），容器内 bash 以
`cat skills/<技能>/SKILL.md` 相对路径可达。

默认跳过（真容器 = 外部依赖，不入常规车道）：AGENTOS_ISO_CONTAINER_E2E=1
显式开启且 docker daemon 可用。链路全程真实——真 manager 同步、真
DockerProvider 建/执/毁容器、真 bind mount——不 mock 任何环节。

本机运行：AGENTOS_ISO_CONTAINER_E2E=1 python -m pytest
plugins/shared/system/isolation/test_container_skill_reach_e2e.py -v
（镜像可用 AGENTOS_ISO_CONTAINER_IMAGE 覆盖，默认 alpine:3.19）
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.e2e

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/system/isolation/
_SHARED_DIR = _PLUGIN_DIR.parent.parent  # plugins/shared/（user_space 平铺模块解析）
for _d in (_PLUGIN_DIR, _SHARED_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

_ENABLED = os.environ.get("AGENTOS_ISO_CONTAINER_E2E") == "1"


def _daemon_available() -> bool:
    if not shutil.which("docker"):
        return False
    proc = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return proc.returncode == 0


if not (_ENABLED and _daemon_available()):
    pytest.skip(
        "真容器车道未开启（AGENTOS_ISO_CONTAINER_E2E=1 且 docker daemon 可用）",
        allow_module_level=True,
    )


def _load(rel: str, mod_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / rel)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_wl = _load("workspace_lifecycle.py", "iso_reach_workspace_lifecycle")
_dp = _load(str(Path("providers") / "docker_provider.py"), "iso_reach_docker_provider")

SKILL_NAME = "skill_reach_probe"
SKILL_BODY = "# 真容器可达验收探针\n"


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_container_bash_reads_synced_skill(tmp_path: Path, monkeypatch: Any) -> None:
    # 1. 真同步：用户根 skills/ 源（装机补种形态）→ 真实 manager 落盘工作空间
    user_root = tmp_path / "userroot"
    (user_root / "skills" / SKILL_NAME).mkdir(parents=True)
    (user_root / "skills" / SKILL_NAME / "SKILL.md").write_text(SKILL_BODY, encoding="utf-8")
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(user_root))
    ws = tmp_path / "ws"
    ws.mkdir()
    manager = _wl.WorkspaceLifecycleManager(
        resource_merge=None,
        config={"workspace": {"root": str(tmp_path / "wsroot")}},
        task_tree=object(),  # 仅 _copy_skills_to_workspace 路径，不触任务树
        ws_meta_store={},
        base_path=str(tmp_path / "proj"),  # 项目根无 skills/（装机形态）
        # mode_skill_sources=None = 生产推导模式（用户根 skills/ 源参与并集）
    )
    manager._copy_skills_to_workspace(str(ws))
    assert (ws / "skills" / SKILL_NAME / "SKILL.md").read_text(encoding="utf-8") == SKILL_BODY

    # 2. 真容器：工作空间整体挂载 → 容器内 bash 相对路径 cat 技能
    provider = _dp.DockerProvider(
        {
            "image": os.environ.get("AGENTOS_ISO_CONTAINER_IMAGE", "alpine:3.19"),
            "auto_build": False,  # 探针镜像走 pull，不走仓内 Dockerfile 构建
            "network_mode": "none",  # 纯文件可达性验证，无需网络
        }
    )
    ctx = _dp.IsolationContext(
        task_id="skill-reach-probe",
        task_type="atomic",  # TaskType.ATOMIC（str 枚举，与 sdk isolation_types 同值）
        operation_type="code_execution",  # OperationType.CODE_EXECUTION
        workspace=str(ws),
    )
    env = _run(provider.create_environment(ctx, container_name="agentos-skill-reach-probe"))
    try:
        assert env.status == _dp.EnvironmentStatus.READY.value, env.provider_info
        # 默认 working_dir = /workspace（挂载根）——相对路径即工作空间相对寻址
        result = _run(
            provider.execute_in_environment(
                env.env_id,
                {"type": "command", "command": f"cat skills/{SKILL_NAME}/SKILL.md"},
            )
        )
    finally:
        _run(provider.destroy_environment(env.env_id, success=True))
    assert result.success, result.error
    assert result.output is not None
    assert SKILL_BODY.strip() in result.output["stdout"]
