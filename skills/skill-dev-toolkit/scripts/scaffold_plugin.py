#!/usr/bin/env python3
"""插件脚手架生成器（开发工具箱技能 skills/skill-dev-toolkit 自带脚本）。

填补空缺：仓内新建插件此前只有静态模板（config/templates/plugin_scaffold/，
占位符式管道类骨架）与样例插件（plugins/shared/hello_pack），无生成器。

两种 kind：
- tool（默认）：生成完整可跑工具插件目录（plugin.json + server.py + 实现 +
  测试），形态对齐 hello_pack 样例；声明契约真源 = tool_core 校验 +
  skills/resource-tool-create/SKILL.md，生成物过不了 G2 以真源为准修。
- core|input|output：按 config/templates/plugin_scaffold/{kind}_plugin.py
  渲染管道类文件（模板占位符集合见模板文件本身）。

用法：
  python skills/skill-dev-toolkit/scripts/scaffold_plugin.py my_tool \\
    --one-line "一句话描述" --target plugins/shared
  python skills/skill-dev-toolkit/scripts/scaffold_plugin.py my_core \\
    --kind core --one-line "一句话描述"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from string import Template

PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_KINDS = ("tool", "core", "input", "output")

_PLUGIN_JSON_TEMPLATE = Template("""{
  "id": "$plugin_id",
  "name": "$class_name",
  "description": "$one_line",
  "version": "0.1.0",
  "plugin_type": "tool",
  "language": "python",
  "host_type": "sidecar",
  "entry": "python server.py",
  "capabilities": {
    "tools": [
      {
        "name": "$plugin_id",
        "description": "$one_line",
        "render": {
          "card": "form",
          "title": "$class_name"
        },
        "input_schema": {
          "type": "object",
          "properties": {}
        },
        "output_schema": {
          "type": "object",
          "required": ["message"],
          "properties": {
            "message": {
              "type": "string",
              "description": "$one_line"
            }
          }
        }
      }
    ]
  },
  "permissions": {},
  "priority": 50,
  "host_group": "light"
}
""")

_SERVER_TEMPLATE = Template('''#!/usr/bin/env python3
"""$plugin_id MCP 服务端（entry "python server.py" 的产物面）。"""

from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)

from $plugin_id import (
    ${UPPER}_DESCRIPTION,
    ${UPPER}_INPUT_SCHEMA,
    ${UPPER}_OUTPUT_SCHEMA,
    ${UPPER}_RENDER,
    $plugin_id,
)

# 合宿契约：server.py 必须暴露模块级 plugin 实例且装载期完成工具注册
# （CohostServer 聚合 plugin._tools，注册留在运行入口则合宿成员零工具）。
plugin = AgentOSPlugin("$plugin_id")


def create_plugin() -> AgentOSPlugin:
    """注册工具（幂等；供独立运行与测试复用）。"""
    plugin.register_tool(
        "$plugin_id",
        ${UPPER}_INPUT_SCHEMA,
        $plugin_id,
        ${UPPER}_DESCRIPTION,
        output_schema=${UPPER}_OUTPUT_SCHEMA,
        render=${UPPER}_RENDER,
    )
    return plugin


create_plugin()


def run() -> None:
    """启动 MCP 服务端。"""
    plugin.run()


if __name__ == "__main__":
    run()
''')

_IMPL_TEMPLATE = Template('''"""$plugin_id 工具实现。

