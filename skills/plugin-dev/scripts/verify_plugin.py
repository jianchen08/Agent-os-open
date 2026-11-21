#!/usr/bin/env python3
"""插件装载验证编排器（插件开发技能 skills/plugin-dev 自带脚本）。

五步流转管线第②步（ADR 2026-11-03 §D2）：对 staging 内插件起 scratch 内核实例
（三隔离），跑后端断言链，输出证据 JSON：

  1. plugin.json 结构预检（本地，零实例）；
  2. 生成 scratch 实例启动配置（分级单副本三根，ADR 2026-11-07）：
     - Tier2 插件：tier1_root/shared_root=内置根，tier2_root=只含被测插件的视图；
     - Tier1 插件：tier1_root=全 Tier1 集合视图（被测件指向 staging），tier2_root=空；
  3. spawn scratch 实例（复用 skill-dev-toolkit.spawn_test_kernel 三隔离；
     端口/DB/口令之外再交付 AGENTOS_INSTANCE_CONFIG / AGENTOS_CONFIG_ROOT /
     AGENTOS_USER_ROOT，内核零推导零回落）；
  4. /health 就绪（spawn 内部轮询）；
  5. GET /api/v1/tools → 断言插件声明的工具出现在 items；
  6. GET /api/v1/system/plugin-load-manifest → 断言运行时加载副本就是 staging
     那份（路径在验证视图内且版本一致）——防「旧副本满足断言」假绿；
  7. GET /api/v1/schema → 声明面证据（弱断言：响应含插件 id）；
  8. 证据 JSON 落 staging（逐项 pass/fail + 响应摘录），实例精确 stop
     （keep_running=True 时保留实例交调用方复用——verify_host.py 前端车道）。

plugin_check（内核声明校验 bin）为可选步：--plugin-check-bin 给了就跑，缺省跳过并标注。

用法：
  python skills/plugin-dev/scripts/verify_plugin.py <plugin_dir> \\
    [--workspace <repo_root>] [--kernel-bin <exe>] [--scratch-root D] [--timeout S]
    [--config-root <config_dir>] [--base-plugins-root <plugins/shared>] [--user-root D]

装机自举（无开发者仓库）：
  python <安装位>/resources/skills/plugin-dev/scripts/verify_plugin.py \\
    <staging>/<plugin_id> --kernel-bin "<安装位>/resources/kernel/agentos-kernel.exe" \\
    --scratch-root "%APPDATA%/agentos/plugins_staging/.verify_scratch"
  配置根缺省取 <安装位>/resources/config（含 plugin_tiers.yaml）；要与现役用户配置
  一致时显式 --config-root "%APPDATA%/agentos/config"。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
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

#: 实例启动配置 schema（与内核 INSTANCE_CONFIG_SCHEMA 同值，ADR 2026-11-07）
_INSTANCE_SCHEMA = 1
#: Tier1 分级清单相对配置根的位置（内核 TIER_MANIFEST_FILENAME 同值）
_TIER_MANIFEST_REL = Path("kernel") / "plugin_tiers.yaml"


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


def read_manifest(plugin_dir: Path) -> dict[str, Any]:
    """读 plugin.json / plugin.yaml 为 dict；缺失/不可解析/非对象一律 VerifyError。"""
    json_path = plugin_dir / "plugin.json"
    yaml_path = plugin_dir / "plugin.yaml"
    if not json_path.is_file() and not yaml_path.is_file():
        raise VerifyError(f"插件目录缺 plugin.json/plugin.yaml: {plugin_dir}")
    try:
        if json_path.is_file():
            manifest = json.loads(json_path.read_text(encoding="utf-8"))
        else:
            yaml = _load_yaml_module()
            try:
                manifest = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise VerifyError(
                    f"插件 manifest 不可解析（{yaml_path}）: {exc}"
                ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise VerifyError(f"插件 manifest 不可解析（{plugin_dir}）: {exc}") from exc
    if not isinstance(manifest, dict):
        raise VerifyError(f"插件 manifest 顶层不是对象: {plugin_dir}")
    return manifest


def read_declared_tools(plugin_dir: Path) -> dict[str, Any]:
    """读 manifest，返回 {id, version, tools: [name...]}；缺 id 抛 VerifyError。"""
    manifest = read_manifest(plugin_dir)
    plugin_id = manifest.get("id")
    if not plugin_id:
        raise VerifyError("插件 manifest 缺 id")
    capabilities = manifest.get("capabilities")
    raw_tools = capabilities.get("tools") if isinstance(capabilities, dict) else []
    tools = [
        str(t.get("name"))
        for t in raw_tools or []
        if isinstance(t, dict) and t.get("name")
    ]
    return {
        "id": str(plugin_id),
        "version": str(manifest.get("version") or ""),
        "tools": tools,
    }


def _load_yaml_module():
    """延迟加载 PyYAML（插件配置同解析器）；缺席时给可执行指引。"""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - 环境缺依赖的显式失败面
        raise VerifyError(
            "读 plugin_tiers.yaml 需要 PyYAML：请用仓内/装机 resources/prereqs 的 "
            "python 运行本脚本（pip install pyyaml 亦可）"
        ) from exc
    return yaml


def http_get_json(url: str, timeout_s: float = 10.0,
                  token: str | None = None) -> dict[str, Any]:
    """GET 并解析 JSON；网络失败抛 VerifyError（fail-visible）。

    /api/v1/tools、/api/v1/schema、/api/v1/system/plugin-load-manifest 在认证
    中间件后，需 Bearer（首跑匿名必 401——snippet_box 实测缺陷①，2026-11-03 修）。
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

    形态借 dsh_adapter/server.py 判例。建完即验（mklink 失败会静默返回非零），
    视图缺链接会让内核装载面缺插件、错因难查。
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
    if not (link.exists() or link.is_symlink()):
        raise VerifyError(f"junction 创建失败: {link} → {target}")


