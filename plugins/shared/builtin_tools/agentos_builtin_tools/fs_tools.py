"""文件系统工具集合。

包含 file_read / file_write / create_directory / copy_file / move_file /
delete_file。file_read 自带目录模式（path 指向目录即返回条目列表，
ADR 2026-10-16-file-read-absorb-list-directory）与文档→markdown 回退
（docx/pdf 走 doc_tools 读取链转 markdown，非视觉模型可读）。
核心业务逻辑从 0.1 src/tools/builtin/ 迁移，外层用 SDK 封装为 MCP 工具。

工作空间约束（punch B5，区域读写语义按 ADR 2026-09-24-read-deny-write-zones 重定）：
- 判定链固定为 内容黑名单 → 位置闸 → 档位；位置轴只由本模块承担，四档
  权限模式一行不动。
- **读 = 黑名单制全放**：凭据黑名单恒拦（.env 族/密钥/SSH 私钥，与区域
  无关）→ 仓库锚拒绝集（运行时/产物目录）→ 用户 read_deny 前缀（名单文件
  ``read_deny`` 节）→ 其余全放。读不再需要名单/授权；登记白名单前缀（
  ``entries``）不再是读的必要条件。
- **写 = 写区白名单**：写/删/move/copy 仅当落点在 workspace/project_root
  锚内，或写区（登记白名单 ``entries`` 前缀）内且不在仓库自保目录时放行
  （交上层按权限档走）；区外拒绝并提示授权通道。仓库锚拒绝集对根锚之外
  的写恒拒（系统自保）。出厂 ``skills/`` 收敛（ADR 2026-10-09 批 C-a 红线①）：
  仓库根一级 skills/ 下写/删/move/copy 恒拒且不可授权——运行期唯一写路径 =
  自进化提案管道（判定单源 zone_policy.factory_skills_write_denied，与位置
  闸同源；项目根 skills/ 播种与工作空间 skills/ 快照不收敛）。
- 相对路径以根解析落空时按白名单前缀根/仓库根回退解析一次（读写各自
  判定链校验回退候选）。
- 凭据类文件硬拒（与根内外无关）：.env 族（.env.example 豁免）、
  .git-credentials / .netrc、id_rsa/id_dsa/id_ecdsa/id_ed25519、
  *.pem/*.p12/*.pfx/*.jks/*.keystore。泛化后缀 *.key 不拦（工作区内
  正常开发文件误伤面大），高危凭据格式已覆盖。
- **fail-closed**：workspace/project_root 均未注入时一律报错，不做 sidecar
  cwd 兜底——否则相对路径会落到插件目录，把宿主仓库当工作区读写。
  workspace/project_root 为运行时注入参数（param_inject 注入，
  不出现在 LLM schema；工具函数签名必须声明，否则 SDK 分发层按签名
  过滤会把注入值静默丢弃）。

[来源: src/tools/builtin/{file_read,file_write,list_directory,create_directory,
copy_file,move_file,delete_file}/tool.py]
"""

# config-write-surface-exempt: 内置文件工具=LLM受控写面;.env字面量为敏感保护清单
from __future__ import annotations

import asyncio
import base64
import fnmatch
import logging
import os
import shutil
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from agentos_plugin_sdk.bootstrap import find_shared_root

from agentos_builtin_tools.result import ToolResult

# 共享根自举复用 SDK 单点判定（BUG-56）：本文件可不经 server bootstrap 直接
# 导入（测试/独立工具语境），自持注入。旧 parents[3] 深度假设在 user_root
# 扁平单插件副本布局下把 user_root 误当共享根——find_shared_root 布局感知
# 解析（仓库指纹步行 + SDK 位置锚），两布局同解。
_SHARED_ROOT = find_shared_root(Path(__file__).resolve())
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)
from _root_api import (  # noqa: E402  # sys.path 注入共享根后才能导入（上方两行）
    resolve_repo_root,
    resolve_uploads_url,
)
from _root_api import zone_policy as _zone_policy  # noqa: E402

# 授权名单服务调用方（T6a：名单读面收口 project_manager 的 project.load_zones
# 服务；server.py on_load 注入 async (method, args) -> dict，on_unload 摘除）。
_zones_service_caller: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None

# 图像切分服务调用方（R20：multimodal.split_image 原图+分块一次注入；
# server.py on_load 注入 async (base64, mime, max_pixels) -> dict，on_unload 摘除）。
_split_image_caller: Callable[[str, str, int], Awaitable[dict[str, Any]]] | None = None


# ── 容器路由（R11）：会话存在外部容器绑定时，fs 原语进收养容器执行 ──
# guard 服务端注入 _container_id/_container_workdir（与 bash 同款信任链，
# LLM 面被 param_inject 剥除）。路径语义：容器绝对路径（/app/...）直通；
# 相对路径锚到绑定 workdir。传输走 docker exec argv（positional $1/$2，
# 不经字符串拼 shell）；写经 base64 stdin（容器无 python3 也可用）。
_CONTAINER_EXEC_TIMEOUT = 60.0


