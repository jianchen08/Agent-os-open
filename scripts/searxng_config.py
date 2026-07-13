"""
生成 SearXNG 自定义配置：开启 JSON 格式 + 禁用被阻断引擎 + 启用可达引擎。

采用最小化文本 patch（保留原始文件结构和注释），避免 PyYAML 整体重写
导致的配置加载异常。

背景：本机网络对部分域名 TLS 中断（duckduckgo/wikipedia/pypi 等），
SearXNG 默认引擎大量报 HTTP connection error。经探测 baidu/bing/sogou 可达，
但官方默认禁用了它们，需显式启用。

用法:
  python scripts/searxng_config.py docker/searxng/settings.yml

幂等：可重复运行。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


# 网络探测确认被 TLS 阻断的引擎族（域名 TLS 握手被中断）
BLOCKED_ENGINES = {
    "duckduckgo", "duckduckgo images", "duckduckgo videos",
    "duckduckgo news", "duckduckgo weather", "ddg definitions",
    "wikipedia",
    "startpage", "startpage news", "startpage images",
    "aol", "aol images", "aol videos",
    "brave", "brave.images", "brave.videos", "brave.news", "braveapi",
    "karmasearch", "karmasearch images", "karmasearch videos", "karmasearch news",
    "google", "google images", "google news", "google videos",
    "google scholar", "google play apps", "google play movies",
}

# 网络可达但被官方默认禁用的引擎 —— 显式启用
FORCE_ENABLE_ENGINES = {
    "baidu",
    "bing",
    "sogou",
}


def _find_engine_block(lines: list[str], name: str) -> tuple[int, int] | None:
    """定位引擎块 [start, end)。start 是 '- name: xxx' 行号，end 是下一个块开始或文件尾。"""
    pattern = re.compile(rf"^  - name: {re.escape(name)}\s*$")
    start = None
    for i, line in enumerate(lines):
        if pattern.match(line):
            start = i
            break
    if start is None:
        return None
    # 找块结束：下一个 "  - name:" 或顶层 key
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^  - name: ", lines[j]) or re.match(r"^[a-zA-Z#]", lines[j]):
            end = j
            break
    return (start, end)


def patch_config(path: Path) -> None:
    """最小化文本 patch：开 JSON + 禁用阻断引擎 + 启用可达引擎。"""
    with path.open(encoding="utf-8") as f:
        lines = f.readlines()

    changes: list[str] = []

    # 1. 开启 JSON 格式：在 "  formats:" 后的列表里加 "    - json"
    formats_idx = None
    for i, line in enumerate(lines):
        if re.match(r"^  formats:\s*$", line):
            formats_idx = i
            break
    if formats_idx is not None:
        # 检查是否已有 - json
        block_end = formats_idx + 1
        has_json = False
        while block_end < len(lines) and re.match(r"^    - ", lines[block_end]):
            if lines[block_end].strip() == "- json":
                has_json = True
            block_end += 1
        if not has_json:
            lines.insert(formats_idx + 1, "    - json\n")
            changes.append("formats: added - json")

    # 2. 处理引擎：先处理 force-enable（移除 disabled），再处理 blocked（加 disabled）
    #    逆序插入避免行号偏移
    insertions: list[tuple[int, str]] = []  # (行号, 内容)
    enable_count = 0
    disable_count = 0

    for name in FORCE_ENABLE_ENGINES:
        loc = _find_engine_block(lines, name)
        if loc is None:
            continue
        start, end = loc
        for j in range(start + 1, end):
            if re.match(r"^    disabled:\s*true\s*$", lines[j]):
                lines[j] = "    disabled: false\n"
                enable_count += 1
                changes.append(f"force-enabled: {name}")
                break

    for name in BLOCKED_ENGINES:
        loc = _find_engine_block(lines, name)
        if loc is None:
            continue
        start, end = loc
        # 检查是否已有 disabled
        already = any(re.match(r"^    disabled:", lines[j]) for j in range(start + 1, end))
        if not already:
            insertions.append((start + 1, "    disabled: true\n"))
            disable_count += 1

    # 逆序插入
    for idx, content in sorted(insertions, key=lambda x: -x[0]):
        lines.insert(idx, content)
    if disable_count:
        changes.append(f"disabled {disable_count} blocked engines")

    with path.open("w", encoding="utf-8") as f:
        f.writelines(lines)

    for c in changes:
        print(f"[patch] {c}")
    print(f"[done] force-enabled={enable_count}, disabled={disable_count}, wrote {path}")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docker/searxng/settings.yml")
    if not target.exists():
        print(f"ERROR: {target} 不存在", file=sys.stderr)
        sys.exit(1)
    patch_config(target)
