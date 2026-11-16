#!/usr/bin/env python
"""发布待检器（全客户端统一发布管线的 CI 载体消费口）。

判定语义：**一个 VERSION 值 = 一次发布**（bump VERSION 是发布的唯一触发方式，
各端禁自 bump）。VERSION 对应的 release/<VERSION>/delivery_manifest.json 存在
且其 version.semver 与 VERSION 一致 → 已发布；否则 → 待发布。

退出码：
  0  = 已发布（无需动作）
  10 = 待发布（CI 轮应执行 deploy/release_all.sh）
  2  = 环境不可判（VERSION 缺失/格式非法——fail-closed，不当作待发布静默放过）

用法（CI 推送轮触发器消费）：
    python scripts/check_release_pending.py            # 人读结论行
    python scripts/check_release_pending.py --quiet    # 只看退出码
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

EXIT_UP_TO_DATE = 0
EXIT_PENDING = 10
EXIT_ERROR = 2


def check_pending(root: Path) -> tuple[str, bool, str]:
    """返回 (VERSION 值, 是否待发布, 说明)。环境不可判时抛 ValueError。"""
    version_file = root / "VERSION"
    if not version_file.is_file():
        raise ValueError(f"VERSION 文件缺失: {version_file}")
    version = version_file.read_text(encoding="utf-8").strip()
    if not SEMVER_RE.match(version):
        raise ValueError(f"VERSION 值 {version!r} 非严格三段式 X.Y.Z")

    manifest_path = root / "release" / version / "delivery_manifest.json"
    if not manifest_path.is_file():
        return version, True, f"release/{version}/delivery_manifest.json 不存在（该版本未发布过）"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return version, True, f"delivery_manifest.json 不可解析（{exc}）——按待发布处理"
    semver = (manifest.get("version") or {}).get("semver")
    if semver != version:
        return version, True, f"manifest version.semver={semver!r} != VERSION {version!r}"
    return version, False, f"release/{version}/ 已发布（build_head={manifest.get('build_head', '?')[:12]}）"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="只出退出码，不出结论行")
    args = parser.parse_args()

    try:
        version, pending, reason = check_pending(ROOT)
    except ValueError as exc:
        if not args.quiet:
            print(f"[release-pending][ERROR] {exc}", file=sys.stderr)
        return EXIT_ERROR
    if not args.quiet:
        state = "PENDING" if pending else "UP-TO-DATE"
        print(f"[release-pending] {state} VERSION={version}：{reason}")
    return EXIT_PENDING if pending else EXIT_UP_TO_DATE


if __name__ == "__main__":
    sys.exit(main())
