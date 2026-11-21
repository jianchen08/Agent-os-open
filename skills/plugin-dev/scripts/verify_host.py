#!/usr/bin/env python3
"""宿主验证编排器：后端装载验证 + 前端浏览器车道（同一 scratch 实例）。

定位（ADR 2026-11-03 §D3/D4）：隔离环境（WSL/Docker）里的编码 agent 没有可执行的
宿主 Windows 内核；由**宿主**跑完整验证，隔离环境经命令转发拿到证据——WSL 场景
就是 interop（`verify_host.sh` 包一层 `wslpath` + 落盘 .bat），Docker 场景才需要
额外 broker。

流程：
  1. verify_plugin(keep_running=True) → 三根实例 + 后端断言（tools/load-manifest/schema）；
  2. 同一实例上跑前端 L2（frontend_probe.cjs）：静态伺服前端 dist + 反代
     /api、/ext、/ws 到实例端口 → Playwright 登录（注入 refresh_token）→ 截图；
  3. 实例精确 stop，后端 + 前端证据合并落 <plugin_dir>/verify_host_evidence.json。

用法（宿主直接跑）：
  python skills/plugin-dev/scripts/verify_host.py <plugin_dir> [--workspace W]
      [--frontend] [--frontend-dist D] [--no-backend]
      [--kernel-bin EXE] [--config-root D] [--base-plugins-root D]
      [--scratch-root D] [--timeout S] [--node NODE_EXE]

隔离环境（WSL）：
  bash skills/plugin-dev/scripts/verify_host.sh <plugin_dir> --frontend
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parents[2]
_FRONTEND_PROBE = _SCRIPT_DIR / "frontend_probe.cjs"


class HostVerifyError(Exception):
    """宿主验证编排失败（前端 dist/node 缺失、探针自身错误等）。"""


def _load_verify_plugin():
    """importlib 加载同目录 verify_plugin（避免脚本目录不在 sys.path 的导入漂移）。"""
    script = _SCRIPT_DIR / "verify_plugin.py"
    spec = importlib.util.spec_from_file_location("verify_plugin", script)
    if spec is None or spec.loader is None:
        raise HostVerifyError(f"无法构造 verify_plugin spec: {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_plugin"] = module
    spec.loader.exec_module(module)
    return module


def resolve_frontend_dist(
    workspace: Path, kernel_bin: str | None = None, explicit: str | None = None
) -> Path:
    """定位前端 dist（含 index.html）。

    优先级：显式 --frontend-dist > 装机 <resources>/frontend/dist（kernel_bin
    直指安装位时）> workspace/frontend/dist > 仓根 frontend/dist。
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if kernel_bin:
        candidates.append(Path(kernel_bin).resolve().parents[1] / "frontend" / "dist")
    candidates.append(Path(workspace) / "frontend" / "dist")
    candidates.append(_REPO_ROOT / "frontend" / "dist")
    seen: list[str] = []
    for candidate in candidates:
        key = str(candidate.resolve())
        if key in seen:
            continue
        seen.append(key)
        if (candidate / "index.html").is_file():
            return Path(key)
    raise HostVerifyError(
        "找不到前端 dist（需含 index.html）：已试 "
        f"{seen}；先构建（npm run build --prefix frontend）或用 --frontend-dist 指定"
    )


def resolve_repo_root(workspace: Path) -> Path:
    """浏览器探针按 frontend/package.json 解析 playwright：优先 workspace 仓根。"""
    if (Path(workspace) / "frontend" / "package.json").is_file():
        return Path(workspace).resolve()
    return _REPO_ROOT


def resolve_node(explicit: str | None = None) -> str:
    node = explicit or shutil.which("node") or shutil.which("node.exe")
    if not node:
        raise HostVerifyError("找不到 node（浏览器车道需要 Node + frontend/node_modules/playwright）")
    return str(node)