本文件 schema 常量与 plugin.json capabilities.tools[0] 声明逐字对应
（G2 声明↔实现严格比对，漂移即剔除）。
"""

from __future__ import annotations

from typing import Any

TOOL_NAME = "$plugin_id"

${UPPER}_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
}

${UPPER}_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["message"],
    "properties": {
        "message": {
            "type": "string",
            "description": "$one_line",
        }
    },
}

${UPPER}_RENDER = {"card": "form", "title": "$class_name"}

${UPPER}_DESCRIPTION = "$one_line"


def $plugin_id() -> dict[str, Any]:
    """$one_line。"""
    # TODO: 替换为真实逻辑（入参加进 INPUT_SCHEMA.properties 并同步 plugin.json）
    return {"message": "hello from $plugin_id"}
''')

_TEST_TEMPLATE = Template('''"""$plugin_id 工具实现单测（不依赖 SDK，直接测实现模块）。"""

from $plugin_id import (
    ${UPPER}_INPUT_SCHEMA,
    ${UPPER}_OUTPUT_SCHEMA,
    $plugin_id,
)


def test_${plugin_id}_returns_message() -> None:
    result = $plugin_id()
    assert result["message"]


def test_schemas_are_object_type() -> None:
    assert ${UPPER}_INPUT_SCHEMA["type"] == "object"
    assert ${UPPER}_OUTPUT_SCHEMA["type"] == "object"
''')


class ScaffoldError(Exception):
    """脚手架失败（参数非法/目标已存在/模板缺失）。"""


def default_repo_root() -> Path:
    """脚本位于 <repo>/skills/skill-dev-toolkit/scripts/，上溯三级即仓根。"""
    return Path(__file__).resolve().parents[3]


def derive_class_name(plugin_id: str) -> str:
    """snake_case 插件 id → PascalCase 类名（hello_pack → HelloPack）。"""
    return "".join(part.capitalize() for part in plugin_id.split("_"))


def render_tool_files(plugin_id: str, one_line: str) -> dict[str, str]:
    """工具插件四件套（plugin.json 已通过 json.loads 验证合法）。"""
    class_name = derive_class_name(plugin_id)
    fields = {
        "plugin_id": plugin_id,
        "class_name": class_name,
        "one_line": one_line,
        "UPPER": plugin_id.upper(),
    }
    files = {
        "plugin.json": _PLUGIN_JSON_TEMPLATE.substitute(fields),
        "server.py": _SERVER_TEMPLATE.substitute(fields),
        f"{plugin_id}.py": _IMPL_TEMPLATE.substitute(fields),
        f"test_{plugin_id}.py": _TEST_TEMPLATE.substitute(fields),
    }
    json.loads(files["plugin.json"])
    return files


def render_pipeline_file(
    repo_root: Path,
    kind: str,
    plugin_id: str,
    one_line: str,
    detailed: str,
    read_state_keys: str,
    write_state_keys: str,
    config_docs: str,
) -> tuple[str, str]:
    """按仓内管道模板渲染（模板缺失/占位符不匹配 → ScaffoldError）。"""
    template_path = repo_root / "config" / "templates" / "plugin_scaffold" / f"{kind}_plugin.py"
    if not template_path.is_file():
        raise ScaffoldError(f"管道模板缺失: {template_path}")
    try:
        rendered = template_path.read_text(encoding="utf-8").format(
            plugin_name=plugin_id,
            PluginClass=derive_class_name(plugin_id),
            one_line_description=one_line,
            detailed_description=detailed,
            read_state_keys=read_state_keys,
            write_state_keys=write_state_keys,
            config_docs=config_docs,
        )
    except KeyError as exc:
        raise ScaffoldError(f"模板 {template_path.name} 存在未提供的占位符: {exc}") from exc
    return f"{plugin_id}.py", rendered


def scaffold_plugin(
    repo_root: Path,
    target_parent: Path,
    plugin_id: str,
    kind: str,
    one_line: str,
    detailed: str | None = None,
    read_state_keys: str = "无",
    write_state_keys: str = "无",
    config_docs: str = "无",
) -> list[Path]:
    """生成插件目录，返回创建的文件路径列表。目标已存在或 id 非法即拒。"""
    if not PLUGIN_ID_RE.match(plugin_id):
        raise ScaffoldError(
            f"插件 id 非法: {plugin_id!r}（须匹配 {PLUGIN_ID_RE.pattern}，snake_case）"
        )
    if kind not in _KINDS:
        raise ScaffoldError(f"kind 非法: {kind!r}（可选 {', '.join(_KINDS)}）")
    target = target_parent / plugin_id
    if target.exists():
        raise ScaffoldError(f"目标目录已存在: {target}")
    if kind == "tool":
        files = render_tool_files(plugin_id, one_line)
    else:
        filename, content = render_pipeline_file(
            repo_root,
            kind,
            plugin_id,
            one_line,
            detailed if detailed is not None else one_line,
            read_state_keys,
            write_state_keys,
            config_docs,
        )
        files = {filename: content}
    target.mkdir(parents=True)
    created = []
    for filename, content in files.items():
        path = target / filename
        path.write_text(content, encoding="utf-8", newline="\n")
        created.append(path)
    return created


def _next_steps(kind: str, plugin_id: str, target: Path) -> str:
    if kind == "tool":
        return (
            f"后续必做：\n"
            f"  1. tool_ids 登记：把 {plugin_id} 加入目标 agent 的 tool_ids 白名单，\n"
            f"     验证 python scripts/check_agent_tool_ids.py\n"
            f"  2. 有第三方依赖则 uv sync（或 ensure_plugin_venvs.py）\n"
            f"  3. pytest {target}\n"
            f"  4. 替换 TODO 实现并同步 plugin.json schema（契约见\n"
            f"     skills/resource-tool-create/SKILL.md）"
        )
    return (
        f"后续必做：\n"
        f"  1. 补齐 {plugin_id}.py 中 TODO 的管道逻辑与 state 键文档\n"
        f"  2. 补测试（参照 plugins/shared/_libs/ 管道基类测试形态）\n"
        f"  3. 管道步骤接线（config/pipelines/）"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="插件脚手架生成器")
    parser.add_argument("plugin_id", help="插件 id（snake_case）")
    parser.add_argument("--kind", default="tool", help="tool|core|input|output（默认 tool）")
    parser.add_argument("--one-line", required=True, help="一句话描述")
    parser.add_argument("--detailed", default=None, help="详细描述（管道类用，默认同 one-line）")
    parser.add_argument("--target", default=None, help="父目录（默认 <repo>/plugins/shared）")
    parser.add_argument("--repo-root", default=None, help="仓根（默认按脚本位置推导）")
    parser.add_argument("--read-state-keys", default="无")
    parser.add_argument("--write-state-keys", default="无")
    parser.add_argument("--config-docs", default="无")
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root) if args.repo_root else default_repo_root()
    target_parent = Path(args.target) if args.target else repo_root / "plugins" / "shared"
    try:
        created = scaffold_plugin(
            repo_root,
            target_parent,
            args.plugin_id,
            args.kind,
            args.one_line,
            detailed=args.detailed,
            read_state_keys=args.read_state_keys,
            write_state_keys=args.write_state_keys,
            config_docs=args.config_docs,
        )
    except ScaffoldError as exc:
        print(f"脚手架失败: {exc}", file=sys.stderr)
        return 1
    plugin_dir = target_parent / args.plugin_id
    print(f"已生成 {len(created)} 个文件于 {plugin_dir}:")
    for path in created:
        print(f"  {path.relative_to(plugin_dir)}")
    print(_next_steps(args.kind, args.plugin_id, plugin_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