async def _container_exec_raw(
    container_id: str,
    script: str,
    *,
    args: list[str] | None = None,
    stdin_data: str | None = None,
    timeout: float = _CONTAINER_EXEC_TIMEOUT,
) -> tuple[int, bytes, bytes]:
    """docker exec argv 直传原始字节面（宿主侧无 shell；容器内 sh -c script $args）。

    stdout/stderr 不解码——二进制安全面（file_read 容器分支图片模态分流
    R14 的读取底座）；文本面调用方走 [`_container_exec`] 解码包装。
    """
    import asyncio as _asyncio

    argv = ["docker", "exec"]
    if stdin_data is not None:
        # 无 -i 不读 stdin：docker exec 立即给容器侧 EOF，base64 -d 写出空文件
        # （真内核探针实证）。stdin 面与 -i 绑定出现。
        argv.append("-i")
    argv += [container_id, "sh", "-c", script, "sh", *(args or [])]
    proc = await _asyncio.create_subprocess_exec(
        *argv,
        stdout=_asyncio.subprocess.PIPE,
        stderr=_asyncio.subprocess.PIPE,
        stdin=_asyncio.subprocess.PIPE if stdin_data is not None else _asyncio.subprocess.DEVNULL,
    )
    try:
        # stdin_data 必经 communicate(input=...) 进容器侧 stdin（base64 -d 写面
        # 依赖；漏传会立即 EOF 写出空文件——真内核探针实证）
        data = stdin_data.encode("utf-8") if stdin_data is not None else None
        out, err = await _asyncio.wait_for(proc.communicate(input=data), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return 124, b"", b"container exec timeout"
    return (proc.returncode or 0, out, err)


async def _container_exec(
    container_id: str,
    script: str,
    *,
    args: list[str] | None = None,
    stdin_data: str | None = None,
    timeout: float = _CONTAINER_EXEC_TIMEOUT,
) -> tuple[int, str, str]:
    """docker exec 文本面（utf-8 replace 解码；二进制安全读取走 raw 版）。"""
    rc, out, err = await _container_exec_raw(
        container_id, script, args=args, stdin_data=stdin_data, timeout=timeout
    )
    return (
        rc,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


def _container_target(path: str, workdir: str | None) -> str:
    """路径锚定：容器绝对路径直通；相对路径锚绑定 workdir（缺省 /）。"""
    import posixpath

    p = (path or "").strip()
    if not p:
        return p
    if p.startswith("/"):
        return p
    base = (workdir or "/").rstrip("/") or "/"
    return posixpath.join(base, p)


def set_zones_service_caller(caller: Any) -> None:
    """注入/清除授权名单服务调用方（server on_load 调用）。"""
    global _zones_service_caller  # noqa: PLW0603
    _zones_service_caller = caller


def set_split_image_caller(caller: Any) -> None:
    """注入/清除图像切分服务调用方（server on_load 调用，R20）。"""
    global _split_image_caller  # noqa: PLW0603
    _split_image_caller = caller


async def _load_zone_lists() -> dict[str, list[str]]:
    """授权名单三节一次取全（entries/read_deny/read_allow）。

    语义自 ADR 2026-09-24-read-deny-write-zones 起名单为**写区**（读黑名单
    制全放）。服务未注入/调用失败 → 三节全空（范围收缩，安全侧）并 error
    留痕，不阻断工具执行（凭据黑名单/仓库锚/工作空间锚均不依赖名单）。
    """
    if _zones_service_caller is None:
        logger.error("[fs_tools] 授权名单服务未注入，按空名单处理（范围收缩）")
        return {"entries": [], "read_deny": [], "read_allow": []}
    try:
        result = await _zones_service_caller("load_zones", {})
    except Exception as exc:  # noqa: BLE001 — 服务故障降级可见（error 级留痕）
        logger.error("[fs_tools] 授权名单服务不可用，按空名单处理（范围收缩）: %s", exc)
        return {"entries": [], "read_deny": [], "read_allow": []}
    if not isinstance(result, dict) or "error" in result:
        logger.error("[fs_tools] 授权名单服务载荷非法，按空名单处理（范围收缩）")
        return {"entries": [], "read_deny": [], "read_allow": []}
    return {
        section: [str(e) for e in (result.get(section) or []) if str(e or "").strip()]
        for section in ("entries", "read_deny", "read_allow")
    }


logger = logging.getLogger(__name__)

# 纯读操作集合：走读黑名单判定链（读全放，凭据/仓库拒绝集/read_deny 除外）；
# 其余操作（写/删/move/copy）走写区判定链。
_READ_OPERATIONS = frozenset({"read", "search"})

# 大文件不再拒绝（task_spill_guard.md 任务 2）：大输出兜底由 pipeline 的
# spill_guard 统一负责（原文存档 + 提取 + 定位符），工具只负责"读文件 +
# 返回内容"。行范围（start_line/end_line/tail）是用户显式指定的查询窗口，
# 不属于静默截断，保留。


# 凭据类文件硬拒清单：basename 精确匹配 / .env 族前缀 / 密钥文件后缀与
# SSH 私钥命名。命中即拒绝（读写同规），与 workspace 锚定无关——.env 类
# 文件常驻项目根内，仅靠根校验拦不住（.env.example 是模板，豁免）。
_SENSITIVE_EXACT = {".env", ".git-credentials", ".netrc"}
_SENSITIVE_SUFFIXES = (".pem", ".p12", ".pfx", ".jks", ".keystore")
_SENSITIVE_SSH_PREFIXES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")


def _sensitive_file_reason(resolved: Path) -> str | None:
    """命中凭据类文件名则返回拒绝原因，否则 None。

    比较统一 casefold：Windows/macOS 文件系统大小写不敏感，`.ENV` 与 `.env`
    同名等价——区分大小写的名单比较会让非小写形态创建/访问全程放行
    （S3：`Path.resolve()` 只在目标已存在时回填真名，新建路径不回填）。
    """
    name = resolved.name
    folded = name.casefold()
    # Windows FS 剥离文件名尾随空格/点：`open('.env ')` 落盘同名 .env——
    # 比较前先归一（resolve() 对不存在的目标不回填真名，此处需自行覆盖）。
    win_normalized = folded.rstrip(" .")
    candidates = {folded, win_normalized}
    if candidates & _SENSITIVE_EXACT:
        return f"凭据类文件禁止访问：{name}"
    if any(c.startswith(".env.") and c != ".env.example" for c in candidates):
        return f"凭据类文件禁止访问：{name}（.env 族，仅豁免 .env.example）"
    if any(c.endswith(_SENSITIVE_SUFFIXES) for c in candidates):
        return f"凭据类文件禁止访问：{name}（密钥/证书私钥格式）"
    if any(c.startswith(_SENSITIVE_SSH_PREFIXES) for c in candidates):
        return f"凭据类文件禁止访问：{name}（SSH 私钥）"
    return None


def _read_outside_verdict(
    resolved: Path,
    extra_grants: list[str] | None = None,
    workspace: str | None = None,
    project_root: str | None = None,
    read_allow: list[str] | None = None,
    read_deny: list[str] | None = None,
) -> tuple[bool, str | None]:
    """根外纯读判定链（读黑名单制 + 读授权前缀，判定单源 = zone_policy.read_verdict）。

    workspace/project_root 携入内建工作空间地界派生（用户裁定 2026-09-29：
    工作空间根必须在白名单内——兄弟工作树读不撞仓库拒绝集）；写链不经此处。
    """
    return _zone_policy.read_verdict(
        resolved,
        extra_grants,
        workspace=workspace,
        project_root=project_root,
        read_allow=read_allow,
        read_deny=read_deny,
    )


def _parse_authorized_zones(raw: str | None) -> list[str]:
    """管道级授权写区解析（判定单源 = zone_policy.parse_zones_raw）。"""
    return _zone_policy.parse_zones_raw(raw)


def _write_zone_verdict(
    resolved: Path,
    extra_zones: list[str] | None = None,
    write_zones: list[str] | None = None,
) -> tuple[bool, str | None]:
    """写区判定链（判定单源 = zone_policy.write_verdict；管道层位置闸同源）。"""
    return _zone_policy.write_verdict(resolved, extra_zones, write_zones=write_zones)


def _fallback_bases(write_zones: list[str]) -> list[str]:
    """相对路径回退解析基准：白名单前缀根 + 仓库根（normcase 去重）。

    仓库根单列：仓库读面本就受 repo_anchor 拒绝集约束（ADR 2026-09-12），
    相对路径与绝对路径享有同一仓库读面（2026-09-19 裁定的读宽口径）；
    白名单缺失/未登记仓库时（如部署布局下名单服务不可达）回退仍可用。
    write_zones = project.load_zones 的 entries 节（T6a 调用方取妥传入）。
    """
    bases: list[str] = []
    seen: set[str] = set()
    for entry in write_zones:
        key = os.path.normcase(os.path.normpath(entry))
        if key not in seen:
            seen.add(key)
            bases.append(entry)
    root = resolve_repo_root()
    if root is not None:
        key = os.path.normcase(str(root))
        if key not in seen:
            bases.append(str(root))
    return bases


async def _read_relative_fallback(
    target: Path,
    extra_grants: list[str] | None = None,
    workspace: str | None = None,
    project_root: str | None = None,
    zones: dict[str, list[str]] | None = None,
) -> tuple[bool, str, str | None]:
    """相对路径白名单前缀根回退解析一次（读操作专用，2026-09-19 用户裁定）。

    相对路径以工作空间解析落空（目标不存在）时，按回退基准根（白名单
    前缀根 + 仓库根，见 :func:`_fallback_bases`）逐个回退解析；首个存在的
    候选经根外读判定链校验（凭据黑名单已在工作空间解析时先行——回退候选
    与原解析 basename 相同，无需重查），命中即以候选为读取路径；全部落空
    → 维持原解析（下游 File not found）。净正向相对路径（未逃出工作空间）
    拼接到任何前缀根下都不会出界，无需二次前缀校验。zones 为调用方一次取
    全的名单三节（T6a：project.load_zones 服务），本函数不再自取。

    阻塞面（候选 resolve/exists）下放 to_thread（D 盘重载实测秒级——
    2026-11-16 方案文档 P0 配方）。

    Returns:
        (是否命中, 拒绝原因, 候选绝对路径)；未命中 = (False, "", None)。
    """
    zone_lists = zones or {"entries": [], "read_deny": [], "read_allow": []}
    for base in _fallback_bases(zone_lists["entries"]):
        candidate = await asyncio.to_thread(_resolve_candidate, base, target)
        if not await asyncio.to_thread(candidate.exists):
            continue
        allow, deny_reason = _read_outside_verdict(
            candidate,
            extra_grants,
            workspace=workspace,
            project_root=project_root,
            read_allow=zone_lists["read_allow"],
            read_deny=zone_lists["read_deny"],
        )
        if allow:
            return True, "", str(candidate)
        if deny_reason is not None:
            return False, deny_reason, str(candidate)
    return False, "", None


# workspace 根 resolve 幂等缓存（root_str → resolved Path）：每轮重复 resolve
# 同一根是纯浪费（D 盘重载下 resolve 本身秒级——2026-11-16 方案文档 P0 配方）。
# 命中即零 I/O；未命中经 to_thread 解析并写入缓存（同输入同输出，幂等）。
_WORKSPACE_ROOT_RESOLVE_CACHE: dict[str, Path] = {}


def _resolve_workspace_root_cached(root_str: str) -> Path:
    """根 resolve（线程池内执行）：解析 + 写缓存，同 root_str 恒同 resolved。"""
    resolved = Path(root_str).resolve()
    _WORKSPACE_ROOT_RESOLVE_CACHE[root_str] = resolved
    return resolved


def _resolve_candidate(base: str, target: Path) -> Path:
    """回退候选解析（线程池内执行）：(base / target).resolve()。"""
    return (Path(base) / target).resolve()


def _resolve_target_against_root(target: Path, root: Path) -> Path:
    """目标路径解析（线程池内执行）：绝对路径原样 resolve，相对路径锚定根。"""
    return target.resolve() if target.is_absolute() else (root / target).resolve()


def _read_file_head(file_path: Path, nbytes: int = 64 * 1024) -> bytes:
    """文件头嗅探读取（线程池内执行）：NUL 探测的 64KB 预读。"""
    with file_path.open("rb") as fh:
        return fh.read(nbytes)


async def _check_workspace_path(
    path: str,
    workspace: str | None,
    project_root: str | None,
    operation: str,
    authorized_zones: str | None = None,
    authorized_read_zones: str | None = None,
) -> tuple[bool, str, str | None]:
    """位置闸单点（ADR 2026-09-24-read-deny-write-zones：读黑名单制，写区白名单）。

    Args:
        path: 待校验路径（绝对或相对；相对路径以根为基准解析）
        workspace: 工作空间路径（运行时注入，可选）
        project_root: 项目根路径（运行时注入，可选；优先于 workspace 作根）
        operation: "read" / "search" / "write" / "delete" / "move" / "copy"
            （决定走读判定链还是写区判定链）
        authorized_zones: 管道级授权写区（JSON 数组串，zone 授权卡落盘）
        authorized_read_zones: 管道级授权读取（JSON 数组串，读授权卡落盘，
            用户裁定 2026-09-28：读黑名单命中经审批放行的前缀）

    Returns:
        (是否允许, 拒绝原因, 校验用绝对路径)
        未注入 workspace/project_root 时返回拒绝——相对路径无处锚定，落回
        sidecar cwd 会把插件目录/宿主仓库当工作区读写。
        读操作（read/search）单根越界时：/uploads/ 附件只读锚（BUG-57）→
        读黑名单判定链（仓库锚拒绝集 + read_deny 前缀，其余全放）；相对
        路径以根解析落空时按白名单前缀根回退解析一次。
        写/删/move/copy 单根越界时：写区判定链（仓库自保目录恒拒 → 白名单
        entries 前缀放行，交上层按权限档走）；区外拒绝并提示授权通道。
    """
    root_str = project_root or workspace
    if not root_str:
        return (
            False,
            f"workspace/project_root 未注入，无法锚定路径（相对路径禁止以进程 cwd 解析）：{path}",
            None,
        )

    root = _WORKSPACE_ROOT_RESOLVE_CACHE.get(root_str)
    if root is None:
        # 根 resolve 是文件系统重操作（D 盘重载下秒级），未命中缓存时下放
        # 线程池（2026-11-16 方案文档 P0 配方；幂等缓存见 _resolve_workspace_root_cached）。
        root = await asyncio.to_thread(_resolve_workspace_root_cached, root_str)
    # WSL 挂载混入形态归一（zone_policy 单源同语义）：脏形态 `D:\mnt\d\X`
    # 不归一则锚匹配失败、存在性检查恒 404。
    path = _zone_policy.normalize_windows_mixed_path(path)
    # 容器挂载点翻译：bash 在容器内以 /workspace 为工作目录（isolation_guard
    # 固定挂载约定），LLM 会沿用该绝对路径调文件工具——宿主侧把 /workspace/
    # 前缀重映射到注入的宿主工作空间，否则写到不存在的宿主绝对路径。
    if path == "/workspace" or path.startswith("/workspace/"):
        path = str(root) + path[len("/workspace") :]
        logger.info("[fs_tools] 容器挂载点 /workspace 重映射到宿主工作空间 | -> %s", path)
    target = Path(path)
    resolved = await asyncio.to_thread(_resolve_target_against_root, target, root)
    sensitive = _sensitive_file_reason(resolved)
    if sensitive:
        return False, sensitive, str(resolved)
    # 名单三节一次取全（T6a：project.load_zones 服务；读链 read_allow/read_deny，
    # 写链 entries；相对路径回退基准同源复用）。
    zone_lists = await _load_zone_lists()
    # 出厂 skills/ 写区收敛（ADR 2026-10-09 批 C-a 红线①）：判定单源 =
    # zone_policy（与管道层位置闸同源），置于根锚放行之前——dev 场景
    # project_root 即仓库根，锚内放行会遮蔽；读面不收敛。唯一写路径 =
    # 自进化提案管道，工具兜底层同样恒拒且不可授权。
    if operation not in _READ_OPERATIONS:
        factory_deny = _zone_policy.factory_skills_write_denied(resolved)
        if factory_deny is not None:
            return False, factory_deny, str(resolved)
    try:
        resolved.relative_to(root)
    except ValueError:
        if operation in _READ_OPERATIONS:
            # /uploads/{filename} 附件只读锚（BUG-57）：共享解析点 basename
            # 拼接（``..`` 穿越形态天然拒绝，租户按构造隔离，见
            # uploads_path.resolve_uploads_url）。置于仓库/白名单锚之前——
            # uploads 兜底落盘可在仓库 data/ 内，仓库拒绝集不得遮蔽附件域。
            candidate = resolve_uploads_url(path)
            if candidate is not None:
                return True, "", str(candidate)
            # 锚携入读判定（用户裁定 2026-09-29）：工作空间根作为内建
            # read_allow 前缀参与放行——兄弟工作树读不撞仓库拒绝集。
            allow, deny_reason = _read_outside_verdict(
                resolved,
                _parse_authorized_zones(authorized_read_zones),
                workspace=workspace,
                project_root=project_root,
                read_allow=zone_lists["read_allow"],
                read_deny=zone_lists["read_deny"],
            )
            if allow:
                return True, "", str(resolved)
            if deny_reason is not None:
                return False, deny_reason, str(resolved)
        if operation not in _READ_OPERATIONS:
            allow, deny_reason = _write_zone_verdict(
                resolved,
                _parse_authorized_zones(authorized_zones),
                write_zones=zone_lists["entries"],
            )
            if deny_reason is not None:
                return False, deny_reason, str(resolved)
            if allow:
                return True, "", str(resolved)
        return (
            False,
            f"路径 {path} 超出 workspace/project_root（{root}）且不在写区名单内，"
            f"{operation} 操作被拒绝；可请用户将目录加入授权名单"
            "（project_whitelist.yaml entries）或通过授权卡临时放行",
            str(resolved),
        )
    # 相对路径在工作空间内落空（目标不存在）→ 白名单前缀根回退解析一次
    # （仅读操作；工作空间内已存在则优先，回退不遮蔽本地副本）。
    if (
        operation in _READ_OPERATIONS
        and not target.is_absolute()
        and not await asyncio.to_thread(resolved.exists)
    ):
        hit, reason, fallback_candidate = await _read_relative_fallback(
            target,
            _parse_authorized_zones(authorized_read_zones),
            workspace=workspace,
            project_root=project_root,
            zones=zone_lists,
        )
        if hit:
            return True, "", fallback_candidate
        if reason:
            return False, reason, fallback_candidate
    return True, "", str(resolved)


# ═════════════════════════════════════════════════════════════
# file_read
# ═════════════════════════════════════════════════════════════

FILE_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "文件或目录路径（相对路径或绝对路径）。目录=返回该目录条目列表，"
                "查看工作空间/目录结构直接用本工具"
            ),
        },
        "start_line": {
            "type": "integer",
            "description": "起始行号（从1开始），仅读取指定行范围（仅文件模式生效）",
            "default": 1,
        },
        "end_line": {
            "type": "integer",
            "description": "结束行号（从1开始，包含该行），仅读取指定行范围（仅文件模式生效）",
        },
        "offset": {
            "type": "integer",
            "description": "起始行号（1-based）。start_line 的业界别名，两者同给时 start_line 优先",
        },
        "limit": {
            "type": "integer",
            "description": (
                "返回行数上限（与 end_line 二选一，end_line 优先）；"
                "换算：end_line = 起始行 + limit - 1"
            ),
        },
        "tail": {"type": "integer", "description": "仅读取文件最后 N 行（仅文件模式生效）"},
        "include_hidden": {
            "type": "boolean",
            "description": "目录模式：是否包含隐藏项（以.开头），默认 false",
            "default": False,
        },
        "pattern": {
            "type": "string",
            "description": "目录模式：文件名匹配模式（glob 语法，如 *.py）",
        },
    },
    "required": ["path"],
}

