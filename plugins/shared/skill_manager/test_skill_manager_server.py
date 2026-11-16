# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""skill_manager server.py 接线测试（T3）。

服务工具（skill.list / skill.get）返回形态即服务契约：list 只回元数据、
get 携带正文、未知 id found=false。server.py 装载即完成 @plugin.tool 注册，
进程内直调装饰后原函数（装饰器返回原函数，search 合宿测试同款）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_SHARED_ROOT = _PLUGIN_DIR.parents[1]
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

_MOD = "skill_manager_server_test"


def _load_server() -> Any:
    if _MOD in sys.modules:
        del sys.modules[_MOD]
    spec = importlib.util.spec_from_file_location(_MOD, _PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MOD] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def skills_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """mini 技能仓：一枚 front matter 技能（带脚本）+ 一枚坏件。"""
    good = tmp_path / "skills" / "good"
    good.mkdir(parents=True)
    (good / "SKILL.md").write_text("---\nname: Good Skill\ndescription: fine\n---\ngood body\n", encoding="utf-8")
    (good / "s.py").write_text("print('x')\n", encoding="utf-8")
    broken = tmp_path / "skills" / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_bytes(b"\xff\xfe\x00bad")

    import skill_manager.skill_registry as sr

    # 四源推导换成封闭单源（X11-F1：缺省走真实四源，测试给注入缝）
    monkeypatch.setattr(sr, "_default_sources", lambda: [(tmp_path / "skills", "repo_root")])
    return tmp_path


class TestSkillServiceTools:
    def test_skill_list_metadata_only(self, skills_repo: Path) -> None:
        server = _load_server()
        out = asyncio.run(server.skill_list())
        assert out == {
            "items": [
                {
                    "id": "good",
                    "name": "Good Skill",
                    "description": "fine",
                    "languages": ["python"],
                    "source": "repo_root",
                }
            ]
        }  # 坏件 skip；列表不含正文（source = 实际胜出源）

    def test_skill_get_found_carries_content(self, skills_repo: Path) -> None:
        server = _load_server()
        out = asyncio.run(server.skill_get("good"))
        assert out["found"] is True
        assert out["skill"]["content"] == "good body\n"

    def test_skill_get_unknown_and_broken(self, skills_repo: Path) -> None:
        server = _load_server()
        assert asyncio.run(server.skill_get("no_such")) == {"found": False}
        assert asyncio.run(server.skill_get("broken")) == {"found": False}

    def test_tool_registration_names(self, skills_repo: Path) -> None:
        server = _load_server()
        assert set(server.plugin._tools.keys()) == {"skill.list", "skill.get"}
