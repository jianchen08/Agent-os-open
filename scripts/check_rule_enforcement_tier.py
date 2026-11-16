"""规范执法档位检查器（方案 AI辅助编程体系落地AgentOS_20261022 §3.6 件 4）。

"规范都变成 CI"的制度化执法：每条规范立项必须标注三档执法位之一——
(a) mechanical 机械闸（可完全机械判定 → run_gates/lint/schema）；
(b) metric 评估指标（半机械 → evaluation_metrics）；
(c) review 审查清单（需语义判断 → 审查 AI 法定检查项）。
无执法档位的规范不得入库——规范面从此没有"纯倡议"条目。

执法三件套：
1. 注册表 config/rules/enforcement_tiers.yaml：文件 → 档位 + 执法载体；
2. 基线棘轮 .github/enforcement-tier-baseline.txt：存量未登记文件放行清单，
   只减不增（触碰即清：改到哪个规则文件就登记哪个并从基线移除）；
3. 防悬空引用：mechanical 档的 gate id 必须在 run_gates GATES 中存在、
   metric 档的指标 id 必须在 evaluation_metrics.yaml 中存在、
   review_docs 路径必须存在——CI 也是 CI 的被检对象。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VALID_TIERS = {"mechanical", "metric", "review", "mixed"}


def validate_registry(
    rules: list[dict],
    gate_ids: set[str],
    metric_ids: set[str],
    root: Path,
) -> list[str]:
    """校验注册表条目：档位合法性 + 三档语义 + 防悬空引用。返回错误列表。"""
    errors: list[str] = []
    seen: set[str] = set()
    for entry in rules:
        path = str(entry.get("path", ""))
        tier = str(entry.get("tier", ""))
        gates = entry.get("gates") or []
        metrics = entry.get("metrics") or []
        review_docs = entry.get("review_docs") or []

        if tier not in VALID_TIERS:
            errors.append(f"非法执法档位 tier={tier!r}（合法：mechanical/metric/review/mixed）: {path}")
            continue
        if path in seen:
            errors.append(f"注册表重复登记: {path}")
        seen.add(path)

        has_mech = tier in ("mechanical", "mixed")
        has_metric = tier in ("metric", "mixed")
        if has_mech and not gates:
            errors.append(f"tier={tier} 须给出 gates（机械执法面）: {path}")
        if has_metric and not metrics:
            errors.append(f"tier={tier} 须给出 metrics（评估指标面）: {path}")
        if tier == "review" and (gates or metrics):
            errors.append(f"tier=review 却声明 gates/metrics（档位矛盾）: {path}")

        for gid in gates:
            if gid not in gate_ids:
                errors.append(f"gates 引用悬空（run_gates 无此门禁）: {gid} @ {path}")
        for mid in metrics:
            if mid not in metric_ids:
                errors.append(f"metrics 引用悬空（evaluation_metrics.yaml 无此指标）: {mid} @ {path}")
        for doc in review_docs:
            if not (root / str(doc)).exists():
                errors.append(f"review_docs 路径不存在: {doc} @ {path}")
    return errors


def check_coverage(
    rules_dir: Path,
    registered: set[str],
    baseline: set[str],
) -> list[str]:
    """覆盖校验（路径相对 rules_dir）：规则目录每个 .md 必须在注册表或基线。

    三类违规按序报出：
    1. 新规则文件未登记且不在基线（无档位入库）；
    2. 已登记档位的路径仍残留基线（棘轮只减不增——防"登记后取消登记"
       回退靠基线蒙混）；
    3. 基线中指向已不存在文件的失效条目（该清未清）。
    """
    actual = {p.relative_to(rules_dir).as_posix() for p in rules_dir.rglob("*.md")}
    errors: list[str] = []
    for f in sorted(actual - registered - baseline):
        errors.append(f"新规则文件未登记执法档位（注册表或基线均无）: {f}")
    for f in sorted(registered & baseline):
        errors.append(f"基线棘轮违规：已登记档位的路径须从基线移除（只减不增）: {f}")
    for f in sorted(baseline - actual - registered):
        errors.append(f"基线失效条目（规则文件已不存在）: {f}")
    return errors


def load_metric_ids(text: str) -> set[str]:
    """从评估指标 YAML 文本提取指标 id 集合。

    结构：活体 ``config/plugins/evaluation/evaluation_metrics.yaml`` 的
    ``metrics:`` 为**条目列表**（``- name: <id>``，含 L0 三指标），dict 形态
    为兼容保留（历史/测试喂入）；无该包裹键时兜底取顶层键集。
    """
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - .venv 环境自带 pyyaml
        raise SystemExit("check_rule_enforcement_tier: 需要 pyyaml（.venv 环境自带）") from exc
    data = yaml.safe_load(text) or {}
    metrics = data.get("metrics")
    if isinstance(metrics, dict):
        return {str(k) for k in metrics}
    if isinstance(metrics, list):
        return {str(entry["name"]) for entry in metrics if isinstance(entry, dict) and entry.get("name")}
    return {str(k) for k in data}


def load_gate_ids() -> set[str]:
    """run_gates --list --json 取门禁 id 全集（CLI 合同，不 import 不重抄）。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_gates.py"), "--list", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"check_rule_enforcement_tier: run_gates --list --json 失败: {proc.stderr}")
    return {str(g["id"]) for g in json.loads(proc.stdout)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="规范执法档位注册表检查（无档位的规范不得入库）")
    parser.add_argument("--rules-dir", default=str(ROOT / "config" / "rules"), help="规则文件目录")
    parser.add_argument(
        "--registry",
        default=str(ROOT / "config" / "rules" / "enforcement_tiers.yaml"),
        help="执法档位注册表",
    )
    parser.add_argument(
        "--baseline",
        default=str(ROOT / ".github" / "enforcement-tier-baseline.txt"),
        help="存量未登记基线（棘轮只减不增）",
    )
    parser.add_argument(
        "--metrics-file",
        default=str(ROOT / "config" / "plugins" / "evaluation" / "evaluation_metrics.yaml"),
        help="评估指标定义文件（防悬空引用校验源）",
    )
    parser.add_argument(
        "--baseline-monotonic",
        action="store_true",
        help="显式声明基线棘轮执法（与 file-length 同款接口形态）",
    )
    parser.add_argument("--init", action="store_true", help="打印当前未登记文件清单（生成基线用）")
    args = parser.parse_args(argv)

    try:
        import yaml
    except ImportError:  # pragma: no cover
        print("check_rule_enforcement_tier: 需要 pyyaml", file=sys.stderr)
        return 1

    registry = yaml.safe_load(Path(args.registry).read_text(encoding="utf-8")) or {}
    rules: list[dict] = registry.get("rules") or []
    registered = {str(r.get("path", "")) for r in rules}

    baseline_path = Path(args.baseline)
    baseline_text = baseline_path.read_text(encoding="utf-8") if baseline_path.exists() else ""
    baseline = {
        line.strip() for line in baseline_text.splitlines() if line.strip() and not line.strip().startswith("#")
    }

    if args.init:
        rules_dir = Path(args.rules_dir)
        actual = {p.relative_to(rules_dir).as_posix() for p in rules_dir.rglob("*.md")}
        for f in sorted(actual - registered):
            print(f)
        return 0

    errors = validate_registry(
        rules,
        load_gate_ids(),
        load_metric_ids(Path(args.metrics_file).read_text(encoding="utf-8")),
        ROOT,
    )
    errors += check_coverage(Path(args.rules_dir), registered, baseline)

    if errors:
        print(
            f"check_rule_enforcement_tier: {len(errors)} 项违规（无档位的规范不得入库）：",
            file=sys.stderr,
        )
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(
        f"check_rule_enforcement_tier: {len(rules)} 条已登记 + "
        f"{len(baseline)} 条基线豁免（棘轮），全部档位合法、引用无悬空（通过）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
