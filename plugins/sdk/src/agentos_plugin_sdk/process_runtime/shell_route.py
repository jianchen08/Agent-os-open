"""本地 shell 五级路由单源（bash 工具与 isolation host provider 共用）。

普查 ⚠️-6（docs/working/多端平台差异代码普查_20261020.md）收敛件：同一台
Windows 机器上此前两套本地执行语义（bash 工具走 WSL/bash 五级路由、host
provider 恒 cmd 直透传），$VAR/pipefail/路径语义不一致。本模块自
bash/process_manager.py 抽出路由核心为唯一实现，两消费方调用同一函数。

边界契约：
- 零第三方依赖（纯 stdlib）：bash 与 isolation 是两个插件、venv 各异，
  本模块禁引兄弟模块与第三方包（safe_cmd_encode 自 encoding.py 移入即为此）。
- 平台谓词参数化：is_windows 由调用方算一次下传（isolation 侧统一
  ``os.name == "nt"``，普查 ⚠️-7 新代码口径）。
- 本模块只决定 spawn 形态（argv/shell 串）；输出解码/超时/日志归消费方。

五级优先级（spawn_local_process）：
1. WSL 命令 → wsl 直连（use_wsl_direct）
2. WSL + bash → wsl -e bash -c（避免 MSYS2 的 $VAR 参数转换 bug）
3. Git Bash → bash -c（MSYS2 bash，有 $VAR 转义问题）
4. CMD → cmd /c（最后手段，无 Unix shell 能力）
5. Linux/POSIX：复合命令用 bash -c（bash 原生支持 pipefail），其余系统 shell

MSYS2 bash 会将命令行参数中的 $VAR 展开为空（参数转换 bug），WSL 的 bash
不存在此问题。因此同时可用时优先 WSL。
"""

from __future__ import annotations

import asyncio
import locale
import logging
import re
import shlex
import shutil

logger = logging.getLogger(__name__)

__all__ = [
    "is_compound_command",
    "is_wsl_command",
    "join_for_bash_c",
    "parse_wsl_args",
    "safe_cmd_encode",
    "spawn_local_process",
    "start_wsl_process",
]

# WSL 命令匹配模式（wsl 或 wsl.exe 开头）
_WSL_COMMAND_RE: re.Pattern[str] = re.compile(r"^\s*wsl(?:\.exe)?(?:\s+|$)", re.IGNORECASE)

# WSL 自身标志（接受一个值参数，如 -d Ubuntu-20.04）
_WSL_FLAGS_WITH_VALUE: frozenset[str] = frozenset(
    {
        "-d",
        "--distribution",
        "-u",
        "--user",
    }
)


def is_compound_command(command: str) -> bool:
    """检测命令是否为 shell 复合命令（含顶层控制操作符）。

    用于容器路径决定是否套 ``exec sh -c '<cmd>'``：POSIX exec 只接受一个
    简单命令，若用户命令含 ``&&``/``;``/``||``/``|``/``()`` 等控制操作符，
    exec 会替换为第一条后丢弃后续段，导致输出丢失。

    判定规则（命中任一即视为复合）：
    - 顶层（非引号内）出现 ``;`` ``&``（单或双） ``|``（单或双） ``(`` ``)``
      换行、反引号
    - ``$(...)`` 命令替换（顶层或双引号内，与 bash 语义一致）

    单遍字符扫描 + 引号状态机，跳过单/双引号内字符（双引号内仍检测反引号
    和 ``$( ``），处理反斜杠转义。避免把 ``echo "a;b"`` 这类引号内的元字符
    误判为复合。不依赖 shlex（shlex 遇不完整语法会抛异常，不适合轻量判断）。

    fail-safe：宁可误判（多套一层 sh，命令仍正确，仅 pid 精度下降），
    也不漏判（漏判会丢输出，正是要修的 bug）。
    """
    in_single = False  # 单引号内（bash 内无任何转义/展开）
    in_double = False  # 双引号内（仅反引号/$( 生效）
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        nxt = command[i + 1] if i + 1 < n else ""
        if in_single:
            if ch == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if ch == "\\":
                i += 2  # 双引号内反斜杠转义下一字符
                continue
            if ch == '"':
                in_double = False
            elif ch == "`" or ch == "$" and nxt == "(":  # 双引号内反引号命令替换
                return True
            i += 1
            continue
        # 顶层
        if ch == "\\":
            i += 2  # 转义下一字符
            continue
        if ch == "'":
            in_single = True
            i += 1
            continue
        if ch == '"':
            in_double = True
            i += 1
            continue
        if ch == "`":
            return True
        if ch == "$" and nxt == "(":
            return True
        if ch in (";", "&", "|", "(", ")", "\n"):
            return True
        i += 1
    return False


def is_wsl_command(command: str) -> bool:
    """检测命令是否为 WSL 调用。"""
    if not _WSL_COMMAND_RE.match(command):
        return False
    return shutil.which("wsl") is not None


