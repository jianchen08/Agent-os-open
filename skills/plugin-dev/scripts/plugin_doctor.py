#!/usr/bin/env python3
"""插件装载诊断器（插件开发技能 skills/plugin-dev 自带脚本）。

回答高频排障问题"插件为什么没生效"：五查一次出报告，逐项判定 + 修复指引。
①声明完整性 ②tool_ids 白名单登记 ③venv 健康 ④permissions 最小授权
⑤实例装载状态（可选：--base-url 给了才查）。

默认纯本地静态（零实例依赖），可在起实例前先跑。

用法：
  python skills/plugin-dev/scripts/plugin_doctor.py <plugin_dir> [--repo-root R] [--base-url URL]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class DoctorError(Exception):
    """诊断前置失败（目录非法）。"""


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "pass": passed, "detail": detail}


def check_manifest(plugin_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """查①声明完整性：G2 同款必填面（id/name/version/entry + tools 六字段契约）。

    返回 (check, manifest)。manifest 缺失/不可解析时 check.fail 且 manifest={}。
    """
    path = plugin_dir / "plugin.json"
    if not path.is_file():
        return _check("manifest", False, f"缺 plugin.json: {path}"), {}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _check("manifest", False, f"plugin.json 不可解析: {exc}"), {}
    missing_top = [k for k in ("id", "name", "version", "entry") if not manifest.get(k)]
    if missing_top:
        return (
            _check("manifest", False, f"缺顶层必填字段: {missing_top}"),
            manifest,
        )
    problems: list[str] = []
    for tool in manifest.get("capabilities", {}).get("tools", []):
        name = tool.get("name") or "<未命名>"
        missing = [f for f in ("name", "description", "input_schema", "output_schema", "render")
                   if not tool.get(f)]
        if missing:
            problems.append(f"工具 {name} 缺 {missing}（tool_core fail-closed，缺声明不入面）")
    detail = "声明完整" if not problems else "；".join(problems)
    return _check("manifest", not problems, detail), manifest


def check_tool_ids(plugin_dir: Path, repo_root: Path,
                   manifest: dict[str, Any]) -> dict[str, Any]:
    """查②tool_ids 白名单：插件声明的工具是否登记进任一 agent 配置。"""
    tool_names = [
        str(t.get("name"))
        for t in manifest.get("capabilities", {}).get("tools", [])
        if t.get("name")
    ]
    if not tool_names:
        return _check("tool_ids", True, "无 LLM 工具声明（功能性插件走 http.handle），跳过")
    try:
        import yaml
    except ImportError:
        return _check("tool_ids", False, "PyYAML 不可用，无法核对 agent 配置（pip install pyyaml）")
    registered: set[str] = set()
    scanned = 0
    for pattern in ("config/agents/*/agentos.yaml", "config/agents/*/*.yaml"):
        for cfg in sorted(repo_root.glob(pattern)):
            scanned += 1
            try:
                data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            except OSError:
                continue
            def _walk(node: Any) -> None:
                if isinstance(node, dict):
                    ids = node.get("tool_ids")
                    if isinstance(ids, list):
                        registered.update(str(x) for x in ids)
                    for value in node.values():
                        _walk(value)
                elif isinstance(node, list):
                    for item in node:
                        _walk(item)
            _walk(data)
    missing = [t for t in tool_names if t not in registered]
    detail = (
        f"全部登记（扫描 {scanned} 个 agent 配置）" if not missing
        else f"未登记进任何 agent 的 tool_ids（扫描 {scanned} 个配置）: {missing}——"
             f"不登记则 LLM 工具面不可见；验证 python scripts/check_agent_tool_ids.py"
    )
    return _check("tool_ids", not missing, detail)


def check_venv(plugin_dir: Path) -> dict[str, Any]:
    """查③venv 健康：有第三方依赖（pyproject 声明依赖）则 .venv 必须在位。"""
    pyproject = plugin_dir / "pyproject.toml"
    if not pyproject.is_file():
        return _check("venv", True, "无 pyproject.toml（零第三方依赖），跳过")
    has_deps = "[project.dependencies]" in pyproject.read_text(encoding="utf-8",
                                                               errors="replace") \
        or "dependencies = [" in pyproject.read_text(encoding="utf-8", errors="replace")
    venv_ready = (plugin_dir / ".venv" / "pyvenv.cfg").is_file()
    if not has_deps:
        return _check("venv", True, "pyproject 无依赖声明，venv 非必需")
    return _check(
        "venv", venv_ready,
        ".venv 在位" if venv_ready else
        "有依赖声明但 .venv 缺失——uv sync 或 ensure_plugin_venvs.py 供给；"
        "dev 侧 502 排障：直接复制仓内插件 .venv 即愈",
    )


def check_permissions(manifest: dict[str, Any]) -> dict[str, Any]:
    """查④permissions 最小授权：filesystem 通配符给警告级提示（不判 fail）。"""
    perms = manifest.get("permissions", {})
    warnings: list[str] = []
    fs = perms.get("filesystem", {}) if isinstance(perms, dict) else {}
    for side in ("read_paths", "write_paths"):
        if "*" in (fs.get(side) or []):
            warnings.append(f"filesystem.{side} 含 '*'（最小授权纪律：确有必要才通配）")
    return _check(
        "permissions", True,
        "授权最小" if not warnings else "；".join(warnings) + "（警告不拦截，按需收窄）",
    )


def check_instance(base_url: str, plugin_id: str) -> dict[str, Any]:
    """查⑤实例装载状态（可选）：实例 /api/v1/tools 面内是否可见插件工具。"""
    try:
        with urllib.request.urlopen(f"{base_url}/api/v1/tools", timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, urllib.error.URLError) as exc:
        return _check("instance", False, f"实例不可达（{base_url}）: {exc}")
    items = payload.get("items") if isinstance(payload, dict) else []
    blob = json.dumps(items, ensure_ascii=False)
    return _check(
        "instance", plugin_id in blob,
        "实例面内发现插件贡献" if plugin_id in blob
        else f"实例面内未发现 {plugin_id}（实例可能在插件装载前启动；重启实例或查 kernel.log）",
    )


def doctor(
    plugin_dir: Path,
    repo_root: Path | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """跑五查，返回报告 JSON（verdict = 硬检查全过）。"""
    if not plugin_dir.is_dir():
        raise DoctorError(f"插件目录不存在: {plugin_dir}")
    root = repo_root or plugin_dir
    checks: list[dict[str, Any]] = []
    manifest_check, manifest = check_manifest(plugin_dir)
    checks.append(manifest_check)
    if manifest:
        checks.append(check_tool_ids(plugin_dir, root, manifest))
        checks.append(check_permissions(manifest))
    else:
        checks.append(_check("tool_ids", False, "跳过（manifest 缺失）"))
        checks.append(_check("permissions", False, "跳过（manifest 缺失）"))
    checks.append(check_venv(plugin_dir))
    if base_url:
        plugin_id = str(manifest.get("id") or plugin_dir.name)
        checks.append(check_instance(base_url, plugin_id))
    hard_fail = [c["name"] for c in checks if not c["pass"]
                 and c["name"] != "permissions"]
    return {
        "plugin_dir": str(plugin_dir),
        "verdict": "healthy" if not hard_fail else "unhealthy",
        "hard_fail": hard_fail,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="插件装载诊断器（五查报告）")
    parser.add_argument("plugin_dir", help="插件目录（staging 或已装副本）")
    parser.add_argument("--repo-root", default=None, help="仓根（默认按脚本位置推导）")
    parser.add_argument("--base-url", default=None, help="可选：运行中实例地址，查⑤装载状态")
    args = parser.parse_args(argv)
    repo_root = (
        Path(args.repo_root) if args.repo_root
        else Path(__file__).resolve().parents[3]
    )
    try:
        report = doctor(Path(args.plugin_dir), repo_root, args.base_url)
    except DoctorError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["verdict"] == "healthy" else 2


if __name__ == "__main__":
    sys.exit(main())
