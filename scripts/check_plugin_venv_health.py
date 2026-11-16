#!/usr/bin/env python3
"""插件关键链 venv 健康门禁（check-only，fail-closed；打包/CI 前置）。

背景（2026-10-23 装机实证，用户令「打包 CI 之前必须保证所有插件关键链路
完整、能够运行、至少一轮 LLM」）：用户空间 llm_core 1.2.0 的 .venv「目录
存在」但 SDK editable 指向 10-22 布局迁移前的死路径，sidecar spawn 即
ModuleNotFoundError，主管道 LLM 补全通道死循环退避——「venv 目录存在」≠
「venv 可用」。

检查对象 = ensure_plugin_venvs.iter_targets（关键链选形单一真值：_host 恒在 +
有 plugin.json+pyproject 的独立插件；合宿成员经 _host 共享 venv 覆盖）。
健康判据零复制，直接复用其 venv_interpreter_path / pyproject_declares_sdk /
sdk_importable。

分级语义：
  .venv 在场但不健康 → FAIL（漂移实证；修法 = python scripts/ensure_plugin_venvs.py）
  .venv 缺席         → 默认记 absent 跳过（CI 冷检出形态，供给责任在 e2e
                       建链步 / ensure 供给）；--strict 下 FAIL（打包前本地
                       形态要求关键链全供给）。

用法：
  python scripts/check_plugin_venv_health.py [--root plugins/shared] [--strict] [--json]

退出码：0 = 无 unhealthy（strict 下另要求无 absent）；1 = 有 FAIL 项。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import ensure_plugin_venvs as epv

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "plugins" / "shared"


def check_targets(
    root: Path,
    import_probe=None,
    strict: bool = False,
) -> tuple[list[str], list[str], list[str]]:
    """检查 iter_targets 全集，返回 (healthy, absent, unhealthy) 三个名单。

    import_probe 注入供测试（透传 sdk_importable 的 runner 形参）；strict 仅
    影响 absent 的归桶（strict 时 absent 计入 unhealthy 语义面由调用方聚合）。
    """
    healthy: list[str] = []
    absent: list[str] = []
    unhealthy: list[str] = []
    for target in epv.iter_targets(root):
        venv = target / ".venv"
        if not venv.is_dir():
            (unhealthy if strict else absent).append(f"{target.name}: .venv 未供给")
            continue
        interpreter = epv.venv_interpreter_path(target)
        needs_sdk = epv.pyproject_declares_sdk(target)
        if not interpreter.is_file():
            unhealthy.append(f"{target.name}: 解释器缺失（{interpreter}）")
            continue
        if needs_sdk and not epv.sdk_importable(interpreter, runner=import_probe):
            unhealthy.append(f"{target.name}: SDK 不可 import（陈旧/残缺 venv）")
            continue
        healthy.append(target.name)
    return healthy, absent, unhealthy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="插件根（默认 <repo>/plugins/shared）")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="absent（.venv 未供给）也记 FAIL（打包前本地形态）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 汇总")
    args = parser.parse_args()

    healthy, absent, unhealthy = check_targets(args.root, strict=args.strict)
    if args.json:
        print(json.dumps({"healthy": healthy, "absent": absent, "unhealthy": unhealthy}, ensure_ascii=False))
    for name in healthy:
        print(f"  [OK] {name}")
    for item in absent:
        print(f"  [SKIP] {item}")
    for item in unhealthy:
        print(f"  [BROKEN] {item}")
    print(
        f"[venv-health] 关键链目标 {len(healthy) + len(absent) + len(unhealthy)}"
        f"：健康 {len(healthy)} / 未供给 {len(absent)} / 不健康 {len(unhealthy)}"
    )
    if unhealthy:
        print(
            "[FAIL] 上述插件关键链 venv 不可用——对应插件 spawn 即崩（502/死通道）。\n"
            "  修复：python scripts/ensure_plugin_venvs.py（补装+断言）后重跑本门禁。",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
