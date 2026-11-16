#!/usr/bin/env python3
"""功能性插件骨架生成器（插件开发技能 skills/plugin-dev 自带脚本）。

与 skill-dev-toolkit 的 scaffold_plugin.py 分工：那边管通用 tool/管道类；本脚本管
**按交付物/前端面类型**的功能性骨架（多文件集：plugin.json + 资产 + server.py +
验收测试）。模板内嵌（形态对齐 scaffold_plugin.render_tool_files），契约抄真实判例：
- theme：contributes.themes 声明 + HTTP 资产递送（判例 plugins/shared/dsh_adapter）；
- page：contributes.pages + 自包含 HTML + http_endpoints 供页（判例
  plugins/shared/mode_evolution 面板，CSP 自包含契约）。

诚实边界：settings_panel / interaction 两类未提供——前端消费面契约未真源化，
不写没把握的声明结构（ADR 2026-11-03 §D5）。

用法：
  python skills/plugin-dev/scripts/scaffold_functional.py my_theme \\
    --kind theme --one-line "一句话描述" --target <staging 父目录>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from string import Template

PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_KINDS = ("theme", "page")


class ScaffoldError(Exception):
    """脚手架失败（参数非法/目标已存在）。"""


# ── theme：contributes.themes + CSS 资产递送（判例 dsh_adapter） ─────────────

_THEME_MANIFEST = Template("""{
  "id": "$plugin_id",
  "name": "$class_name",
  "description": "$one_line",
  "version": "0.1.0",
  "plugin_type": "tool",
  "language": "python",
  "host_type": "sidecar",
  "entry": "python server.py",
  "capabilities": {
    "tools": []
  },
  "http_endpoints": [
    {
      "route_id": "${plugin_id}_theme_css",
      "method": "GET",
      "path": "/ext/$plugin_id/styles/theme.css",
      "auth": "user",
      "handler_capability": "http.handle",
      "timeout_ms": 5000,
      "max_concurrency": 8,
      "description": "$one_line（主题 CSS 递送）"
    }
  ],
  "contributes": {
    "themes": [
      {
        "id": "$theme_id",
        "name": "$class_name",
        "description": "$one_line",
        "base": "light",
        "variables": {
          "--ds-bg-canvas": "#f5f6fa",
          "--ds-bg-elevated": "#ffffff",
          "--ds-bg-panel": "#ffffff",
          "--ds-accent-primary": "#4a5fa8",
          "--ds-accent-ai": "#4a5fa8",
          "--ds-bg-hover": "color-mix(in srgb, #4a5fa8 12%, transparent)",
          "--ds-text-primary": "#1d2539",
          "--ds-text-secondary": "color-mix(in srgb, #1d2539 78%, #f5f6fa)",
          "--ds-text-muted": "color-mix(in srgb, #1d2539 55%, #f5f6fa)",
          "--ds-border-subtle": "color-mix(in srgb, #1d2539 16%, transparent)"
        }
      }
    ]
  },
  "permissions": {},
  "priority": 50,
  "host_group": "light"
}
""")

_THEME_SERVER = Template('''#!/usr/bin/env python3
"""$plugin_id 主题插件服务端：递送包内 theme.css（HttpHandleResponse 契约）。

http.handle 为运行面注册（http_endpoints.handler_capability 指向），不进 plugin.json
capabilities.tools（LLM 声明面）——dsh_adapter 判例形态。
"""

from __future__ import annotations

import base64
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("$plugin_id")

_THEME_CSS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "theme.css")

_HTTP_HANDLE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "method": {"type": "string"},
        "plugin_id": {"type": "string"},
        "raw_body": {"type": "string"},
        "headers": {"type": "object"},
        "query": {"type": "object"},
    },
}


def handle(path: str = "", method: str = "GET") -> dict:
    """serve 包内 theme.css（dispatcher 契约：body base64 原样回写）。"""
    if method != "GET" or not path.endswith("/styles/theme.css"):
        return {"status": 404, "headers": {}, "body": "", "body_encoding": "utf-8"}
    with open(_THEME_CSS, "rb") as fh:
        body = fh.read()
    return {
        "status": 200,
        "headers": {"Content-Type": "text/css; charset=utf-8"},
        "body": base64.b64encode(body).decode("ascii"),
        "body_encoding": "base64",
    }


def create_plugin() -> AgentOSPlugin:
    """注册 http.handle 工具（幂等；供独立运行与测试复用）。"""
    plugin.register_tool(
        "http.handle",
        _HTTP_HANDLE_SCHEMA,
        handle,
        "HTTP endpoint handler for /ext/$plugin_id/styles/**",
    )
    return plugin


create_plugin()


def run() -> None:
    """启动 MCP 服务端。"""
    plugin.run()


if __name__ == "__main__":
    run()
''')

_THEME_CSS = Template("""/* $plugin_id 主题资产（contributes.themes.variables 的镜像展开，供直接覆盖调试）。
 * 已知限制：变量键集为最小起步集；前端皮肤运行时的完整 --ds-* 消费面
 * 以 plugins/shared/dsh_adapter 判例与前端主题注入实现为准，改键集前先对账。 */