FILE_READ_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file": {"type": "string"},
        "lines": {"type": "integer"},
        "size": {"type": "integer"},
        "content": {"type": "string"},
        # 目录模式字段
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": ["file", "directory"]},
                    "size": {"type": "integer"},
                },
            },
            "description": "目录模式：条目列表（名称/类型/字节大小）",
        },
        "count": {"type": "integer", "description": "目录模式：条目数"},
        # 文档→markdown 回退字段
        "converted": {
            "type": "boolean",
            "description": "文档回退：内容经 markdown 转换产出（docx/pdf）",
        },
        "source_format": {
            "type": "string",
            "description": "文档回退：原文档格式（docx/pdf）",
        },
        "path": {"type": "string", "description": "图片模态读取时的文件绝对路径"},
        "mime_type": {"type": "string", "description": "图片模态读取时的 MIME 类型"},
        "base64_data": {"type": "string", "description": "图片模态读取时的 base64 内容"},
        # R20 图像切分字段（仅超规格切分时出现；向后兼容，常规读取无这些键）
        "image_grid": {
            "type": "object",
            "properties": {"rows": {"type": "integer"}, "cols": {"type": "integer"}},
            "description": "图片切分：等分网格（行×列）",
        },
        "tile_count": {
            "type": "integer",
            "description": "图片切分：分块数量（images 数组长度）",
        },
        "images": {
            "type": "array",
            "description": "图片切分：原生分辨率分块列表（行优先序，"
            "每块 {base64, mime_type, path}）",
            "items": {
                "type": "object",
                "properties": {
                    "base64": {"type": "string"},
                    "mime_type": {"type": "string"},
                    "path": {"type": "string"},
                },
            },
        },
    },
}

