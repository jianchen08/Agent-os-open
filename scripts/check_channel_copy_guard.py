#!/usr/bin/env python
"""渠道共享拷贝守卫（channel copy guard）——channel_common 模块名黑名单比对。

单一事实源（C1 合流）：

  - plugins/shared/system/channel_common/   —— 适配器三件
  - agentos_plugin_sdk.pipeline_types       —— 管道类型（SDK 子集归并）

复制模式回潮的典型路径：新渠道开发时"顺手"从老渠道目录拷一份基础模块。
本守卫做机械化拦截：

  1. 黑名单 = channel_common/ 下全部模块名 ∪ 四个历史拷贝名
     （pipeline_types / input_adapter / output_adapter / base_combo_adapter）；
  2. 扫描 plugins/shared/system/channel_*/（channel_common 自身除外）顶层
     *.py，文件名（去扩展）命中黑名单 → 退出码 1，逐条列出；
  3. 自检：channel_common 缺失或为空 → 退出码 1（守卫空转变成本身就是错）。

用法：
    python scripts/check_channel_copy_guard.py          # 绿：退出 0
    # 负样本演示：手工复制一份应被抓红——
    cp plugins/shared/system/channel_common/input_adapter.py \
       plugins/shared/system/channel_qq/input_adapter.py
    python scripts/check_channel_copy_guard.py          # 红：退出 1，列出违规
    rm plugins/shared/system/channel_qq/input_adapter.py

门禁归属：run_gates.py 的 channel-copy-guard gate（fast），
CI 经 .github/workflows/ci.yml python-coverage job 调度。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYSTEM_DIR = ROOT / "plugins" / "shared" / "system"
CHANNEL_COMMON_DIR = SYSTEM_DIR / "channel_common"

# 历史拷贝名（C1 合流前的四类基础模块；pipeline_types 已归并 SDK，渠道目录同样不得再现）
HISTORICAL_COPY_NAMES = frozenset(
    {
        "pipeline_types",
        "input_adapter",
        "output_adapter",
        "base_combo_adapter",
    }
)


def build_blacklist() -> frozenset[str]:
    """黑名单 = channel_common 模块名 ∪ 历史拷贝名。

    Raises:
        SystemExit: channel_common 缺失或无模块时（守卫空转防呆）。
    """
    if not CHANNEL_COMMON_DIR.is_dir():
        print(f"[channel-copy-guard] FAIL: 共享包目录不存在: {CHANNEL_COMMON_DIR}")
        raise SystemExit(1)
    names = {p.stem for p in CHANNEL_COMMON_DIR.glob("*.py") if p.stem != "__init__"}
    if not names:
        print(f"[channel-copy-guard] FAIL: 共享包目录无模块（守卫空转）: {CHANNEL_COMMON_DIR}")
        raise SystemExit(1)
    return frozenset(names | HISTORICAL_COPY_NAMES)


def find_violations(blacklist: frozenset[str]) -> list[Path]:
    """扫描全部 channel_* 目录（channel_common 除外），返回命中黑名单的文件。"""
    violations: list[Path] = []
    for channel_dir in sorted(SYSTEM_DIR.glob("channel_*")):
        if not channel_dir.is_dir() or channel_dir.name == CHANNEL_COMMON_DIR.name:
            continue
        for py in sorted(channel_dir.glob("*.py")):
            if py.stem in blacklist:
                violations.append(py)
    return violations


def main() -> int:
    """守卫主入口：绿 → 0；违规/守卫失效 → 1。"""
    blacklist = build_blacklist()
    print(f"[channel-copy-guard] 黑名单（{len(blacklist)} 名）: {', '.join(sorted(blacklist))}")

    violations = find_violations(blacklist)
    if violations:
        print(f"[channel-copy-guard] FAIL: 检出 {len(violations)} 处渠道目录拷贝回潮：")
        for v in violations:
            print(f"  - {v.relative_to(ROOT)}")
        print(
            "[channel-copy-guard] 渠道公共模块请从共享包/SDK 导入"
            "（server.py 已 sys.path.append channel_common），不要复制文件。"
            "见 docs/working/渠道合流C1C2与CLI插件化方案_20260819.md §1.3 第 6 步。"
        )
        return 1

    print("[channel-copy-guard] OK: channel_* 目录无共享模块拷贝（黑名单 0 命中）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
