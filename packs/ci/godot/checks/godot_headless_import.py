#!/usr/bin/env python3
# @feature: FP-0.2.〇 项目类型 CI 包
"""headless 导入门（godot/block）：资源引用断裂/脚本解析错即红——编译权威门。

godot 二进制解析序：run argv 烘焙值（--godot，出生探测）→ env GODOT_BIN → PATH；
全缺 = skip（exit 2）。导出/导入行为：`godot --headless --import`（Godot 4）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

from _common import PROJECT_ROOT

TIMEOUT_SECONDS = 300


def resolve_godot(explicit: str) -> str:
    if explicit:
        return explicit
    env_bin = os.environ.get("GODOT_BIN", "").strip()
    if env_bin:
        return env_bin
    return shutil.which("godot") or ""


def _print_tail(stdout: bytes, stderr: bytes, limit: int = 20) -> None:
    """打印工具输出尾部（UTF-8 宽松解码，不依赖机器 locale）。"""
    text = stdout.decode("utf-8", "replace") + stderr.decode("utf-8", "replace")
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    for line in lines[-limit:]:
        print(f"  {line}")


def main() -> int:
    parser = argparse.ArgumentParser(description="godot headless 导入门")
    parser.add_argument("--godot", default="", help="godot 二进制路径（出生组装烘焙）")
    args = parser.parse_args()
    godot = resolve_godot(args.godot)
    if not godot:
        print("skip: godot 二进制缺席（探测序 烘焙值→GODOT_BIN→PATH）——导入门本提交跳过")
        return 2
    try:
        out = subprocess.run(
            [godot, "--headless", "--import"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(f"导入超时（>{TIMEOUT_SECONDS}s）")
        return 1
    except OSError as exc:
        print(f"无法启动 godot（{godot}）: {exc}")
        return 1
    if out.returncode != 0:
        print(f"headless 导入失败（退出码 {out.returncode}）——输出尾部：")
        _print_tail(out.stdout, out.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
