# @feature: FP-0.2.一 插件协议 | @vision: V3 可嵌入 | @ci: python-coverage
"""MCP 服务与 skills 冒烟测试——独立于插件矩阵的第三类资产。

覆盖：
1. mcp-servers/ 下 3 个 Python MCP 服务端（bing-search / demo-tools / llm-sidecar）：
   子进程语法编译 + mcp.json 清单校验；
2. mcp-servers/web-search-mcp（Node）：package.json / mcp.json / 源码目录存在性
   （npm 单测需 node_modules，由仓库外 npm test 执行，见 web-search-mcp/tests）；
3. skills/ 下全部 18 个 skill：SKILL.md 存在且 frontmatter 合法。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MCP_DIR = ROOT / "mcp-servers"
SKILLS_DIR = ROOT / "skills"

PYTHON_MCP_SERVERS = [
    MCP_DIR / "bing-search",
    MCP_DIR / "demo-tools",
    MCP_DIR / "llm-sidecar",
]


def _compile_check(server_path: Path) -> None:
    """子进程语法编译 server 脚本。"""
    cmd = [
        sys.executable,
        "-c",
        "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())",
        str(server_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, f"{server_path.name} 语法错误: {proc.stderr[-400:]}"


@pytest.mark.parametrize("server_dir", PYTHON_MCP_SERVERS, ids=[d.name for d in PYTHON_MCP_SERVERS])
def test_python_mcp_server_loadable(server_dir: Path) -> None:
    """Python MCP 服务端脚本语法合法、mcp.json 清单完整。"""
    server_py = server_dir / "server.py"
    assert server_py.exists(), f"{server_dir.name} 缺少 server.py"
    _compile_check(server_py)

    mcp_json = server_dir / "mcp.json"
    if mcp_json.exists():
        meta = json.loads(mcp_json.read_text(encoding="utf-8"))
        servers = meta.get("mcpServers", {})
        assert servers, f"{server_dir.name}/mcp.json 缺少 mcpServers"
        for name, spec in servers.items():
            assert spec.get("command"), f"{server_dir.name}: {name} 缺少 command"
            assert spec.get("args"), f"{server_dir.name}: {name} 缺少 args"
            # args 引用的脚本必须存在（相对 mcp.json 所在目录）
            for arg in spec.get("args", []):
                p = server_dir / arg
                assert p.exists(), f"{server_dir.name}: {name} 引用不存在的文件 {arg}"


def test_llm_sidecar_extra_scripts_valid() -> None:
    """llm-sidecar 的 litellm_proxy.py 也应语法合法。"""
    proxy = MCP_DIR / "llm-sidecar" / "litellm_proxy.py"
    assert proxy.exists()
    _compile_check(proxy)


def test_web_search_mcp_node_project_structure() -> None:
    """web-search-mcp（Node）项目结构完整；测试脚本以 test-*.js 形式存在。"""
    pkg = json.loads((MCP_DIR / "web-search-mcp" / "package.json").read_text(encoding="utf-8"))
    assert pkg.get("name"), "package.json 缺少 name"
    mcp_json = MCP_DIR / "web-search-mcp" / "mcp.json"
    assert mcp_json.exists()
    meta = json.loads(mcp_json.read_text(encoding="utf-8"))
    spec = list(meta["mcpServers"].values())[0]
    assert spec["command"] == "node"
    assert (MCP_DIR / "web-search-mcp" / "src").is_dir(), "缺少 src/ 源码目录"
    tests_dir = MCP_DIR / "web-search-mcp" / "tests"
    assert tests_dir.is_dir(), "缺少 tests/ 单测目录"
    assert list(tests_dir.glob("test-*.js")), "tests/ 下缺少 test-*.js 测试脚本"


def test_skills_frontmatter_valid() -> None:
    """全部 skill 的 SKILL.md 存在且 frontmatter（name/description）合法。"""
    skill_dirs = sorted(d for d in SKILLS_DIR.iterdir() if d.is_dir())
    assert len(skill_dirs) >= 1, "skills/ 目录为空"
    missing = []
    bad_frontmatter = []
    for d in skill_dirs:
        skill_md = d / "SKILL.md"
        if not skill_md.exists():
            missing.append(d.name)
            continue
        text = skill_md.read_text(encoding="utf-8")
        if not text.startswith("---") or "---" not in text[3:]:
            bad_frontmatter.append(f"{d.name} (无 frontmatter)")
            continue
        fm = text[3 : text.index("---", 3)]
        if "name:" not in fm or "description:" not in fm:
            bad_frontmatter.append(f"{d.name} (缺 name/description)")
    assert not missing, f"缺少 SKILL.md: {missing}"
    assert not bad_frontmatter, f"frontmatter 不合法: {bad_frontmatter}"