# 图片模态内联上限：对齐平台缺省 multimodal.max_image_size（20MB）。模型级
# 精确能力（supported_image_types/上限）由 llm 域声明，工具层只做平台级兜底。
_MAX_IMAGE_EMBED_BYTES = 20 * 1024 * 1024


def _sniff_image_mime(head: bytes) -> str | None:
    """魔数嗅探图片 MIME（模态契约仅声明内容类型，模型支持性由管道闸门判定）。"""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    return None


async def _call_split_image(
    base64_data: str, mime_type: str, max_pixels: int
) -> dict[str, Any] | None:
    """调 multimodal.split_image（切分属增强面）：不可用/异常 → None 降级原样单图。

    降级策略 warn 留痕可观测，不阻断 file_read 主功能（读图路径与 R14 既往
    行为一致）；服务返回 error 键（解码失败/参数非法）同判。
    """
    if _split_image_caller is None:
        return None
    try:
        result = await _split_image_caller(base64_data, mime_type, max_pixels)
    except Exception as exc:  # noqa: BLE001 — 增强面故障降级可见（warn 级留痕）
        logger.warning("[fs_tools] 图像切分服务调用失败，按原样单图注入: %s", exc)
        return None
    if not isinstance(result, dict) or "error" in result:
        logger.warning(
            "[fs_tools] 图像切分服务返回失败，按原样单图注入: %s",
            result.get("error") if isinstance(result, dict) else type(result).__name__,
        )
        return None
    return result


async def _read_image_modality(
    path_label: str,
    raw: bytes,
    mime: str,
    max_pixels: int | None,
    container_id: str | None = None,
) -> ToolResult:
    """图片模态契约装配单点（宿主/容器双分支共用，R20 定稿：原图+分块一次注入）。

    无规格（max_pixels 非法/未注入）→ 原样单图契约（path/mime_type/base64_data/
    size[/container]），与 R14 既往行为逐键一致（向后兼容）。
    有规格 → 调 multimodal.split_image：超规格时返回 原图(base64_data，前端
    卡片与 tool_core 注图首图)+分块(images[]，tool_core 注图随后的块)+切分标注
    （content 键：serialize_for_content 文本直通通道，行/列与坐标拼合关系随
    工具消息进上下文）；未超规格/服务降级 → 同原样单图。
    """
    size = len(raw)
    if size > _MAX_IMAGE_EMBED_BYTES:
        return ToolResult.failure_result(
            f"Image too large to inline: {path_label} ({size} bytes, 上限 {_MAX_IMAGE_EMBED_BYTES})"
        )
    result: dict[str, Any] = {
        "path": path_label,
        "mime_type": mime,
        "base64_data": base64.b64encode(raw).decode("ascii"),
        "size": size,
    }
    if container_id:
        result["container"] = container_id
    # 规格合法性内联（mypy 收窄：bool 是 int 子类，显式排除后 max_pixels: int > 0）
    if not isinstance(max_pixels, int) or isinstance(max_pixels, bool) or max_pixels <= 0:
        return ToolResult.success_result(result)
    split = await _call_split_image(result["base64_data"], mime, max_pixels)
    tiles = (split or {}).get("tiles") or []
    if not split or not split.get("tiled") or not tiles:
        return ToolResult.success_result(result)
    result["image_grid"] = split.get("grid") or {}
    result["tile_count"] = len(tiles)
    result["content"] = str(split.get("annotation") or "")
    # tool_core collect_pending_images 契约键：images[].base64（tile 键名
    # base64_data 经此映射）；注入顺序 = base64_data 原图在前、分块按行优先在后。
    result["images"] = [
        {
            "base64": tile.get("base64_data", ""),
            "mime_type": tile.get("mime_type", "image/png"),
            "path": path_label,
        }
        for tile in tiles
    ]
    return ToolResult.success_result(result)


def _read_directory(
    file_path: Path, include_hidden: bool, pattern: str | None
) -> ToolResult:
    """目录模式（list_directory 合并件）：只返回目录条目列表。

    items = {name, type, size} 名称序（原 list_directory 输出形状不变）；
    不读任何文件内容。查看工作空间/目录结构用，无需 Shell。
    """
    items: list[dict[str, Any]] = []
    for entry in sorted(file_path.iterdir(), key=lambda e: e.name):
        name = entry.name
        if not include_hidden and name.startswith("."):
            continue
        if pattern and not fnmatch.fnmatch(name, pattern):
            continue
        is_dir = entry.is_dir()
        items.append(
            {
                "name": name,
                "type": "directory" if is_dir else "file",
                "size": 0 if is_dir else entry.stat().st_size,
            }
        )
    return ToolResult.success_result(
        {
            "file": str(file_path.resolve()),
            "items": items,
            "count": len(items),
        },
        count=len(items),
    )


# 文档→markdown 回退支持的原生格式（doc_tools 读取链范围；xlsx/pptx 等
# 不在内，维持原拒绝）。按扩展名预筛——非文档二进制在此落回原模态/拒绝链，
# 不产生「转换失败」误导。
_DOCUMENT_FALLBACK_SUFFIX_FORMATS = {
    ".docx": "docx",
    ".pdf": "pdf",
}


def _ensure_doc_tools_importable() -> None:
    """doc_tools sibling 目录加入 sys.path（跨插件单源复用的路径前提）。"""
    doc_tools_dir = str(Path(_SHARED_ROOT) / "doc_tools")
    if doc_tools_dir not in sys.path:
        sys.path.insert(0, doc_tools_dir)


