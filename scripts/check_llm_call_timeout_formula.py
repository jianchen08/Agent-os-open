#!/usr/bin/env python3
"""L3 复合界公式化校验器（LLM 链；ADR 2026-11-05-timeout-taxonomy-and-merge）。

设计真值：docs/working/超时体系分类设计_20261105.md §4.2 L3 / §5.3——一步内含
N 次尝试（LLM 兜底链在单次调用内逐跳完整走 B1-B6 一遍）时：

    call_timeout_seconds >= 跳数 × max(B1, B2, B3) + 跳数 × B5 + 余量

- 跳数 = 1（主模型尝试）+ len(fallback_chain[tier])（该档兜底尝试数）；
  多档（tiers.fallback_chain 多键）取所需最大档——manifest 单值覆盖全档。
- B1/B2/B3 = llm.yaml defaults 的 stream_idle_timeout / call_timeout /
  first_token_timeout（域内权威缺省；模型级覆盖不进本口径——链是 defaults
  概念，按缺省档保守计算）。
- B5 = key 槽排队界（plugins/shared/llm/key_pool.py ``acquire_slot(timeout=…)``
  常量，只读参照；``--slot-wait-secs`` 可覆写，缺省 60）。
- 余量 = ``--margin-secs``，缺省 60（与 L4 外部黑盒余量示例同值）。

对表对象：plugins/shared/llm_core/plugin.json 的
``capabilities.call_timeout_seconds``（M2 单声明 = 步壳 B10 界 + 传输 A3 派生源）。
另附 M2 倒挂守卫：manifest 若同时声明 ``mcp.request_timeout_secs`` 且小于
``call_timeout_seconds`` → 红（兜底链全程 > 传输界即复发 600s 同族症状——
llm_core 900<1800 倒挂的回归面；单声明形态下键缺席 = 直接绿）。

本闸让「改 llm.yaml 兜底链长度 / 跳数」时门禁强制提醒同步重算 B8（manifest
声明值）——链变长而声明未重算即红，禁止静默截断。

用法：python scripts/check_llm_call_timeout_formula.py
      （门禁执法 = tests/gates/test_check_llm_call_timeout_formula.py 车道）
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = ROOT / "plugins" / "shared" / "llm_core" / "plugin.json"
DEFAULT_LLM_YAML = ROOT / "config" / "plugins" / "llm" / "llm.yaml"


@dataclass
class FormulaInputs:
    """公式输入与对表值（测试直接构造，不碰文件面）。"""

    fallback_chain: dict[str, list[str]]
    stream_idle_timeout: float
    call_timeout: float
    first_token_timeout: float
    slot_wait_secs: float
    margin_secs: float
    manifest_call_timeout_seconds: float | None
    manifest_request_timeout_secs: float | None


@dataclass
class FormulaVerdict:
    """对表结论：required=公式要求值；violation 非空 = 违规描述（逐条）。"""

    hops: int
    per_hop_bound: float
    required: float
    declared: float | None
    violations: list[str]


def required_call_timeout(inputs: FormulaInputs) -> FormulaVerdict:
    """L3 公式求值 + 倒挂守卫（纯函数，测试面直接消费）。"""
    violations: list[str] = []
    per_hop_bound = max(inputs.stream_idle_timeout, inputs.call_timeout, inputs.first_token_timeout)
    # 跳数 = 1 主尝试 + 该档兜底数；无链声明 = 主尝试单跳。多档取最大需求档
    # （manifest 单值覆盖全档）。
    hops = max((1 + len(chain) for chain in inputs.fallback_chain.values()), default=1)
    required = hops * per_hop_bound + hops * inputs.slot_wait_secs + inputs.margin_secs
    declared = inputs.manifest_call_timeout_seconds
    if declared is None:
        violations.append("llm_core manifest 缺 capabilities.call_timeout_seconds 声明（M2 单声明键）")
    elif declared < required:
        violations.append(
            f"call_timeout_seconds={declared:g}s < L3 公式要求 {required:g}s"
            f"（{hops} 跳 × max(B1,B2,B3)={per_hop_bound:g}s + {hops} × B5="
            f"{inputs.slot_wait_secs:g}s + 余量 {inputs.margin_secs:g}s）——"
            "改兜底链/单跳界后须同步重算 B8"
        )
    # M2 倒挂守卫：传输界 < 调用需求 = 传输先掐、自报成死键。
    if (
        inputs.manifest_request_timeout_secs is not None
        and declared is not None
        and inputs.manifest_request_timeout_secs < declared
    ):
        violations.append(
            f"mcp.request_timeout_secs={inputs.manifest_request_timeout_secs:g}s < "
            f"call_timeout_seconds={declared:g}s（M2 倒挂：传输界先掐，自报成死键）"
        )
    return FormulaVerdict(
        hops=hops,
        per_hop_bound=per_hop_bound,
        required=required,
        declared=declared,
        violations=violations,
    )


def _llm_defaults(llm_yaml: Path) -> dict[str, Any]:
    import yaml

    doc = yaml.safe_load(llm_yaml.read_text(encoding="utf-8")) or {}
    defaults = doc.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise SystemExit(f"{llm_yaml}: defaults 段缺失或非映射")
    return defaults


def _manifest_declared(manifest_path: Path) -> tuple[float | None, float | None]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    caps = manifest.get("capabilities") or {}
    call_need = caps.get("call_timeout_seconds")
    mcp = manifest.get("mcp") or {}
    request_timeout = mcp.get("request_timeout_secs")
    return (
        float(call_need) if call_need else None,
        float(request_timeout) if request_timeout else None,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="llm_core plugin.json 路径")
    parser.add_argument("--llm-yaml", type=Path, default=DEFAULT_LLM_YAML, help="llm.yaml 路径")
    parser.add_argument(
        "--slot-wait-secs",
        type=float,
        default=60,
        help="B5 key 槽排队界（key_pool.acquire_slot 常量；改常量时同步传值）",
    )
    parser.add_argument("--margin-secs", type=float, default=60, help="余量（秒）")
    args = parser.parse_args(argv)

    defaults = _llm_defaults(args.llm_yaml)
    chain = (defaults.get("tiers") or {}).get("fallback_chain") or {}
    if not isinstance(chain, dict):
        raise SystemExit(f"{args.llm_yaml}: defaults.tiers.fallback_chain 缺失或非映射")
    declared, request_timeout = _manifest_declared(args.manifest)
    verdict = required_call_timeout(
        FormulaInputs(
            fallback_chain=chain,
            stream_idle_timeout=float(defaults.get("stream_idle_timeout", 0)),
            call_timeout=float(defaults.get("call_timeout", 0)),
            first_token_timeout=float(defaults.get("first_token_timeout", 0)),
            slot_wait_secs=args.slot_wait_secs,
            margin_secs=args.margin_secs,
            manifest_call_timeout_seconds=declared,
            manifest_request_timeout_secs=request_timeout,
        )
    )
    if verdict.violations:
        for v in verdict.violations:
            print(f"L3 公式违规：{v}", file=sys.stderr)
        return 1
    print(
        f"L3 公式合规：call_timeout_seconds={verdict.declared:g}s ≥ "
        f"{verdict.hops} 跳 × {verdict.per_hop_bound:g}s + 排队 + 余量 "
        f"= {verdict.required:g}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
