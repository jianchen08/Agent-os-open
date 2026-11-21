"""插件 webview 内联脚本语法自检（无浏览器、无第三方依赖）。

背景契约：插件 webview 的 HTML 页面把全部渲染逻辑写在 `<script>` 内联块里，
宿主管道（pipeline 预览 / Electron 视图）只负责灌数据。pytest 不解析 HTML、
前端 lint / vitest 不收集 `plugins/**/webview/*.html`，所以内联脚本里的
JS 语法错误在两类车道里都不可见——页面照常加载出静态骨架，所有数据区块空白，
只在真实浏览器里报一条 `SyntaxError`。本脚本把"提取内联块 + 交给 JS 解析器
校验"这一步机械化，让这类错误在无浏览器环境下就能拦到。

判据：`node --check` 对提取出的脚本块逐个解析，任一非零退出即红（fail-closed）。
node 缺失按环境缺陷处理并退出非零——静默放行等于恢复本脚本要堵的天窗。

用法：
    python scripts/check_webview_inline_script_syntax.py            # 全部 webview
    python scripts/check_webview_inline_script_syntax.py <file>...  # 指定文件
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 无 git 环境时的降级扫描面：只下钻两级，避开插件自带 venv（.venv*/）
DEFAULT_GLOBS = ("plugins/*/webview/*.html", "plugins/*/*/webview/*.html")

# 属性里带 src = 外链脚本（不在本页解析）；带非 JS type = 数据模板块，跳过。
_SCRIPT_BLOCK = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
_ATTR_SRC = re.compile(r"\bsrc\s*=", re.IGNORECASE)
_ATTR_TYPE = re.compile(r"\btype\s*=\s*[\"']?([^\"'\s>]+)", re.IGNORECASE)
_JS_TYPES = {"", "module", "text/javascript", "application/javascript", "text/ecmascript"}


def extract_inline_scripts(html: str) -> list[tuple[int, bool, str]]:
    """提取内联脚本块，返回 ``[(块起始行号, 是否 module, 脚本正文)]``。

    块起始行号 = `<script>` 标签所在行的 1-based 行号，用于把解析报错定位
    回 HTML 文件（脚本体自身行号从该行起算）。
    """
    out: list[tuple[int, bool, str]] = []
    for m in _SCRIPT_BLOCK.finditer(html):
        attrs, body = m.group(1), m.group(2)
        if _ATTR_SRC.search(attrs):
            continue
        type_match = _ATTR_TYPE.search(attrs)
        script_type = type_match.group(1).lower() if type_match else ""
        if script_type not in _JS_TYPES:
            continue
        is_module = script_type == "module"
        start_line = html.count("\n", 0, m.start(0)) + 1
        out.append((start_line, is_module, body))
    return out


def _node_check(body: str, *, is_module: bool) -> tuple[bool, str]:
    """把脚本交给 ``node --check`` 解析；返回 (是否通过, 首条错误文本)。"""
    suffix = ".mjs" if is_module else ".js"
    with tempfile.NamedTemporaryFile("w", suffix=suffix, encoding="utf-8", delete=False) as fh:
        fh.write(body)
        tmp = Path(fh.name)
    try:
        proc = subprocess.run(
            ["node", "--check", str(tmp)], capture_output=True, text=True, check=False
        )
    finally:
        tmp.unlink(missing_ok=True)
    if proc.returncode == 0:
        return True, ""
    # node 的 stderr 形如：<tmp>:<行>\n<源码行>\n ^\nSyntaxError: ...
    detail = [ln.strip() for ln in proc.stderr.splitlines() if ln.strip()]
    for line in detail:
        if line.startswith(("SyntaxError", "ReferenceError", "Error")):
            return False, line
    return False, detail[0] if detail else f"node --check 退出码 {proc.returncode}"


def check_html(path: Path) -> list[str]:
    """校验单个 HTML 文件的内联脚本；返回问题列表（空 = 通过）。"""
    try:
        html = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{path}: 读取失败: {exc}"]
    findings: list[str] = []
    for start_line, is_module, body in extract_inline_scripts(html):
        if not body.strip():
            continue
        ok, detail = _node_check(body, is_module=is_module)
        if not ok:
            findings.append(f"{path}:{start_line}: 内联脚本语法错误: {detail}")
    return findings


def _git_webview_files() -> list[Path]:
    """按 git 跟踪面取 webview HTML（不走文件系统递归）。

    目录递归会在插件自带 venv（`.venv*/Lib/site-packages`）上耗尽内存/触达
    不仅属本仓的路径；跟踪面天然只含本仓源文件，且与 CI 干净检出同口径。
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "plugins"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    if proc.returncode != 0:
        return []
    out: list[Path] = []
    for rel in proc.stdout.split("\0"):
        if rel.endswith(".html") and "/webview/" in rel:
            out.append(ROOT / rel)
    return sorted(out)


def collect_targets(patterns: list[str]) -> list[Path]:
    """收集待检文件；缺省 = 本仓跟踪面内全部 webview HTML（无 git 时降级两级 glob）。"""
    if patterns:
        return [Path(p) for p in patterns]
    tracked = _git_webview_files()
    if tracked:
        return tracked
    found: list[Path] = []
    for pat in DEFAULT_GLOBS:
        found.extend(ROOT.glob(pat))
    return sorted(set(found))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="插件 webview 内联脚本语法自检（node --check）")
    parser.add_argument("paths", nargs="*", help="待检 HTML 路径（缺省 = plugins/**/webview/*.html）")
    args = parser.parse_args(argv)

    if shutil.which("node") is None:
        print("webview-inline-script: FAIL —— 未找到 node，无法做 JS 语法解析（环境缺陷，不静默放行）")
        return 1

    targets = collect_targets(args.paths)
    if not targets:
        print("webview-inline-script: FAIL —— 未匹配到任何 webview HTML 文件")
        return 1

    findings: list[str] = []
    total_blocks = 0
    for path in targets:
        findings.extend(check_html(path))
        try:
            total_blocks += len(extract_inline_scripts(path.read_text(encoding="utf-8")))
        except OSError:
            continue

    if findings:
        for line in findings:
            print(line)
        print(f"webview-inline-script: FAIL —— {len(findings)} 个内联脚本块解析失败")
        return 1

    print(f"webview-inline-script: OK —— {len(targets)} 个文件 / {total_blocks} 个内联脚本块语法通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