def _document_to_markdown(file_path: Path) -> tuple[str, str] | None:
    """二进制文档 → (markdown 文本, 源格式)；非回退扩展名返回 None。

    doc_tools 读取链单源复用（跨插件 sibling import，ADR
    2026-10-16-file-read-absorb-list-directory）——调用方须先
    :func:`_ensure_doc_tools_importable`；其模块级依赖全为标准库，
    pypdf/python-docx/markdown-it-py 在读取函数内惰性 import，已列入本插件
    venv 依赖。解析失败（无文本层 PDF/损坏文档）以 DocumentError 传播，
    由调用方翻译为显式失败。
    """
    from doc_convert import read_blocks  # noqa: PLC0415
    from doc_writers import blocks_to_markdown  # noqa: PLC0415

    fmt = _DOCUMENT_FALLBACK_SUFFIX_FORMATS.get(file_path.suffix.lower())
    if fmt is None:
        return None
    blocks = read_blocks(file_path, fmt)
    return blocks_to_markdown(blocks), fmt


async def file_read(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    tail: int | None = None,
    include_hidden: bool = False,
    pattern: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
    llm_vision_max_pixels: int | None = None,
    workspace: str | None = None,
    project_root: str | None = None,
    authorized_read_zones: str | None = None,
    _container_id: str | None = None,
    _container_workdir: str | None = None,
) -> ToolResult:
    """读取文件内容；路径为目录时返回该目录条目列表。

    文件模式支持行范围/尾部读取；目录模式返回 items+文本树（查工作空间/
    目录结构用，无需 Shell）。二进制按模态/文档分流（2026-09-14 用户裁定 +
    文档回退；R14 起容器分支同款）：图片走 base64 模态契约（tool_core
    inject_multimodal 按 state.llm_supports_vision 注图或给文本引导）；
    R20/R21 定稿：注入规格 llm_vision_max_pixels（服务端注入，非 LLM 可见参数；R21 起总像素口径）
    且图片超规格时，经 multimodal.split_image 做**原生分辨率等分网格切分**，
    原图+分块一次注入（大图直喂会被模型 API 端视觉编码器缩放丢文字细节；
    分块带行列拼合标注，无缩放损失）——规格来源 llm.yaml
    multimodal.max_pixels，模型切换自动跟随；无规格/未超规格/切分服务降级
    时维持原样单图契约。
    docx/pdf 转 markdown 进 content（非视觉模型可读）；其余二进制干净拒绝。
    未注入 workspace/project_root 时直接报错（fail-closed，无 cwd 兜底）。
    返回的 file 字段恒为宿主侧绝对路径（容器挂载路径 /workspace/* → 宿主
    工作空间、相对路径 → 根锚定）——前端工具卡片按 file 字段直读宿主
    文件系统，原样回传 agent 视角相对路径将打不开（_local 根解析不到）。
    """
    # 业界分页别名归一（offset/limit ↔ start_line/end_line）：显式声明值优先，
    # 别名只补缺（DSH read / Claude Code Read 先验：offset=1-based 起始行，
    # limit=行数 → end_line = 起始行 + limit - 1）。存量调用零行为变化。
    if start_line is None:
        start_line = offset if offset is not None else 1
    if end_line is None and limit is not None:
        end_line = start_line + max(int(limit), 0) - 1

    # 容器路由（R11）：绑定会话读收养容器内文件（cat，行范围/尾部客户端切片）。
    # R14：读取走原始字节面 + 模态分流（对齐宿主分支同款契约）——图片魔数命中
    # → base64_data+mime_type（tool_core 按 state.llm_supports_vision 注图或
    # 文本引导）；此前 PNG 按 utf-8 replace 解码成一屏乱码文本直吐模型（终跑
    # 887b8135f4c5 实证），图片注入双重断裂的第二断点。
    if _container_id:
        target = _container_target(path, _container_workdir)
        rc, raw, rerr = await _container_exec_raw(
            _container_id, 'cat "$1"', args=[target], timeout=30.0
        )
        if rc != 0:
            detail = (
                rerr.decode("utf-8", errors="replace").strip()
                or raw.decode("utf-8", errors="replace").strip()
                or f"exit {rc}"
            )
            return ToolResult.failure_result(f"Read error: {detail}")
        mime = _sniff_image_mime(raw[:64])
        if mime is not None:
            # 图片模态：与宿主分支同上限同契约（装配单点 _read_image_modality，
            # R20 起含原图+分块一次注入）。
            return await _read_image_modality(
                target, raw, mime, llm_vision_max_pixels, container_id=_container_id
            )
        if b"\x00" in raw[:64 * 1024]:
            # 非模态二进制（NUL 探测，git 同款）：干净拒绝，不吐乱码文本。
            return ToolResult.failure_result(f"Binary file or encoding issue: {target}")
        out = raw.decode("utf-8", errors="replace")
        size = len(out.encode("utf-8"))
        lines = out.splitlines()
        total = len(lines)
        newline = chr(10)
        if tail is not None:
            container_content = newline.join(lines[-int(tail):]) if int(tail) > 0 else ""
        else:
            end = int(end_line) if end_line is not None else total
            start = max(int(start_line) - 1, 0)
            container_content = newline.join(lines[start:end])
        return ToolResult.success_result(
            {
                "file": target,
                "content": container_content,
                "lines": total,
                "size": size,
                "container": _container_id,
            }
        )

    # 工作空间约束（读路径：根外放行但记录；无注入上下文一律拒绝）
    allowed, reason, resolved = await _check_workspace_path(
        path,
        workspace,
        project_root,
        operation="read",
        authorized_read_zones=authorized_read_zones,
    )
    if not allowed:
        return ToolResult.failure_result(reason)
    if resolved is not None:
        path = resolved

    file_path = Path(path)
    if not await asyncio.to_thread(file_path.exists):
        return ToolResult.failure_result(f"File not found: {path}")
    if await asyncio.to_thread(file_path.is_dir):
        return _read_directory(file_path, include_hidden=include_hidden, pattern=pattern)

    size = (await asyncio.to_thread(file_path.stat)).st_size

    # 二进制闸（git 同款 NUL 探测）：NUL 是合法 UTF-8 码点，纯解码异常
    # 检测不出含 NUL 的二进制——先嗅探文件头再读文本（2026-09-14 用户裁定）。
    try:
        head = await asyncio.to_thread(_read_file_head, file_path)
    except OSError as e:
        return ToolResult.failure_result(f"Read error: {e}")

    content: str | None = None
    if b"\x00" not in head:
        try:
            content = await asyncio.to_thread(file_path.read_text, "utf-8")
        except UnicodeDecodeError:
            # 无 NUL 的非 UTF-8 内容（如 JPEG）：同样按二进制分流。
            content = None
        except OSError as e:
            return ToolResult.failure_result(f"Read error: {e}")

    if content is None:
        # 文档→markdown 回退（docx/pdf）：先于模态分流——转换成功则任何
        # 模型可读；不适用格式（含图片）落回原模态/拒绝链。
        _ensure_doc_tools_importable()
        from doc_core import DocumentError  # noqa: PLC0415 — 惰性（sibling 路径先行）

        try:
            converted = await asyncio.to_thread(_document_to_markdown, file_path)
        except DocumentError as exc:
            # 无文本层/损坏/加密：显式失败（禁静默丢内容）；其余异常照常传播。
            return ToolResult.failure_result(f"文档转换失败：{exc}")
        if converted is not None:
            md_text, fmt = converted
            return ToolResult.success_result(
                {
                    "file": str(await asyncio.to_thread(file_path.resolve)),
                    "converted": True,
                    "source_format": fmt,
                    "lines": md_text.count("\n") + 1 if md_text else 0,
                    "size": size,
                    "content": md_text,
                },
            )
        mime = _sniff_image_mime(head)
        if mime is None:
            return ToolResult.failure_result(f"Binary file or encoding issue: {file_path}")
        raw = await asyncio.to_thread(file_path.read_bytes)
        return await _read_image_modality(
            str(await asyncio.to_thread(file_path.resolve)), raw, mime, llm_vision_max_pixels
        )

    all_lines = content.split("\n")
    total = len(all_lines)

    if tail is not None:
        # split 对结尾 "\n" 会产生一个空串元素，tail 按真实行数计算（去掉末尾空串）
        lines = all_lines[:-1] if all_lines and all_lines[-1] == "" else all_lines
        start_idx = max(0, len(lines) - tail)
        selected = lines[start_idx:]
    elif start_line > 1 or end_line is not None:
        start_idx = max(0, start_line - 1)
        end_idx = end_line if end_line is not None else total
        selected = all_lines[start_idx:end_idx]
    else:
        selected = all_lines

    return ToolResult.success_result(
        {
            "file": str(await asyncio.to_thread(file_path.resolve)),
            "lines": len(selected),
            "size": size,
            "content": "\n".join(selected),
        },
        total_lines=total,
    )


