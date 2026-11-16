# @feature: FP-0.2.〇 项目类型 CI 包 | @ci: python-coverage
"""端到端验收（设计稿 §9.1/9.2/9.8 项目内部分）：真 git 仓 + 真钩子 + 真 runner +
真 checker。工具链（godot 二进制/gdtoolkit）缺席机器上编程门走 skip 语义，卫生门照常
执法——构建产物提交被拦、干净提交放行、local 补门下次提交即执法。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

_SHARED_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "shared"
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

from project_manager import project_create_tool  # noqa: E402

from tests.ci_packs.conftest import git  # noqa: E402


@pytest.fixture
def godot_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """godot 配方 + 仓内真实 packs 真源（端到端用出厂件）。"""
    tasks_root = tmp_path / "tasks_data"
    ws_base = tmp_path / "ws"
    monkeypatch.setenv("TASKS_STORAGE_DIR", str(tasks_root))
    monkeypatch.setattr("project_manager.project_registry.workspace_base_dir", lambda: ws_base)
    src_root = tmp_path / "addons_src"
    for name in ("agentos", "godot_mcp"):
        addon_dir = src_root / name
        addon_dir.mkdir(parents=True)
        (addon_dir / "plugin.cfg").write_text(f'[plugin]\nname="{name}"\n', encoding="utf-8")
    cfg = tmp_path / "project_create.yaml"
    cfg.write_text(
        "project_types:\n"
        "  godot:\n"
        "    schema_summary: Godot 4 测试配方\n"
        "    detect_file: project.godot\n"
        "    addons_source: " + src_root.as_posix() + "\n"
        "    addons:\n      - agentos\n      - godot_mcp\n"
        "    project_file: project.godot\n"
        '    scaffold: "config_version=5\\n\\n[application]\\n\\nconfig/name=\\"{title}\\"\\n"\n'
        "    enable_section: editor_plugins\n"
        "    enable_entries:\n      - res://addons/agentos/plugin.cfg\n"
        "    ci_packs:\n      - base\n      - godot\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(project_create_tool, "_TYPE_CONFIG_PATH", cfg)
    monkeypatch.delenv("GODOT_BIN", raising=False)
    return {"ws_base": ws_base}


async def _create(env: dict[str, Any], name: str) -> Path:
    target = env["ws_base"] / name
    r = await project_create_tool.ProjectCreateTool().execute(
        {"goal": name, "path": str(target), "project_type": "godot"}
    )
    assert r.success, r.error
    return target


def _commit(project: Path, message: str) -> Any:
    return git(project, "commit", "-m", message)


class TestEndToEndCommitGate:
    async def test_artifact_commit_blocked_clean_commit_passes(self, godot_env: dict[str, Any]) -> None:
        """验收②：产物暂存提交被拦（基础强制档实证）；干净提交绿。"""
        project = await _create(godot_env, "e1")
        assert (project / ".agentos" / "ci" / "gates.json").is_file()
        assert (project / ".git" / "hooks" / "pre-commit").is_file()

        # 出生提交（--no-verify）后造产物：下一次提交必须被钩子拦下
        pyc = project / "core" / "__pycache__" / "m.pyc"
        pyc.parent.mkdir(parents=True)
        pyc.write_bytes(b"x")
        git(project, "add", "-A")
        blocked = _commit(project, "带产物提交")
        assert blocked.returncode != 0, f"产物提交未被拦: {blocked.stdout}{blocked.stderr}"
        assert "m.pyc" in blocked.stdout + blocked.stderr

        # 清理后干净提交 → 钩子全绿放行
        import shutil

        shutil.rmtree(project / "core")
        (project / "project.godot").write_text(
            'config_version=5\n\n[application]\n\nconfig/name="e1"\n', encoding="utf-8"
        )
        (project / "README.md").write_text("# E1\n", encoding="utf-8")
        git(project, "add", "-A")
        clean = _commit(project, "干净提交")
        assert clean.returncode == 0, f"干净提交被误拦: {clean.stdout}{clean.stderr}"

    async def test_local_gate_enforced_next_commit(self, godot_env: dict[str, Any]) -> None:
        """验收④：local/gates.json 补门 → 下次提交即执法；撤门后放行。"""
        project = await _create(godot_env, "e2")
        local = project / ".agentos" / "ci" / "local"
        (local / "checks").mkdir()
        (local / "checks" / "ban_e2.py").write_text(
            "import sys\nprint('local 门：发现禁改文件')\nsys.exit(1)\n", encoding="utf-8"
        )
        (local / "gates.json").write_text(
            json.dumps(
                {
                    "gates": [
                        {
                            "id": "ban_e2",
                            "lane": "commit",
                            "tier": "block",
                            "run": ["python", "local/checks/ban_e2.py"],
                            "est_seconds": 1,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (project / "README.md").write_text("# E2\n", encoding="utf-8")
        git(project, "add", "-A")
        blocked = _commit(project, "触发 local 门")
        assert blocked.returncode != 0, f"local 门未执法: {blocked.stdout}{blocked.stderr}"
        assert "ban_e2" in blocked.stdout + blocked.stderr

        (local / "gates.json").write_text(json.dumps({"gates": []}), encoding="utf-8")
        git(project, "add", "-A")
        clean = _commit(project, "撤门后放行")
        assert clean.returncode == 0, f"撤门后仍被拦: {clean.stdout}{clean.stderr}"

    async def test_unshipped_toolchain_skips_not_blocks(self, godot_env: dict[str, Any]) -> None:
        """验收⑦（项目内可实证部分）：无 godot 二进制机器 → 编程门 skip 记原因，
        提交不被编程门误拦（卫生门仍执法）。"""
        project = await _create(godot_env, "e3")
        (project / "scripts").mkdir()
        (project / "scripts" / "a.gd").write_text("extends Node\n", encoding="utf-8")
        (project / "README.md").write_text("# E3\n", encoding="utf-8")
        git(project, "add", "-A")
        clean = _commit(project, "编程门应 skip")
        assert clean.returncode == 0, f"skip 语义未生效: {clean.stdout}{clean.stderr}"
        last_run = json.loads(
            (project / ".agentos" / "ci" / "last_run.json").read_text(encoding="utf-8")
        )
        by_id = {r["id"]: r for r in last_run["results"]}
        for gate_id in ("gdparse_syntax", "godot_headless_import", "godot_boot_smoke"):
            assert by_id[gate_id]["status"] == "skip", f"{gate_id} 应 skip"
            assert by_id[gate_id].get("reason")