def _reset_dir(path: Path) -> Path:
    """清空并重建目录（junction 为 reparse 点，rmtree 只删链接不递归目标）。"""
    if path.exists() or path.is_symlink():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def resolve_config_root(
    workspace: Path, kernel_bin: str | None = None, explicit: str | None = None
) -> Path:
    """定位配置根（含 kernel/plugin_tiers.yaml）。

    优先级：显式 --config-root > 装机包内 <resources>/config（kernel_bin 直指
    安装位时）> workspace/config。全部候选都不含分级清单 = 显式报错（内核对
    配置根零推导，脚本也不猜）。
    """
    required = _TIER_MANIFEST_REL
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if kernel_bin:
        candidates.append(Path(kernel_bin).resolve().parents[1] / "config")
    candidates.append(Path(workspace) / "config")
    seen: list[str] = []
    for candidate in candidates:
        key = str(candidate.resolve())
        if key in seen:
            continue
        seen.append(key)
        if (candidate / required).is_file():
            return Path(key)
    raise VerifyError(
        "找不到配置根（需含 kernel/plugin_tiers.yaml）：已试 "
        f"{seen or ['<无候选>']}；用 --config-root 显式指定（dev=仓根 config，"
        "装机=<安装位>/resources/config 或 %APPDATA%/agentos/config）"
    )


