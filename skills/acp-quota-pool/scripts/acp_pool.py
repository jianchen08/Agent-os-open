#!/usr/bin/env python3
"""acp_pool —— ACP 额度池 CLI（派发 + 记账面报表）。

用法（工作空间内相对寻址，技能包随会话播种到 skills/ 下）::

    python skills/acp-quota-pool/scripts/acp_pool.py dispatch --goal "目标全文" \
        [--provider opencode] [--cwd DIR] [--session-id SID] [--json]
    python skills/acp-quota-pool/scripts/acp_pool.py report [--json]

依赖：``agent-client-protocol``（无则 ``uv run --with agent-client-protocol==0.12.1
python ...``）。注册表 = 同目录 providers.toml（--registry 可覆盖）；
账本 = 用户空间（AGENTOS_DATA_DIR / AGENTOS_USER_ROOT 可覆盖，--ledger 可覆盖）。
退出码：dispatch status=failed → 1；配置/环境错误 → 2。
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from acp_engine import DispatchRequest, dispatch  # noqa: E402
from acp_ledger import QuotaLedger, default_ledger_path  # noqa: E402
from acp_registry import ProviderSpec, load_registry_file  # noqa: E402

_DEFAULT_REGISTRY = _SCRIPT_DIR / "providers.toml"

__all__ = ["main"]


def _pick_provider(registry: dict[str, ProviderSpec], provider: str) -> ProviderSpec:
    """provider 选择：显式指定按名取；省略时仅注册表恰一家可用（M0 无路由）。"""
    if provider:
        spec = registry.get(provider)
        if spec is None:
            known = ", ".join(sorted(registry)) or "(空)"
            raise ValueError(f"unknown provider '{provider}'（已注册: {known}）")
        return spec
    if len(registry) == 1:
        return next(iter(registry.values()))
    raise ValueError(
        f"provider 省略需注册表恰一家（路由策略未接线），当前注册 {len(registry)} 家"
        "——请用 --provider 显式指定"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="acp_pool", description="ACP 额度池：派发 + 记账面报表")
    sub = parser.add_subparsers(dest="command", required=True)

    p_dispatch = sub.add_parser("dispatch", help="把一个目标派给 provider（ACP 回合）")
    p_dispatch.add_argument("--goal", required=True, help="派发目标全文（一次写全）")
    p_dispatch.add_argument("--provider", default="", help="provider id；省略 = 唯一注册家")
    p_dispatch.add_argument("--cwd", default="", help="agent 工作目录；省略 = 当前目录")
    p_dispatch.add_argument("--session-id", default="", help="ACP 会话 id（续做）；省略 = 新会话")
    p_dispatch.add_argument("--timeout", type=float, default=0, help="单回合秒数上限（缺省用注册表值）")
    p_dispatch.add_argument("--registry", default=str(_DEFAULT_REGISTRY), help="注册表 TOML 路径")
    p_dispatch.add_argument("--ledger", default="", help="账本 JSON 路径（缺省用户空间）")
    p_dispatch.add_argument("--json", action="store_true", help="输出 JSON")

    p_report = sub.add_parser("report", help="per-provider 额度记账面报表（v0 只报账本）")
    p_report.add_argument("--registry", default=str(_DEFAULT_REGISTRY), help="注册表 TOML 路径")
    p_report.add_argument("--ledger", default="", help="账本 JSON 路径（缺省用户空间）")
    p_report.add_argument("--json", action="store_true", help="输出 JSON")
    return parser


def _ledger(ledger_arg: str) -> QuotaLedger:
    return QuotaLedger(Path(ledger_arg) if ledger_arg else default_ledger_path())


def _report_rows(registry: dict[str, ProviderSpec], ledger: QuotaLedger) -> list[dict[str, Any]]:
    by_ledger = {row["provider"]: row for row in ledger.report()}
    rows: list[dict[str, Any]] = []
    for pid in sorted(registry):
        spec = registry[pid]
        account = by_ledger.get(pid, {})
        wall = account.get("last_wall_hit") or {}
        rows.append(
            {
                "provider": pid,
                "name": spec.name,
                "cost_class": spec.cost_class,
                "strength": spec.strength,
                "quota_model": spec.quota_model,
                "dispatch_total": account.get("dispatch_total", 0),
                "ok_total": account.get("ok_total", 0),
                "last_session_id": account.get("last_session_id", ""),
                "last_dispatch_at": account.get("last_dispatch_at", ""),
                "last_status": account.get("last_status", ""),
                "wall_hit": bool(account.get("wall_hit_total", 0)),
                "wall_hit_total": account.get("wall_hit_total", 0),
                "last_wall_hit_class": wall.get("error_class", ""),
                "last_wall_hit_at": wall.get("ts", ""),
            }
        )
    return rows


def _print_report(rows: list[dict[str, Any]]) -> None:
    header = f"{'provider':<12} {'cost':<6} {'strength':<11} {'派发':>4} {'ok':>4} {'撞墙':>4}  最近会话 / 最近状态 / 额度模型"
    print(header)
    for row in rows:
        print(
            f"{row['provider']:<12} {row['cost_class']:<6} {row['strength']:<11} "
            f"{row['dispatch_total']:>4} {row['ok_total']:>4} {row['wall_hit_total']:>4}  "
            f"{row['last_session_id'] or '-'} / {row['last_status'] or '-'} / {row['quota_model']}"
        )


def _cmd_dispatch(args: argparse.Namespace) -> int:
    registry = load_registry_file(args.registry)
    spec = _pick_provider(registry, args.provider)
    if args.timeout > 0:
        spec = dataclasses.replace(spec, timeout_seconds=args.timeout)
    result = asyncio.run(
        dispatch(
            DispatchRequest(
                goal=args.goal,
                cwd=args.cwd or os.getcwd(),
                session_id=args.session_id,
            ),
            spec,
            _ledger(args.ledger),
        )
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(f"provider:   {result['provider']}")
        print(f"session_id: {result['session_id'] or '-'}")
        print(f"status:     {result['status']}")
        print(f"summary:    {result['summary']}")
    return 0 if result["status"] == "ok" else 1


def _cmd_report(args: argparse.Namespace) -> int:
    registry = load_registry_file(args.registry)
    rows = _report_rows(registry, _ledger(args.ledger))
    if args.json:
        print(json.dumps({"providers": rows}, ensure_ascii=False, indent=1))
    else:
        _print_report(rows)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "dispatch":
            return _cmd_dispatch(args)
        return _cmd_report(args)
    except (ValueError, RuntimeError, OSError) as exc:
        # 配置/环境面错误：stderr 报错 + 退出码 2（与派发 failed=1 区分）
        print(f"acp_pool: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
