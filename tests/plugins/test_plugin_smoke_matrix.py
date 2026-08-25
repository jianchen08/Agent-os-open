# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-heavy-suites
"""全插件端到端冒烟矩阵——逐个插件验证"能否正常执行"。

范围：plugins/ 下所有带 plugin.json 的插件（system / tools / pipeline / external_mcp）。

对每个 Python sidecar 插件（entry 为 python 且存在 server.py）：
1. 子进程加载 server.py（cwd=插件目录，与生产 sidecar 一致）——模拟内核拉起插件；
2. 断言 AgentOSPlugin 对象存在、工具注册非空；
3. 触发 on_load / on_unload 生命周期（内核 notification 语义）；
4. 对精选的无副作用工具执行真实调用，断言 handler 返回非 None。

用子进程而不是同进程 import 的原因见 plugin_probe.py 文档头：
插件大量使用裸名导入，同进程批量加载必然互相污染 sys.path。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
PROBE = Path(__file__).resolve().parent / "plugin_probe.py"

# 仓库根目录（供 PYTHONPATH）
_SRC = str(ROOT / "src")
_SDK = str(ROOT / "plugins" / "sdk" / "src")
_PLUGINS = str(ROOT / "plugins")

_PROBE_ENV = dict(os.environ)
# 探针子进程必须能看到 src（工具实现）/ sdk（SDK 源码）/ plugins。
# 不能用于串判断"是否已存在"——父进程 PYTHONPATH 含全部路径时会把
# join 结果清成空串，导致子进程完全丢路径；必须按路径列表成员判断。
_existing_py_paths = [p for p in _PROBE_ENV.get("PYTHONPATH", "").split(os.pathsep) if p]
_PROBE_ENV["PYTHONPATH"] = os.pathsep.join(
    [p for p in (_SRC, _SDK, _PLUGINS) if p not in _existing_py_paths] + _existing_py_paths
)

# venv 单轨（uv 迁移批 D，2026-08-19）：生产 invoker 已 fail-closed——Python sidecar
# 必须以插件 .venv 解释器启动、无 PYTHONPATH 注入（SDK/依赖由 editable install 解析）。
# 冒烟矩阵对齐该语义：优先 venv 解释器 + 剥离 PYTHONPATH；仅 venv 缺失的场景
# （探针自检等非插件目录）回退 sys.executable + PYTHONPATH。
_PROBE_ENV_NO_PYTHONPATH = {
    k: v for k, v in _PROBE_ENV.items() if k != "PYTHONPATH"
}


def _venv_python(plugin_dir: Path) -> str | None:
    """插件 venv 解释器绝对路径（与 kernel invoker `find_venv_interpreter` 同布局探测）。"""
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
        p = plugin_dir / rel
        if p.is_file():
            return str(p)
    return None


# ────────────────────────────────────────────────────────────
# 插件目录发现
# ────────────────────────────────────────────────────────────

_SKIP_DIRS = {"sdk", "data", "shared/data", "shared/pipeline/_base", "shared/native_test"}
_SKIP_NAMES = {"native_test", "wasm_hello"}


def _discover_plugin_dirs() -> list[Path]:
    """发现所有含 plugin.json 的插件目录。

    用带剪枝的 os.walk（rglob 会钻进 dsh_adapter runtime 的 node_modules
    连接点迷宫卡死——与 scripts/migrate_plugins_to_uv.py SKIP_DIRS 同款处置，
    2026-08-19 批 D 本地复现实证）。
    """
    skip_walk = {
        "node_modules", "__pycache__", "target", "runtime", "dsh_plugins",
        ".venv", ".venv-hindsight", ".ai_workspaces",
    }
    dirs: list[Path] = []
    for cur, subdirs, files in os.walk(ROOT / "plugins"):
        subdirs[:] = [d for d in subdirs if d not in skip_walk]
        if "plugin.json" not in files:
            continue
        d = Path(cur)
        rel = d.relative_to(ROOT / "plugins")
        parts = rel.parts
        if any(part in _SKIP_NAMES for part in parts):
            continue
        if any(str(rel) == s or str(rel).startswith(s + os.sep) for s in _SKIP_DIRS):
            continue
        dirs.append(d)
    return sorted(dirs)


def _plugin_meta(plugin_dir: Path) -> dict:
    return json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))


def _entry_of(plugin_dir: Path) -> str:
    return str(_plugin_meta(plugin_dir).get("entry", ""))


def _is_python_sidecar(plugin_dir: Path) -> bool:
    """entry 为 python 且存在 server.py 的插件。

    判定对齐生产 invoker（批 D 单轨）：entry **首词为 PATH 裸 python/python3**
    （含 .exe 等扩展）才算 Python sidecar——rust native 插件（sensitive_checker
    等带遗留 server.py 但 entry=*.dll、host_type=in_process）不经 python 轨启动，
    不纳入（其 venv 契约不适用）。
    """
    entry = _entry_of(plugin_dir)
    if not (plugin_dir / "server.py").exists() or entry.startswith("mcp:external"):
        return False
    first = entry.split()[0] if entry else ""
    if "/" in first or "\\" in first:  # 显式路径解释器是刻意选择，非 venv 契约面
        return False
    stem = first.lower()
    for ext in (".exe", ".bat", ".cmd"):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
            break
    return stem in ("python", "python3")


def _is_native_plugin(plugin_dir: Path) -> bool:
    """entry 为 .dll/.wasm 等原生产物的插件（无 python 侧可探）。"""
    entry = _entry_of(plugin_dir)
    return entry.endswith((".dll", ".wasm"))


def _is_external_mcp(plugin_dir: Path) -> bool:
    return _entry_of(plugin_dir) == "mcp:external"


PLUGIN_DIRS = [d for d in _discover_plugin_dirs() if _is_python_sidecar(d)]
EXTERNAL_MCP_DIRS = [d for d in _discover_plugin_dirs() if _is_external_mcp(d)]
NATIVE_PLUGIN_DIRS = [d for d in _discover_plugin_dirs() if _is_native_plugin(d)]

# 纯转发型 server.py（无 plugin 对象、无 create_plugin，仅 run()）：
# 注册逻辑在包内实现，由插件自有测试覆盖（plugins/shared/tools/builtin_tools/tests）
WRAPPER_ONLY_PLUGINS = {"builtin_tools"}


def _run_probe(plugin_dir: Path, invoke: dict | None = None) -> dict:
    """子进程运行探针，返回 JSON 报告；超时/崩溃记为失败。

    生产等价（批 D 单轨）：插件目录有 .venv → 用 venv 解释器且**不注入
    PYTHONPATH**（对齐 invoker 翻转后的真实 spawn 环境）；无 .venv 的目录
    （探针自检等）回退 sys.executable + PYTHONPATH。
    """
    venv_py = _venv_python(plugin_dir)
    if venv_py:
        cmd = [venv_py, str(PROBE), str(plugin_dir)]
        env = _PROBE_ENV_NO_PYTHONPATH
    else:
        cmd = [sys.executable, str(PROBE), str(plugin_dir)]
        env = _PROBE_ENV
    if invoke:
        cmd += ["--invoke", json.dumps(invoke, ensure_ascii=False)]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(plugin_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"dir": str(plugin_dir), "load_ok": False, "load_error": "probe timeout (90s)"}
    try:
        report = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {
            "dir": str(plugin_dir),
            "load_ok": False,
            "load_error": f"probe crashed (rc={proc.returncode}): {proc.stderr[-400:]}",
        }
    return report


# ────────────────────────────────────────────────────────────
# 安全工具调用清单：只调用无网络/无 LLM/无副作用的纯工具
# 键为插件相对路径（插件名会撞车：system/cost_control vs pipeline/input/cost_control 等）
# ────────────────────────────────────────────────────────────

def _safe_invocations(plugin_dir: Path, tmp_path: Path) -> dict:
    """按插件目录返回可安全执行的 {tool: kwargs}。"""
    rel = str(plugin_dir.relative_to(ROOT / "plugins")).replace("\\", "/")
    table: dict[str, dict] = {
        "shared/system/cost_control": {"cost_control.get_status": {}},
        "shared/system/human_interaction": {"interaction.get_pending": {}},
        "shared/system/monitoring": {"monitoring.get_health": {}},
        "shared/system/review": {"review.get_report": {"review_id": "smoke_nonexistent"}},
        "shared/tools/triggers": {"trigger.list": {}},
        "shared/tools/simple": {
            "yaml_validate": {"content": "a: 1\nb: 2\n"},
        },
    }
    if rel == "shared/system/evaluation":
        target = tmp_path / "smoke_artifact.txt"
        target.write_text("smoke")
        table[rel] = {
            "evaluation.run": {
                "task_id": "smoke",
                "metrics": [{"metric_id": "m1", "type": "file_check", "params": {"path": str(target)}}],
                "gate_mode": False,
            }
        }
    if rel == "shared/tools/builtin_tools":
        target = tmp_path / "smoke_read.txt"
        target.write_text("smoke content")
        table[rel] = {"file_read": {"path": str(target)}}
    return table.get(rel, {})


# ────────────────────────────────────────────────────────────
# 矩阵测试
# ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("plugin_dir", PLUGIN_DIRS, ids=[str(d.relative_to(ROOT)) for d in PLUGIN_DIRS])
def test_plugin_loads_and_executes(plugin_dir: Path, tmp_path: Path) -> None:
    """插件可加载、注册工具、触发生命周期；精选工具可真实执行。"""
    report = _run_probe(plugin_dir, _safe_invocations(plugin_dir, tmp_path))

    # 纯转发型插件（builtin_tools）没有 plugin 对象，注册逻辑在包内，由插件自有测试覆盖；
    # 探针能走到 "no plugin object" 说明 server.py 及其依赖导入成功
    if plugin_dir.name in WRAPPER_ONLY_PLUGINS:
        assert report.get("load_error") == "no plugin object (wrapper-only server.py)", (
            f"插件 {plugin_dir.name} 加载失败: {report.get('load_error')}"
        )
        return

    assert report.get("load_ok") is True, (
        f"插件加载失败: {report.get('load_error')}\n"
        f"探针报告: {json.dumps(report, ensure_ascii=False)[:500]}"
    )

    # 声明式插件（capabilities.tools 空 + contributes 前端声明，如 debug_center
    # 纯页面路由、visual_customization_demo 纯主题包）：零工具是**契约本身**，
    # 不适用"至少注册 1 个工具"断言——只验加载 + 生命周期。声明了 tools 的
    # 插件注册数为零仍是红灯。
    meta = _plugin_meta(plugin_dir)
    declared_tools = meta.get("capabilities", {}).get("tools", [])
    declarative = not declared_tools and bool(meta.get("contributes"))

    tools = report.get("tools", [])
    if not declarative:
        assert len(tools) >= 1, f"插件 {plugin_dir.name} 未注册任何工具"

    assert report.get("lifecycle_on_load") is True, (
        f"插件 {plugin_dir.name} on_load 失败: {report.get('lifecycle_on_load_error')}"
    )
    assert report.get("lifecycle_on_unload") is True, (
        f"插件 {plugin_dir.name} on_unload 失败: {report.get('lifecycle_on_unload_error')}"
    )

    invocations = report.get("invocations", {})
    for tool_name, result in invocations.items():
        assert result.get("ok") is True, (
            f"插件 {plugin_dir.name} 工具 {tool_name} 执行失败: {result.get('error')}"
        )


@pytest.mark.parametrize("plugin_dir", EXTERNAL_MCP_DIRS, ids=[str(d.relative_to(ROOT)) for d in EXTERNAL_MCP_DIRS])
def test_external_mcp_plugin_json_valid(plugin_dir: Path) -> None:
    """external_mcp 插件：plugin.json 结构完整，mcp 端点指向存在的服务。

    契约对齐 kernel core traits（2026-08 收敛）：endpoint 嵌套在顶层 `mcp` 下
    （McpConfig{transport, endpoint}），transport ∈ {stdio, streamable_http}
    （serde snake_case）。旧 `mcp_endpoint` 顶层字段已废弃。
    """
    meta = _plugin_meta(plugin_dir)
    assert meta.get("entry") == "mcp:external"
    mcp = meta.get("mcp")
    assert isinstance(mcp, dict), f"{plugin_dir.name} 缺少顶层 mcp 配置（McpConfig）"
    transport = str(mcp.get("transport", ""))
    assert transport in ("stdio", "streamable_http"), (
        f"{plugin_dir.name} mcp.transport 未知: {transport}"
    )
    endpoint = mcp.get("endpoint")
    assert isinstance(endpoint, dict), f"{plugin_dir.name} mcp.endpoint 缺失"
    if transport == "stdio":
        # stdio 端点必须给出 command + args，且引用的 python 服务端存在
        command = str(endpoint.get("command", ""))
        args = endpoint.get("args", [])
        assert command, f"{plugin_dir.name} mcp.endpoint 缺少 command"
        assert args, f"{plugin_dir.name} mcp.endpoint 缺少 args"
        for arg in args:
            p = ROOT / arg
            if p.suffix == ".py" and p.exists():
                _assert_python_server_loadable(p)
            elif p.suffix == ".py":
                pytest.fail(f"{plugin_dir.name} 引用不存在的服务端脚本: {arg}")
    else:
        # streamable_http 端点必须给出 url
        assert "url" in endpoint, f"{plugin_dir.name} streamable_http 端点缺少 url"


@pytest.mark.parametrize("plugin_dir", NATIVE_PLUGIN_DIRS, ids=[str(d.relative_to(ROOT)) for d in NATIVE_PLUGIN_DIRS])
def test_native_plugin_json_valid(plugin_dir: Path) -> None:
    """原生插件（.dll/.wasm）：plugin.json 完整，entry 指向存在的产物文件。

    产物存在性按**生产同规则**解析：取 `native.artifact` 裸名，按平台补
    cdylib 前后缀（对齐 kernel NativePluginLoader::platform_artifact_name：
    Windows `{name}.dll`、Linux `lib{name}.so`、macOS `lib{name}.dylib`）。
    manifest `entry` 字段常写死构建机后缀（.dll），跨平台 CI 直接拿 entry
    找文件会误报缺失——加载器实际走的是 native.artifact 解析路径。
    """
    meta = _plugin_meta(plugin_dir)
    entry = _entry_of(plugin_dir)
    artifact = str(meta.get("native", {}).get("artifact", ""))
    assert artifact, f"{plugin_dir.name} native.artifact 缺失"
    resolved = _platform_artifact_name(artifact)
    assert (plugin_dir / resolved).exists(), (
        f"{plugin_dir.name} native 产物不存在: {resolved}（cdylib 未构建或 artifact 声明有误）"
    )
    assert meta.get("plugin_type") in ("tool", "pipeline", "system")
    assert entry.endswith((".dll", ".wasm")), f"{plugin_dir.name} entry 非 cdylib/wasm: {entry}"


def _platform_artifact_name(artifact: str) -> str:
    """按平台约定补全 cdylib 文件名前缀/后缀（镜像 kernel native_loader）。"""
    if artifact.lower().endswith((".dll", ".so", ".dylib")):
        return artifact
    if sys.platform == "win32":
        return f"{artifact}.dll"
    if sys.platform == "darwin":
        return f"lib{artifact}.dylib"
    return f"lib{artifact}.so"


def _assert_python_server_loadable(server_path: Path) -> None:
    """子进程验证 mcp server 脚本可导入执行。"""
    cmd = [
        sys.executable,
        "-c",
        "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())",
        str(server_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, f"{server_path.name} 语法错误: {proc.stderr[-300:]}"


def test_probe_script_self_check() -> None:
    """探针脚本自身可运行（空目录返回 load_ok=False 的合法报告）。"""
    report = _run_probe(ROOT / "plugins" / "sdk")  # 无 server.py 的目录
    assert report["load_ok"] is False
    assert report["load_error"] == "no server.py"


def test_python_sidecar_plugins_have_uv_contract() -> None:
    """批 D 翻转镜像（fail-closed 契约闸）：Python sidecar 插件必须
    pyproject.toml + .venv 双标志齐备——生产 invoker 已删 plain PATH python 回退
    （kernel/crates/invoker resolve_sidecar_command，2026-08-19），缺任一标志
    该插件在生产侧启动即失败。矩阵层提前暴露，防"新增插件忘建 venv"回归。
    """
    assert PLUGIN_DIRS, "插件发现异常：0 个 python sidecar"
    missing = [
        str(d.relative_to(ROOT))
        for d in PLUGIN_DIRS
        if not (d / "pyproject.toml").is_file() or _venv_python(d) is None
    ]
    assert not missing, (
        "以下 Python sidecar 插件缺 uv 单轨契约（pyproject.toml + .venv），"
        "生产 invoker 将 fail-closed 拒绝启动：\n  " + "\n  ".join(missing)
    )