def run_frontend_probe(
    plugin_dir: Path,
    workspace: Path,
    instance: dict[str, Any],
    dist_dir: Path,
    node_bin: str | None = None,
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    """跑 frontend_probe.cjs，返回 result.json（含 checks/screenshots/error）。"""
    if not _FRONTEND_PROBE.is_file():
        raise HostVerifyError(f"缺少前端探针脚本: {_FRONTEND_PROBE}")
    out_dir = plugin_dir / "verify_frontend_evidence"
    out_dir.mkdir(parents=True, exist_ok=True)
    port = int(instance["port"])
    config = {
        "repo_root": str(resolve_repo_root(workspace)),
        "dist_dir": str(dist_dir),
        "out_dir": str(out_dir.resolve()),
        "plugin_dir": str(plugin_dir.resolve()),
        "kernel_base": f"http://127.0.0.1:{port}",
        "kernel_host": "127.0.0.1",
        "kernel_port": port,
        "admin_password": str(instance.get("admin_password") or ""),
    }
    config_path = out_dir / "probe_config.json"
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n"
    )
    node = resolve_node(node_bin)
    proc = subprocess.run(
        [node, str(_FRONTEND_PROBE), str(config_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout_s, check=False, cwd=str(workspace),
    )
    result_path = out_dir / "result.json"
    if not result_path.is_file():
        raise HostVerifyError(
            f"前端探针未产出 result.json（exit={proc.returncode}）："
            f"{(proc.stderr or proc.stdout).strip()[:500]}"
        )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["exit_code"] = proc.returncode
    if proc.returncode != 0 and not result.get("error") and not result.get("checks"):
        result["error"] = (proc.stderr or proc.stdout).strip()[:500] or f"exit {proc.returncode}"
    return result


def verify_host(
    plugin_dir: Path,
    workspace: Path,
    frontend: bool = False,
    backend: bool = True,
    frontend_dist: str | None = None,
    scratch_root: Path | None = None,
    timeout_s: float = 120.0,
    kernel_bin: str | None = None,
    config_root: str | None = None,
    base_plugins_root: str | None = None,
    node_bin: str | None = None,
) -> dict[str, Any]:
    """后端 + 前端串跑，返回并落盘合并证据。"""
    vp = _load_verify_plugin()
    checks: list[dict[str, Any]] = []
    backend_evidence: dict[str, Any] | None = None
    frontend_result: dict[str, Any] | None = None
    instance: dict[str, Any] | None = None
    spawn_kernel = vp._load_spawn_test_kernel()

    try:
        if backend:
            backend_evidence = vp.verify_plugin(
                plugin_dir, workspace,
                scratch_root=scratch_root,
                timeout_s=timeout_s,
                kernel_bin=kernel_bin,
                config_root=config_root,
                base_plugins_root=base_plugins_root,
                keep_running=True,
            )
            instance = backend_evidence.get("instance")
            checks.append({
                "name": "backend",
                "pass": bool(backend_evidence.get("all_pass")),
                "detail": f"checks={len(backend_evidence.get('checks') or [])}（明细见 verify_evidence.json）",
            })
        if frontend:
            if not instance:
                raise HostVerifyError("前端车道需要后端 scratch 实例（不要同时用 --no-backend）")
            dist_dir = resolve_frontend_dist(workspace, kernel_bin, frontend_dist)
            frontend_result = run_frontend_probe(
                plugin_dir, workspace, instance, dist_dir,
                node_bin=node_bin, timeout_s=max(timeout_s, 180.0),
            )
            checks.extend(frontend_result.get("checks") or [])
            if frontend_result.get("error"):
                checks.append({
                    "name": "frontend_probe",
                    "pass": False,
                    "detail": str(frontend_result["error"]),
                })
    finally:
        if instance:
            try:
                spawn_kernel.stop(Path(instance["scratch_root"]), instance.get("run_id"))
                checks.append({"name": "instance_cleanup", "pass": True,
                               "detail": "已 stop（端口释放已验证）"})
            except Exception as exc:  # noqa: BLE001 - 清场失败必须可见
                checks.append({"name": "instance_cleanup", "pass": False,
                               "detail": f"stop 失败（需手工清场）: {exc}"})

    evidence: dict[str, Any] = {
        "plugin_id": (backend_evidence or {}).get("plugin_id")
        or vp.read_declared_tools(plugin_dir)["id"],
        "all_pass": all(c.get("pass") for c in checks),
        "checks": checks,
        # 回传/落盘脱敏：scratch admin 口令只在前端探针调用期间留在内存。
        "backend": vp.redact_instance_secret(backend_evidence) if backend_evidence else None,
        "frontend": frontend_result,
    }
    evidence_path = plugin_dir / "verify_host_evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n"
    )
    evidence["evidence_path"] = str(evidence_path)
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="宿主验证编排器（后端 + 前端浏览器车道）")
    parser.add_argument("plugin_dir", help="待验证插件目录（staging 内，宿主路径）")
    parser.add_argument("--workspace", default=None, help="内核工作区（默认仓根）")
    parser.add_argument("--frontend", action="store_true", help="跑前端浏览器车道（Playwright + 截图）")
    parser.add_argument("--frontend-dist", default=None, help="前端 dist 目录（缺省按 workspace/装机包推导）")
    parser.add_argument("--no-backend", action="store_true", help="跳过后端装载验证（仅前端，需已有实例——不支持）")
    parser.add_argument("--kernel-bin", default=None, help="内核 exe（装机包内二进制）")
    parser.add_argument("--config-root", default=None, help="配置根（含 kernel/plugin_tiers.yaml）")
    parser.add_argument("--base-plugins-root", default=None, help="内置插件根（Tier1 + 共享件）")
    parser.add_argument("--scratch-root", default=None, help="scratch 目录（默认插件同级 .verify_scratch）")
    parser.add_argument("--timeout", type=float, default=120.0, help="实例就绪超时秒")
    parser.add_argument("--node", default=None, help="node 可执行文件（缺省 PATH 查找）")
    args = parser.parse_args(argv)
    workspace = Path(args.workspace) if args.workspace else _REPO_ROOT
    try:
        evidence = verify_host(
            Path(args.plugin_dir), workspace,
            frontend=args.frontend,
            backend=not args.no_backend,
            frontend_dist=args.frontend_dist,
            scratch_root=Path(args.scratch_root) if args.scratch_root else None,
            timeout_s=args.timeout,
            kernel_bin=args.kernel_bin,
            config_root=args.config_root,
            base_plugins_root=args.base_plugins_root,
            node_bin=args.node,
        )
    except Exception as exc:  # noqa: BLE001 - CLI 边界统一转退出码
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(evidence, ensure_ascii=False, indent=1))
    return 0 if evidence["all_pass"] else 2


if __name__ == "__main__":
    sys.exit(main())
