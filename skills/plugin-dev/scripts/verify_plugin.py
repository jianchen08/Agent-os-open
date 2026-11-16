#!/usr/bin/env python3
"""插件装载验证编排器（插件开发技能 skills/plugin-dev 自带脚本）。

五步流转管线第②步（ADR 2026-11-03 §D2）：对 staging 内插件起 scratch 内核实例
（三隔离），跑后端断言链，输出证据 JSON：

  1. plugin.json 结构预检（本地，零实例）；
  2. spawn scratch 实例（复用 skill-dev-toolkit.spawn_test_kernel 三隔离；
     AGENTOS_PLUGINS_DIR 经 base 环境指向插件父目录）；
  3. /health 就绪（spawn 内部轮询）；
  4. GET /api/v1/tools → 断言插件声明的工具出现在 items；
  5. GET /api/v1/schema → 声明面证据（ui_schema 含插件贡献——弱断言：响应含插件 id）；
  6. 证据 JSON 落 staging（逐项 pass/fail + 响应摘录），实例精确 stop。

plugin_check（内核声明校验 bin）为可选步：--plugin-check-bin 给了就跑，缺省跳过并标注。

用法：
  python skills/plugin-dev/scripts/verify_plugin.py <plugin_dir> \\
    [--workspace <repo_root>] [--kernel-bin <exe>] [--scratch-root D] [--timeout S]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_DEV_TOOLKIT_SCRIPTS = (
    Path(__file__).resolve().parents[3] / "skills" / "skill-dev-toolkit" / "scripts"
)


class VerifyError(Exception):
    """验证编排失败（目录非法/无法起实例/断言前置缺失）。"""


def _load_spawn_test_kernel():
    """importlib 加载 skill-dev-toolkit 的 spawn_test_kernel（指针复用，不复制）。"""
    script = _DEV_TOOLKIT_SCRIPTS / "spawn_test_kernel.py"
    if not script.is_file():
        raise VerifyError(f"找不到 spawn_test_kernel.py: {script}")
    spec = importlib.util.spec_from_file_location("spawn_test_kernel", script)
    if spec is None or spec.loader is None:
        raise VerifyError(f"无法构造模块 spec: {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["spawn_test_kernel"] = module
    spec.loader.exec_module(module)
    return module


def read_declared_tools(plugin_dir: Path) -> dict[str, Any]:
    """读 plugin.json，返回 {id, tools: [name...]}；缺声明即抛 VerifyError。"""
    manifest_path = plugin_dir / "plugin.json"
    if not manifest_path.is_file():
        raise VerifyError(f"插件目录缺 plugin.json: {plugin_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerifyError(f"plugin.json 不可解析: {exc}") from exc
    plugin_id = manifest.get("id")
    if not plugin_id:
        raise VerifyError("plugin.json 缺 id")
    tools = [
        str(t.get("name"))
        for t in manifest.get("capabilities", {}).get("tools", [])
        if t.get("name")
    ]
    return {"id": str(plugin_id), "tools": tools}


def http_get_json(url: str, timeout_s: float = 10.0,
                  token: str | None = None) -> dict[str, Any]:
    """GET 并解析 JSON；网络失败抛 VerifyError（fail-visible）。

    /api/v1/tools、/api/v1/schema 在认证中间件后，需 Bearer（首跑匿名必 401
    ——snippet_box 实测缺陷①，2026-11-03 修）。
    """
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise VerifyError(f"GET {url} → HTTP {exc.code}（401=token 失效或未带）") from exc
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        raise VerifyError(f"GET {url} 失败: {exc}") from exc


def _make_junction(link: Path, target: Path) -> None:
    """建目录 junction（Windows mklink /J，POSIX os.symlink 兜底）；已存在跳过。

    形态借 dsh_adapter/server.py 判例。
    """
    if link.exists() or link.is_symlink():
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=False, capture_output=True,
        )


def build_user_plugins_view(
    staging_parent: Path, plugin_id: str, cache_dir: Path
) -> Path:
    """干净用户根视图：junction 只含被测插件（snippet_box 实测终版姿势）。

    AGENTOS_USER_PLUGINS_DIR 指向本视图（user_space.rs:34 双源 BuiltinFirst，
    内置根不动）。视图隔离 staging 内他人在飞插件与 scratch 残渣——用户根
    被 kernel 整目录扫描，脏目录（_tmp 半成品/scratch 残渣）会污染装载。
    """
    root = cache_dir / ".verify_user_plugins"
    if root.exists():
        shutil.rmtree(root)  # junction 为 reparse 点，rmtree 只删链接不递归目标
    root.mkdir(parents=True)
    _make_junction(root / plugin_id, staging_parent / plugin_id)
    return root


def login_token(base_url: str, admin_password: str, timeout_s: float = 10.0) -> str:
    """admin 登录换 Bearer token（口令来自 spawn 实例档案）。"""
    req = urllib.request.Request(
        f"{base_url}/api/v1/auth/login",
        data=json.dumps({"username": "admin", "password": admin_password}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError, KeyError) as exc:
        raise VerifyError(f"登录失败（{base_url}）: {exc}") from exc
    token = body.get("access_token")
    if not token:
        raise VerifyError(f"登录响应缺 access_token: {str(body)[:120]}")
    return str(token)


def check_tools_visible(base_url: str, expected_tools: list[str],
                        token: str | None = None) -> dict[str, Any]:
    """断言插件声明的工具出现在 /api/v1/tools items（{items,total} 信封）。"""
    payload = http_get_json(f"{base_url}/api/v1/tools", token=token)
    items = payload.get("items") if isinstance(payload, dict) else None
    names = {
        str(t.get("name"))
        for t in items
        if isinstance(t, dict) and t.get("name")
    } if isinstance(items, list) else set()
    missing = [t for t in expected_tools if t not in names]
    return {
        "name": "tools_visible",
        "pass": not missing and bool(expected_tools),
        "detail": (
            f"声明 {len(expected_tools)} 个工具，缺失 {missing}；面内共 {len(names)}"
            if missing else f"声明 {len(expected_tools)} 个工具全部可见"
        ),
    }


def check_schema_declared(base_url: str, plugin_id: str,
                          token: str | None = None) -> dict[str, Any]:
    """/api/v1/schema 声明面证据（弱断言：响应含插件 id 字符串）。

    诚实边界：ui_schema 内部结构待核，本轮以"响应可解析且含插件 id"为证据，
    原始摘录随证据 JSON 落盘供人工复核。
    """
    payload = http_get_json(f"{base_url}/api/v1/schema", token=token)
    blob = json.dumps(payload, ensure_ascii=False)
    found = plugin_id in blob
    return {
        "name": "schema_declared",
        "pass": found,
        "detail": (
            f"schema 响应含插件 id {plugin_id!r}（弱断言；摘录 "
            f"{blob[:200]}…）" if found else
            f"schema 响应未发现插件 id {plugin_id!r}（摘录 {blob[:200]}…）"
        ),
    }


def verify_plugin(
    plugin_dir: Path,
    workspace: Path,
    scratch_root: Path | None = None,
    timeout_s: float = 120.0,
    plugin_check_bin: str | None = None,
    kernel_bin: str | None = None,
) -> dict[str, Any]:
    """跑完整断言链，返回证据 JSON（调用方负责落盘）。失败抛 VerifyError。"""
    meta = read_declared_tools(plugin_dir)
    checks: list[dict[str, Any]] = []

    checks.append({
        "name": "manifest_precheck",
        "pass": True,
        "detail": f"id={meta['id']}, 声明工具={meta['tools'] or '（无——功能性插件走 http.handle）'}",
    })

    if plugin_check_bin:
        import subprocess
        proc = subprocess.run(
            [plugin_check_bin, "--root", str(plugin_dir.parent)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        checks.append({
            "name": "plugin_check",
            "pass": proc.returncode == 0,
            "detail": (proc.stdout or proc.stderr).strip()[:500] or "exit 0",
        })
    else:
        checks.append({
            "name": "plugin_check",
            "pass": True,
            "detail": "跳过（--plugin-check-bin 未提供；G2 同款校验由内核装载期执行）",
        })

    spawn_kernel = _load_spawn_test_kernel()
    scratch = scratch_root or plugin_dir.parent / ".verify_scratch"
    # 插件根语义（snippet_box 实测终版）：干净用户根视图 + AGENTOS_USER_PLUGINS_DIR
    # （双源 BuiltinFirst，内置根不动）。AGENTOS_PLUGINS_DIR 是单值替换语义，直指
    # staging 缺内置依赖必拒启——勿用。
    view = build_user_plugins_view(
        plugin_dir.parent.resolve(), meta["id"], scratch.parent
    )
    record = spawn_kernel.spawn(
        workspace,
        scratch_root=str(scratch),
        timeout_s=timeout_s,
        kernel_bin=kernel_bin,
        env_extra={"AGENTOS_USER_PLUGINS_DIR": str(view.resolve())},
    )
    port = record["port"]
    base_url = f"http://127.0.0.1:{port}"
    try:
        checks.append({
            "name": "instance_spawn",
            "pass": True,
            "detail": f"scratch 实例就绪 port={port} run_id={record.get('run_id')}",
        })
        token = login_token(base_url, str(record.get("admin_password") or ""))
        checks.append(check_tools_visible(base_url, meta["tools"], token=token))
        checks.append(check_schema_declared(base_url, meta["id"], token=token))
    finally:
        try:
            spawn_kernel.stop(scratch, record.get("run_id"))
            stopped = "已 stop（端口释放已验证）"
        except Exception as exc:  # noqa: BLE001 - 清场失败必须可见但不上抛吞断言结果
            stopped = f"stop 失败（需手工清场）: {exc}"
        checks.append({"name": "instance_cleanup", "pass": "失败" not in stopped,
                       "detail": stopped})

    evidence = {
        "plugin_id": meta["id"],
        "all_pass": all(c["pass"] for c in checks),
        "checks": checks,
    }
    evidence_path = plugin_dir / "verify_evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n"
    )
    evidence["evidence_path"] = str(evidence_path)
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="插件装载验证编排器（scratch 实例断言链）")
    parser.add_argument("plugin_dir", help="待验证插件目录（staging 内）")
    parser.add_argument("--workspace", default=None, help="内核工作区（默认按 spawn 脚本推导仓根）")
    parser.add_argument("--kernel-bin", default=None, help="内核 exe（装机自举场景直指包内二进制）")
    parser.add_argument("--scratch-root", default=None, help="scratch 目录（默认插件同级 .verify_scratch）")
    parser.add_argument("--timeout", type=float, default=120.0, help="实例就绪超时秒")
    parser.add_argument("--plugin-check-bin", default=None, help="plugin_check bin（可选）")
    args = parser.parse_args(argv)
    try:
        spawn_mod = _load_spawn_test_kernel()
        # 装机自举：kernel_bin 直指 <安装目录>/resources/kernel/<exe>，workspace=安装目录
        # （cwd 与 scratch 根基准）；scratch 建议显式 --scratch-root 指用户空间（安装目录
        # 可能只读）。dev 场景 workspace 缺省按 spawn 脚本推导仓根。
        workspace = (
            Path(args.kernel_bin).resolve().parents[2]
            if args.kernel_bin else
            Path(args.workspace) if args.workspace
            else spawn_mod.default_workspace()
        )
        evidence = verify_plugin(
            Path(args.plugin_dir), workspace,
            scratch_root=Path(args.scratch_root) if args.scratch_root else None,
            timeout_s=args.timeout,
            plugin_check_bin=args.plugin_check_bin,
            kernel_bin=args.kernel_bin,
        )
    except Exception as exc:  # noqa: BLE001 - CLI 边界统一转退出码
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(evidence, ensure_ascii=False, indent=1))
    return 0 if evidence["all_pass"] else 2


if __name__ == "__main__":
    sys.exit(main())