:root {
  --ds-bg-canvas: #f5f6fa;
  --ds-bg-elevated: #ffffff;
  --ds-bg-panel: #ffffff;
  --ds-accent-primary: #4a5fa8;
  --ds-accent-ai: #4a5fa8;
  --ds-bg-hover: color-mix(in srgb, #4a5fa8 12%, transparent);
  --ds-text-primary: #1d2539;
  --ds-text-secondary: color-mix(in srgb, #1d2539 78%, #f5f6fa);
  --ds-text-muted: color-mix(in srgb, #1d2539 55%, #f5f6fa);
  --ds-border-subtle: color-mix(in srgb, #1d2539 16%, transparent);
}
""")

_THEME_TEST = Template('''"""$plugin_id 主题骨架自验收（声明结构 + CSS 变量集 + handler 信封）。"""

import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent


def _load_server():
    spec = importlib.util.spec_from_file_location("${plugin_id}_server", PLUGIN_DIR / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_theme_declared() -> None:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    # http.handle 是运行面注册（server.py + handler_capability 指向），LLM 声明面为空
    assert manifest["capabilities"]["tools"] == []
    themes = manifest["contributes"]["themes"]
    assert themes and themes[0]["id"] == "$theme_id"
    assert themes[0]["base"] in ("light", "dark")
    assert "--ds-bg-canvas" in themes[0]["variables"]


def test_theme_css_minimum_variables() -> None:
    css = (PLUGIN_DIR / "theme.css").read_text(encoding="utf-8")
    for key in ("--ds-bg-canvas", "--ds-accent-primary", "--ds-text-primary"):
        assert key in css


def test_handle_returns_http_envelope() -> None:
    # SDK 不可达时 handler 验证显式降级为跳过（manifest/CSS 断言不依赖 SDK 不受影响）
    pytest.importorskip("agentos_plugin_sdk")
    server = _load_server()
    resp = server.handle(path="/ext/$plugin_id/styles/theme.css", method="GET")
    assert resp["status"] == 200
    assert resp["body_encoding"] == "base64"
    assert resp["body"]
    assert server.handle(path="/ext/$plugin_id/styles/other.css")["status"] == 404
''')


# ── page：contributes.pages + 自包含 HTML + 供页端点（判例 mode_evolution） ──

_PAGE_MANIFEST = Template("""{
  "id": "$plugin_id",
  "name": "$class_name",
  "description": "$one_line",
  "version": "0.1.0",
  "plugin_type": "tool",
  "language": "python",
  "host_type": "sidecar",
  "entry": "python server.py",
  "capabilities": {
    "tools": []
  },
  "http_endpoints": [
    {
      "route_id": "${plugin_id}_page",
      "method": "GET",
      "path": "/ext/$plugin_id/page/$page_id",
      "auth": "user",
      "handler_capability": "http.handle",
      "timeout_ms": 5000,
      "max_concurrency": 4,
      "description": "Webview HTML page: $one_line"
    }
  ],
  "contributes": {
    "pages": [
      {
        "id": "$page_id",
        "title": "$class_name",
        "icon": "🧩",
        "space": "workspace",
        "slot": "tab",
        "path": "/p/$page_id",
        "widget": "webview",
        "props": {
          "pluginId": "$plugin_id",
          "htmlPath": "/page/$page_id",
          "widgetId": "$page_id"
        },
        "order": 200,
        "description": "$one_line"
      }
    ]
  },
  "permissions": {},
  "priority": 50,
  "host_group": "light"
}
""")

_PAGE_SERVER = Template('''#!/usr/bin/env python3
"""$plugin_id 页面插件服务端：供页包内 page.html（HttpHandleResponse 契约）。

http.handle 为运行面注册（http_endpoints.handler_capability 指向），不进 plugin.json
capabilities.tools（LLM 声明面）——mode_evolution/dsh_adapter 判例形态。
"""

from __future__ import annotations

import base64
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("$plugin_id")

_PAGE_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.html")

_HTTP_HANDLE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "method": {"type": "string"},
        "plugin_id": {"type": "string"},
        "raw_body": {"type": "string"},
        "headers": {"type": "object"},
        "query": {"type": "object"},
    },
}


def handle(path: str = "", method: str = "GET") -> dict:
    """serve 包内 page.html（dispatcher 契约：body base64 原样回写）。"""
    if method != "GET" or not path.endswith("/page/$page_id"):
        return {"status": 404, "headers": {}, "body": "", "body_encoding": "utf-8"}
    with open(_PAGE_HTML, "rb") as fh:
        body = fh.read()
    return {
        "status": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": base64.b64encode(body).decode("ascii"),
        "body_encoding": "base64",
    }


def create_plugin() -> AgentOSPlugin:
    """注册 http.handle 工具（幂等；供独立运行与测试复用）。"""
    plugin.register_tool(
        "http.handle",
        _HTTP_HANDLE_SCHEMA,
        handle,
        "HTTP endpoint handler for /ext/$plugin_id/page/$page_id",
    )
    return plugin


create_plugin()


def run() -> None:
    """启动 MCP 服务端。"""
    plugin.run()


if __name__ == "__main__":
    run()
''')

# 自包含契约（mode_evolution 面板判例）：单 script、禁外链 src/href、禁 fetch/websocket、
# 图标用原生 title、刷新用重新供页——CSP connect-src 'none' 下这些通道全部不可用。
_PAGE_HTML = Template("""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>$class_name</title>
  <!-- 自包含契约：单 script、无外链资源引用、无出网调用（CSP connect-src 'none'
       挡全部动态通道）。
       数据注入走"快照锚点内联"（供页时服务端替换本注释下方锚点），刷新=重新供页。 -->
  <style>
    body { font-family: system-ui, sans-serif; margin: 24px; color: #1d2539; }
    .placeholder { color: #667; border: 1px dashed #99a; padding: 16px; border-radius: 8px; }
  </style>
</head>
<body>
  <h1>$class_name</h1>
  <div class="placeholder">TODO: 替换为真实页面内容（数据快照锚点见上方契约注释）</div>
  <script>
    // 单 script：页面自包含逻辑只此一处；无出网调用，数据由供页端点内联。
    console.log("$plugin_id page loaded");
  </script>
</body>
</html>
""")

_PAGE_TEST = Template('''"""$plugin_id 页面骨架自验收（CSP 自包含契约 + 声明结构 + handler 信封）。"""

import importlib.util
import json
import re
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent

# 禁串清单（mode_evolution 面板同款）：外链资源与出网通道在 CSP 下全部不可用。
_FORBIDDEN = re.compile(r"\\bsrc=|\\bhref=|url\\(|fetch\\(|websocket|XMLHttpRequest|import\\(")


def _load_server():
    spec = importlib.util.spec_from_file_location("${plugin_id}_server", PLUGIN_DIR / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_page_html_self_contained() -> None:
    html = (PLUGIN_DIR / "page.html").read_text(encoding="utf-8")
    assert len(re.findall(r"<script", html)) == 1
    assert not _FORBIDDEN.search(html)


def test_manifest_page_and_endpoint_declared() -> None:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    # http.handle 是运行面注册（server.py + handler_capability 指向），LLM 声明面为空
    assert manifest["capabilities"]["tools"] == []
    pages = manifest["contributes"]["pages"]
    assert pages and pages[0]["widget"] == "webview"
    assert pages[0]["props"]["pluginId"] == "$plugin_id"
    endpoint = manifest["http_endpoints"][0]
    assert endpoint["path"] == "/ext/$plugin_id/page/$page_id"
    assert endpoint["handler_capability"] == "http.handle"


def test_handle_returns_html_envelope() -> None:
    # SDK 不可达时 handler 验证显式降级为跳过（CSP/声明断言不依赖 SDK 不受影响）
    pytest.importorskip("agentos_plugin_sdk")
    server = _load_server()
    resp = server.handle(path="/ext/$plugin_id/page/$page_id", method="GET")
    assert resp["status"] == 200
    assert "text/html" in resp["headers"]["Content-Type"]
    assert server.handle(path="/ext/$plugin_id/page/other", method="GET")["status"] == 404
''')


def derive_class_name(plugin_id: str) -> str:
    """snake_case 插件 id → PascalCase 类名/展示名（my_theme → MyTheme）。"""
    return "".join(part.capitalize() for part in plugin_id.split("_"))


def render_theme_files(plugin_id: str, one_line: str) -> dict[str, str]:
    """主题插件四件套（plugin.json 已通过 json.loads 验证合法）。"""
    fields = {
        "plugin_id": plugin_id,
        "theme_id": f"{plugin_id}-theme",
        "class_name": derive_class_name(plugin_id),
        "one_line": one_line,
    }
    files = {
        "plugin.json": _THEME_MANIFEST.substitute(fields),
        "server.py": _THEME_SERVER.substitute(fields),
        "theme.css": _THEME_CSS.substitute(fields),
        f"test_{plugin_id}.py": _THEME_TEST.substitute(fields),
    }
    json.loads(files["plugin.json"])
    return files


def render_page_files(plugin_id: str, one_line: str, page_id: str) -> dict[str, str]:
    """页面插件四件套（plugin.json 已通过 json.loads 验证合法）。"""
    fields = {
        "plugin_id": plugin_id,
        "page_id": page_id,
        "class_name": derive_class_name(plugin_id),
        "one_line": one_line,
    }
    files = {
        "plugin.json": _PAGE_MANIFEST.substitute(fields),
        "server.py": _PAGE_SERVER.substitute(fields),
        "page.html": _PAGE_HTML.substitute(fields),
        f"test_{plugin_id}.py": _PAGE_TEST.substitute(fields),
    }
    json.loads(files["plugin.json"])
    return files


def scaffold_functional(
    target_parent: Path,
    plugin_id: str,
    kind: str,
    one_line: str,
    page_id: str | None = None,
) -> list[Path]:
    """生成功能性插件目录，返回创建的文件路径列表。目标已存在或 id 非法即拒。"""
    if not PLUGIN_ID_RE.match(plugin_id):
        raise ScaffoldError(
            f"插件 id 非法: {plugin_id!r}（须匹配 {PLUGIN_ID_RE.pattern}，snake_case）"
        )
    if kind not in _KINDS:
        raise ScaffoldError(f"kind 非法: {kind!r}（可选 {', '.join(_KINDS)}）")
    if kind == "page":
        page_id = page_id or plugin_id
        if not PLUGIN_ID_RE.match(page_id):
            raise ScaffoldError(
                f"page id 非法: {page_id!r}（须匹配 {PLUGIN_ID_RE.pattern}，snake_case）——"
                f"page_id 进入端点路径声明，非法字符会产出坏路由"
            )
    target = target_parent / plugin_id
    if target.exists():
        raise ScaffoldError(f"目标目录已存在: {target}")
    if kind == "theme":
        files = render_theme_files(plugin_id, one_line)
    else:
        files = render_page_files(plugin_id, one_line, page_id or plugin_id)
    target.mkdir(parents=True)
    created = []
    for filename, content in files.items():
        path = target / filename
        path.write_text(content, encoding="utf-8", newline="\n")
        created.append(path)
    return created


def _next_steps(kind: str, plugin_id: str, target: Path) -> str:
    common = (
        f"后续必做：\n"
        f"  1. 有第三方依赖则 uv sync（或 ensure_plugin_venvs.py）\n"
        f"  2. pytest {target}（骨架自带自验收）\n"
        f"  3. verify_plugin.py 装载验证（§SKILL.md 五步流水线第②步）"
    )
    if kind == "theme":
        return common + (
            "\n  4. 改 theme.css / contributes.themes.variables 后：verify_plugin_frontend.py"
            "\n     L2 断言变量注入（对比度用 WCAG 比值静态算）"
        )
    return common + (
        "\n  4. 替换 page.html TODO 内容（保持自包含契约：单 script/无外链/无 fetch），"
        "\n     每次改完重跑 pytest（禁串扫描是骨架自带测试）"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="功能性插件骨架生成器（theme/page）")
    parser.add_argument("plugin_id", help="插件 id（snake_case）")
    parser.add_argument("--kind", default="theme", help="theme|page（默认 theme）")
    parser.add_argument("--one-line", required=True, help="一句话描述")
    parser.add_argument("--page-id", default=None, help="页面 id（page kind 用，缺省同插件 id）")
    parser.add_argument("--target", default=None, help="父目录（默认 <repo>/plugins/shared）")
    args = parser.parse_args(argv)
    target_parent = (
        Path(args.target) if args.target
        else Path(__file__).resolve().parents[3] / "plugins" / "shared"
    )
    try:
        created = scaffold_functional(
            target_parent, args.plugin_id, args.kind, args.one_line, args.page_id
        )
    except ScaffoldError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    target = target_parent / args.plugin_id
    print(f"已生成 {len(created)} 个文件 → {target}")
    print(_next_steps(args.kind, args.plugin_id, target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