# ═════════════════════════════════════════════════════════════
# file_write
# ═════════════════════════════════════════════════════════════

FILE_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "文件路径"},
        "content": {"type": "string", "description": "文件内容"},
        "action": {
            "type": "string",
            "enum": ["write", "search_replace", "insert", "delete_lines", "append"],
            "description": "编辑操作类型",
            "default": "write",
        },
        "start_line": {
            "type": "integer",
            "description": "起始行号（write/insert/delete_lines 使用）",
        },
        "end_line": {"type": "integer", "description": "结束行号（write/delete_lines 使用）"},
        "line": {"type": "integer", "description": "插入位置行号（insert 使用）"},
        "old_str": {"type": "string", "description": "要搜索的原始文本（search_replace 使用）"},
        "new_str": {"type": "string", "description": "替换后的新文本（search_replace 使用）"},
        "create_backup": {"type": "boolean", "description": "是否创建 .bak 备份", "default": True},
    },
    "required": ["path", "action"],
}

FILE_WRITE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file": {"type": "string", "description": "写盘后的宿主绝对路径（前端卡片打开用）"},
        "added": {"type": "integer"},
        "removed": {"type": "integer"},
        # 写后磁盘字节数（前端 file_card 大小徽标数据源，R174）
        "size": {"type": "integer"},
        "backup": {"type": ["string", "null"]},
        # 写前/写后全文：前端 diff 卡数据源（plugin.json ui.chat_card 的
        # diffOldSource/diffNewSource）。大文件溢出由 pipeline 的 spill_guard
        # 统一兜底（对齐 file_read 的全文返回口径）。
        "old_content": {"type": ["string", "null"]},
        "new_content": {"type": ["string", "null"]},
    },
}


def _disk_size(path: Path) -> int:
    """写后文件磁盘字节数（file_card 大小徽标；以磁盘为准，不经编码推算）。"""
    return path.stat().st_size


async def file_write(
    path: str,
    action: str = "write",
    content: str = "",
    start_line: int | None = None,
    end_line: int | None = None,
    line: int | None = None,
    old_str: str | None = None,
    new_str: str | None = None,
    create_backup: bool = True,
    workspace: str | None = None,
    project_root: str | None = None,
    authorized_zones: str | None = None,
    _container_id: str | None = None,
    _container_workdir: str | None = None,
) -> ToolResult:
    """写入/编辑文件。workspace/project_root 可用时禁止根外路径（B5）。

    写文件语义自包含：write/append/insert 在目标父目录缺失时自动创建
    （parents）；search_replace/delete_lines 只改已存在文件，不建目录。
    """
    # 容器路由（R11）：绑定会话写收养容器内文件——cat 读旧内容、base64 stdin
    # 写新内容（动作语义客户端复刻，与宿主 write/append/search_replace/
    # insert/delete_lines 一致；容器侧无备份面）。
    if _container_id:
        target = _container_target(path, _container_workdir)
        rc, existing, err = await _container_exec(
            _container_id, 'cat "$1" 2>/dev/null || true', args=[target], timeout=30.0
        )
        if rc != 0:
            existing = ""
        new_content = existing
        removed = 0
        newline = chr(10)
        if action == "write":
            new_content = content
        elif action == "append":
            new_content = existing + content
        elif action == "search_replace":
            if old_str is None:
                return ToolResult.failure_result("old_str is required for search_replace")
            if old_str not in existing:
                return ToolResult.failure_result(f"old_str not found in {path}")
            new_content = existing.replace(old_str, new_str if new_str is not None else "")
        elif action == "insert":
            if line is None:
                return ToolResult.failure_result("line is required for insert")
            lines = existing.split(newline)
            lines.insert(min(max(line, 0), len(lines)), content)
            new_content = newline.join(lines)
        elif action == "delete_lines":
            if start_line is None:
                return ToolResult.failure_result("start_line is required for delete_lines")
            lines = existing.split(newline)
            end = end_line if end_line is not None else len(lines)
            del lines[start_line - 1 : end]
            new_content = newline.join(lines)
            removed = end - start_line + 1
        else:
            return ToolResult.failure_result(f"Unsupported action: {action}")
        b64 = base64.b64encode(new_content.encode("utf-8")).decode("ascii")
        rc, _out, werr = await _container_exec(
            _container_id,
            'mkdir -p "$(dirname "$1")" && base64 -d > "$1"',
            args=[target],
            stdin_data=b64,
            timeout=30.0,
        )
        if rc != 0:
            return ToolResult.failure_result(f"Write error: {werr.strip() or f'exit {rc}'}")
        return ToolResult.success_result(
            {
                "file": target,
                "added": content.count(newline) + 1 if content else 0,
                "removed": removed,
                "size": len(new_content.encode("utf-8")),
                "backup": "",
                "old_content": existing,
                "new_content": new_content,
                "container": _container_id,
            }
        )

    # 工作空间约束（写路径：根外一律拒绝；相对路径以根锚定后执行）
    allowed, reason, resolved = await _check_workspace_path(
        path, workspace, project_root, operation="write", authorized_zones=authorized_zones
    )
    if not allowed:
        return ToolResult.failure_result(reason)
    if resolved is not None:
        path = resolved

    file_path = Path(path)
    if action in ("write", "append", "insert"):
        await asyncio.to_thread(file_path.parent.mkdir, parents=True, exist_ok=True)

    try:
        if action == "write":
            existing = (
                await asyncio.to_thread(file_path.read_text, "utf-8")
                if await asyncio.to_thread(file_path.exists)
                else ""
            )
            backup = await asyncio.to_thread(_maybe_backup, file_path, create_backup)
            await asyncio.to_thread(file_path.write_text, content, "utf-8")
            added = content.count("\n") + 1 if content else 0
            return ToolResult.success_result(
                {
                    "file": str(await asyncio.to_thread(file_path.resolve)),
                    "added": added,
                    "removed": 0,
                    "size": await asyncio.to_thread(_disk_size, file_path),
                    "backup": backup,
                    "old_content": existing,
                    "new_content": content,
                },
            )

        if action == "append":
            backup = await asyncio.to_thread(_maybe_backup, file_path, create_backup)
            existing = (
                await asyncio.to_thread(file_path.read_text, "utf-8")
                if await asyncio.to_thread(file_path.exists)
                else ""
            )
            await asyncio.to_thread(file_path.write_text, existing + content, "utf-8")
            return ToolResult.success_result(
                {
                    "file": str(await asyncio.to_thread(file_path.resolve)),
                    "added": content.count("\n") + 1 if content else 0,
                    "removed": 0,
                    "size": await asyncio.to_thread(_disk_size, file_path),
                    "backup": backup,
                    "old_content": existing,
                    "new_content": existing + content,
                },
            )

        if action == "search_replace":
            if old_str is None:
                return ToolResult.failure_result("old_str is required for search_replace")
            if not await asyncio.to_thread(file_path.exists):
                return ToolResult.failure_result(f"File not found: {path}")
            backup = await asyncio.to_thread(_maybe_backup, file_path, create_backup)
            existing = await asyncio.to_thread(file_path.read_text, "utf-8")
            replacement = new_str if new_str is not None else ""
            count_replace = existing.count(old_str)
            if count_replace == 0:
                return ToolResult.failure_result(
                    f"old_str not found in {path}",
                    backup=backup or "",
                )
            new_content = existing.replace(old_str, replacement)
            await asyncio.to_thread(file_path.write_text, new_content, "utf-8")
            return ToolResult.success_result(
                {
                    "file": str(await asyncio.to_thread(file_path.resolve)),
                    "added": 0,
                    "removed": 0,
                    "size": await asyncio.to_thread(_disk_size, file_path),
                    "backup": backup,
                    "old_content": existing,
                    "new_content": new_content,
                },
                replacements=count_replace,
            )

        if action == "insert":
            if line is None:
                return ToolResult.failure_result("line is required for insert")
            backup = await asyncio.to_thread(_maybe_backup, file_path, create_backup)
            existing = (
                await asyncio.to_thread(file_path.read_text, "utf-8")
                if await asyncio.to_thread(file_path.exists)
                else ""
            )
            lines = existing.split("\n")
            insert_idx = min(max(line, 0), len(lines))
            lines.insert(insert_idx, content)
            await asyncio.to_thread(file_path.write_text, "\n".join(lines), "utf-8")
            return ToolResult.success_result(
                {
                    "file": str(await asyncio.to_thread(file_path.resolve)),
                    "added": content.count("\n") + 1 if content else 0,
                    "removed": 0,
                    "size": await asyncio.to_thread(_disk_size, file_path),
                    "backup": backup,
                    "old_content": existing,
                    "new_content": "\n".join(lines),
                },
            )

        if action == "delete_lines":
            if start_line is None:
                return ToolResult.failure_result("start_line is required for delete_lines")
            if not await asyncio.to_thread(file_path.exists):
                return ToolResult.failure_result(f"File not found: {path}")
            backup = await asyncio.to_thread(_maybe_backup, file_path, create_backup)
            existing = await asyncio.to_thread(file_path.read_text, "utf-8")
            lines = existing.split("\n")
            end = end_line if end_line is not None else len(lines)
            del lines[start_line - 1 : end]
            await asyncio.to_thread(file_path.write_text, "\n".join(lines), "utf-8")
            return ToolResult.success_result(
                {
                    "file": str(await asyncio.to_thread(file_path.resolve)),
                    "added": 0,
                    "removed": end - start_line + 1,
                    "size": await asyncio.to_thread(_disk_size, file_path),
                    "backup": backup,
                    "old_content": existing,
                    "new_content": "\n".join(lines),
                },
            )

        return ToolResult.failure_result(f"Unknown action: {action}")

    except OSError as e:
        return ToolResult.failure_result(f"IO error: {e}")


