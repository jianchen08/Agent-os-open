"""执行环境自述与修复提示（工具契约增强）。

解决（2026-11-14/15 装机隔离验证三轮实测）：工具失败返回只有干巴巴报错，
agent 不知道「自己在哪执行、这错在该执行面意味着什么、怎么修」——三次
同点重试烧尽轮次后误判根因（"没有 Linux 二进制"，实为存在）。

设计裁定（用户 2026-11-15 两次修正）：
1. **不注入提示词**：执行环境信息按需通过工具/失败返回获得；
2. **失败返回=单条最相关的详细诊断**，不是档案全量倒出——按症状定向
   核对（真读配置/密钥/二进制），给出具体值与精确修复步骤；
3. 落点=SDK 工具分发异常出口单点（一处修，全部 sidecar 工具受益），
   内核零改动。
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["describe_execution_face", "fix_hints_for_error", "enrich_error_message"]


# ── 基础探测（廉价 stat，仅失败路径调用）──────────────────


def _repo_root() -> Path:
    """定位仓库根：从本模块向上找含 config/plugins/llm/llm.yaml 或 .git 的祖先。

    兼容两种落位：dev 仓库（plugins/sdk/src/…）与装机 site-packages
    （resources/plugins/sdk/src/…——后者再向上即 resources，同样含 config）。
    """
    here = Path(__file__).resolve()
    for ancestor in here.parents[:6]:
        try:
            if (ancestor / "config" / "plugins" / "llm" / "llm.yaml").is_file() or (
                ancestor / ".git"
            ).exists():
                return ancestor
        except OSError:
            continue
    return here.parents[4] if len(here.parents) > 4 else here


def _in_wsl() -> bool:
    try:
        with open("/proc/version", encoding="utf-8", errors="replace") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


def _read_env_keys(user_env: Path) -> dict[str, bool]:
    """user_root/.env 的密钥名→是否存在（只回报名，不回值）。"""
    keys: dict[str, bool] = {}
    try:
        for line in user_env.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^([A-Z][A-Z0-9_]+)\s*=", line)
            if m:
                keys[m.group(1)] = True
    except OSError:
        pass
    return keys


def _llm_models() -> tuple[Path, list[str]]:
    """读 config/plugins/llm/llm.yaml 的 models 表模型名清单（yaml 缺失则空）。"""
    path = _repo_root() / "config" / "plugins" / "llm" / "llm.yaml"
    if not path.is_file():
        return path, []
    try:
        import yaml  # noqa: PLC0415

        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace")) or {}
        models = (data.get("models") or {})
        return path, sorted(models.keys())
    except Exception:
        # 无 yaml 库/解析失败：退化文本扫描（models: 段内两空格缩进键）
        names: list[str] = []
        in_models = False
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if re.match(r"^\S", line):
                    in_models = line.strip().startswith("models:")
                    continue
                if in_models:
                    m = re.match(r"^  ([^#\s][^:]*):\s*(#.*)?$", line)
                    if m:
                        names.append(m.group(1).strip())
        except OSError:
            pass
        return path, sorted(set(names))


# ── 症状定向诊断器（每个返回单条详细诊断，首行=根因）──────────


def _diag_provider_missing(error_text: str) -> str:
    m = re.search(r"模型\s*([^\s）(]+)", error_text)
    model = m.group(1) if m else "(未解析)"
    path, available = _llm_models()
    keys = _read_env_keys(_repo_root() / "user_root" / ".env")
    hit = "在" if model in available else "不在"
    key_report = "、".join(f"{k}={'在' if v else '缺'}" for k, v in keys.items()) or "（文件缺失）"
    lines = [
        f"DIAGNOSIS: 模型 {model} 未在 provider 配置定义（{hit} models 表）",
        f"核对: provider 配置={path}；models 表可用模型={available or '（空）'}",
        f"核对: 密钥供给 user_root/.env → {key_report}",
        "修复（二选一）:",
        f"  1) 改用可用模型之一（如 {available[0] if available else '任一已定义模型'}）",
        f"  2) 或为 {model} 补 provider 定义（api_base + 密钥；密钥进 user_root/.env，勿入库）",
    ]
    return "\n".join(lines)


def _diag_sandbox_masked(error_text: str) -> str:
    installed_visible = os.path.isdir("D:/agentos-r297-main/resources/config") or os.path.isdir(
        "/mnt/d/agentos-r297-main/resources/config"
    )
    lines = [
        "DIAGNOSIS: 目标路径不可达——沙箱 /mnt 整体 tmpfs 遮蔽（装机目录/宿主盘不可见属预期）",
        f"核对: 装机目录在沙箱内可见={installed_visible}",
        "修复: 验证需自含供给（工作区内的配置/密钥/二进制），勿引用装机路径；"
        "若确需装机资源，改宿主侧执行",
    ]
    return "\n".join(lines)


def _diag_platform_mismatch(error_text: str) -> str:
    elf = os.path.isfile("kernel/target/release/agentos-kernel")
    lines = [
        "DIAGNOSIS: 平台错配——Windows .exe 在 Linux 侧不可执行",
        f"核对: Linux ELF kernel/target/release/agentos-kernel 存在={elf}",
        "修复: WSL 内改用 kernel/target/release/agentos-kernel（无扩展名）",
    ]
    return "\n".join(lines)


def _diag_wsl_nat(error_text: str) -> str:
    lines = [
        "DIAGNOSIS: 目标端口不可达——WSL NAT 下 127.0.0.1 是 WSL 自身，不通 Windows 宿主",
        "修复: 验证应起自含 scratch 内核（工作区配置+密钥即可全栈自洽），"
        "勿依赖装机 9101；确需连宿主请用宿主网关地址",
    ]
    return "\n".join(lines)


def _diag_python_env(error_text: str) -> str:
    venv_scripts = os.path.isdir("plugins/shared/llm_core/.venv/Scripts")
    venv_bin = os.path.isdir("plugins/shared/llm_core/.venv/bin")
    lines = [
        "DIAGNOSIS: sidecar Python 环境缺位/布局不匹配",
        f"核对: llm_core/.venv Scripts(Windows)={venv_scripts}、bin(Linux)={venv_bin}",
        "修复: WSL 内跑 venv_provision 双平台同建（同 .venv 补 bin/python+lib），"
        "装一次持久；或确认 python3+PYTHONPATH 直跑通路",
    ]
    return "\n".join(lines)


def _diag_generic(error_text: str) -> str:
    in_wsl = _in_wsl()
    timing = (
        "内核装载经 /mnt/d(drvfs) 约 5-8 分钟——若为启动后立刻调用，先排除「还没装载完」"
        if in_wsl
        else "NTFS 本地装载 ~14s"
    )
    return "\n".join(
        [
            "DIAGNOSIS: 未命中已知症状表",
            f"核对: 执行面={'WSL/Linux' if in_wsl else os.name}；装载时序={timing}",
            "修复: 按执行环境核对供给位置（密钥 user_root/.env、provider 配置 "
            "config/plugins/llm/llm.yaml、二进制 kernel/target/release/）后重试；"
            "同点失败 3 次熔断上报（勿原地重试）",
        ]
    )


# 症状→诊断器（顺序=优先级，首个命中生效）
_DIAG_HANDLERS: list[tuple[str, Callable[[str], str]]] = [
    (r"缺 api_base|缺 api_key|provider \?|no provider", _diag_provider_missing),
    (r"agentos-kernel\.exe|Exec format error", _diag_platform_mismatch),
    (r"9101|Max retries|connection refused|Failed to establish", _diag_wsl_nat),
    (r"No module named|venv", _diag_python_env),
    (r"No such file|cannot access|not found", _diag_sandbox_masked),
]


# ── 对外接口 ─────────────────────────────────────────────


def describe_execution_face() -> dict[str, Any]:
    """执行面档案（按需查询用；失败返回走 enrich_error_message 的定向诊断）。"""
    in_wsl = _in_wsl()
    root = _repo_root()
    llm_yaml = root / "config" / "plugins" / "llm" / "llm.yaml"
    user_env = root / "user_root" / ".env"
    installed_visible = os.path.isdir("D:/agentos-r297-main/resources/config") or os.path.isdir(
        "/mnt/d/agentos-r297-main/resources/config"
    )
    return {
        "platform": "WSL/Linux" if in_wsl else os.name,
        "workspace_root": str(root),
        "sandbox": {
            "mnt_masked": in_wsl and not installed_visible,
            "installed_app_visible": installed_visible,
        },
        "supply": {
            "llm_provider_config": {"path": str(llm_yaml), "present": llm_yaml.is_file()},
            "model_keys": {"path": str(user_env), "present": user_env.is_file()},
            "kernel_binaries": {
                "linux_elf": str(root / "kernel/target/release/agentos-kernel"),
                "windows_exe": str(root / "kernel/target/release/agentos-kernel.exe"),
            },
        },
    }


def fix_hints_for_error(error_text: str) -> list[str]:
    """兼容接口：按错误文本返回命中根因+FIX 单行（诊断细节走 enrich）。"""
    import contextlib
    import io

    for pattern, handler in _DIAG_HANDLERS:
        if re.search(pattern, error_text, re.IGNORECASE):
            with contextlib.redirect_stdout(io.StringIO()):
                detail = handler(error_text)
            first = detail.splitlines()[0]
            fix_lines = [ln.strip() for ln in detail.splitlines() if ln.strip().startswith(("1)", "2)", "修复:"))]
            return [f"{first} | {'; '.join(fix_lines) or detail}"]
    return []


def enrich_error_message(error_text: str) -> str:
    """工具失败出口统一包装：单条最相关的详细诊断（根因+核对+精确修复）。

    未命中已知症状时输出简短兜底（执行面+供给核对指引+熔断提示），
    不倒档案全量。
    """
    for pattern, handler in _DIAG_HANDLERS:
        if re.search(pattern, error_text, re.IGNORECASE):
            return f"原错误: {error_text}\n" + handler(error_text)
    return f"原错误: {error_text}\n" + _diag_generic(error_text)
