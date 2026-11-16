"""ecosystem_import 测试 fixtures：临时夹具目录 + 用户根 env + 工具实例。

构造器与模块装载在 ``codex_fixtures``（独名模块，跨插件 conftest 同名不互扰）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from codex_fixtures import (  # noqa: E402
    add_skill,
    load_tool_module,
    make_codex_plugin,
    skill_md,
    write_apps,
    write_mcp,
)


@pytest.fixture
def user_root_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """用户根摘出到临时目录（工具侧经 ``diagnostics_settings.user_root()`` 解析）。"""
    root = tmp_path / "user-root"
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(root))
    return root


@pytest.fixture
def config_root_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """配置根摘出到临时目录（``AGENTOS_CONFIG_ROOT`` 形态 = ``<根>/config``）。

    夹具自校验解析确实落在临时根上——解析不到时用例（而非仓内真配置）承担失败，
    MCP 写面用例由构造保证一份真配置都不碰。根解析经 ``project_root`` 单源的
    进程级缓存（键不含 env），每个用例前后重置缓存，避免临时根跨用例互串。
    """
    import mcp_config
    import project_root

    root = tmp_path / "config"
    root.mkdir(parents=True)
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(root))
    project_root.reset_cache()
    assert mcp_config.resolve_config_root() == root, "配置根未解析到临时根（拒写真实配置）"
    yield root
    project_root.reset_cache()


@pytest.fixture
def mit_skills_plugin(tmp_path: Path) -> Path:
    """夹具①：纯技能 MIT 插件（两技能，含 assets/ 与 scripts/ 子目录）。"""
    plugin = make_codex_plugin(
        tmp_path,
        name="superpowers-lite",
        version="6.3.0",
        license="MIT",
        description="纯技能包：方法论与计划写作",
        author="Obra",
    )
    add_skill(
        plugin,
        "brainstorming",
        content=skill_md("brainstorming", "先头脑风暴再动手", body="渲染脚本：scripts/helper.js\n"),
        extra_files={"assets/frame.html": "<html>frame</html>", "scripts/helper.js": "console.log(1)\n"},
    )
    add_skill(
        plugin,
        "writing-plans",
        content=skill_md("writing-plans", "写执行计划"),
        extra_files={"references/notes.md": "# 参考\n"},
    )
    return plugin


@pytest.fixture
def proprietary_mcp_plugin(tmp_path: Path) -> Path:
    """夹具②：`.mcp.json`（http+OAuth 块 / stdio 各一）+ `.app.json` + Proprietary。"""
    plugin = make_codex_plugin(
        tmp_path,
        name="codex-security-lite",
        version="0.1.32",
        license="Proprietary",
        description="安全扫描工作流",
        author="OpenAI",
    )
    add_skill(plugin, "track-findings", content=skill_md("track-findings", "跟踪安全缺陷"))
    write_mcp(
        plugin,
        {
            "github": {
                "type": "http",
                "url": "https://api.githubcopilot.com/mcp/",
                "oauth": {
                    "client_id": "Iv23liZgMPSa3samkW2k",
                    "client_secret": "3f2e1d0c9b8a",
                    "callback_port": 12799,
                },
            },
            "local-tools": {"command": "cmd.exe", "args": ["/c", "run.cmd"]},
        },
    )
    write_apps(
        plugin,
        {
            "linear": {"id": "asdk_app_69a089a326dc8191b32a3f2553f5be2c"},
            "github": {"id": "connector_76869538009648d5b282a4bb21c3d157"},
        },
    )
    return plugin


@pytest.fixture
def translate_tool():
    return load_tool_module().EcosystemTranslateTool()


@pytest.fixture
def apply_tool():
    return load_tool_module().EcosystemApplyTool()