def _maybe_backup(path: Path, create: bool) -> str | None:
    """创建 .bak 备份。"""
    if not create or not path.exists():
        return None
    bak_path = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, bak_path)
    return str(bak_path)


# ═════════════════════════════════════════════════════════════
# create_directory
# ═════════════════════════════════════════════════════════════

CREATE_DIRECTORY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "目录路径"},
        "parents": {
            "type": "boolean",
            "description": "是否创建父目录（默认 true）",
            "default": True,
        },
    },
    "required": ["path"],
}


async def create_directory(
    path: str,
    parents: bool = True,
    workspace: str | None = None,
    project_root: str | None = None,
    authorized_zones: str | None = None,
    _container_id: str | None = None,
    _container_workdir: str | None = None,
) -> ToolResult:
    """创建目录（幂等：目录已存在直接返回成功；相对路径以注入根锚定）。"""
    # 容器路由（R11）：绑定会话 mkdir -p 收养容器内目录
    if _container_id:
        target = _container_target(path, _container_workdir)
        rc, _out, err = await _container_exec(
            _container_id, 'mkdir -p "$1"', args=[target], timeout=30.0
        )
        if rc != 0:
            return ToolResult.failure_result(f"Create error: {err.strip() or f'exit {rc}'}")
        return ToolResult.success_result({"path": target, "container": _container_id})
    allowed, reason, resolved = await _check_workspace_path(
        path, workspace, project_root, operation="write", authorized_zones=authorized_zones
    )
    if not allowed:
        return ToolResult.failure_result(reason)
    dir_path = Path(resolved) if resolved is not None else Path(path)
    try:
        await asyncio.to_thread(dir_path.mkdir, parents=parents, exist_ok=True)
    except OSError as e:
        return ToolResult.failure_result(f"Create error: {e}")

    return ToolResult.success_result({"path": str(await asyncio.to_thread(dir_path.resolve))})


# ═════════════════════════════════════════════════════════════
# copy_file
# ═════════════════════════════════════════════════════════════

COPY_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {"type": "string", "description": "源文件或目录路径（与 copies 二选一）"},
        "destination": {"type": "string", "description": "目标路径（与 copies 二选一）"},
        "copies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"source": {"type": "string"}, "destination": {"type": "string"}},
            },
            "description": "批量复制列表（与 source/destination 二选一）",
        },
        "overwrite": {
            "type": "boolean",
            "description": "是否覆盖已存在的目标（默认 false）",
            "default": False,
        },
    },
    "required": [],
}


async def copy_file(
    source: str | None = None,
    destination: str | None = None,
    copies: list[dict[str, str]] | None = None,
    overwrite: bool = False,
    workspace: str | None = None,
    project_root: str | None = None,
    authorized_zones: str | None = None,
    _container_id: str | None = None,
    _container_workdir: str | None = None,
) -> ToolResult:
    """复制文件或目录。源与目标均以注入根锚定（无注入报错）。"""
    # 容器路由（R11）：绑定会话 cp -a 收养容器内复制（overwrite=false 时先测存在）
    if _container_id:
        if copies:
            container_results: list[dict[str, Any]] = []
            for item in copies:
                r = await copy_file(
                    item["source"],
                    item["destination"],
                    overwrite=overwrite,
                    _container_id=_container_id,
                    _container_workdir=_container_workdir,
                )
                container_results.append(
                    {
                        "source": item["source"],
                        "destination": item["destination"],
                        "success": r.success,
                    }
                )
            return ToolResult.success_result({"results": container_results})
        if not source or not destination:
            return ToolResult.failure_result("source and destination are required (or use copies)")
        src_t = _container_target(source, _container_workdir)
        dst_t = _container_target(destination, _container_workdir)
        if not overwrite:
            rc, _out, _err = await _container_exec(
                _container_id, 'test -e "$2"', args=[src_t, dst_t], timeout=30.0
            )
            if rc == 0:
                return ToolResult.failure_result(f"Destination exists: {dst_t}")
        cp_script = 'mkdir -p "$(dirname "$2")" && cp -a "$1" "$2"'
        rc, _out, err = await _container_exec(
            _container_id, cp_script, args=[src_t, dst_t], timeout=60.0
        )
        if rc != 0:
            return ToolResult.failure_result(f"Copy error: {err.strip() or f'exit {rc}'}")
        return ToolResult.success_result(
            {"source": src_t, "destination": dst_t, "container": _container_id}
        )
    if copies:
        results: list[dict[str, Any]] = []
        for item in copies:
            r = await copy_file(
                item["source"],
                item["destination"],
                overwrite=overwrite,
                workspace=workspace,
                project_root=project_root,
                authorized_zones=authorized_zones,
            )
            results.append(
                {"source": item["source"], "destination": item["destination"], "success": r.success}
            )
        return ToolResult.success_result({"results": results})

    if not source or not destination:
        return ToolResult.failure_result("source and destination are required (or use copies)")

    for _field, value, op in (("source", source, "read"), ("destination", destination, "write")):
        allowed, reason, resolved = await _check_workspace_path(
            value, workspace, project_root, operation=op, authorized_zones=authorized_zones
        )
        if not allowed:
            return ToolResult.failure_result(reason)

    async def _anchor(value: str, op: str) -> str:
        _, _, resolved = await _check_workspace_path(
            value, workspace, project_root, operation=op, authorized_zones=authorized_zones
        )
        return resolved if resolved is not None else value

    src = Path(await _anchor(source, "move"))
    dst = Path(await _anchor(destination, "write"))
    if not await asyncio.to_thread(src.exists):
        return ToolResult.failure_result(f"Source not found: {source}")
    if await asyncio.to_thread(dst.exists) and not overwrite:
        return ToolResult.failure_result(f"Destination already exists: {destination}")

    try:
        if await asyncio.to_thread(src.is_dir):
            await asyncio.to_thread(shutil.copytree, src, dst, dirs_exist_ok=overwrite)
        else:
            await asyncio.to_thread(shutil.copy2, src, dst)
    except OSError as e:
        return ToolResult.failure_result(f"Copy error: {e}")

    return ToolResult.success_result({"source": source, "destination": destination})