def resolve_base_plugins_root(
    workspace: Path, kernel_bin: str | None = None, explicit: str | None = None
) -> Path:
    """定位内置插件根（Tier1 运行件 + 共享件 + Tier2 种子）。

    优先级：显式 --base-plugins-root > <install>/resources/plugins/shared >
    workspace/plugins/shared。缺席即错（验证实例必须有 Tier1/共享件兜底，
    否则启动守卫按分级清单拒启）。
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if kernel_bin:
        candidates.append(Path(kernel_bin).resolve().parents[1] / "plugins" / "shared")
    candidates.append(Path(workspace) / "plugins" / "shared")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    raise VerifyError(
        "找不到内置插件根（需含 Tier1 插件与 _libs/_host 共享件）：已试 "
        f"{[str(c) for c in candidates]}；用 --base-plugins-root 显式指定"
    )


def read_tier1_ids(config_root: Path) -> set[str]:
    """读分级清单 tier1.safety_plugins（内核启动守卫的单一真源同文件）。"""
    manifest_path = config_root / _TIER_MANIFEST_REL
    if not manifest_path.is_file():
        raise VerifyError(f"配置根缺 Tier1 分级清单: {manifest_path}")
    yaml = _load_yaml_module()
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise VerifyError(f"plugin_tiers.yaml 不可读: {exc}") from exc
    except yaml.YAMLError as exc:
        raise VerifyError(f"plugin_tiers.yaml 不可解析: {exc}") from exc
    tier1 = data.get("tier1") if isinstance(data, dict) else None
    ids = tier1.get("safety_plugins") if isinstance(tier1, dict) else None
    if not isinstance(ids, list) or not ids:
        raise VerifyError(f"plugin_tiers.yaml 缺 tier1.safety_plugins: {manifest_path}")
    return {str(item) for item in ids}


def discover_plugin_dirs_by_id(root: Path) -> dict[str, Path]:
    """递归扫描插件目录 → {manifest id: 目录}（与内核 collect_plugin_dirs 同规则）。

    含 manifest 的目录即插件目录，不再下钻；manifest 不可读 = VerifyError
    （内核守卫同样 fail-closed，此处提前暴露）。
    """
    found: dict[str, Path] = {}

    def walk(current: Path) -> None:
        try:
            entries = sorted(current.iterdir())
        except OSError as exc:
            raise VerifyError(f"插件根不可读（{current}）: {exc}") from exc
        for path in entries:
            if not path.is_dir():
                continue
            if (path / "plugin.json").is_file() or (path / "plugin.yaml").is_file():
                manifest = read_manifest(path)
                plugin_id = manifest.get("id")
                if not plugin_id:
                    raise VerifyError(f"插件目录 manifest 缺 id: {path}")
                found.setdefault(str(plugin_id), path)
            else:
                walk(path)

    walk(root)
    return found


def build_plugin_view(plugin_dir: Path, cache_dir: Path) -> Path:
    """干净 Tier2 视图：junction 只含被测插件（内核整目录扫描，脏目录会污染装载）。"""
    root = _reset_dir(cache_dir / ".verify_tier2_view")
    _make_junction(root / plugin_dir.name, plugin_dir.resolve())
    return root


def build_tier1_view(
    base_root: Path, tier1_ids: set[str], plugin_dir: Path,
    plugin_id: str, cache_dir: Path,
) -> Path:
    """Tier1 验证视图：清单内全部安全插件各一 junction，被测件替换为 staging。

    启动守卫要求清单内 Tier1 id 全部实际位于 tier1_root（缺席拒启），故视图
    不能只放被测件；共享件仍由 shared_root=内置根提供，不重复搬。
    """
    by_id = discover_plugin_dirs_by_id(base_root)
    missing = sorted(tid for tid in tier1_ids if tid not in by_id and tid != plugin_id)
    if missing:
        raise VerifyError(
            f"Tier1 清单插件在内置根缺席: {missing}（{base_root}）——"
            "分级清单与实例配置不一致，内核启动守卫会拒启"
        )
    view = _reset_dir(cache_dir / ".verify_tier1_view")
    for tid in sorted(tier1_ids):
        source = plugin_dir if tid == plugin_id else by_id.get(tid)
        if source is None:  # 新 Tier1 id（清单已登记、内置根尚无目录）
            continue
        rel = by_id[tid].relative_to(base_root) if tid in by_id else Path(plugin_dir.name)
        link = view / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        _make_junction(link, source.resolve())
    return view


def build_instance_config(
    plugin_dir: Path, plugin_id: str, base_root: Path,
    config_root: Path, work_dir: Path,
) -> dict[str, Any]:
    """生成 scratch 实例的 instance.yaml（三根显式，ADR 2026-11-07）。

    返回 {tier, path, tier1_root, tier2_root, shared_root, expected_runtime_root}；
    expected_runtime_root 供装载清单断言（运行时副本必须落在这里）。
    """
    tier1_ids = read_tier1_ids(config_root)
    if plugin_id in tier1_ids:
        tier = "tier1"
        tier1_root = build_tier1_view(base_root, tier1_ids, plugin_dir, plugin_id, work_dir)
        tier2_root = _reset_dir(work_dir / ".verify_tier2_empty")
    else:
        tier = "tier2"
        tier1_root = base_root
        tier2_root = build_plugin_view(plugin_dir, work_dir)
    config_path = work_dir / "instance.yaml"
    config_path.write_text(
        "# scratch 验证实例（verify_plugin 生成；ADR 2026-11-03 D3 + 2026-11-07 三根）\n"
        f"schema: {_INSTANCE_SCHEMA}\n"
        f"tier1_root: {json.dumps(str(tier1_root))}\n"
        f"tier2_root: {json.dumps(str(tier2_root))}\n"
        f"shared_root: {json.dumps(str(base_root))}\n",
        encoding="utf-8", newline="\n",
    )
    return {
        "tier": tier,
        "path": config_path,
        "tier1_root": tier1_root,
        "tier2_root": tier2_root,
        "shared_root": base_root,
        "expected_runtime_root": tier1_root if tier == "tier1" else tier2_root,
    }


def redact_instance_secret(evidence: dict[str, Any]) -> dict[str, Any]:
    """证据落盘/回传前脱敏：scratch 实例 admin 口令只留在内存。

    口令只用于前端车道内部登录；写进 workspace 证据文件等于把活实例凭证交给
    被验证方（agent），2026-11-11 安全评估实锤。
    """
    clone = json.loads(json.dumps(evidence, ensure_ascii=False))
    instance = clone.get("instance")
    if isinstance(instance, dict) and instance.get("admin_password") is not None:
        instance["admin_password"] = "***"
    return clone


def login_tokens(base_url: str, admin_password: str,
                 timeout_s: float = 10.0) -> dict[str, str]:
    """admin 登录换 token 组（access + refresh；口令来自 spawn 实例档案）。

    refresh_token 供浏览器车道注入 localStorage（前端 access 仅内存持有、
    刷新令牌落盘，见 frontend/src/services/auth/tokenLifecycle.ts）。
    """
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
    return {
        "access_token": str(token),
        "refresh_token": str(body.get("refresh_token") or ""),
    }


def login_token(base_url: str, admin_password: str, timeout_s: float = 10.0) -> str:
    """admin 登录换 Bearer access token（兼容入口）。"""
    return login_tokens(base_url, admin_password, timeout_s=timeout_s)["access_token"]


def check_tools_visible(base_url: str, expected_tools: list[str],
                        token: str | None = None) -> dict[str, Any]:
    """断言插件声明的工具出现在 /api/v1/tools items（{items,total} 信封）。

    功能性插件（theme/page）tools==[] 是合法形态（运行面走 http.handle），
    工具面断言不适用 → 判 pass 并在 detail 标注（装载/声明由 loaded_copy /
    schema 断言覆盖）。
    """
    if not expected_tools:
        return {
            "name": "tools_visible",
            "pass": True,
            "detail": "无 LLM 工具声明（功能性插件走 http.handle）——工具面断言不适用",
        }
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


def _path_is_under(path: str, root: Path) -> bool:
    """路径归属判定：abspath 归一但**不解析 junction**（resolve 会把视图链接
    还原成 staging 真实路径，导致视图内副本被误判为越界）。"""
    try:
        candidate = Path(os.path.abspath(path))
        base = Path(os.path.abspath(str(root)))
        return candidate.is_relative_to(base)
    except (OSError, ValueError):
        return False


def check_loaded_copy(base_url: str, plugin_id: str, expected_version: str,
                      expected_root: Path, token: str | None = None) -> dict[str, Any]:
    """/api/v1/system/plugin-load-manifest 断言：运行时副本=验证视图内那份。

    这是「改一个已存在插件」的假绿防线（ADR 2026-11-07 分级单副本后，同 id
    旧副本可能仍在内置根）：路径不在 expected_root 下或版本不一致 = fail。
    """
    payload = http_get_json(f"{base_url}/api/v1/system/plugin-load-manifest", token=token)
    manifest = payload.get("manifest") if isinstance(payload, dict) else None
    entries = manifest.get("plugins") if isinstance(manifest, dict) else None
    entry = next(
        (e for e in entries or []
         if isinstance(e, dict) and e.get("plugin_id") == plugin_id),
        None,
    )
    if entry is None:
        return {
            "name": "loaded_copy",
            "pass": False,
            "detail": f"装载清单无 {plugin_id}（未装载/G2 校验剔除；查内核日志）",
        }
    runtime_path = str(entry.get("path") or "")
    path_ok = bool(runtime_path) and _path_is_under(runtime_path, expected_root)
    runtime_version = str(entry.get("version") or "")
    version_ok = not expected_version or runtime_version == expected_version
    hint = ""
    if not path_ok and expected_version and runtime_version == expected_version:
        hint = "；同 id 旧副本与 staging 同版本（内核 version-max 归约旧副本胜出）——改动已存在插件时 staging 版本须严格大于内置副本"
    return {
        "name": "loaded_copy",
        "pass": path_ok and version_ok,
        "detail": (
            f"运行时 path={runtime_path or '?'} version={runtime_version} "
            f"tier={entry.get('tier')} source_root={entry.get('source_root')}；"
            f"期望根={expected_root} 版本={expected_version or '（manifest 未声明）'}{hint}"
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
    config_root: str | None = None,
    base_plugins_root: str | None = None,
    user_root: str | None = None,
    keep_running: bool = False,
) -> dict[str, Any]:
    """跑完整断言链，返回证据 JSON（调用方负责落盘）。失败抛 VerifyError。

    keep_running=True 时保留 scratch 实例（返回 evidence["instance"]：
    port/run_id/admin_password/scratch_root），供同一实例上的前端车道复用；
    **调用方必须在 finally 里精确 stop**（verify_host.py 即此契约）。
    """
    meta = read_declared_tools(plugin_dir)
    checks: list[dict[str, Any]] = []

    checks.append({
        "name": "manifest_precheck",
        "pass": True,
        "detail": f"id={meta['id']}, version={meta['version'] or '?'}, 声明工具={meta['tools'] or '（无——功能性插件走 http.handle）'}",
    })

    if plugin_check_bin:
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
    # instance.yaml 内的相对路径按**配置文件所在目录**解析，写相对值会被二次拼接
    # （scratch 默认值 = 插件同级相对路径时必现）——统一钉绝对路径再落盘。
    scratch = Path(scratch_root or plugin_dir.parent / ".verify_scratch").resolve()
    lane_dir = _reset_dir(scratch / "lane")
    resolved_config_root = resolve_config_root(
        workspace, kernel_bin=kernel_bin, explicit=config_root
    )
    base_root = resolve_base_plugins_root(
        workspace, kernel_bin=kernel_bin, explicit=base_plugins_root
    )
    lane_user_root = Path(user_root).resolve() if user_root else lane_dir / "user_root"
    lane_user_root.mkdir(parents=True, exist_ok=True)
    instance = build_instance_config(
        plugin_dir.resolve(), meta["id"], base_root, resolved_config_root, lane_dir
    )
    checks.append({
        "name": "instance_config",
        "pass": True,
        "detail": (
            f"tier={instance['tier']} config_root={resolved_config_root} "
            f"tier1={instance['tier1_root']} tier2={instance['tier2_root']}"
        ),
    })
    record = spawn_kernel.spawn(
        workspace,
        scratch_root=str(scratch),
        timeout_s=timeout_s,
        kernel_bin=kernel_bin,
        instance_config=str(instance["path"]),
        config_root=str(resolved_config_root),
        user_root=str(lane_user_root),
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
        checks.append(check_loaded_copy(
            base_url, meta["id"], meta["version"],
            Path(instance["expected_runtime_root"]), token=token,
        ))
        checks.append(check_schema_declared(base_url, meta["id"], token=token))
    finally:
        if not keep_running:
            try:
                spawn_kernel.stop(scratch, record.get("run_id"))
                stopped = "已 stop（端口释放已验证）"
            except Exception as exc:  # noqa: BLE001 - 清场失败必须可见但不上抛吞断言结果
                stopped = f"stop 失败（需手工清场）: {exc}"
            checks.append({"name": "instance_cleanup", "pass": "失败" not in stopped,
                           "detail": stopped})

    evidence = {
        "plugin_id": meta["id"],
        "tier": instance["tier"],
        "all_pass": all(c["pass"] for c in checks),
        "checks": checks,
    }
    if keep_running:
        evidence["instance"] = {
            "port": port,
            "base_url": base_url,
            "run_id": record.get("run_id"),
            "admin_password": record.get("admin_password"),
            "scratch_root": str(scratch),
        }
    evidence_path = plugin_dir / "verify_evidence.json"
    # 落盘脱敏：scratch admin 口令只留在内存（前端车道复用），证据文件不带凭证。
    evidence_path.write_text(
        json.dumps(redact_instance_secret(evidence), ensure_ascii=False, indent=1),
        encoding="utf-8", newline="\n",
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
    parser.add_argument("--config-root", default=None, help="配置根（含 kernel/plugin_tiers.yaml；缺省按内核 bin/workspace 推导）")
    parser.add_argument("--base-plugins-root", default=None, help="内置插件根（含 Tier1 与共享件；缺省按内核 bin/workspace 推导）")
    parser.add_argument("--user-root", default=None, help="可写用户数据根（缺省 scratch/lane/user_root，不碰真实用户空间）")
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
            config_root=args.config_root,
            base_plugins_root=args.base_plugins_root,
            user_root=args.user_root,
        )
    except Exception as exc:  # noqa: BLE001 - CLI 边界统一转退出码
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(evidence, ensure_ascii=False, indent=1))
    return 0 if evidence["all_pass"] else 2


if __name__ == "__main__":
    sys.exit(main())