def parse_wsl_args(command: str) -> list[str]:
    """解析 WSL 命令行参数，始终使用 bash -c 包装以保留 shell 变量展开。"""
    stripped = _WSL_COMMAND_RE.sub("", command).strip()
    if not stripped:
        return ["wsl"]

    try:
        tokens = shlex.split(stripped)
    except ValueError:
        # 复杂 shell 语法（引号、变量等），整个交给 bash -c，使用 -e 跳过登录 shell
        return ["wsl", "-e", "bash", "-c", stripped]

    wsl_opts: list[str] = []
    cmd_start = 0
    while cmd_start < len(tokens):
        t = tokens[cmd_start]
        if t in _WSL_FLAGS_WITH_VALUE:
            # -d Ubuntu-20.04 之类：标志 + 值
            wsl_opts.append(t)
            cmd_start += 1
            if cmd_start < len(tokens):
                wsl_opts.append(tokens[cmd_start])
                cmd_start += 1
        elif t.startswith("-") and t not in ("-c", "-e", "--exec"):
            # 其他 WSL 标志，不包括 -c/-e（属于后续命令）
            wsl_opts.append(t)
            cmd_start += 1
        else:
            break

    cmd_tokens = tokens[cmd_start:]
    if not cmd_tokens:
        return ["wsl", *wsl_opts] if wsl_opts else ["wsl"]

    # wsl -e bash -c：-e 跳过登录 shell，$VAR 正确展开
    cmd_str = join_for_bash_c(cmd_tokens)
    return ["wsl", *wsl_opts, "-e", "bash", "-c", cmd_str]


def join_for_bash_c(tokens: list[str]) -> str:
    """将 token 列表拼接为 bash -c 的命令字符串。"""
    parts: list[str] = []
    for t in tokens:
        if any(c in t for c in (" ", "\t", "\n")):
            # 含空格 → 需要引号保护
            parts.append(shlex.quote(t))
        else:
            # 不含空格 → 保持原样，保留 $VAR、|、; 等 shell 元字符
            parts.append(t)
    return " ".join(parts)


def safe_cmd_encode(text: str) -> str:
    """将文本编码为 CMD 可以安全处理的格式。

    在 Windows 上，当命令通过 cmd /c 执行时，
    命令字符串需要与 CMD 的代码页兼容。
    如果文本中包含系统编码无法表示的字符，
    则进行安全替换。

    Args:
        text: 原始命令文本（Unicode）

    Returns:
        编码安全的命令文本
    """
    try:
        system_enc = locale.getpreferredencoding() or "utf-8"
        # 检查文本是否能无损编码为系统编码
        text.encode(system_enc)
        return text  # 所有字符可表示，无需转换
    except (UnicodeEncodeError, LookupError):
        # 部分字符无法表示，进行安全替换
        try:
            return text.encode(system_enc, errors="replace").decode(system_enc)
        except Exception:
            return text  # 实在不行就返回原文本


async def start_wsl_process(
    command: str,
    working_dir: str | None,
    env: dict[str, str],
) -> asyncio.subprocess.Process:
    """直接启动 WSL 进程，绕过 cmd.exe 和 bash。"""
    wsl_args = parse_wsl_args(command)
    logger.debug("WSL direct exec: %s", wsl_args)

    # 确保 WSL 内的 locale 为 UTF-8
    if "LANG" not in env:
        env["LANG"] = "en_US.UTF-8"

    return await asyncio.create_subprocess_exec(
        *wsl_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE,
        cwd=working_dir,
        env=env,
    )


async def spawn_local_process(
    command: str,
    working_dir: str | None,
    merged_env: dict[str, str],
    is_windows: bool,
) -> asyncio.subprocess.Process:
    """按后端优先级选择本地 shell 并拉起进程（模块 docstring 五级优先级）。"""
    use_bash_msys = is_windows and shutil.which("bash")
    wsl_available = is_windows and shutil.which("wsl")
    use_wsl_bash = is_windows and wsl_available
    use_wsl_direct = is_windows and is_wsl_command(command)

    if use_wsl_direct:
        # WSL 直连
        return await start_wsl_process(
            command=command,
            working_dir=working_dir,
            env=merged_env,
        )
    if use_wsl_bash:
        # WSL -e bash -c：跳过登录 shell，$VAR 正确展开
        if "LANG" not in merged_env:
            merged_env["LANG"] = "en_US.UTF-8"
        # 复合命令加 pipefail（bash 原生支持），让管道失败退出码正确冒泡
        effective_cmd = f"set -o pipefail; {command}" if is_compound_command(command) else command
        return await asyncio.create_subprocess_exec(
            "wsl",
            "-e",
            "bash",
            "-c",
            effective_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=merged_env,
        )
    if use_bash_msys:
        # MSYS2 Git Bash（有 $VAR 参数转换问题，但作为后备）
        if "LANG" not in merged_env:
            merged_env["LANG"] = "en_US.UTF-8"
        # 复合命令加 pipefail，让管道失败退出码正确冒泡
        effective_cmd = f"set -o pipefail; {command}" if is_compound_command(command) else command
        return await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            effective_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=merged_env,
        )
    if is_windows:
        # Windows CMD（最后手段，无 Unix shell 能力）：使用 safe_cmd_encode 确保中文路径正确编码
        safe_command = safe_cmd_encode(command)
        full_command = f'cmd /c "{safe_command}"'
        return await asyncio.create_subprocess_shell(
            full_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=merged_env,
        )
    # Linux/POSIX：复合命令用 bash -c 执行（bash 原生支持 pipefail）。
    # 不能走 /bin/sh（Ubuntu 即 dash）—— dash 不支持 `set -o pipefail`，
    # 即使 2>/dev/null 屏蔽报错，set 本身退出码 2 仍会让简单命令异常失败、
    # 输出丢失。bash 在所有 Linux 发行版都可用，与上面 WSL/MSYS 分支语义对齐。
    if is_compound_command(command) and shutil.which("bash"):
        effective_cmd = f"set -o pipefail; {command}"
        return await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            effective_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=merged_env,
        )
    return await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE,
        cwd=working_dir,
        env=merged_env,
    )