# ═════════════════════════════════════════════════════════════
# move_file
# ═════════════════════════════════════════════════════════════

MOVE_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {"type": "string", "description": "源路径"},
        "destination": {"type": "string", "description": "目标路径"},
        "moves": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"source": {"type": "string"}, "destination": {"type": "string"}},
            },
            "description": "批量移动列表",
        },
        "overwrite": {
            "type": "boolean",
            "description": "是否覆盖已存在的目标（默认 false）",
            "default": False,
        },
    },
    "required": [],
}


async def move_file(
    source: str | None = None,
    destination: str | None = None,
    moves: list[dict[str, str]] | None = None,
    overwrite: bool = False,
    workspace: str | None = None,
    project_root: str | None = None,
    authorized_zones: str | None = None,
    _container_id: str | None = None,
    _container_workdir: str | None = None,
) -> ToolResult:
    """移动或重命名文件/目录。源与目标均须在 workspace/project_root 内（B5）。"""
    # 容器路由（R11）：绑定会话 mv 收养容器内移动/重命名
    if _container_id:
        if moves:
            container_results: list[dict[str, Any]] = []
            for item in moves:
                r = await move_file(
                    item["source"],
                    item["destination"],
                    overwrite=overwrite,
                    _container_id=_container_id,
                    _container_workdir=_container_workdir,
                )
                container_results.append(
                    {
                        "source": item["source"],
                        "destination": item["destination"],
                        "success": r.success,
                    }
                )
            return ToolResult.success_result({"results": container_results})
        if not source or not destination:
            return ToolResult.failure_result("source and destination are required (or use moves)")
        src_t = _container_target(source, _container_workdir)
        dst_t = _container_target(destination, _container_workdir)
        if not overwrite:
            rc, _out, _err = await _container_exec(
                _container_id, 'test -e "$2"', args=[src_t, dst_t], timeout=30.0
            )
            if rc == 0:
                return ToolResult.failure_result(f"Destination exists: {dst_t}")
        mv_script = 'mkdir -p "$(dirname "$2")" && mv -f "$1" "$2"'
        rc, _out, err = await _container_exec(
            _container_id, mv_script, args=[src_t, dst_t], timeout=60.0
        )
        if rc != 0:
            return ToolResult.failure_result(f"Move error: {err.strip() or f'exit {rc}'}")
        return ToolResult.success_result(
            {"source": src_t, "destination": dst_t, "container": _container_id}
        )
    # 工作空间约束：move 同时改动源（移出）与目标（写入），两端都校验；
    # 相对路径以根锚定后执行（校验的路径 = 实际操作的路径）
    if source:
        allowed, reason, resolved = await _check_workspace_path(
            source, workspace, project_root, operation="move"
        )
        if not allowed:
            return ToolResult.failure_result(reason)
        if resolved is not None:
            source = resolved
    if destination:
        allowed, reason, resolved = await _check_workspace_path(
            destination, workspace, project_root, operation="move"
        )
        if not allowed:
            return ToolResult.failure_result(reason)
        if resolved is not None:
            destination = resolved

    if moves:
        results: list[dict[str, Any]] = []
        for item in moves:
            r = await move_file(
                item["source"],
                item["destination"],
                overwrite=overwrite,
                workspace=workspace,
                project_root=project_root,
                authorized_zones=authorized_zones,
            )
            results.append(
                {"source": item["source"], "destination": item["destination"], "success": r.success}
            )
        return ToolResult.success_result({"results": results})

    if not source or not destination:
        return ToolResult.failure_result("source and destination are required (or use moves)")

    src = Path(source)
    dst = Path(destination)
    if not await asyncio.to_thread(src.exists):
        return ToolResult.failure_result(f"Source not found: {source}")
    if await asyncio.to_thread(dst.exists) and not overwrite:
        return ToolResult.failure_result(f"Destination already exists: {destination}")

    def _sync_move() -> None:
        """覆盖分支（删目录/删文件）+ 移动在同一线程体内跑完。

        两段都是阻塞文件操作（覆盖目标可能是大目录），逐段跨线程无收益；
        OSError 语义由调用方同层 except 原样承接。
        """
        if overwrite and dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        shutil.move(str(src), str(dst))

    try:
        await asyncio.to_thread(_sync_move)
    except OSError as e:
        return ToolResult.failure_result(f"Move error: {e}")

    return ToolResult.success_result({"source": source, "destination": destination})


# ═════════════════════════════════════════════════════════════
# delete_file
# ═════════════════════════════════════════════════════════════

DELETE_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "要删除的文件或目录路径（与 paths 二选一）"},
        "paths": {"type": "array", "items": {"type": "string"}, "description": "批量删除路径列表"},
        "recursive": {
            "type": "boolean",
            "description": "是否递归删除目录（默认 false）",
            "default": False,
        },
        "force": {
            "type": "boolean",
            "description": "是否强制删除（包括只读文件，默认 false）",
            "default": False,
        },
    },
    "required": [],
}


async def delete_file(
    path: str | None = None,
    paths: list[str] | None = None,
    recursive: bool = False,
    force: bool = False,
    workspace: str | None = None,
    project_root: str | None = None,
    authorized_zones: str | None = None,
    _container_id: str | None = None,
    _container_workdir: str | None = None,
) -> ToolResult:
    """删除文件或目录。workspace/project_root 可用时禁止根外路径（B5）。"""
    # 容器路由（R11）：绑定会话 rm 收养容器内删除
    if _container_id:
        container_targets = paths if paths else [path] if path else []
        if not container_targets:
            return ToolResult.failure_result("path or paths is required")
        flag = "-rf" if recursive else "-f"
        container_results: list[dict[str, Any]] = []
        for tp in container_targets:
            t = _container_target(tp, _container_workdir)
            rc, _out, err = await _container_exec(
                _container_id, f'rm {flag} "$1"', args=[t], timeout=30.0
            )
            container_results.append(
                {"path": t, "success": rc == 0, "error": err.strip() if rc != 0 else ""}
            )
        failed = [r for r in container_results if not r["success"]]
        if failed:
            deleted_ok = [r["path"] for r in container_results if r["success"]]
            return ToolResult.failure_result(
                f"Delete error: {failed[0]['error']}",
                deleted=deleted_ok,
            )
        return ToolResult.success_result({"deleted": [r["path"] for r in container_results]})

    target_paths = paths if paths else [path] if path else []
    if not target_paths:
        return ToolResult.failure_result("path or paths is required")

    # 工作空间约束（删除路径：根外一律拒绝，批量场景逐条校验；
    # 相对路径以根锚定后执行）
    anchored: list[str] = []
    for p in target_paths:
        allowed, reason, resolved = await _check_workspace_path(
            p, workspace, project_root, operation="delete", authorized_zones=authorized_zones
        )
        if not allowed:
            return ToolResult.failure_result(reason)
        anchored.append(resolved if resolved is not None else p)
    target_paths = anchored

    results: list[dict[str, Any]] = []
    all_success = True

    for p in target_paths:
        target = Path(p)
        if not await asyncio.to_thread(target.exists):
            results.append({"path": p, "deleted": False, "error": "not found"})
            all_success = False
            continue
        try:
            if await asyncio.to_thread(target.is_dir):
                if recursive:
                    await asyncio.to_thread(shutil.rmtree, target)
                else:
                    await asyncio.to_thread(target.rmdir)  # 仅删除空目录
            else:
                if force:
                    await asyncio.to_thread(target.chmod, 0o644)
                await asyncio.to_thread(target.unlink)
            results.append({"path": p, "deleted": True})
        except OSError as e:
            results.append({"path": p, "deleted": False, "error": str(e)})
            all_success = False

    if all_success:
        return ToolResult.success_result({"results": results})
    return ToolResult(
        success=False,
        output={"results": results},
        error="some deletions failed",
    )
