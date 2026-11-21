"""
bash 工具面（0.2 自包含版）—— 四单词工具：bash / log / input / kill

⚠️ 安全威胁模型（H3）
=================

bash 工具的设计语义就是"执行用户命令"，**无法靠输入过滤根治**——
shell 元字符（| $() `` ;）天然有效，黑名单不可穷举（python -c "import os;
os.system('...')" 即绕过 SecurityChecker）。

SecurityChecker 的正则黑名单只拦**不可逆灾难**（rm -rf /、mkfs、dd 等
手滑即无法挽回的操作），**不是安全边界**。curl | sh 这类"危险但合法"的
模式不在此层硬拦，而是降级为 warning + 管道层审批。真正的控制是**隔离**。

本工具作为独立 sidecar 进程运行（MCP stdio），不依赖 src 树；
隔离决策由内核统一处理，本工具只负责执行命令。

进程生命周期与 handle（ADR 2026-11-07 决策 1，工具面四单词重构）：
- 本模块被 server.py 以**模块级单例**持有——所有 MCP 调用共享同一个
  BashTool/ProcessManager，跨调用保留 active_processes。
- 四工具统一凭 handle（不透明串）定位进程，取代裸 pid：内含 pid + owner
  + 校验和（encode_handle/decode_handle），越权校验分两段——handle 内
  owner vs 调用方注入 owner（防伪造句柄）、handle 内 owner vs 活进程
  metadata / 磁盘日志头 owner（防 pid 复用劫持）。
- 捕获契约（T1，SDK process_runtime）：绝对路径持久日志、击杀前有界排空、
  终止原因如实透传——kill 后 log 仍可读完整输出。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from result_types import (
    ToolResult,
    create_failure_result,
    create_success_result,
)

from agentos_plugin_sdk.process_runtime import (
    BackendUnhealthyError,
    ProcessInfo,
    ProcessLogReadError,
    ProcessManager,
    SpawnFirstLineTimeoutError,
)
from agentos_plugin_sdk.workspace_aware import WorkspaceAwareMixin

# POSIX 信号编号 → 名称映射。进程在 Linux 容器内运行，退出码遵循 POSIX
# 128+n 语义，但本工具的宿主可能是 Windows——Windows 的 signal.Signals 枚举
# 不含 SIGKILL(9)/SIGSEGV(11) 等 POSIX 信号（Windows 无这些信号），直接用
# signal.Signals(n) 反查会抛 ValueError。故内置 POSIX 标准信号名表，跨平台
# 一致，不依赖宿主 OS 的 signal 模块。
_POSIX_SIGNAL_NAMES: dict[int, str] = {
    1: "SIGHUP",
    2: "SIGINT",
    3: "SIGQUIT",
    4: "SIGILL",
    6: "SIGABRT",
    8: "SIGFPE",
    9: "SIGKILL",
    11: "SIGSEGV",
    13: "SIGPIPE",
    14: "SIGALRM",
    15: "SIGTERM",
}


def describe_exit_code(exit_code: int | None) -> str | None:
    """把进程退出码解释为人类可读的终止原因（仅信号终止时返回非空）。

    POSIX 退出码语义：
    - 0        → 正常成功
    - 1..124   → 程序自身的错误码（含义因程序而异，不在此解释）
    - 128+n    → 被信号 n 终止（如 137 = 128 + 9 = SIGKILL）

    本函数**只在被信号终止时**返回描述字符串（含信号编号 + 名称），
    正常退出/程序自身错误码返回 None——避免给每个退出码硬编码提醒。

    信号名经内置 POSIX 信号表反查（SIGKILL/SIGTERM/SIGSEGV 等），查不到
    就只给编号。让 LLM 拿到的是「原始信号」而非编造的归因。

    Args:
        exit_code: process.wait() 返回的退出码（可能为 None，表示尚未结束）。

    Returns:
        被信号终止时返回 "信号 N (SIGXXX) 终止" 形式的描述；否则 None。
    """
    if exit_code is None or exit_code < 128 or exit_code > 192:
        return None
    signum = exit_code - 128
    name = _POSIX_SIGNAL_NAMES.get(signum, f"signal {signum}")
    return f"信号 {signum} ({name}) 终止"


# ── 图片产物引导（2026-11-16） ─────────────────────────────────────
# bash/log 结果检测到图片文件产物时附一行引导。背景：端到端验证实证，模型用
# bash 截图后只 ls/rm 管产物、从不读图，以文本正则代行视觉判定产出假绿。
# 本层只做「存在性验证过的路径浮出」；是否看、看哪张仍由模型决策，注图闸门
# 依旧在 tool_core inject_multimodal——本层绝不注图。

_IMAGE_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/])?[\w.\\/-]+\.(?:png|jpe?g|gif|webp|bmp)\b",
    re.IGNORECASE,
)
_IMAGE_HINT_MAX_LISTED = 3
_IMAGE_HINT_MAX_COLLECT = 20
IMAGE_HINT_MARK = "[图片产物提示]"


def build_image_artifact_hint(
    text: str | None, working_dir: str | None
) -> str | None:
    """从命令与输出文本提取图片产物路径，组装一行看图引导（无产物返回 None）。

    只认「存在性验证过」的路径——相对路径按 working_dir 解析（无锚点则跳过），
    绝对路径直取；输出里仅提及但不存在的路径不提示。命中去重保序，列出前
    _IMAGE_HINT_MAX_LISTED 个，其余以计数带过（收集上限
    _IMAGE_HINT_MAX_COLLECT，防目录级清单拖慢结果组装）。
    """
    if not text:
        return None
    base = Path(working_dir) if working_dir else None
    found: list[str] = []
    seen: set[str] = set()
    for m in _IMAGE_PATH_RE.finditer(text):
        token = m.group(0)
        cand = Path(token)
        if cand.is_absolute():
            pass
        elif base is not None:
            cand = base / cand
        else:
            # 相对路径无解析锚点（working_dir 缺席）：无法有意义地验证存在性，
            # 不提示——防误挂宿主 CWD 同名文件。
            continue
        try:
            if not cand.is_file():
                continue
        except OSError:
            continue
        key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        found.append(token)
        if len(found) >= _IMAGE_HINT_MAX_COLLECT:
            break
    if not found:
        return None
    listed = "、".join(found[:_IMAGE_HINT_MAX_LISTED])
    if len(found) >= _IMAGE_HINT_MAX_COLLECT:
        count_suffix = f" 等共 {len(found)}+ 个"
    elif len(found) > _IMAGE_HINT_MAX_LISTED:
        count_suffix = f" 等共 {len(found)} 个"
    else:
        count_suffix = ""
    return (
        f"{IMAGE_HINT_MARK} {listed}{count_suffix}"
        "——UI/界面证据请用 file_read 查看核验，勿以文本匹配代行视觉判定"
    )


# 失败时回传给 LLM 的错误消息由 LogCompressor 提取行组装（信号描述 + 错误行 +
# 最新消息），不做原始尾部截取——大输出兜底（原文存档 + 提取 + 定位符）已统一
# 移交 pipeline 的 spill_guard 输出插件（task_spill_guard.md 任务 2）。


class SecurityChecker:
    """命令防误操作检查器（**非安全边界**）。

    用正则黑名单匹配 rm -rf /、fork bomb 等**不可逆灾难**模式，防止手滑
    造成无法挽回的破坏。这是"防误操作"层级，**不能抵御恶意构造的命令**
    ——shell 元字符天然有效，黑名单不可穷举（见模块文档 H3 威胁模型）。
    抵御恶意命令依靠容器隔离，不靠这里。

    分层原则（按"可逆性"而非"看起来像不像黑客"）：
    - DANGEROUS_PATTERNS：**不可逆灾难**硬拦——一旦执行无法挽回（rm -rf、
      mkfs、dd、format、shutdown 等）。硬拦与审批层互不干涉，是最后兜底。
    - CAUTION_PATTERNS：**危险但合法**降级——curl/wget/管道到 shell 等
      是主流工具的官方用法，不应硬拦。命中只标 warning，是否放行交给管道
      层审批决策（用户批准即可执行）。
    """

    # 不可逆灾难命令：硬拦（一旦执行无法挽回）
    DANGEROUS_PATTERNS: ClassVar[list[str]] = [
        r"\brm\s+-rf\b",  # rm -rf（词边界匹配，覆盖 rm -rf / 等所有变体）
        r";\s*rm\b",  # 分号连接 rm
        r";\s*del\b",  # 分号连接 del
        r";\s*format\b",  # 分号连接 format
        r"\bmkfs\b",  # 格式化命令
        r"\bdd\s+if=",  # dd 写入
        r">\s*/dev/sd[a-z]",  # 写入磁盘设备
        r":\(\)\s*\{\s*:\|:&\s*\};:",  # Fork bomb
        r"\bdel\s+/f\s+/s\s+/q\b",  # Windows 强制删除
        r"\brmdir\s+/s\s+/q\b",  # Windows 强制删除目录
        r"\bformat\s+[a-z]:",  # Windows 格式化
        r"\bshutdown\b",  # 关机
        r"\breboot\b",  # 重启
        r"\bpoweroff\b",  # 关机
        r"\bhalt\b",  # 停机
    ]

    # 危险但合法的命令：降级标 warning（不阻断），由管道层审批把关
    CAUTION_PATTERNS: ClassVar[list[str]] = [
        "curl",
        "wget",
        "rm ",
        "del ",
        "rmdir",
        "mv ",
        "move ",
        "cp ",
        "copy ",
        ">",
        ">>",
        "$(",  # 命令替换（脚本常用，不应阻断）
        "`",  # 反引号命令替换
        "| sh",  # 管道到 sh（合法常见模式，降级审批而非硬拦）
        "| bash",  # 管道到 bash
        "| zsh",  # 管道到 zsh
        "| fish",  # 管道到 fish
    ]

    # 手动后台化模式：降级标 warning（不阻断）。
    # 本工具自带后台执行（background=true）+ log 轮询语义，手动 nohup/
    # setsid/disown/行尾& 会让进程脱离 ProcessManager 管理，沦为孤儿。
    BACKGROUND_PATTERNS: ClassVar[list[str]] = [
        r"(?:^|[;|&]\s*)\s*nohup\b",  # nohup 作为命令 token
        r"(?:^|[;|&]\s*)\s*setsid\b",  # setsid 作为命令 token
        r"(?:^|[;|&]\s*)\s*disown\b",  # disown 作为命令 token
        r"\s&\s*$",  # 行尾独立的 & 后台符（不误伤 a&b / 2>&1 中段 &）
    ]

    def __init__(self, allowed_commands: list[str] | None = None):
        """初始化安全检查器"""
        self.allowed_commands = set(allowed_commands) if allowed_commands else None
        self._compiled_dangerous = [re.compile(p, re.IGNORECASE) for p in self.DANGEROUS_PATTERNS]
        self._compiled_background = [re.compile(p, re.IGNORECASE) for p in self.BACKGROUND_PATTERNS]

    def check(self, command: str) -> tuple[bool, bool, str | None]:
        """
        检查命令安全性

        使用正则表达式模式匹配检测危险命令，防止命令注入绕过。

        Returns:
            tuple[bool, bool, str | None]: (是否安全, 是否需要警告, 错误消息)
        """
        cmd_stripped = command.strip()

        for pattern, compiled in zip(self.DANGEROUS_PATTERNS, self._compiled_dangerous, strict=True):
            if compiled.search(cmd_stripped):
                return False, False, f"命令包含危险操作: {pattern}"

        if self.allowed_commands is not None:
            base_cmd = cmd_stripped.split()[0] if cmd_stripped else ""
            if base_cmd not in self.allowed_commands:
                return False, False, f"命令不在允许列表中: {base_cmd}"

        cmd_lower = cmd_stripped.lower()
        for pattern in self.CAUTION_PATTERNS:
            if pattern.lower() in cmd_lower:
                return True, True, f"命令包含潜在风险操作: {pattern}"

        # 检查手动后台化模式（nohup/setsid/disown/行尾&）→ warning，不阻断
        for compiled in self._compiled_background:
            if compiled.search(cmd_stripped):
                return (
                    True,
                    True,
                    (
                        "命令包含手动后台化操作，本工具已自带 background=true 后台执行，"
                        "手动后台化会使进程脱离管理；长任务用 background=true 启动，"
                        "或前台超时返回 handle 后用 log 轮询即可"
                    ),
                )

        return True, False, None


# ── handle 编解码（ADR 2026-11-07 决策 1）─────────────────────────
# 形态：`bs1.<base64url(json{pid,owner})>.<sha256 前 8 位>`。校验和拦手误与
# 注入串（畸形 handle → INVALID_HANDLE，不进任何查询/路径拼接）；owner 经
# base64 内嵌不外露明文（不透明引用），解码后供两段越权校验。

_HANDLE_SCHEME = "bs1"


def encode_handle(pid: int, owner: str | None) -> str:
    """把进程身份编码为不透明 handle（内含 pid + owner + 校验和）。

    Args:
        pid: 进程 ID（SDK ProcessManager 的定位键，仅内部消费不外露）。
        owner: 会话身份（spawn 时刻记录；None = 无身份会话）。

    Returns:
        `bs1.<body>.<digest>` 形态的不透明串；digest = body 的 sha256 前 8 位。
    """
    payload = json.dumps({"pid": pid, "owner": owner}, ensure_ascii=False, separators=(",", ":"))
    body = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    digest = hashlib.sha256(body.encode("ascii")).hexdigest()[:8]
    return f"{_HANDLE_SCHEME}.{body}.{digest}"


def decode_handle(handle: str) -> tuple[int, str | None] | None:
    """解码 handle → (pid, owner)；形态/校验和不符返回 None（不猜不兜）。

    任何篡改（body 或 digest 任一位）都会因 sha256 不匹配被拒绝；非字符串
    输入安全返回 None。pid <= 0 视为非法（OS pid 从 1 起）。
    """
    if not isinstance(handle, str):
        return None
    parts = handle.split(".")
    if len(parts) != 3:
        return None
    scheme, body, digest = parts
    if scheme != _HANDLE_SCHEME:
        return None
    if not re.fullmatch(r"[0-9a-f]{8}", digest):
        return None
    if hashlib.sha256(body.encode("ascii", errors="ignore")).hexdigest()[:8] != digest:
        return None
    pad = "=" * (-len(body) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + pad).decode("utf-8"))
        pid = int(payload["pid"])
        owner = payload.get("owner")
    except (KeyError, TypeError, ValueError):
        # KeyError: payload 缺 pid；TypeError/ValueError: base64 非法字符
        # （binascii.Error 为 ValueError 子类）/JSON 结构错/int() 失败。
        return None
    if pid <= 0:
        return None
    if owner is not None and not isinstance(owner, str):
        return None
    return pid, owner


class BashTool(WorkspaceAwareMixin):
    """bash 工具面（0.2 sidecar）—— 四单词工具入口。

    - run_command：bash 工具（执行命令，前台阻塞或 background 立即返回）
    - read_log：log 工具（block=false 读当前态 / block=true 等到完成）
    - send_input：input 工具（向等待输入的进程发文本）
    - terminate：kill 工具（终止进程，reason=agent 透传）

    隔离决策由内核统一处理，本工具只负责执行命令。
    进程状态由内部 ProcessManager 维护，**实例需在 sidecar 进程内长期存活**
    （server.py 以模块级单例持有），跨 MCP 调用保留 active_processes。
    """

    # 默认超时时间（秒）
    DEFAULT_TIMEOUT: ClassVar[int] = 30

    # 最大允许超时（秒）。ADR 2026-11-07 决策 3：290 → 600 per-call 可调；
    # 旧 290 上限的动因（内核 invoker 缺省工具界 300s）由 plugin.json 的
    # bash 工具 timeout_ms: 660000 声明同批解除（600s + 启动余量）。
    MAX_TIMEOUT: ClassVar[int] = 600

    # 短输出阈值（字符数）：低于此值直接全量返回原始 output，不走任何提取——
    # 短输出本身信息量小、噪音少，提取反而可能切坏（如半行被截）。
    # 用字符数而非行数：行数受换行风格影响大（CRLF/LF、超长单行 vs 短行），
    # 字符数才是真实体积。约 2KB（~500 token）是 LLM 一次能轻松消化的量。
    SHORT_OUTPUT_CHAR_THRESHOLD: ClassVar[int] = 2000

    def _append_image_hint(
        self,
        data: dict[str, Any],
        command: str | None,
        working_dir: str | None,
    ) -> None:
        """向结果 output 追加图片产物引导行（幂等；无产物/非文本 output 不追加）。"""
        output = data.get("output")
        if not isinstance(output, str):
            return
        scan_text = f"{command}\n{output}" if command else output
        hint = build_image_artifact_hint(scan_text, working_dir)
        if hint and hint not in output:
            data["output"] = output + "\n" + hint

    @staticmethod
    def _compact_result_data(
        handle: str,
        output: str | None,
        summary_obj: dict[str, Any],
        exit_code: int | None,
        status: str = "completed",
    ) -> dict[str, Any]:
        """精简 result_data，去掉 LLM 不需要的字段以节省 token。

        核心原则：**短输出全量返回，长输出附语义提取、原文不截断**。同一套逻辑
        覆盖 completed（进程结束）和 running（超时/background 返回中间态）两种
        场景——区别仅是 status 字段 + exit_code 有无，提取策略完全一致。

        - 短输出（<=SHORT_OUTPUT_CHAR_THRESHOLD 字符）：信息量小、噪音少，直接
          全量返回原始 output，不走提取——提取反而可能切坏（如半行被截）。
        - 长输出（>阈值）：**完整 output 原样返回**（大输出兜底由 pipeline 的
          spill_guard 统一负责：原文存档 + 头尾提取 + 定位符，task_spill_guard
          任务 2 已删除工具内截断），额外附 summary/error_lines/latest_message/
          progress（LogCompressor 已筛掉刷屏噪音的高价值信息）。

        保留字段：
        - handle/output/status：始终保留（exit_code 仅 completed 时有）
        - terminated_by_signal：信号终止时附加（如实暴露原始信号，不编造归因）
        - errors/warnings/error_lines：非空时保留（LogCompressor 过滤后的错误，去重）
        - summary/latest_message/progress：仅长输出时保留（统计摘要 + 最新输出行）
        """
        output_len = len(output or "")
        is_short = output_len <= BashTool.SHORT_OUTPUT_CHAR_THRESHOLD

        data: dict[str, Any] = {
            "handle": handle,
            "output": output,
            "status": status,
        }
        if status == "completed":
            data["exit_code"] = exit_code if exit_code is not None else 0

        # 信号终止时附加结构化字段：把原始信号（编号+名称）如实暴露给 LLM，
        # 而非让它从孤立数字猜。137=SIGKILL 常见但非唯一（OOM/timeout/外部 kill
        # 都可能发 SIGKILL，具体归因由调用方结合上下文判断，这里不编造）。
        if exit_code is not None:
            signal_desc = describe_exit_code(exit_code)
            if signal_desc:
                data["terminated_by_signal"] = signal_desc

        # errors/warnings 是 LogCompressor 提取的高价值信息（去重后的错误行），
        # 无论输出长短都应保留——短输出即便只有几行，只要命中 error 模式就该让
        # LLM 看到，避免它在一小段输出里还要自己找问题。
        # errors/warnings 是计数（int），error_lines 是具体错误行（list[str]）。
        # 计数让 LLM 知道"有几条错误"，error_lines 让它直接看到内容——两者互补。
        errors = summary_obj.get("errors", 0)
        warnings = summary_obj.get("warnings", 0)
        if errors:
            data["errors"] = errors
        if warnings:
            data["warnings"] = warnings
        error_lines = summary_obj.get("error_lines", [])
        if error_lines:
            data["error_lines"] = error_lines

        # 短输出到此为止：信息已全量在 output 里，summary（[N行]/类型/进度等
        # 统计性描述）对短输出是冗余噪音，跳过。
        if is_short:
            return data

        # 长输出：output 保持完整原文（spill_guard 统一兜底），附 summary/
        # latest_message/progress（LogCompressor 已筛掉刷屏噪音）。
        summary_lines = summary_obj.get("summary", [])
        if summary_lines:
            data["summary"] = summary_lines
        latest = summary_obj.get("latest_message") or ""
        if latest:
            data["latest_message"] = latest
        progress = summary_obj.get("progress")
        if progress:
            data["progress"] = progress

        return data

    @staticmethod
    def _build_failure_message(
        exit_code: int,
        output: str | None,
        summary_obj: dict[str, Any],
        command: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """组装失败时的 error_msg + metadata（bash 完成路径与 log block=true 共用）。

        按"短全量、长提取"原则：
        - 短输出（<=SHORT_OUTPUT_CHAR_THRESHOLD 字符）：直接全量原文，信息量小、
          截断反而可能切坏。
        - 长输出：信号描述 + LogCompressor 提取的错误列表（已去重筛噪音）+
          latest_message——不拼原始输出尾部（大输出兜底由 spill_guard 统一负责，
          task_spill_guard.md 任务 2：工具只保留"执行 + 语义提取"）。

        R9 信封上下文：失败信封必须让模型免猜——error_msg 首行带实际执行的
        命令原文；metadata 携带 command / stdout / stderr / exit_code 四件
        （stdout/stderr 按 reader 的 `[stderr] ` 行前缀从合并捕获里拆分；
        单字段超 64KB 截断并留痕，防病态大输出打爆信封）。
        """
        signal_desc = describe_exit_code(exit_code)
        output_len = len(output or "")
        is_short = output_len <= BashTool.SHORT_OUTPUT_CHAR_THRESHOLD

        parts: list[str] = []
        if command:
            parts.append(f"实际执行的命令：\n{command}")
        if signal_desc:
            parts.append(f"{signal_desc}（退出码 {exit_code}）")
        else:
            parts.append(f"命令执行失败，退出码: {exit_code}")

        if is_short:
            if output:
                parts.append(f"输出：\n{output.rstrip()}")
        else:
            # error_lines 是 LogCompressor 提取的错误行原文列表（去重筛噪音）；
            # 注意与 errors（计数 int）区分——这里要的是具体行内容。
            error_lines = summary_obj.get("error_lines") or []
            latest = summary_obj.get("latest_message") or ""
            if error_lines:
                parts.append("错误行：\n" + "\n".join(error_lines[:20]))
            if latest:
                parts.append(f"最新输出：{latest}")

        error_msg = "\n".join(parts)
        stdout_text, stderr_text = BashTool._split_streams(output or "")
        fail_meta: dict[str, Any] = {
            "exit_code": exit_code,
            "command": BashTool._clip_envelope_field(command or ""),
            "stdout": BashTool._clip_envelope_field(stdout_text),
            "stderr": BashTool._clip_envelope_field(stderr_text),
        }
        if signal_desc:
            fail_meta["terminated_by_signal"] = signal_desc
        return error_msg, fail_meta

    FAILURE_CONTEXT_FIELD_LIMIT: ClassVar[int] = 65536

    @staticmethod
    def _clip_envelope_field(text: str) -> str:
        """信封字段截断（64KB 上限，截断留痕防病态大输出打爆信封）。"""
        limit = BashTool.FAILURE_CONTEXT_FIELD_LIMIT
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n…（截断，原始 {len(text)} 字符）"

    @staticmethod
    def _split_streams(output: str) -> tuple[str, str]:
        """合并捕获（stderr 行带 ``[stderr] `` 前缀）拆回 stdout / stderr 全文。"""
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        prefix = "[stderr] "
        for line in output.splitlines():
            if line.startswith(prefix):
                stderr_lines.append(line[len(prefix) :])
            else:
                stdout_lines.append(line)
        return "\n".join(stdout_lines), "\n".join(stderr_lines)

    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        allowed_commands: list[str] | None = None,
    ):
        """初始化 Bash 工具"""
        self.timeout = timeout

        # 安全组件
        self.security = SecurityChecker(allowed_commands)

        # 进程管理器（跨 MCP 调用共享——单例持有本实例时状态得以保留）
        self.process_manager = ProcessManager()

    # ── 会话身份（owner） ────────────────────────────────────────

    @staticmethod
    def _owner_from_inputs(inputs: dict[str, Any]) -> str | None:
        """从调用参数提取会话身份（owner）。

        0.2 内核在 tool-executor.invoke 时向 args 注入 `_owner`
        （thread_id/session_id 派生），插件据此做 handle 级越权防护。
        优先级：_owner > session_id > thread_id > workspace > project_root。
        全部缺失返回 None（无身份调用——仅双无身份场景放行）。
        """
        for key in ("_owner", "session_id", "thread_id", "workspace", "project_root"):
            value = inputs.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    @staticmethod
    def _check_owner(
        pid: int,
        caller_owner: str | None,
        proc_owner: str | None,
    ) -> tuple[bool, str]:
        """handle/pid 级操作的 owner 校验（拒绝分支 err 非空，放行分支 err 为空串）。

        严格语义（防跨会话越权/劫持）：
        - 双方身份一致 → 放行
        - 双方皆无身份 → 放行（无身份可校验的兼容路径，0.2 注入后不出现）
        - 其余（身份缺失/不匹配）→ 拒绝
        """
        if caller_owner is None and proc_owner is None:
            return True, ""
        if caller_owner is None:
            return False, f"进程 {pid} 属于已标识会话，无身份调用被拒绝（PROCESS_FORBIDDEN）"
        if proc_owner is None:
            return False, f"进程 {pid} 无归属会话，不允许跨身份操作（PROCESS_FORBIDDEN）"
        if caller_owner != proc_owner:
            return False, (
                f"进程 {pid} 属于其他会话，越权操作被拒绝（PROCESS_FORBIDDEN，"
                f"caller={caller_owner!r} vs owner={proc_owner!r}）"
            )
        return True, ""

    # ── handle 解析与身份链 ──────────────────────────────────────

    def _resolve_handle(self, inputs: dict[str, Any]) -> tuple[int, str | None, ToolResult | None]:
        """解码 handle 并完成第一段身份校验 → (pid, handle_owner) 或失败结果。

        校验链第一段：handle 缺失 → MISSING_HANDLE；形态/校验和不符 →
        INVALID_HANDLE（畸形串不进任何查询/路径拼接）；handle 内 owner vs
        调用方注入 owner（_check_owner 严格语义）→ 不符即 PROCESS_FORBIDDEN。
        第二段（handle owner vs 活进程 metadata / 磁盘日志头 owner）由各操作
        定位到目标后执行（_check_live_owner / 磁盘路径内联）。
        """
        handle = inputs.get("handle")
        if not handle or not str(handle).strip():
            return 0, None, create_failure_result("handle 不能为空", "MISSING_HANDLE")
        decoded = decode_handle(str(handle).strip())
        if decoded is None:
            return 0, None, create_failure_result(
                f"handle 无效或已损坏（不匹配任何已知进程引用）: {handle!r}",
                "INVALID_HANDLE",
            )
        pid, handle_owner = decoded
        caller_owner = self._owner_from_inputs(inputs)
        ok, err = self._check_owner(pid, caller_owner, handle_owner)
        if not ok:
            return 0, None, create_failure_result(err, "PROCESS_FORBIDDEN")
        return pid, handle_owner, None

    def _check_live_owner(
        self,
        pid: int,
        handle_owner: str | None,
        proc_info: ProcessInfo,
    ) -> ToolResult | None:
        """身份链第二段（活进程）：handle 内 owner vs 进程 metadata owner。

        不符（含 pid 复用后新进程归属另一会话）→ PROCESS_FORBIDDEN；
        相符返回 None 放行。
        """
        ok, err = self._check_owner(pid, handle_owner, proc_info.metadata.get("owner"))
        if not ok:
            return create_failure_result(err, "PROCESS_FORBIDDEN")
        return None

    # ── bash 工具（执行命令） ────────────────────────────────────

    async def run_command(
        self,
        inputs: dict[str, Any],
        on_output: Callable[[str], None] | None = None,
        abort_event: asyncio.Event | None = None,
    ) -> ToolResult:
        """bash 工具：执行 Shell 命令。

        - 前台（background 缺省 false）：阻塞到完成或等待上限（timeout，默认
          30 / 上限 MAX_TIMEOUT=600）。**超时不杀进程**：返回 running + handle
          并附带全部已捕获输出（ADR 决策 3 用户裁定修正版——控制权经 handle
          留给 agent，进程不死，完整输出恒可从 log 取回）。
        - background=true：立即返回 running + handle，不等命令完成。

        on_output：每行输出回调（server.py 经 ProgressReporter 节流后推
        tool_progress 进度）。

        abort_event：run 中止信号（run_abort_watch 检出 run_status=suspended 时
        置位）。前台等待期间置位 → 整树杀子进程并返回 RUN_ABORTED，run 立刻回到
        step 边界收尾。不置位/缺席时行为与旧版一致（看门狗关闭不改变本函数语义）。
        """
        self._init_workspace(inputs)
        command = inputs.get("command")
        if not command:
            return create_failure_result(
                error="command 不能为空",
                error_code="MISSING_COMMAND",
            )

        # 安全检查：容器隔离模式下跳过内部安全检查
        # 容器内执行已有独立的安全边界，反引号等 shell 特性是正常行为。
        # 注意：is_isolated 只信 _container_id（isolation_guard 服务端注入，
        # 且 param_inject 已剥离 LLM 夹带的下划线键）——不信任 _isolation_provider
        # 之类的声明式标记（声明式标记可被 LLM 伪造，不构成隔离凭据）。
        warning = None
        container_id = inputs.get("_container_id")
        is_isolated = bool(container_id)
        if not is_isolated:
            is_safe, needs_warning, message = self.security.check(command)
            if not is_safe:
                return create_failure_result(
                    error=f"安全检查失败: {message}",
                    error_code="SECURITY_CHECK_FAILED",
                )
            warning = message if needs_warning else None

        timeout = min(inputs.get("timeout", self.timeout), self.MAX_TIMEOUT)
        wd = self.get_working_dir(inputs)
        # fail-closed：非容器模式必须注入 workspace/working_dir——缺省时进程会
        # 落在 sidecar cwd（插件目录），把宿主仓库当执行工作区。
        if not container_id and wd is None:
            return create_failure_result(
                error="workspace/working_dir 未注入，拒绝在未锚定目录执行命令",
                error_code="WORKSPACE_NOT_INJECTED",
            )
        working_dir = str(wd) if wd else None
        owner = self._owner_from_inputs(inputs)
        # exec_backend 与 _container_id 同级的服务端注入（isolation_guard）：
        # 仅容器路径消费；无 _container_id 时不取（声明式标记不构成隔离凭据）
        raw_exec_backend = inputs.get("_exec_backend") if container_id else None
        exec_backend = raw_exec_backend if isinstance(raw_exec_backend, dict) else None

        try:
            # 启动进程。start_process 收到 log_dir 时会同步更新 process_manager.log_dir，
            # 保证后续 log 磁盘降级读时用同一目录。
            project_root = getattr(self, "_project_root", None)
            bash_log_dir = Path(project_root) / "logs" / "bash" if project_root else None
            pid, log_file = await self.process_manager.start_process(
                command=command,
                working_dir=working_dir,
                log_dir=bash_log_dir,
                container_id=container_id,
                owner=owner,
                on_output=on_output,
                exec_backend=exec_backend,
            )
            handle = encode_handle(pid, owner)

            if bool(inputs.get("background", False)):
                # background=true：立即返回 handle（进程状态经 log/input/kill 管理）
                proc_info = self.process_manager.get_process_info(pid)
                summary = self.process_manager.get_summary(pid) or {}
                data = self._compact_result_data(
                    handle=handle,
                    output=self.process_manager.get_output(pid),
                    summary_obj=summary,
                    exit_code=None,
                    status="running",
                )
                data["elapsed"] = (
                    round(time.time() - proc_info.start_time, 1) if proc_info is not None else 0.0
                )
                self._append_image_hint(data, command, working_dir)
                return create_success_result(
                    data=data,
                    metadata={"tool": "bash", "command": command, "warning": warning},
                )

            # 前台：轮询到完成或等待上限（超时进程不死，返回 running+handle+已捕获输出）
            start_time = time.time()
            while True:
                proc_info = self.process_manager.get_process_info(pid)
                # run 中止（先于超时判定）：run 已被停止时立刻收口，run 回到 step
                # 边界即按既有 suspended 语义收尾，注入队列随派发链 drain 消费。
                # 仅在进程确实还在跑时执行——已自然结束的调用按正常完成路径返回，
                # 不得伪造成「被中止」。
                if (
                    abort_event is not None
                    and abort_event.is_set()
                    and proc_info is not None
                    and proc_info.status == "running"
                ):
                    return await self._abort_result(pid, handle, command, warning)
                if time.time() - start_time >= timeout:
                    # 超时：running + handle + 全部已捕获输出（summary 缺失不阻拦
                    # 返回——输出即契约，get_output 恒可用）
                    summary = self.process_manager.get_summary(pid) or {}
                    data = self._compact_result_data(
                        handle=handle,
                        output=self.process_manager.get_output(pid),
                        summary_obj=summary,
                        exit_code=None,
                        status="running",
                    )
                    data["elapsed"] = (
                        round(time.time() - proc_info.start_time, 1)
                        if proc_info is not None
                        else None
                    )
                    self._append_image_hint(data, command, working_dir)
                    return create_success_result(
                        data=data,
                        metadata={"tool": "bash", "command": command, "warning": warning},
                    )
                if not proc_info or proc_info.status != "running":
                    break

                await asyncio.sleep(0.5)

            # 进程已完成，等输出读取任务收尾再获取摘要和输出
            # （_sync_poll_process 可能抢在输出 flush 落盘前观测到退出）
            await self.process_manager.wait_output_settled(pid)
            summary = self.process_manager.get_summary(pid) or {}
            output = self.process_manager.get_output(pid)

            if summary:
                exit_code = summary.get("exit_code", 0)
                result_data = self._compact_result_data(
                    handle=handle,
                    output=output,
                    summary_obj=summary,
                    exit_code=exit_code,
                )

                if exit_code != 0:
                    error_msg, fail_meta = BashTool._build_failure_message(
                        exit_code, output, summary, command=command,
                    )
                    return create_failure_result(
                        error=error_msg,
                        error_code="COMMAND_FAILED",
                        metadata=fail_meta,
                    )

                self._append_image_hint(result_data, command, working_dir)
                return create_success_result(
                    data=result_data,
                    metadata={"tool": "bash", "command": command, "warning": warning},
                )
            # summary 不可得（记录已清且磁盘兜底双 miss）：观测失败 ≠ 调用
            # 失败——降级返回 handle + 已捕获输出（输出即契约，同超时路径
            # 口径）。退出码未知，不得伪造缺省 0。degraded 标记 + log_dir
            # 现场随 metadata 落痕，下次发生自动留证。
            degraded = self._compact_result_data(
                handle=handle,
                output=output,
                summary_obj={},
                exit_code=None,
                status="completed",
            )
            degraded.pop("exit_code", None)
            degraded["degraded"] = "summary_unavailable"
            degraded["note"] = "进程已完成但摘要不可得；可凭 handle 用 log 工具重读"
            self._append_image_hint(degraded, command, working_dir)
            return create_success_result(
                data=degraded,
                metadata={
                    "tool": "bash",
                    "command": command,
                    "warning": warning,
                    "degraded": "summary_unavailable",
                    "log_dir": str(getattr(self.process_manager, "log_dir", "") or ""),
                },
            )

        except SpawnFirstLineTimeoutError as e:
            return create_failure_result(
                error=(
                    f"命令未能启动：{e.timeout:.0f}s 内未收到执行环境 pid 上报"
                    f"（attempt {e.attempt}/{e.max_attempts}），判定 spawn 通道阻塞。"
                    f"已终止挂起的执行客户端（host_pid={e.host_pid}），命令未确认执行、"
                    f"不会造成重复副作用。可先排查隔离后端（如 WSL："
                    f"`wsl -d Ubuntu --exec true`）后重试。"
                ),
                error_code="SPAWN_NO_FIRST_LINE",
                metadata={
                    "tool": "bash",
                    "command": command,
                    "host_pid": e.host_pid,
                    "attempt": e.attempt,
                    "max_attempts": e.max_attempts,
                    "log_file": str(e.log_file) if e.log_file else None,
                },
            )
        except BackendUnhealthyError as e:
            return create_failure_result(
                error=(
                    f"执行后端不可用：{e.reason}。已快速失败，未启动命令"
                    f"（避免再次挂起与客户端泄漏）。排查：WSL 场景先执行 "
                    f"`wsl -d Ubuntu --exec true`，无响应则重启 WSL 服务"
                    f"（管理员 PowerShell：`Restart-Service WSLService`）。"
                ),
                error_code="BACKEND_UNHEALTHY",
                metadata={"tool": "bash", "command": command, "backend": e.backend_key},
            )
        except Exception as e:
            return create_failure_result(
                error=f"执行命令失败: {str(e)}",
                error_code="EXECUTION_FAILED",
            )

    # 终止等待宽限（秒）：终止走 SDK 整树杀，超此宽限视为后端路径不可用，
    # 转直杀兜底（见 `_abort_result`）。取 1.5s：psutil 在场时整树杀是毫秒级，
    # 宽限只是兜底阈值；取大值会把「停止」拖成秒级等待。
    ABORT_TERMINATE_GRACE_SECS: ClassVar[float] = 1.5

    async def _abort_result(
        self,
        pid: int,
        handle: str,
        command: str,
        warning: str | None,
    ) -> ToolResult:
        """run 中止收口：整树杀前台子进程并返回结构化失败（RUN_ABORTED）。

        调用前提由 [`run_command`] 保证：abort_event 已置位且进程仍在跑。

        两级终止（先整树、后兜底）：
        1. SDK `terminate_process(force=True, reason="run_abort")`——整树杀防
           孙子进程孤儿，并落终止行簿记；终止原因如实穿透（不伪装 user/agent）。
        2. 宽限内未收口或进程仍活（后端不可用，如 psutil 缺席导致树杀空转）时，
           直接杀 asyncio 子进程句柄——run 不被卡住是硬要求，簿记可缺、
           进程必须死。
        """
        proc_info = self.process_manager.get_process_info(pid)
        success, err = False, None
        try:
            success, err = await asyncio.wait_for(
                self.process_manager.terminate_process(pid, force=True, reason="run_abort"),
                self.ABORT_TERMINATE_GRACE_SECS,
            )
        except TimeoutError:
            err = f"整树杀 {self.ABORT_TERMINATE_GRACE_SECS}s 内未收口"
        if proc_info is not None and proc_info.process is not None:
            if proc_info.process.returncode is None:
                try:
                    proc_info.process.kill()
                    await asyncio.wait_for(proc_info.process.wait(), 5.0)
                    success = True
                except ProcessLookupError:
                    success = True  # 已退出：等价于终止达成
                except TimeoutError:
                    err = (err + "；" if err else "") + "子进程句柄强杀 5s 内未退出"
        output = self.process_manager.get_output(pid)
        data = self._compact_result_data(
            handle=handle,
            output=output,
            summary_obj=self.process_manager.get_summary(pid) or {},
            exit_code=None,
            status="terminated",
        )
        data["terminated_by"] = "run_abort"
        if not success and err:
            data["terminate_error"] = err
        return create_failure_result(
            error=(
                "命令因 run 被停止而中止（run_status=suspended，看门狗整树杀进程）；"
                "本条失败是停止语义的如实结果，不是命令自身出错"
                + (f"；终止返回: {err}" if not success and err else "")
            ),
            error_code="RUN_ABORTED",
            metadata={
                "tool": "bash",
                "command": command,
                "terminated_by": "run_abort",
                "data": data,
                "warning": warning,
            },
        )
        data = self._compact_result_data(
            handle=handle,
            output=output,
            summary_obj=self.process_manager.get_summary(pid) or {},
            exit_code=None,
            status="terminated",
        )
        data["terminated_by"] = "run_abort"
        if not success and err:
            data["terminate_error"] = err
        return create_failure_result(
            error=(
                "命令因 run 被停止而中止（run_status=suspended，看门狗整树杀进程）；"
                "本条失败是停止语义的如实结果，不是命令自身出错"
                + (f"；终止返回: {err}" if not success and err else "")
            ),
            error_code="RUN_ABORTED",
            metadata={
                "tool": "bash",
                "command": command,
                "terminated_by": "run_abort",
                "data": data,
                "warning": warning,
            },
        )

    # ── log 工具（读当前态 / 等待完成） ──────────────────────────

    async def read_log(self, inputs: dict[str, Any]) -> ToolResult:
        """log 工具：凭 handle 读日志。

        - block=false（缺省）：立即返回当前态/完整日志。进程活跃 → 内存路径
          （实时 status + 输出）；进程已清 → 磁盘路径（T1 持久日志，任何时候
          可读，含 sidecar 重启后凭 handle 解码 pid 跨会话定位）。
        - block=true：等到完成或等待上限（min(默认 30s, MAX_TIMEOUT)，承接旧
          continue 的轮询节奏）；完成返回 completed/失败结果，到上限返回
          running+handle。
        """
        self._init_workspace(inputs)
        pid, handle_owner, failure = self._resolve_handle(inputs)
        if failure:
            return failure
        handle = str(inputs["handle"]).strip()
        block = bool(inputs.get("block", False))
        _wd = self.get_working_dir(inputs)
        working_dir = str(_wd) if _wd else None

        proc_info = self.process_manager.get_process_info(pid)

        if not block:
            # block=false：立即读（承接旧 read_log 双路径）
            if proc_info is not None:
                forbidden = self._check_live_owner(pid, handle_owner, proc_info)
                if forbidden:
                    return forbidden
                summary = self.process_manager.get_summary(pid)
                data: dict[str, Any] = {
                    "status": proc_info.status,
                    "handle": handle,
                    "command": proc_info.command,
                    "output": self.process_manager.get_output(pid),
                    "summary": summary.get("summary", []) if summary else [],
                    "warnings": summary.get("warnings", 0) if summary else 0,
                    "errors": summary.get("errors", 0) if summary else 0,
                }
                if proc_info.exit_code is not None:
                    data["exit_code"] = proc_info.exit_code
                self._append_image_hint(data, proc_info.command, working_dir)
                return create_success_result(
                    data=data,
                    metadata={"tool": "log", "source": "memory"},
                )
            disk = self._log_result_from_disk(pid, handle, handle_owner)
            if disk is not None:
                return disk
            return create_failure_result(
                error=(
                    f"进程 {pid} 不存在且无对应日志文件（可能从未执行过，"
                    "或日志文件已被外部清理）。"
                ),
                error_code="LOG_FILE_NOT_FOUND",
            )

        # block=true：等到完成或等待上限（承接旧 continue 等待语义）
        if proc_info is None:
            # 进程已被即时清理（completed 后 _on_output_task_done 触发）。
            # 降级走磁盘日志。
            disk = self._log_result_from_disk(pid, handle, handle_owner)
            if disk is not None:
                return disk
            return create_failure_result(
                error=(
                    f"进程 {pid} 不存在或已结束，且无对应日志文件。"
                    "可能从未执行过或日志已被清理。"
                ),
                error_code="PROCESS_NOT_FOUND",
            )

        forbidden = self._check_live_owner(pid, handle_owner, proc_info)
        if forbidden:
            return forbidden

        # 已完成：直接返回结果（对齐 bash 完成路径，带 output）
        if proc_info.status != "running":
            return await self._completed_result(pid, handle, proc_info, working_dir)

        # 等待循环（等待上限 = min(默认 30s, MAX_TIMEOUT)）
        timeout = min(self.timeout, self.MAX_TIMEOUT)
        start_time = time.time()
        while True:
            if time.time() - start_time >= timeout:
                summary = self.process_manager.get_summary(pid) or {}
                data = self._compact_result_data(
                    handle=handle,
                    output=self.process_manager.get_output(pid),
                    summary_obj=summary,
                    exit_code=None,
                    status="running",
                )
                data["elapsed"] = round(time.time() - proc_info.start_time, 1)
                self._append_image_hint(data, proc_info.command, working_dir)
                return create_success_result(data=data, metadata={"tool": "log"})

            proc_info = self.process_manager.get_process_info(pid)
            if not proc_info or proc_info.status != "running":
                break

            await asyncio.sleep(0.5)

        # 等待循环跨过了退出清理时点：记录已被 _on_output_task_done 在
        # 「置终态→清内存」同瞬间移除，轮询下一拍只能拿到 None。此刻
        # exit_code 只存在于磁盘日志尾部，必须回读——禁止把真实退出码
        # 兜成 0（等待期恰逢进程退出时的固定竞态，非偶发）。
        if proc_info is None:
            disk = self._log_result_from_disk(pid, handle, handle_owner)
            if disk is not None:
                return disk
            return create_failure_result(
                error=f"进程 {pid} 已结束且日志不可读，无法取回退出码。",
                error_code="PROCESS_NOT_FOUND",
            )
        return await self._completed_result(pid, handle, proc_info)

    async def _completed_result(
        self,
        pid: int,
        handle: str,
        proc_info: ProcessInfo,
        working_dir: str | None = None,
    ) -> ToolResult:
        """进程已完成态的统一结果（log block=true 的完成路径）。

        非零退出走 COMMAND_FAILED 失败信封（携带信号归因与提取行），
        零退出返回 completed + output + exit_code + elapsed。
        """
        await self.process_manager.wait_output_settled(pid)
        summary = self.process_manager.get_summary(pid)
        exit_code = proc_info.exit_code
        if exit_code is not None and exit_code != 0:
            output_fail = self.process_manager.get_output(pid)
            error_msg, fail_meta = BashTool._build_failure_message(
                exit_code, output_fail, summary or {}, command=proc_info.command,
            )
            return create_failure_result(
                error=error_msg,
                error_code="COMMAND_FAILED",
                metadata=fail_meta,
            )
        output = self.process_manager.get_output(pid)
        result_data = self._compact_result_data(
            handle=handle,
            output=output,
            summary_obj=summary or {},
            exit_code=exit_code if exit_code is not None else 0,
        )
        # 补回 elapsed（_compact_result_data 不带此字段）
        result_data["elapsed"] = summary.get("elapsed_seconds", 0) if summary else 0
        self._append_image_hint(result_data, proc_info.command, working_dir)
        return create_success_result(data=result_data, metadata={"tool": "log"})

    def _log_result_from_disk(
        self,
        pid: int,
        handle: str,
        handle_owner: str | None,
    ) -> ToolResult | None:
        """从磁盘日志恢复 log 结果（内存记录已被清理时的唯一真值源）。

        进程结束的「置终态→清内存」是原子的，log 无论是入口即查不到
        记录，还是等待循环跨过清理时点，output/exit_code 都只能从日志
        文件回读（尾部 "# Process ended with exit code: N"）。身份链
        第二段走磁盘日志头 # Owner:（handle owner vs 日志头 owner）。

        Returns:
            ToolResult；日志文件不存在返回 None（调用方负责 NOT_FOUND）。
        """
        try:
            file_data = self.process_manager.read_log_by_pid(pid)
        except ProcessLogReadError as e:
            return create_failure_result(
                error=f"日志文件读取失败（IO 错误）：{e}",
                error_code="LOG_FILE_IO_ERROR",
            )
        if file_data is None:
            return None
        ok, err = self._check_owner(pid, handle_owner, file_data.get("owner"))
        if not ok:
            return create_failure_result(error=err, error_code="PROCESS_FORBIDDEN")
        data: dict[str, Any] = {
            "status": "completed",
            "handle": handle,
            "output": file_data["output"],
            "summary": file_data["summary"],
            "exit_code": file_data.get("exit_code") or 0,  # 磁盘日志可能无 exit_code
        }
        command = file_data.get("command")
        if command:
            data["command"] = command
        terminated_by = file_data.get("terminated_by")
        if terminated_by:
            data["terminated_by"] = terminated_by
        return create_success_result(
            data=data,
            metadata={"tool": "log", "source": "file"},
        )

    # ── input 工具（向进程发文本） ───────────────────────────────

    async def send_input(self, inputs: dict[str, Any]) -> ToolResult:
        """input 工具：凭 handle 向等待输入的进程发文本（yes/no、密码等）。"""
        self._init_workspace(inputs)
        pid, handle_owner, failure = self._resolve_handle(inputs)
        if failure:
            return failure
        handle = str(inputs["handle"]).strip()
        text = inputs.get("text")
        if text is None:
            return create_failure_result(
                error="text 不能为空",
                error_code="MISSING_TEXT",
            )

        proc_info = self.process_manager.get_process_info(pid)
        if proc_info is None:
            return create_failure_result(
                error=f"进程 {pid} 不存在或已结束",
                error_code="PROCESS_NOT_FOUND",
            )
        forbidden = self._check_live_owner(pid, handle_owner, proc_info)
        if forbidden:
            return forbidden

        # 获取当前状态。
        # ⚠️ 顺序说明（WSL 竞态）：send_input 后 ~ms 窗口内若读取该进程日志文件，
        # Windows WSL2 的 stdio relay 会丢失进程退出前的最后一批输出（预存在
        # 环境问题）。因此摘要在**发送前**取（发送前/后语义等价，都是"输入已
        # 发送"时刻的状态快照），发送后不再落盘任何读操作，避开竞态窗口。
        summary = self.process_manager.get_summary(pid)

        # 发送输入
        success, error = await self.process_manager.send_input(pid, text)

        if not success:
            return create_failure_result(
                error=error or "发送输入失败",
                error_code="INPUT_FAILED",
            )

        return create_success_result(
            data={
                "status": "running",
                "handle": handle,
                "message": "输入已发送",
                "summary": summary.get("summary", []) if summary else [],
            },
            metadata={"tool": "input"},
        )

    # ── kill 工具（终止进程） ────────────────────────────────────

    async def terminate(self, inputs: dict[str, Any]) -> ToolResult:
        """kill 工具：凭 handle 终止进程。

        reason="agent" 透传 SDK（ADR 决策 3：agent 发起即 agent 语义，穿透
        到日志终止行，不再伪装 user）；SDK graceful 终止 5s 未退即兜底强杀，
        整树杀防孙子进程孤儿。
        """
        self._init_workspace(inputs)
        pid, handle_owner, failure = self._resolve_handle(inputs)
        if failure:
            return failure
        handle = str(inputs["handle"]).strip()

        proc_info = self.process_manager.get_process_info(pid)
        if proc_info is None:
            return create_failure_result(
                error=f"进程 {pid} 不存在或已结束",
                error_code="PROCESS_NOT_FOUND",
            )
        # 身份校验（终止是破坏性操作，必须严格）
        forbidden = self._check_live_owner(pid, handle_owner, proc_info)
        if forbidden:
            return forbidden

        # 终止进程（reason=agent：agent 发起即 agent 语义）
        success, error = await self.process_manager.terminate_process(
            pid, force=False, reason="agent"
        )

        if not success:
            return create_failure_result(
                error=error or "终止进程失败",
                error_code="TERMINATE_FAILED",
            )

        summary = self.process_manager.get_summary(pid)

        return create_success_result(
            data={
                "status": "terminated",
                "handle": handle,
                "message": "进程已终止",
                "summary": summary.get("summary", []) if summary else [],
            },
            metadata={"tool": "kill"},
        )
