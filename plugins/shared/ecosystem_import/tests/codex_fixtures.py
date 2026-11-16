"""ecosystem_import 测试夹具构造器（Codex 格式 + 模块装载 + 快照）。

夹具一律在 ``tmp_path`` 内构造**真实** Codex 插件目录（不依赖本机
``~/.codex``），落地 / 门禁 / 溯源登记全程走真实文件系统——本套件不 mock
内部实现，mock 仅出现在「文件系统故障面」（copy 失败）与外部依赖（用户根
env、git）。

源目录晋升（与 plugins/conftest.py 的 ``_PLUGIN_SOURCE_DIRS`` 机制同面）：
插件目录（本插件裸名模块）+ 共享根（``_root_api``/``_libs``）+ mode_evolution
（``skill_audit`` 技能安检闸门——``server.py`` 的 bootstrap extra 同源注入）
+ 本 tests 目录（``codex_fixtures`` 自身）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

_TESTS_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _TESTS_DIR.parent
_SHARED_DIR = _PLUGIN_DIR.parent

_PLUGIN_SOURCE_DIRS = [
    str(_PLUGIN_DIR),
    str(_SHARED_DIR),
    str(_SHARED_DIR / "mode_evolution"),
    str(_TESTS_DIR),
]
for _dir in _PLUGIN_SOURCE_DIRS:
    if _dir not in sys.path:
        sys.path.insert(0, _dir)


def load_tool_module() -> Any:
    """按唯一模块名装载本插件 ``tool.py``（bare ``tool`` 跨插件同名，不裸名 import）。"""
    module_name = "ecosystem_import_tool_impl"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, _PLUGIN_DIR / "tool.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_server_module() -> Any:
    """按唯一模块名装载 ``server.py``（导入即注册，模块级 plugin 实例即合宿契约面）。"""
    module_name = "ecosystem_import_server_probe"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, _PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# ── Codex 格式夹具构造器 ─────────────────────────────────────────────────


def skill_md(name: str | None, description: str | None = "技能说明", *, body: str = "") -> str:
    """SKILL.md 文本；``name=None`` = frontmatter 无 name 键（测目录名回落）。"""
    front = ["---"]
    if name is not None:
        front.append(f"name: {name}")
    if description is not None:
        front.append(f"description: {description}")
    front.append("---")
    return "\n".join([*front, "", f"# {name or '未命名'}", "", body, ""])


def make_codex_plugin(
    root: Path,
    *,
    name: str = "demo-plugin",
    version: str = "1.0.0",
    license: str | None = "MIT",
    description: str = "演示插件",
    author: str = "Demo Author",
) -> Path:
    """构造 Codex 插件骨架（``.codex-plugin/plugin.json``），返回插件目录。"""
    plugin_dir = root / name
    (plugin_dir / ".codex-plugin").mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "name": name,
        "version": version,
        "description": description,
        "author": {"name": author},
        "skills": "./skills/",
    }
    if license is not None:
        manifest["license"] = license
    (plugin_dir / ".codex-plugin" / "plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return plugin_dir


def add_skill(
    plugin_dir: Path,
    dir_name: str,
    *,
    content: str,
    extra_files: dict[str, str] | None = None,
) -> Path:
    """写单个技能目录（SKILL.md + 同目录资源），返回技能目录。"""
    skill_dir = plugin_dir / "skills" / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    for rel, text in (extra_files or {}).items():
        path = skill_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return skill_dir


def write_mcp(plugin_dir: Path, servers: dict[str, Any]) -> None:
    (plugin_dir / ".mcp.json").write_text(json.dumps({"mcpServers": servers}, ensure_ascii=False), encoding="utf-8")


def write_apps(plugin_dir: Path, apps: dict[str, Any]) -> None:
    (plugin_dir / ".app.json").write_text(json.dumps({"apps": apps}, ensure_ascii=False), encoding="utf-8")


def file_snapshot(root: Path) -> dict[str, bytes]:
    """目录树内容快照（相对 posix 路径 → 字节）：只读性与真值同步断言的观测面。"""
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}
