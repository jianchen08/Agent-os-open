"""CI 变更面分档：一次推送该跑哪些车道。

车道模型（2026-09-04 CI 分档改造）：
  full        内核 / CI 工作流 / 门禁脚本变更 → 全量（Rust + Python + 前端全跑）
  python_full 核心面（pipeline 管道插件 / SDK / 依赖锁）→ 全量 Python 车道
  related     非核心插件（system/tools）/ 配置 / 测试面 → 仅变更关联的单元+集成测试
  frontend / electron / endpoints   独立叠加车道（endpoints 为廉价一致性闸）

重车道互斥瀑布：full > python_full > related（一档命中即覆盖低档，
避免同一次推送重跑两套全量）。输出写 GITHUB_OUTPUT（CI）并打印摘要；
workflow_dispatch 或无历史（空仓）→ 全量（fail-open）。
"""

from __future__ import annotations

import argparse
import os
import sys

from ci_common import diff_names, resolve_changed_files

# ── 车道判定的路径规则（前缀 / 精确文件）──────────────────────────────
FULL_PREFIXES = ("kernel/", ".github/workflows/", "scripts/")
FULL_FILES = ("rust-toolchain.toml",)

# 核心面：管道插件承载执行循环、SDK 是插件契约底座、依赖锁影响全部车道。
PYTHON_CORE_PREFIXES = ("plugins/sdk/", "plugins/shared/pipeline/")
PYTHON_CORE_FILES = ("pyproject.toml", "uv.lock")

# 非核心面：叶子能力插件与配置/测试/技能/MCP 清单 → 关联测试车道。
RELATED_PREFIXES = (
    "plugins/shared/system/",
    "plugins/shared/tools/",
    "mcp-servers/",
    "skills/",
    "config/",
    "tests/",
)
RELATED_FILES = ("docker-compose.yml", ".env.example", "pytest.ini")

FRONTEND_PREFIXES = ("frontend/",)
FRONTEND_FILES = ("pnpm-lock.yaml",)

ELECTRON_PREFIXES = ("electron/",)
ELECTRON_FILES = ("package.json", "package-lock.json")

ENDPOINTS_PREFIXES = ("plugins/", "frontend/src/")
ENDPOINTS_FILES = ("scripts/check_frontend_endpoints_sync.py",)

# 文档面：仅此类文件变更时重车道全跳（pre-commit 恒跑，md 格式防线不缩水）。
DOC_SUFFIXES = (".md",)
DOC_FILES = (
    ".gitignore",
    ".gitattributes",
    "LICENSE",
    "NOTICE",
    "AUTHORS.md",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "ROADMAP.md",
    "README.md",
    "README_EN.md",
)


def _hits(files: list[str], prefixes: tuple[str, ...], exact: tuple[str, ...]) -> bool:
    return any(f.startswith(prefixes) or f in exact for f in files)


def _is_doc(f: str) -> bool:
    return f.endswith(DOC_SUFFIXES) or f.startswith("docs/") or f in DOC_FILES


def compute_lanes(files: list[str]) -> dict[str, bool]:
    full = _hits(files, FULL_PREFIXES, FULL_FILES)
    python_core = _hits(files, PYTHON_CORE_PREFIXES, PYTHON_CORE_FILES)
    related_sources = _hits(files, RELATED_PREFIXES, RELATED_FILES)
    return {
        "full": full,
        "python_full": full or python_core,
        "related": (not full and not python_core) and related_sources,
        "frontend": _hits(files, FRONTEND_PREFIXES, FRONTEND_FILES),
        "electron": _hits(files, ELECTRON_PREFIXES, ELECTRON_FILES),
        "endpoints": _hits(files, ENDPOINTS_PREFIXES, ENDPOINTS_FILES),
        "docs_only": bool(files) and all(_is_doc(f) for f in files),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="CI 变更面分档（车道选择）")
    parser.add_argument("--base", help="基线分支/提交（默认 GITHUB_BASE_REF / push 口径）")
    parser.add_argument("--range", help="显式范围 A..B（本地手工模拟，优先于 --base）")
    parser.add_argument("--from-file", help="从文件读变更清单（每行一个路径），跳过 git diff")
    args = parser.parse_args()

    desc = "workflow_dispatch / 手动触发"
    if args.from_file:
        with open(args.from_file, encoding="utf-8") as fh:
            files = [line.strip().replace("\\", "/") for line in fh if line.strip()]
        desc = f"--from-file {args.from_file}"
    elif args.range:
        a, _, b = args.range.partition("..")
        files = diff_names(a.strip(), (b.strip() or "HEAD"))
        desc = f"--range {args.range}"
    elif os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        files = []
    else:
        resolved = resolve_changed_files(args.base or "")
        if resolved is None:
            files = []
            desc = "无可解析范围（空仓）→ 全量"
        else:
            files, desc = resolved

    if files:
        lanes = compute_lanes(files)
    else:
        # workflow_dispatch / 空仓：手动触发语义 = 全量验证，全车道点亮。
        lanes = dict.fromkeys(
            ("full", "python_full", "related", "frontend", "electron", "endpoints", "docs_only"),
            True,
        )
        lanes["docs_only"] = False

    lines = [f"{k}={'true' if v else 'false'}" for k, v in lanes.items()]
    out_file = os.environ.get("GITHUB_OUTPUT")
    if out_file:
        with open(out_file, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    print(f"[ci-lanes] 变更范围: {desc}（{len(files)} 个文件）")
    print("[ci-lanes] " + " ".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
